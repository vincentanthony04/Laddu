"""Train and register Project Laddu's first governed NSE ML model.

This runs in the isolated research environment. It builds features from the
curated Parquet/DuckDB lake, produces genuinely out-of-sample fold predictions,
submits those observations to the walk-forward authority, and registers the
final model as SHADOW or APPROVED. It never self-approves from in-sample metrics,
never runs in the live HTTP process, and accepts training observations only
from the production-authority Parquet/DuckDB catalogue.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
import os
import pickle
from pathlib import Path
import sqlite3
import sys
from urllib import request as urllib_request
from urllib.error import URLError, HTTPError


HERE = Path(__file__).resolve()
BACKEND = HERE.parents[1]
sys.path.insert(0, str(BACKEND))

from config import ML_HISTORICAL_TRAIN_MIN_DAYS, ML_HISTORICAL_TRAIN_TARGET_DAYS, ML_HISTORICAL_TRAIN_MAX_DAYS
from core.ml_history_policy import policy_for_mode, resolve_mode_history_policy, training_frame_and_weights
from core.ai_governance_service import AIGovernanceService
from core.walk_forward_validation_service import WalkForwardValidationService
from core.factor_authority_service import FactorAuthorityService
from core.historical_session_index_authority import HistoricalSessionIndexAuthority
from core.incremental_feature_store import (
    feature_manifest_path,
    feature_store_path,
    materialize_feature_store,
    read_manifest,
    training_is_current,
)
from core.factors.factor_decay_monitor import evaluate_decay
from core.factors.factor_store import (
    FactorRegistryRow, ensure_factor_tables, get_factor_registry,
    record_decay_report, upsert_factor_registry,
)
from core.factors.factor_thresholds import DEFAULT_ALIVE_IC_THRESHOLD
from core.factor_dedup_service import FactorDedupService, DEFAULT_MIN_OVERLAP, DEFAULT_THRESHOLD
from core.strict_json import json_safe, strict_json_dumps
from core.storage_layout import (
    StorageLayout,
    atomic_write_json,
    cleanup_abandoned_sqlite_artifacts,
    interprocess_lock,
    remove_sqlite_family,
)


MOMENTUM_LIQUIDITY_FEATURES = [
    "ret_1", "ret_5", "ret_20", "volatility_20", "range_pct",
    "volume_z20", "delivery_z20", "delivery_spread",
]

# Price is not judged merely as "above/below DMA".  The model receives
# scale-free distance, volatility-normalised displacement, moving-average
# slope, and long-range location so it can learn when extension represents
# healthy trend continuation versus mean-reversion risk.
EQUILIBRIUM_FEATURES = [
    "close_sma20_dist", "close_sma50_dist", "close_sma100_dist", "close_sma200_dist",
    "close_ema20_dist", "close_ema50_dist",
    "equilibrium_z20", "equilibrium_atr20",
    "sma20_slope_5", "sma50_slope_10", "price_location_252",
]
NSE_OFFICIAL_AVAILABILITY_FEATURES = [
    "nse_has_bhavcopy", "nse_has_delivery", "nse_has_security_master",
    "nse_has_risk", "nse_has_index_context", "nse_has_deal_events",
    "nse_has_corporate_action", "nse_has_filings", "nse_has_surveillance",
]
NSE_OFFICIAL_FEATURES = [
    "nse_turnover_z20", "nse_trades_z20", "nse_daily_volatility",
    "nse_var_margin", "nse_impact_cost", "nse_index_weight", "nse_beta",
    "nse_market_cap_log", "nse_free_float_ratio", "nse_event_pressure",
    "nse_signed_deal_ratio", "nse_short_ratio", "nse_margin_ratio",
    "nse_surveillance_flag", "nse_price_band_position", "nse_price_band_change_pct",
    "distance_52w_high", "distance_52w_low", "nse_net_profit_margin",
    "nse_ebitda_margin", "nse_eps", "nse_promoter_holding_pct",
    "nse_institutional_holding_pct", "nse_ownership_change_pct",
    "nse_source_family_coverage",
] + NSE_OFFICIAL_AVAILABILITY_FEATURES
FEATURES = MOMENTUM_LIQUIDITY_FEATURES + EQUILIBRIUM_FEATURES + NSE_OFFICIAL_FEATURES
MODEL_ID = "laddu-nse-delivery-10d-hgbr-equilibrium-v7-materialized-panel-shadow"
FIRST_MODE_MODEL_ID = "laddu-nse-delivery-10d-hgbr-equilibrium-first-mode-v4-materialized-panel-shadow"
TRAINING_SOURCE_POLICY = "PARQUET_DUCKDB_ONLY"
TRAINING_DATA_AUTHORITY = "PARQUET_DUCKDB"
TRAINING_PIPELINE_SOURCE = "R46_MATERIALIZED_RESEARCH_PANEL_TO_INCREMENTAL_FEATURE_STORE"
PUBLICATION_AUTHORITY = "GOVERNANCE_POSTGRESQL_VIA_LIVE_SERVICE"
PRODUCTION_WEIGHT_POLICY = "GOVERNED_ACTIVE_CAP_15_WITH_DETERMINISTIC_FALLBACK"
FOLD_LOCAL_ARTIFACT_CONTRACT = "fold-local-capital-wfa-artifact-1.0.0-pl44"
SUPERVISED_TARGET_POLICY = "NONFINITE_TARGETS_TO_MISSING_1.0.0_PL28"
SUPERVISED_TARGET_COLUMNS = ("forward_return", "forward_equilibrium_atr20", "reverted_to_equilibrium")

SURVIVORSHIP_CONTROLLED_UNIVERSE_AUTHORITIES = frozenset({
    "POINT_IN_TIME_SECURITY_MASTER",
    "CANONICAL_DAILY_CANDLE_OBSERVED_MEMBERSHIP",
})

def resolve_survivorship_authority(authority_values) -> dict:
    values = {str(value) for value in (authority_values or ()) if str(value or "").strip()}
    controlled = bool(values) and values.issubset(SURVIVORSHIP_CONTROLLED_UNIVERSE_AUTHORITIES)
    if values == {"POINT_IN_TIME_SECURITY_MASTER"}:
        state = "POINT_IN_TIME_SECURITY_MASTER"
    elif controlled:
        state = "PIT_PLUS_CANONICAL_OBSERVED_MEMBERSHIP"
    else:
        state = "UNCONTROLLED_OR_CURRENT_UNIVERSE_FALLBACK"
    return {"controlled": controlled, "state": state, "authorities": sorted(values)}

ALL_NSE_SOURCE_FAMILIES = frozenset({
    "cm_udiff_bhavcopy", "security_delivery_positions", "mii_security_file",
    "daily_volatility_var_price_band", "index_snapshot_constituents_weights",
    "bulk_block_short_margin", "corporate_actions",
    "filings_results_announcements_shareholding",
    "surveillance_52w_price_band_changes",
})
# Capital WFA is fail-closed on the official sources required by the base
# Delivery feature/label authority. Additional NSE families are independent
# enrichment features with explicit availability indicators; their absence is
# measured, not allowed to permanently block otherwise valid historical WFA.
CORE_NSE_WFA_SOURCE_FAMILIES = frozenset({
    "cm_udiff_bhavcopy",
    "security_delivery_positions",
})
OPTIONAL_NSE_ENRICHMENT_SOURCE_FAMILIES = ALL_NSE_SOURCE_FAMILIES - CORE_NSE_WFA_SOURCE_FAMILIES
# Compatibility name: required means required for WFA qualification, not every
# optional enrichment source the product knows how to consume.
REQUIRED_NSE_SOURCE_FAMILIES = CORE_NSE_WFA_SOURCE_FAMILIES

def resolve_historical_training_policy(available_label_dates: int, *, horizon_days: int, mode: str = "delivery") -> dict:
    """Resolve adaptive all-eligible history for one trade mode.

    The Delivery 500-session value is a stability reference for the first WFA
    fold, never a hard training cap.  With maximum_days=0, later folds and the
    final model consume every eligible pre-cutoff observation.
    """
    result = resolve_mode_history_policy(mode, available_label_dates, horizon_days=horizon_days)
    return {
        **result,
        "target_days": int(result["reference_days"]),
        "available_label_dates": int(result["available_dates"]),
        "capacity_after_horizon_reserve": int(result["eligible_capacity_days"]),
    }


def recent_date_window(frame, days: int):
    """Return only the latest N distinct dates while preserving the full cross-section."""
    import pandas as pd
    wanted = sorted(pd.to_datetime(frame["date"], errors="coerce").dropna().dt.strftime("%Y-%m-%d").unique())[-max(1, int(days)):]
    wanted_set = set(wanted)
    return frame[frame.date.dt.strftime("%Y-%m-%d").isin(wanted_set)].copy(), wanted


def rolling_train_date_slice(dates, train_end: int, train_window_days=None):
    """Pure date-window selector used by WFA and its deterministic QC."""
    end = max(0, int(train_end))
    start = 0 if not train_window_days else max(0, end - int(train_window_days))
    return list(dates[start:end])


def make_delivery_fold_local_trainer(labelled, *, model_spec_hash: str, artifact_dir: Path):
    """Return a real fold-local trainer for WalkForwardValidationService.

    Every callback fit uses only eligible observations whose feature date is at or
    before the validator fold's purged train-end.  It intentionally uses earlier
    retained history too when available: the validator's minimum train window is a
    lower bound, never a hard cap.  Each fitted estimator is written atomically and
    represented by a canonical JSON artifact whose hash is bound to every test-row
    prediction.  No economic/statistical gate is changed here.
    """
    import joblib
    import pandas as pd
    from sklearn.ensemble import HistGradientBoostingRegressor

    frame = labelled.copy()
    frame["_pl44_date_key"] = pd.to_datetime(frame["date"], errors="coerce").dt.strftime("%Y-%m-%d")
    frame["_pl44_symbol_key"] = frame["symbol"].astype(str).str.upper()
    frame = frame.dropna(subset=["_pl44_date_key"]).copy()
    artifact_dir = Path(artifact_dir)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    cache = {}

    def trainer(context):
        train_dates = [str(x)[:10] for x in tuple(context.get("train_dates") or ())]
        test_dates = [str(x)[:10] for x in tuple(context.get("test_dates") or ())]
        prediction_inputs = [dict(x or {}) for x in tuple(context.get("prediction_inputs") or ())]
        if not train_dates or not test_dates or not prediction_inputs:
            raise RuntimeError("fold-local trainer received an empty train/test context")
        declared_train_start = min(train_dates)
        train_end = max(train_dates)
        test_start = min(test_dates)
        purge_days = max(0, int(context.get("purge_days") or 0))
        embargo_days = max(0, int(context.get("embargo_days") or 0))
        requested_keys = sorted({
            (str(row.get("date") or "")[:10], str(row.get("symbol") or "").upper())
            for row in prediction_inputs
            if str(row.get("date") or "")[:10] and str(row.get("symbol") or "").strip()
        })
        signature_basis = {
            "contract": FOLD_LOCAL_ARTIFACT_CONTRACT,
            "model_spec_hash": str(model_spec_hash),
            "fold": int(context.get("fold") or 0),
            "declared_train_start": declared_train_start,
            "train_end": train_end,
            "test_start": test_start,
            "test_end": max(test_dates),
            "purge_days": purge_days,
            "embargo_days": embargo_days,
            "prediction_keys_sha256": hashlib.sha256(json.dumps(requested_keys, separators=(",", ":")).encode()).hexdigest(),
        }
        signature = hashlib.sha256(json.dumps(signature_basis, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        if signature in cache:
            return cache[signature]

        # Use every eligible retained pre-cutoff observation, not only the
        # validator's minimum window.  Per-stock eligibility and recency/sample
        # balancing remain owned by the PL42 history policy.
        train_source = frame[frame["_pl44_date_key"] <= train_end].copy()
        train_frame, train_weights, history_summary = training_frame_and_weights(train_source, mode="delivery")
        if len(train_frame) < 1000:
            raise RuntimeError(f"fold-local train set too small after eligibility: {history_summary}")
        actual_train_dates = sorted(train_frame["_pl44_date_key"].dropna().unique())
        if not actual_train_dates or max(actual_train_dates) > train_end:
            raise RuntimeError("fold-local training crossed the declared train-end")

        key_frame = pd.DataFrame(requested_keys, columns=["_pl44_date_key", "_pl44_symbol_key"])
        test_frame = key_frame.merge(frame, on=["_pl44_date_key", "_pl44_symbol_key"], how="left", validate="one_to_one")
        if len(test_frame) != len(requested_keys) or test_frame[FEATURES].isna().all(axis=1).any():
            raise RuntimeError("fold-local prediction inputs could not be resolved to the retained feature panel")

        model = HistGradientBoostingRegressor(
            max_iter=180, learning_rate=.045, max_leaf_nodes=15,
            l2_regularization=1.0, random_state=4510,
        )
        model.fit(
            train_frame[FEATURES], train_frame["forward_return"],
            sample_weight=train_weights.loc[train_frame.index] if train_weights is not None else None,
        )
        temp = artifact_dir / f".{signature[:24]}.{os.getpid()}.joblib.tmp"
        joblib.dump(model, temp)
        binary_sha = hashlib.sha256(temp.read_bytes()).hexdigest()
        binary_path = artifact_dir / f"{signature[:16]}-{binary_sha[:16]}.joblib"
        if binary_path.is_file():
            temp.unlink(missing_ok=True)
        else:
            os.replace(temp, binary_path)

        model_artifact = {
            "contract": FOLD_LOCAL_ARTIFACT_CONTRACT,
            "artifact_kind": "PURGED_FOLD_LOCAL_HIST_GRADIENT_BOOSTING",
            "binary_sha256": binary_sha,
            "binary_file": binary_path.name,
            "model_spec_hash": str(model_spec_hash),
            "fold_signature": signature,
            "estimator": {
                "type": "HistGradientBoostingRegressor", "max_iter": 180,
                "learning_rate": .045, "max_leaf_nodes": 15,
                "l2_regularization": 1.0, "random_state": 4510,
            },
            "features": list(FEATURES),
            "actual_training_start": min(actual_train_dates),
            "actual_training_end": max(actual_train_dates),
            "declared_fold_train_start": declared_train_start,
            "train_rows": int(len(train_frame)),
            "train_symbols": int(train_frame["symbol"].nunique()),
            "history_summary": history_summary,
            "purge_days": purge_days,
            "embargo_days": embargo_days,
            "artifact_generated_at": _now(),
        }
        canonical_model_hash = hashlib.sha256(
            json.dumps(model_artifact, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        raw_predictions = model.predict(test_frame[FEATURES])
        predictions = []
        for (date_key, symbol_key), value in zip(requested_keys, raw_predictions):
            predictions.append({
                "date": date_key, "symbol": symbol_key,
                "prediction": float(value), "prediction_score": float(value),
                "model_hash": canonical_model_hash,
                "prediction_timestamp": f"{date_key}T15:29:00+05:30",
                "fold_local_artifact_contract": FOLD_LOCAL_ARTIFACT_CONTRACT,
            })
        artifact = {
            "model_hash": canonical_model_hash,
            "model_artifact": model_artifact,
            # The validator's declared fold begins at its evidence-window start,
            # while actual_training_start may be earlier because PL42 uses all
            # eligible prior history.  train_end remains exact/fail-closed.
            "train_start": declared_train_start,
            "train_end": train_end,
            "actual_training_start": min(actual_train_dates),
            "feature_cutoff": f"{train_end}T15:30:00+05:30",
            "trained_at": f"{train_end}T15:31:00+05:30",
            "predictions": predictions,
        }
        cache[signature] = artifact
        return artifact

    return trainer


class Store:
    def __init__(self, conn):
        self.conn = conn


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def load_panel(conn):
    """Legacy compatibility entry point; SQLite candles are not a training authority.

    The active trainer is fail-closed on ``load_panel_from_lake`` and the
    materialized Parquet/DuckDB research panel.  Keeping this named entry point
    explicit-but-disabled prevents a future caller from silently reintroducing
    the old operational SQLite recent-window training path.
    """
    raise RuntimeError(
        "LEGACY_SQLITE_TRAINING_AUTHORITY_DISABLED: use load_panel_from_lake / research_delivery_training_panel"
    )


def lake_views(layout) -> set[str]:
    try:
        import duckdb
        if not Path(layout.analytics_db).exists():
            return set()
        db = duckdb.connect(str(layout.analytics_db), read_only=True)
        try:
            return {row[0] for row in db.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema='main'"
            ).fetchall()}
        finally:
            db.close()
    except Exception:
        return set()


def lake_training_available(layout) -> bool:
    return {"curated_candles", "current_instruments", "research_delivery_training_panel"}.issubset(lake_views(layout))


def data_quality_authority(layout) -> dict:
    views = lake_views(layout)
    blockers = []
    observed_sources: set[str] = set()
    source_lineage_hash = None
    historical_sessions = None
    historical_session_official_count = 0
    historical_session_canonical_daily_count = 0
    historical_session_source_policy = "POSITIVE_OBSERVATIONS_ONLY_NO_CALENDAR_INFERENCE"
    if "curated_adjusted_candles" not in views:
        blockers.append("point-in-time corporate-action-adjusted candles are unavailable")
    if "point_in_time_security_master" not in views:
        blockers.append("point-in-time security master/universe history is unavailable")
    if "curated_nse_daily_features" not in views or "curated_nse_official_reports" not in views:
        blockers.append("official NSE point-in-time model-enrichment features are unavailable")
    else:
        try:
            import duckdb
            db = duckdb.connect(str(layout.analytics_db), read_only=True)
            try:
                rows = db.execute("SELECT DISTINCT source_key FROM curated_nse_official_reports WHERE source_key IS NOT NULL").fetchall()
                observed_sources = {str(row[0]) for row in rows if row and row[0]}
                lineage = db.execute("SELECT string_agg(DISTINCT content_hash, ',' ORDER BY content_hash) FROM curated_nse_official_reports").fetchone()[0]
                source_lineage_hash = hashlib.sha256(str(lineage or '').encode()).hexdigest() if lineage else None
                official_session_rows = [
                    {"trade_date": row[0], "source_key": "cm_udiff_bhavcopy", "content_hash": row[1]}
                    for row in db.execute(
                        """SELECT DISTINCT CAST(trade_date AS VARCHAR), CAST(content_hash AS VARCHAR)
                             FROM curated_nse_official_reports
                            WHERE CAST(source_key AS VARCHAR)='cm_udiff_bhavcopy'
                              AND trade_date IS NOT NULL AND content_hash IS NOT NULL
                            ORDER BY 1,2"""
                    ).fetchall()
                ]
                historical_session_official_count = len({row["trade_date"] for row in official_session_rows})

                # A completed canonical NSE equity daily bar is direct positive
                # evidence that the exchange had a trading session on that date.
                # This deliberately does NOT infer weekdays, holidays, or missing
                # sessions.  Official bhavcopy remains preferred where present;
                # canonical daily observations extend the immutable session index
                # across retained history when the official archive is sparse.
                canonical_session_rows = []
                catalogue_fingerprint = None
                if "research_catalog_meta" in views:
                    meta_rows = dict(db.execute(
                        "SELECT CAST(key AS VARCHAR), CAST(value AS VARCHAR) FROM research_catalog_meta"
                    ).fetchall())
                    catalogue_fingerprint = str(meta_rows.get("catalogue_fingerprint") or "").strip() or None
                if "curated_candles" in views:
                    canonical_session_rows = [
                        {
                            "trade_date": row[0],
                            "source": "PARQUET_CANONICAL_NSE_EQUITY_DAILY_SESSION_OBSERVATION",
                        }
                        for row in db.execute(
                            """SELECT DISTINCT CAST(CAST(ts AS DATE) AS VARCHAR)
                                 FROM curated_candles
                                WHERE LOWER(CAST(interval AS VARCHAR)) IN ('1d','day','1day')
                                  AND UPPER(CAST(instrument_key AS VARCHAR)) LIKE 'NSE_EQ|%'
                                  AND ts IS NOT NULL
                                ORDER BY 1"""
                        ).fetchall()
                        if row and row[0]
                    ]
                historical_session_canonical_daily_count = len(canonical_session_rows)
                combined_session_rows = official_session_rows + canonical_session_rows
                session_fingerprint_material = "|".join(filter(None, [
                    str(source_lineage_hash or ""),
                    str(catalogue_fingerprint or ""),
                    str(historical_session_official_count),
                    str(historical_session_canonical_daily_count),
                ]))
                session_fingerprint = hashlib.sha256(session_fingerprint_material.encode()).hexdigest() if session_fingerprint_material else None
                historical_sessions = HistoricalSessionIndexAuthority.from_records(
                    combined_session_rows,
                    source="COMPOSITE_NSE_OFFICIAL_BHAVCOPY_AND_CANONICAL_DAILY_OBSERVATIONS",
                    source_fingerprint=session_fingerprint,
                ) if combined_session_rows else None
            finally:
                db.close()
        except Exception as exc:
            blockers.append(f"official NSE source authority could not be inspected: {exc}")
    missing_sources = sorted(CORE_NSE_WFA_SOURCE_FAMILIES - observed_sources)
    optional_missing_sources = sorted(OPTIONAL_NSE_ENRICHMENT_SOURCE_FAMILIES - observed_sources)
    if missing_sources:
        blockers.append("core official NSE WFA source families missing: " + ", ".join(missing_sources))
    if historical_sessions is None:
        blockers.append("positive historical NSE session evidence is unavailable")
    return {
        "eligible": not blockers,
        "state": "DATA_QUALITY_AUTHORIZED" if not blockers else "DATA_QUALITY_SHADOW_ONLY",
        "price_basis": "CORPORATE_ACTION_ADJUSTED" if "curated_adjusted_candles" in views else "SOURCE_CLOSE_UNADJUSTED",
        "point_in_time_universe": "point_in_time_security_master" in views,
        "official_nse_sources": sorted(observed_sources),
        "official_nse_source_count": len(observed_sources),
        "official_nse_core_source_count": len(CORE_NSE_WFA_SOURCE_FAMILIES.intersection(observed_sources)),
        "official_nse_core_required_count": len(CORE_NSE_WFA_SOURCE_FAMILIES),
        "official_nse_missing_sources": missing_sources,
        "official_nse_optional_missing_sources": optional_missing_sources,
        "official_nse_optional_enrichment_coverage": (
            len(OPTIONAL_NSE_ENRICHMENT_SOURCE_FAMILIES.intersection(observed_sources)) / len(OPTIONAL_NSE_ENRICHMENT_SOURCE_FAMILIES)
            if OPTIONAL_NSE_ENRICHMENT_SOURCE_FAMILIES else 1.0
        ),
        "official_nse_lineage_hash": source_lineage_hash,
        "historical_session_authority": historical_sessions.authority if historical_sessions else None,
        "historical_session_authority_version": historical_sessions.authority_version if historical_sessions else None,
        "historical_session_index_fingerprint": historical_sessions.session_index_fingerprint if historical_sessions else None,
        "historical_session_count": historical_sessions.session_count if historical_sessions else 0,
        "historical_session_official_count": historical_session_official_count,
        "historical_session_canonical_daily_count": historical_session_canonical_daily_count,
        "historical_session_source_policy": historical_session_source_policy,
        "historical_session_coverage_start": historical_sessions.coverage_start.isoformat() if historical_sessions and historical_sessions.coverage_start else None,
        "historical_session_coverage_end": historical_sessions.coverage_end.isoformat() if historical_sessions and historical_sessions.coverage_end else None,
        "historical_session_dates": [day.isoformat() for day in historical_sessions.session_dates] if historical_sessions else [],
        "blockers": blockers,
    }


def reconcile_panel_quality_authority(quality_authority: dict, feature_manifest: dict | None) -> dict:
    """Bind qualification claims to the panel that was actually materialised.

    A point-in-time view may exist while historical effective dates are damaged.
    In that case the loader can intentionally use the current-identity map for
    SHADOW research continuity.  Presence of the PIT view must never be allowed
    to relabel that fallback panel as survivorship controlled.
    """
    out = dict(quality_authority or {})
    panel = dict((feature_manifest or {}).get("panel_authority") or {})
    if not panel:
        return out
    out["panel_authority"] = panel
    out["universe_join_authority"] = panel.get("universe_join_authority")
    if panel.get("price_basis") not in (None, "", "UNKNOWN"):
        out["price_basis"] = panel.get("price_basis")
    if not bool(panel.get("point_in_time_universe")):
        out["point_in_time_universe"] = False
        out["eligible"] = False
        out["state"] = "DATA_QUALITY_SHADOW_ONLY"
        blockers = list(out.get("blockers") or [])
        reason = "training panel uses current-instrument identity fallback; survivorship control is not proven"
        if reason not in blockers:
            blockers.append(reason)
        out["blockers"] = blockers
    return out


def lake_source_state(layout) -> tuple[list[str], dict]:
    """Read the catalogue watermark without enumerating the entire candle lake."""
    import duckdb

    if not Path(layout.analytics_db).exists():
        return [], {}
    db = duckdb.connect(str(layout.analytics_db), read_only=True)
    try:
        views = {row[0] for row in db.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema='main'"
        ).fetchall()}
        if "curated_candles" not in views:
            return [], {}
        meta = {}
        if "research_catalog_meta" in views:
            meta = {str(key): str(value) for key, value in db.execute(
                "SELECT key,value FROM research_catalog_meta"
            ).fetchall()}
        start = str(meta.get("candle_start_date") or "")[:10]
        end = str(meta.get("candle_end_date") or "")[:10]
        if not start or not end:
            row = db.execute("""
                SELECT CAST(min(CAST(ts AS DATE)) AS VARCHAR),
                       CAST(max(CAST(ts AS DATE)) AS VARCHAR)
                  FROM curated_candles
                 WHERE LOWER(interval) IN ('1d','day','1day')
            """).fetchone()
            start = str(row[0])[:10] if row and row[0] else ""
            end = str(row[1])[:10] if row and row[1] else ""
        dates = [value for value in (start, end) if value]
        watermark = {
            "catalogue_fingerprint": meta.get("catalogue_fingerprint"),
            "catalog_version": meta.get("catalog_version"),
            "candle_file_count": meta.get("candle_file_count"),
            "source_start": start or None,
            "source_end": end or None,
        }
        return dates, watermark
    finally:
        db.close()


class ResearchPanelStageError(RuntimeError):
    def __init__(self, stage: str, detail: str):
        self.stage = str(stage)
        self.detail = str(detail)
        super().__init__(f"{self.stage}: {self.detail}")


def load_panel_from_lake(layout, start_date=None):
    """Read the R46 materialized Delivery research projection.

    R46 moves identity/delivery/NSE joins into the catalogue refresh and stores a
    deterministic bounded cross-section for every historical date.  The trainer
    therefore no longer performs three multi-million-row ``fetchdf`` operations
    plus Pandas merges.  Source history remains complete in Parquet; this is only
    the isolated shadow-training compute projection and has production influence 0.
    """
    import duckdb
    import pandas as pd

    if not Path(layout.analytics_db).exists():
        raise ResearchPanelStageError("ANALYTICS_DB_MISSING", str(layout.analytics_db))
    try:
        db = duckdb.connect(str(layout.analytics_db), read_only=True)
    except Exception as exc:
        raise ResearchPanelStageError("ANALYTICS_DB_OPEN_FAILED", f"{type(exc).__name__}: {exc}") from exc
    try:
        views = {row[0] for row in db.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema='main'"
        ).fetchall()}
        if "research_delivery_training_panel" not in views:
            raise ResearchPanelStageError(
                "MATERIALIZED_PANEL_MISSING",
                "refresh_research_catalog.py must materialize research_delivery_training_panel before training",
            )
        params = []
        where = ""
        if start_date:
            where = "WHERE date >= CAST(? AS DATE)"
            params.append(str(start_date))
        try:
            panel = db.execute(f"""
                SELECT * EXCLUDE(research_liquidity_value)
                  FROM research_delivery_training_panel
                  {where}
                 ORDER BY symbol,date
            """, params).fetchdf()
        except Exception as exc:
            raise ResearchPanelStageError("MATERIALIZED_PANEL_READ_FAILED", f"{type(exc).__name__}: {exc}") from exc
        if panel.empty:
            raise ResearchPanelStageError("MATERIALIZED_PANEL_EMPTY", f"start_date={start_date or 'FULL_HISTORY'}")
        try:
            panel["date"] = pd.to_datetime(panel["date"], errors="coerce", format="%Y-%m-%d")
            panel = panel.dropna(subset=["date", "symbol", "close"])
            # R46 removes the three giant merge copies.  Downcast numeric source
            # columns immediately so the one full-history frame uses bounded RAM
            # without discarding any eligible symbol/date row.
            for column in panel.columns:
                if column not in {"date", "symbol", "instrument_key", "universe_join_authority", "research_selection_bucket", "nse_source_lineage"}:
                    if str(panel[column].dtype).startswith(("float", "int", "uint")):
                        panel[column] = pd.to_numeric(panel[column], errors="coerce", downcast="float")
            if panel.empty:
                raise ValueError("all materialized rows were invalid after date/symbol/close normalization")
        except Exception as exc:
            raise ResearchPanelStageError("PANEL_NORMALIZATION_FAILED", f"{type(exc).__name__}: {exc}") from exc
        # Historical membership is controlled when every row is backed either
        # by the exact effective-dated security master or by a positive canonical
        # NSE-equity daily observation on that date. Current-universe membership
        # is never allowed to certify survivorship control.
        authority_values = set(str(value) for value in panel.get("universe_join_authority", []).dropna().unique())
        universe_proof = resolve_survivorship_authority(authority_values)
        point_in_time_used = bool(universe_proof["controlled"])
        panel.attrs["price_basis"] = (
            "CORPORATE_ACTION_ADJUSTED" if "curated_adjusted_candles" in views else "SOURCE_CLOSE_UNADJUSTED"
        )
        panel.attrs["point_in_time_universe"] = point_in_time_used
        panel.attrs["universe_join_authority"] = universe_proof["state"]
        panel.attrs["universe_join_authorities"] = universe_proof["authorities"]
        panel.attrs["research_panel_authority"] = "R46_MATERIALIZED_FULL_TEMPORAL_DEPTH_FULL_CROSS_SECTION"
        panel.attrs["production_influence"] = 0
        return panel
    finally:
        db.close()


def build_features(panel, horizon):
    import numpy as np
    import pandas as pd

    frames = []
    for symbol, g in panel.groupby("symbol", sort=False):
        g = g.sort_values("date").copy()
        # Point-in-time state sources remain effective until superseded. Daily
        # activity/risk/event quantities are deliberately not forward-filled.
        persistent_official = [
            "nse_index_weight", "nse_beta", "nse_market_cap", "nse_free_float_market_cap",
            "nse_revenue", "nse_ebitda", "nse_net_profit", "nse_eps",
            "nse_promoter_holding_pct", "nse_fii_holding_pct", "nse_dii_holding_pct",
            "nse_ownership_change_pct", "nse_has_index_context", "nse_has_filings",
            "nse_has_security_master",
        ]
        for column in persistent_official:
            if column in g.columns:
                g[column] = g[column].ffill()
        close = pd.to_numeric(g["close"], errors="coerce")
        volume = pd.to_numeric(g["volume"], errors="coerce").replace(0, np.nan)
        delivery = pd.to_numeric(g["deliverable_qty"], errors="coerce")
        delivery_pct = pd.to_numeric(g["delivery_pct"], errors="coerce")
        high = pd.to_numeric(g["high"], errors="coerce")
        low = pd.to_numeric(g["low"], errors="coerce")
        def optional_number(name):
            if name in g.columns:
                return pd.to_numeric(g[name], errors="coerce")
            return pd.Series(np.nan, index=g.index, dtype="float64")
        nse_turnover = optional_number("nse_turnover").replace(0, np.nan)
        nse_trades = optional_number("nse_number_of_trades").replace(0, np.nan)
        nse_bulk = optional_number("nse_bulk_qty")
        nse_block = optional_number("nse_block_qty")
        nse_short = optional_number("nse_short_qty")
        nse_margin = optional_number("nse_margin_qty")
        nse_signed_deal = optional_number("nse_signed_deal_qty")
        nse_high_52w = optional_number("nse_high_52w")
        nse_low_52w = optional_number("nse_low_52w")
        for availability in NSE_OFFICIAL_AVAILABILITY_FEATURES:
            g[availability] = optional_number(availability).fillna(0.0).clip(0, 1)
        g["ret_1"] = close.pct_change(1)
        g["ret_5"] = close.pct_change(5)
        g["ret_20"] = close.pct_change(20)
        g["volatility_20"] = g["ret_1"].rolling(20, min_periods=12).std()
        g["range_pct"] = (high - low) / close.replace(0, np.nan)
        g["volume_z20"] = (volume - volume.rolling(20, min_periods=12).mean()) / volume.rolling(20, min_periods=12).std().replace(0, np.nan)
        g["delivery_z20"] = (delivery - delivery.rolling(20, min_periods=12).mean()) / delivery.rolling(20, min_periods=12).std().replace(0, np.nan)
        g["delivery_spread"] = delivery_pct - delivery_pct.rolling(20, min_periods=12).mean()
        turnover_log = np.log1p(nse_turnover)
        trades_log = np.log1p(nse_trades)
        g["nse_turnover_z20"] = (turnover_log - turnover_log.rolling(20, min_periods=12).mean()) / turnover_log.rolling(20, min_periods=12).std().replace(0, np.nan)
        g["nse_trades_z20"] = (trades_log - trades_log.rolling(20, min_periods=12).mean()) / trades_log.rolling(20, min_periods=12).std().replace(0, np.nan)
        g["nse_daily_volatility"] = optional_number("nse_daily_volatility")
        g["nse_var_margin"] = optional_number("nse_var_margin")
        g["nse_impact_cost"] = optional_number("nse_impact_cost")
        g["nse_index_weight"] = optional_number("nse_index_weight")
        g["nse_beta"] = optional_number("nse_beta")
        market_cap = optional_number("nse_market_cap")
        free_float_cap = optional_number("nse_free_float_market_cap")
        g["nse_market_cap_log"] = np.log1p(market_cap.clip(lower=0))
        g["nse_free_float_ratio"] = free_float_cap / market_cap.replace(0, np.nan)
        traded_base = optional_number("traded_qty").fillna(volume).replace(0, np.nan)
        deal_available = g["nse_has_deal_events"] > 0
        g["nse_event_pressure"] = ((nse_bulk.fillna(0) + nse_block.fillna(0) - nse_short.fillna(0)) / traded_base).where(deal_available)
        g["nse_signed_deal_ratio"] = (nse_signed_deal / traded_base).where(deal_available)
        g["nse_short_ratio"] = (nse_short / traded_base).where(deal_available)
        g["nse_margin_ratio"] = (nse_margin / traded_base).where(deal_available)
        g["nse_surveillance_flag"] = optional_number("nse_surveillance_flag").where(g["nse_has_surveillance"] > 0)
        band_low = optional_number("nse_price_band_low")
        band_high = optional_number("nse_price_band_high")
        g["nse_price_band_position"] = ((close - band_low) / (band_high - band_low).replace(0, np.nan)).where(g["nse_has_risk"] > 0)
        g["nse_price_band_change_pct"] = optional_number("nse_price_band_change_pct").where(g["nse_has_surveillance"] > 0)
        g["distance_52w_high"] = (close / nse_high_52w.replace(0, np.nan) - 1.0).where(g["nse_has_surveillance"] > 0)
        g["distance_52w_low"] = (close / nse_low_52w.replace(0, np.nan) - 1.0).where(g["nse_has_surveillance"] > 0)
        revenue = optional_number("nse_revenue")
        g["nse_net_profit_margin"] = (optional_number("nse_net_profit") / revenue.replace(0, np.nan)).where(g["nse_has_filings"] > 0)
        g["nse_ebitda_margin"] = (optional_number("nse_ebitda") / revenue.replace(0, np.nan)).where(g["nse_has_filings"] > 0)
        g["nse_eps"] = optional_number("nse_eps").where(g["nse_has_filings"] > 0)
        g["nse_promoter_holding_pct"] = optional_number("nse_promoter_holding_pct").where(g["nse_has_filings"] > 0)
        g["nse_institutional_holding_pct"] = (optional_number("nse_fii_holding_pct") + optional_number("nse_dii_holding_pct")).where(g["nse_has_filings"] > 0)
        g["nse_ownership_change_pct"] = optional_number("nse_ownership_change_pct").where(g["nse_has_filings"] > 0)
        g["nse_source_family_coverage"] = optional_number("nse_source_family_count").fillna(0.0).clip(0, 9) / 9.0

        sma20 = close.rolling(20, min_periods=12).mean()
        sma50 = close.rolling(50, min_periods=30).mean()
        sma100 = close.rolling(100, min_periods=60).mean()
        sma200 = close.rolling(200, min_periods=120).mean()
        ema20 = close.ewm(span=20, adjust=False, min_periods=12).mean()
        ema50 = close.ewm(span=50, adjust=False, min_periods=30).mean()
        price_std20 = close.rolling(20, min_periods=12).std().replace(0, np.nan)
        previous_close = close.shift(1)
        true_range = pd.concat([
            high - low,
            (high - previous_close).abs(),
            (low - previous_close).abs(),
        ], axis=1).max(axis=1)
        atr20 = true_range.rolling(20, min_periods=12).mean().replace(0, np.nan)
        low252 = close.rolling(252, min_periods=126).min()
        high252 = close.rolling(252, min_periods=126).max()

        g["close_sma20_dist"] = close / sma20.replace(0, np.nan) - 1.0
        g["close_sma50_dist"] = close / sma50.replace(0, np.nan) - 1.0
        g["close_sma100_dist"] = close / sma100.replace(0, np.nan) - 1.0
        g["close_sma200_dist"] = close / sma200.replace(0, np.nan) - 1.0
        g["close_ema20_dist"] = close / ema20.replace(0, np.nan) - 1.0
        g["close_ema50_dist"] = close / ema50.replace(0, np.nan) - 1.0
        g["equilibrium_z20"] = (close - sma20) / price_std20
        g["equilibrium_atr20"] = (close - sma20) / atr20
        g["sma20_slope_5"] = sma20.pct_change(5, fill_method=None)
        g["sma50_slope_10"] = sma50.pct_change(10, fill_method=None)
        g["price_location_252"] = (close - low252) / (high252 - low252).replace(0, np.nan)

        # Auxiliary Shadow labels answer a different question from return:
        # where will price sit relative to its evolving equilibrium, and does
        # it revisit the near-equilibrium band within the forecast horizon?
        full_horizon = close.shift(-horizon).notna()
        future_equilibrium = g["equilibrium_atr20"].shift(-horizon)
        future_abs_path = pd.concat(
            [g["equilibrium_atr20"].shift(-step).abs() for step in range(1, horizon + 1)],
            axis=1,
        ).min(axis=1, skipna=False)
        g["forward_equilibrium_atr20"] = future_equilibrium.where(full_horizon)
        g["reverted_to_equilibrium"] = ((future_abs_path <= 0.5).astype(float)).where(full_horizon)
        g["outcome_date"] = g["date"].shift(-horizon).where(full_horizon)
        g["forward_return"] = close.shift(-horizon) / close - 1.0
        frames.append(g)
    out = pd.concat(frames, ignore_index=True)
    # Missing delivery/OI is neutral and explicitly imputed only after the
    # availability-sensitive rolling formulas have been calculated.
    out[FEATURES] = out[FEATURES].replace([np.inf, -np.inf], np.nan)
    cross_section_medians = out.groupby("date")[FEATURES].transform("median")
    out[FEATURES] = out[FEATURES].fillna(cross_section_medians).fillna(0.0)
    out, target_audit = sanitize_supervised_targets(out)
    out.attrs["supervised_target_sanitation"] = target_audit
    return out


def sanitize_supervised_targets(frame):
    """Fail closed on mathematically undefined supervised labels.

    Historical source anomalies such as a zero close can create +/-Infinity in
    a future-return label even when the feature matrix itself is finite. Those
    rows are invalid supervised observations. Convert only non-finite target
    values to missing so existing dropna admission excludes them. Finite values
    are never clipped and no WFA gate is changed.
    """
    import numpy as np
    import pandas as pd

    out = frame.copy()
    audit = {"policy": SUPERVISED_TARGET_POLICY, "columns": {}, "nonfinite_removed": 0}
    for column in SUPERVISED_TARGET_COLUMNS:
        if column not in out.columns:
            continue
        values = pd.to_numeric(out[column], errors="coerce")
        nonfinite = values.isin([np.inf, -np.inf])
        count = int(nonfinite.sum())
        if count:
            values = values.mask(nonfinite, np.nan)
        out[column] = values
        audit["columns"][column] = {"nonfinite_removed": count}
        audit["nonfinite_removed"] += count
    return out, audit


def attach_historical_regime_labels(featured):
    """Attach point-in-time, cross-sectional historical regime labels.

    Thresholds use only prior sessions (all rolling references are shifted), so
    the label can be consumed by walk-forward validation without future leakage.
    The live transition authority remains ``MarketRegimeChangeService``.
    """
    import numpy as np
    import pandas as pd

    frame = featured.copy()
    daily = frame.groupby("date", as_index=False).agg(
        breadth=("ret_1", lambda values: float((values > 0).mean())),
        mean_return=("ret_1", "mean"),
        cross_section_vol=("ret_1", "std"),
        trend_score=("sma20_slope_5", "median"),
        participation=("delivery_spread", "median"),
    ).sort_values("date")
    daily["cross_section_vol"] = daily["cross_section_vol"].fillna(0.0)
    expanding_vol = daily["cross_section_vol"].expanding(min_periods=20).quantile(.80).shift(1)
    rolling_vol = daily["cross_section_vol"].rolling(126, min_periods=40).quantile(.80).shift(1)
    daily["volatility_threshold"] = rolling_vol.fillna(expanding_vol).fillna(float("inf"))

    reference_columns = ["breadth", "mean_return", "cross_section_vol", "trend_score", "participation"]
    scores = []
    for column in reference_columns:
        mean = daily[column].rolling(63, min_periods=20).mean().shift(1)
        std = daily[column].rolling(63, min_periods=20).std().shift(1).replace(0, np.nan)
        scores.append(((daily[column] - mean) / std).fillna(0.0).clip(-6, 6) ** 2)
    distance = np.sqrt(sum(scores) / len(scores))
    daily["regime_change_probability"] = (1.0 / (1.0 + np.exp(-(distance - 1.5) * 1.6))).clip(0.0, 1.0)

    def classify(row):
        if row.cross_section_vol >= row.volatility_threshold:
            return "VOLATILE"
        if row.breadth >= .58 and row.mean_return > 0 and row.trend_score > 0:
            return "BULL"
        if row.breadth <= .42 and row.mean_return < 0 and row.trend_score < 0:
            return "BEAR"
        return "RANGE"

    daily["market_regime"] = daily.apply(classify, axis=1)
    daily["regime_changed"] = daily["market_regime"].ne(daily["market_regime"].shift(1))
    return frame.merge(
        daily[["date", "market_regime", "regime_change_probability", "regime_changed"]],
        on="date",
        how="left",
        validate="many_to_one",
    )


def equilibrium_diagnostics(row, *, expected_distance_atr20=None, reversion_probability=None) -> dict:
    def f(name):
        try:
            value = float(row[name])
            return value if value == value and abs(value) != float("inf") else None
        except (KeyError, TypeError, ValueError):
            return None

    atr_distance = f("equilibrium_atr20")
    sma20_distance = f("close_sma20_dist")
    sma50_distance = f("close_sma50_dist")
    slope = f("sma20_slope_5")
    if atr_distance is None:
        state = "UNKNOWN"
    elif atr_distance >= 2.0:
        state = "EXTENDED_ABOVE"
    elif atr_distance >= 0.5:
        state = "ABOVE_EQUILIBRIUM"
    elif atr_distance <= -2.0:
        state = "EXTENDED_BELOW"
    elif atr_distance <= -0.5:
        state = "BELOW_EQUILIBRIUM"
    else:
        state = "NEAR_EQUILIBRIUM"
    trend = "RISING" if slope is not None and slope > 0.005 else "FALLING" if slope is not None and slope < -0.005 else "FLAT"
    return {
        "state": state,
        "trend": trend,
        "distance_atr20": atr_distance,
        "distance_sma20_pct": sma20_distance,
        "distance_sma50_pct": sma50_distance,
        "price_location_252": f("price_location_252"),
        "expected_distance_atr20_horizon": expected_distance_atr20,
        "raw_reversion_probability": reversion_probability,
        "reversion_probability_authority": "UNCALIBRATED_SHADOW" if reversion_probability is not None else "UNAVAILABLE",
    }


def dataset_fingerprint(frame, horizon):
    basis = {
        "rows": int(len(frame)), "symbols": int(frame.symbol.nunique()),
        "start": str(frame.date.min()), "end": str(frame.date.max()),
        "horizon": int(horizon), "features": FEATURES,
        "close_sum": round(float(frame.close.fillna(0).sum()), 4),
    }
    return hashlib.sha256(json.dumps(basis, sort_keys=True).encode()).hexdigest(), basis


def out_of_sample_predictions(
    train,
    horizon,
    min_train_dates=252,
    test_dates=63,
    max_folds=6,
    *,
    cache_path=None,
    model_spec_hash="",
    train_window_days=None,
    adaptive_history_mode=None,
):
    """Produce purged OOF predictions while reusing unchanged fold artifacts."""
    import joblib
    import pandas as pd
    from sklearn.ensemble import HistGradientBoostingRegressor

    dates = sorted(train.date.dt.strftime("%Y-%m-%d").unique())
    cache = {"model_spec_hash": model_spec_hash, "folds": {}}
    cache_path = Path(cache_path) if cache_path else None
    if cache_path and cache_path.is_file():
        try:
            loaded = joblib.load(cache_path)
            if isinstance(loaded, dict) and loaded.get("model_spec_hash") == model_spec_hash:
                cache = loaded
        except Exception:
            pass
    outputs = []
    reused_folds = 0
    trained_folds = 0
    start = min_train_dates
    fold = 0
    # max_folds <= 0 means consume every eligible chronological fold. This is
    # the deep-evidence path used by the standard model; first-use mode may
    # still pass a positive bound for a deliberately faster shadow cycle.
    while start < len(dates) and (int(max_folds) <= 0 or fold < int(max_folds)):
        train_end = start - horizon
        end = min(len(dates), start + test_dates)
        if train_end <= 0:
            break
        tr_dates, te_dates = set(rolling_train_date_slice(dates, train_end, train_window_days)), set(dates[start:end])
        tr = train[train.date.dt.strftime("%Y-%m-%d").isin(tr_dates)]
        te = train[train.date.dt.strftime("%Y-%m-%d").isin(te_dates)]
        fit_tr = tr
        fit_weights = None
        history_summary = None
        if adaptive_history_mode:
            fit_tr, fit_weights, history_summary = training_frame_and_weights(tr, mode=str(adaptive_history_mode))
            eligible_symbols = set(fit_tr["symbol"].astype(str).str.upper().unique()) if len(fit_tr) else set()
            te = te[te["symbol"].astype(str).str.upper().isin(eligible_symbols)].copy()
        if len(fit_tr) < 1000 or te.empty:
            start = end
            continue
        fit_dates = sorted(fit_tr.date.dt.strftime("%Y-%m-%d").unique())
        signature_basis = {
            "model_spec_hash": model_spec_hash,
            "horizon": int(horizon),
            "train_start": min(fit_dates),
            "train_end": max(fit_dates),
            "test_start": min(te_dates),
            "test_end": max(te_dates),
            "train_rows": int(len(fit_tr)),
            "train_symbols": int(fit_tr["symbol"].nunique()),
            "test_rows": int(len(te)),
            "train_close_sum": round(float(fit_tr["close"].fillna(0).sum()), 6),
            "test_close_sum": round(float(te["close"].fillna(0).sum()), 6),
            "history_summary": history_summary,
        }
        signature = hashlib.sha256(json.dumps(signature_basis, sort_keys=True).encode()).hexdigest()
        cached = (cache.get("folds") or {}).get(signature)
        if isinstance(cached, dict) and isinstance(cached.get("rows"), list) and isinstance(cached.get("lineage"), dict) and len(cached["rows"]) == len(te):
            part = pd.DataFrame(cached["rows"])
            part["date"] = pd.to_datetime(part["date"], errors="coerce")
            part["outcome_date"] = pd.to_datetime(part["outcome_date"], errors="coerce")
            lineage = dict(cached["lineage"])
            reused_folds += 1
        else:
            model = HistGradientBoostingRegressor(
                max_iter=180, learning_rate=.045, max_leaf_nodes=15,
                l2_regularization=1.0, random_state=4510,
            )
            if fit_weights is not None:
                model.fit(fit_tr[FEATURES], fit_tr["forward_return"], sample_weight=fit_weights.loc[fit_tr.index])
            else:
                model.fit(fit_tr[FEATURES], fit_tr["forward_return"])
            model_hash = hashlib.sha256(pickle.dumps(model, protocol=pickle.HIGHEST_PROTOCOL)).hexdigest()
            lineage = {
                "oof_model_hash": model_hash,
                "oof_train_start": min(fit_dates),
                "oof_train_end": max(fit_dates),
                "oof_feature_cutoff": f"{max(fit_dates)}T15:30:00+05:30",
                "oof_train_rows": int(len(fit_tr)),
                "oof_train_symbols": int(fit_tr["symbol"].nunique()),
                "oof_history_policy": (history_summary or {}).get("history_policy") if history_summary else "BOUNDED_FIRST_USE_SHADOW",
                "oof_symbol_history_min": (history_summary or {}).get("symbol_history_min") if history_summary else None,
                "oof_symbol_history_median": (history_summary or {}).get("symbol_history_median") if history_summary else None,
                "oof_symbol_history_max": (history_summary or {}).get("symbol_history_max") if history_summary else None,
                "oof_artifact_kind": "HISTORICAL_FOLD_MODEL_BINARY_SHA256",
                "oof_artifact_generated_at": _now(),
                "oof_fold_signature": signature,
            }
            columns = ["date", "outcome_date", "symbol", "forward_return", "ret_20", "sma20_slope_5", "market_regime", "regime_change_probability", "regime_changed"]
            for optional in ("corporate_action_adjusted", "corporate_action_coverage_hash", "universe_join_authority"):
                if optional in te.columns:
                    columns.append(optional)
            part = te[columns].copy()
            part["prediction"] = model.predict(te[FEATURES])
            part["fold"] = fold + 1
            part["fold_signature"] = signature
            for key, value in lineage.items():
                part[key] = value
            part["oof_prediction_timestamp"] = part["date"].dt.strftime("%Y-%m-%dT15:29:00+05:30")
            cache.setdefault("folds", {})[signature] = {
                "rows": json.loads(part.to_json(orient="records", date_format="iso")),
                "lineage": lineage,
            }
            trained_folds += 1
        if "oof_model_hash" not in part.columns:
            for key, value in lineage.items():
                part[key] = value
            part["oof_prediction_timestamp"] = part["date"].dt.strftime("%Y-%m-%dT15:29:00+05:30")
        outputs.append(part)
        fold += 1
        start = end
    if not outputs:
        raise RuntimeError("History cannot form an expanding purged train/test fold yet.")
    if cache_path:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        temp = cache_path.with_name(cache_path.name + f".{os.getpid()}.tmp")
        joblib.dump(cache, temp)
        os.replace(temp, cache_path)
    result = pd.concat(outputs, ignore_index=True)
    result.attrs["fold_cache"] = {
        "reused_folds": reused_folds,
        "trained_folds": trained_folds,
        "cache_path": str(cache_path) if cache_path else None,
    }
    return result


def validation_observations(
    oof,
    *,
    dataset_fingerprint,
    feature_manifest_hash,
    quality_authority,
    horizon,
    cost_return=.0015,
):
    observations = []
    for date, g in oof.groupby("date"):
        g = g.copy()
        g["rank"] = g.prediction.rank(pct=True, method="average")
        benchmark = float(g.forward_return.median())
        momentum_cut = g.ret_20.quantile(.80)
        trend_cut = g.sma20_slope_5.quantile(.80)
        momentum_baseline = float(g.loc[g.ret_20 >= momentum_cut, "forward_return"].mean())
        trend_baseline = float(g.loc[g.sma20_slope_5 >= trend_cut, "forward_return"].mean())
        decision_date = date.strftime("%Y-%m-%d")
        decision_as_of = f"{decision_date}T15:30:00+05:30"
        historical_session_dates = set(quality_authority.get("historical_session_dates") or [])
        session_observed = decision_date in historical_session_dates
        for _, row in g[g["rank"] >= .90].iterrows():
            outcome = row.outcome_date
            outcome_date = outcome.strftime("%Y-%m-%d") if hasattr(outcome, "strftime") else str(outcome)[:10]
            observations.append({
                "date": decision_date,
                "symbol": row.symbol,
                "mode": "delivery",
                "forward_return": float(row.forward_return),
                "benchmark_return": benchmark,
                "baseline_returns": {
                    "equal_weight_universe": benchmark,
                    "top_momentum_quintile": momentum_baseline,
                    "top_trend_quintile": trend_baseline,
                },
                "cost_return": cost_return,
                "prediction": float(row.prediction),
                "prediction_score": float(row.prediction),
                "fold": int(row.fold),
                "dataset_fingerprint": dataset_fingerprint,
                "feature_manifest_hash": feature_manifest_hash,
                "universe_id": f"nse-cash-point-in-time:{decision_date}",
                "cost_model_version": "india-cash-cost-model-1.0.0",
                "cost_model_profile": "DELIVERY_CONSERVATIVE_POST_COST",
                "execution_model_version": "model-paper-close-to-next-session-1.0.0",
                "admission_policy_version": "canonical-admission-policy-1.0.0",
                "corporate_action_adjusted": bool(row.get("corporate_action_adjusted")),
                "corporate_action_coverage_hash": row.get("corporate_action_coverage_hash"),
                "survivorship_bias_controlled": bool(resolve_survivorship_authority([row.get("universe_join_authority")]).get("controlled")),
                "decision_as_of": decision_as_of,
                "feature_as_of": decision_as_of,
                "universe_as_of": decision_as_of,
                "fundamental_as_of": decision_as_of,
                "outcome_as_of": f"{outcome_date}T15:30:00+05:30",
                "market_regime": str(row.get("market_regime") or "UNKNOWN").upper(),
                "official_nse_source_count": int(quality_authority.get("official_nse_source_count") or 0),
                "official_nse_core_source_count": int(quality_authority.get("official_nse_core_source_count") or 0),
                "official_nse_core_required_count": int(quality_authority.get("official_nse_core_required_count") or 0),
                "official_nse_sources": list(quality_authority.get("official_nse_sources") or []),
                "official_nse_missing_sources": list(quality_authority.get("official_nse_missing_sources") or []),
                "official_nse_optional_missing_sources": list(quality_authority.get("official_nse_optional_missing_sources") or []),
                "official_nse_optional_enrichment_coverage": float(quality_authority.get("official_nse_optional_enrichment_coverage") or 0.0),
                "official_nse_lineage_hash": quality_authority.get("official_nse_lineage_hash"),
                "official_nse_complete": not bool(quality_authority.get("official_nse_missing_sources")),
                "session_authority": quality_authority.get("historical_session_authority"),
                "session_authority_version": quality_authority.get("historical_session_authority_version"),
                "session_index_fingerprint": quality_authority.get("historical_session_index_fingerprint"),
                "session_observed": session_observed,
                "session_authority_ready": bool(session_observed and quality_authority.get("historical_session_index_fingerprint")),
                "session_authority_state": "HISTORICAL_SESSION_PROVEN" if session_observed else "HISTORICAL_SESSION_UNVERIFIED",
                "regime_change_probability": float(row.get("regime_change_probability") or 0.0),
                "regime_changed": bool(row.get("regime_changed")),
                "horizon_days": int(horizon),
                "oof_model_hash": row.get("oof_model_hash"),
                "oof_train_start": row.get("oof_train_start"),
                "oof_train_end": row.get("oof_train_end"),
                "oof_feature_cutoff": row.get("oof_feature_cutoff"),
                "oof_prediction_timestamp": row.get("oof_prediction_timestamp"),
                "oof_artifact_kind": row.get("oof_artifact_kind"),
                "oof_fold_signature": row.get("oof_fold_signature") or row.get("fold_signature"),
            })
    return observations


def delivery_capital_portfolio_simulator(featured, *, initial_capital=500000.0, max_concurrent_positions=10):
    """Build a deterministic no-leverage mark-to-market Delivery simulator.

    It is deliberately simple: long-only top-ranked observations, fixed slot
    capacity, no pyramiding in the same symbol, explicit round-trip cost split
    between entry/exit, and daily close mark-to-market from the same PIT panel.
    """
    import pandas as pd
    panel = featured[["date", "symbol", "close"]].dropna().copy()
    panel["date_key"] = pd.to_datetime(panel["date"], errors="coerce").dt.strftime("%Y-%m-%d")
    price = {(str(r.date_key), str(r.symbol).upper()): float(r.close) for r in panel.itertuples() if r.date_key}
    calendar = sorted({key[0] for key in price})

    def simulate(payload):
        observations = [dict(row) for row in payload.get("observations") or []]
        if not observations:
            return {"capital_constraints_enforced": False, "concurrency_constraints_enforced": False, "position_sizing_enforced": False, "mark_to_market_enforced": False, "no_leverage": False, "initial_capital": initial_capital, "max_concurrent_positions": max_concurrent_positions, "equity_curve": []}
        entries = {}
        first = min(str(row.get("date") or "")[:10] for row in observations)
        last = max(str(row.get("outcome_as_of") or row.get("date") or "")[:10] for row in observations)
        for row in observations:
            day = str(row.get("date") or "")[:10]
            entries.setdefault(day, []).append(row)
        days = [day for day in calendar if first <= day <= last]
        if not days:
            days = sorted(entries)
        cash = float(initial_capital)
        positions = []
        curve = [{"timestamp": f"{first}T09:00:00+05:30", "equity": cash}]
        missing_prices = 0
        admitted = 0
        peak_open = 0
        for day in days:
            # Exit at the day's close before admitting fresh end-of-day signals.
            remaining = []
            for pos in positions:
                if str(pos["exit_date"]) <= day:
                    px = price.get((day, pos["symbol"])) or price.get((str(pos["exit_date"]), pos["symbol"]))
                    if px is None:
                        missing_prices += 1
                        remaining.append(pos)
                        continue
                    proceeds = pos["shares"] * px
                    cash += proceeds - proceeds * pos["half_cost_rate"]
                else:
                    remaining.append(pos)
            positions = remaining

            candidates = sorted(entries.get(day, []), key=lambda row: float(row.get("prediction_score") or row.get("prediction") or row.get("rank_score") or 0.0), reverse=True)
            open_symbols = {p["symbol"] for p in positions}
            for row in candidates:
                if len(positions) >= int(max_concurrent_positions):
                    break
                symbol = str(row.get("symbol") or "").upper()
                if not symbol or symbol in open_symbols:
                    continue
                px = price.get((day, symbol))
                if px is None or px <= 0:
                    missing_prices += 1
                    continue
                equity_now = cash + sum(p["shares"] * (price.get((day, p["symbol"])) or p["entry_price"]) for p in positions)
                slot = min(cash, max(0.0, equity_now / float(max_concurrent_positions)))
                half_cost_rate = max(0.0, float(row.get("cost_return") or 0.0)) / 2.0
                shares = int(slot / (px * (1.0 + half_cost_rate)))
                if shares <= 0:
                    continue
                notional = shares * px
                cash -= notional + notional * half_cost_rate
                positions.append({
                    "symbol": symbol, "shares": shares, "entry_price": px,
                    "exit_date": str(row.get("outcome_as_of") or day)[:10],
                    "half_cost_rate": half_cost_rate,
                })
                open_symbols.add(symbol)
                admitted += 1
            peak_open = max(peak_open, len(positions))
            marked = cash
            for pos in positions:
                px = price.get((day, pos["symbol"]))
                if px is None:
                    missing_prices += 1
                    px = pos["entry_price"]
                marked += pos["shares"] * px
            curve.append({"timestamp": f"{day}T15:30:00+05:30", "equity": max(0.01, marked)})
        return {
            "initial_capital": float(initial_capital),
            "max_concurrent_positions": int(max_concurrent_positions),
            "capital_constraints_enforced": admitted > 0 and cash >= -1e-6,
            "concurrency_constraints_enforced": peak_open <= int(max_concurrent_positions),
            "position_sizing_enforced": admitted > 0 and missing_prices == 0,
            "mark_to_market_enforced": True,
            "no_leverage": True,
            "orders_admitted": admitted,
            "missing_price_events": missing_prices,
            "equity_curve": curve,
        }
    return simulate


def _factor_family(name):
    if name in MOMENTUM_LIQUIDITY_FEATURES:
        return "momentum_liquidity"
    if name in EQUILIBRIUM_FEATURES:
        return "price_equilibrium"
    if name in NSE_OFFICIAL_FEATURES:
        return "nse_official"
    return "model_feature"


def persist_factor_governance(featured, conn, horizon, dataset_fingerprint):
    """Persist truthful local NSE IC/IR, decay and redundancy evidence.

    PL26 closes the former registry-write gap without changing any threshold.
    Formula identity remains UNVERIFIED and production influence remains zero;
    empirical qualification is a hash of the measured NSE IC/IR evidence only.
    """
    from core.factors.ic_ir_runner import compute_ic_series

    ensure_factor_tables(conn)
    close = featured.pivot(index="date", columns="symbol", values="close").sort_index()
    forward = close.shift(-horizon) / close - 1.0
    decay_reports = []
    ic_details = {}
    measured_at = _now()

    for name in FEATURES:
        panel = featured.pivot(index="date", columns="symbol", values=name).reindex(index=close.index, columns=close.columns)
        daily_ic = compute_ic_series(panel, forward, min_names_per_date=5).dropna().astype(float)
        values = daily_ic.tolist()
        if len(values) < 10:
            mean_ic = std_ic = ir = hit_rate = None
            status = "insufficient_data"
        else:
            mean_ic = float(daily_ic.mean())
            std_ic = float(daily_ic.std(ddof=1))
            ir = (mean_ic / std_ic) if std_ic > 0 and math.isfinite(std_ic) else None
            hit_rate = float((daily_ic.apply(lambda v: 1 if v * mean_ic > 0 else 0)).mean()) if mean_ic else None
            if not math.isfinite(mean_ic) or abs(mean_ic) < DEFAULT_ALIVE_IC_THRESHOLD:
                status = "dead"
            elif mean_ic > 0:
                status = "alive"
            else:
                status = "reversed"

        empirical_basis = {
            "factor_name": name, "horizon_days": int(horizon), "n_dates": len(values),
            "mean_ic": mean_ic, "std_ic": std_ic, "ir": ir, "hit_rate": hit_rate,
            "status": status, "dataset_fingerprint": str(dataset_fingerprint or ""),
            "alive_ic_threshold": DEFAULT_ALIVE_IC_THRESHOLD,
        }
        empirical_hash = hashlib.sha256(strict_json_dumps(empirical_basis, sort_keys=True).encode("utf-8")).hexdigest()
        upsert_factor_registry(conn, FactorRegistryRow(
            factor_name=name, family=_factor_family(name), ic_score=mean_ic, ir_score=ir,
            status=status, last_validated=measured_at, formula_class="UNVERIFIED",
            formula_verification_hash=None, empirical_qualification_hash=empirical_hash,
            production_influence=0,
        ))
        ic_details[name] = dict(empirical_basis, empirical_qualification_hash=empirical_hash)

        decay = evaluate_decay(name, values, recent_dates=20)
        record_decay_report(conn, decay)
        decay_reports.append(decay.to_dict())

    # Redundancy is measured directly on a bounded, disclosed recent slice of
    # the already-materialized PIT feature panel. No factor-value history is
    # invented or backfilled merely to satisfy governance.
    unique_dates = sorted(featured["date"].dropna().unique())
    audit_dates = unique_dates[-252:]
    audit_source = featured.loc[featured["date"].isin(audit_dates), FEATURES]
    pre_rows = [vars(row) for row in get_factor_registry(conn)]
    dedup = FactorDedupService().audit_frame(
        audit_source, registry_rows=pre_rows, threshold=DEFAULT_THRESHOLD,
        min_overlap=DEFAULT_MIN_OVERLAP,
    )
    dedup.update({
        "source": "IN_MEMORY_MATERIALIZED_PIT_TRAINING_PANEL",
        "sample_dates": len(audit_dates),
        "sample_start": str(audit_dates[0])[:10] if audit_dates else None,
        "sample_end": str(audit_dates[-1])[:10] if audit_dates else None,
        "production_influence": 0,
    })
    if dedup.get("ok"):
        FactorDedupService.persist_report(conn, dedup)

    registry_evidence = []
    for row in get_factor_registry(conn):
        item = vars(row).copy()
        item.update({
            "n_dates": ic_details.get(row.factor_name, {}).get("n_dates", 0),
            "std_ic": ic_details.get(row.factor_name, {}).get("std_ic"),
            "hit_rate": ic_details.get(row.factor_name, {}).get("hit_rate"),
            "horizon_days": int(horizon),
            "alive_ic_threshold": DEFAULT_ALIVE_IC_THRESHOLD,
        })
        registry_evidence.append(item)
    return decay_reports, registry_evidence, dedup


def _publish_bundle(bundle, api_url, outbox_dir):
    # RFC-compliant transport/persistence: non-finite research diagnostics are
    # unknown values, not JSON numbers. Preserve the evidence shape as null.
    safe_bundle = dict(json_safe(bundle) or {})
    payload = strict_json_dumps(safe_bundle, sort_keys=True).encode("utf-8")
    endpoint = str(api_url).rstrip("/") + "/api/ai/training-publication"
    req = urllib_request.Request(endpoint, data=payload, method="POST", headers={"Content-Type": "application/json"})
    try:
        with urllib_request.urlopen(req, timeout=30) as response:
            body = json.loads(response.read().decode("utf-8") or "{}")
        if not body.get("ok"):
            raise RuntimeError(body.get("error") or body.get("state") or "publication endpoint rejected bundle")
        return dict(body, publication_transport="HTTP")
    except (URLError, HTTPError, TimeoutError, json.JSONDecodeError, RuntimeError) as exc:
        outbox_dir = Path(outbox_dir)
        outbox_dir.mkdir(parents=True, exist_ok=True)
        path = outbox_dir / f"{safe_bundle['publication_id']}.json"
        atomic_write_json(path, safe_bundle)
        return {
            "ok": True,
            "state": "PUBLICATION_PENDING",
            "publication_transport": "DURABLE_OUTBOX",
            "outbox_file": str(path),
            "endpoint_error": str(exc),
        }


def _atomic_dump_model(joblib, payload, artifact):
    artifact = Path(artifact)
    artifact.parent.mkdir(parents=True, exist_ok=True)
    temp = artifact.with_name(artifact.name + f".{os.getpid()}.tmp")
    joblib.dump(payload, temp)
    os.replace(temp, artifact)
    return artifact


def coverage_readiness(coverage: dict, *, min_dates: int = 315, first_mode: bool = False) -> dict:
    production_ready = (
        int(coverage.get("dates") or 0) >= int(min_dates)
        and int(coverage.get("rows") or 0) >= 3000
        and int(coverage.get("symbols") or 0) >= 25
        and int(coverage.get("delivery_dates") or 0) >= 252
    )
    standard_shadow_ready = (
        int(coverage.get("dates") or 0) >= 126
        and int(coverage.get("rows") or 0) >= 1500
        and int(coverage.get("symbols") or 0) >= 15
        and int(coverage.get("delivery_dates") or 0) >= 100
    )
    first_mode_shadow_ready = (
        bool(first_mode)
        and int(coverage.get("dates") or 0) >= 126
        and int(coverage.get("rows") or 0) >= 800
        and int(coverage.get("symbols") or 0) >= 10
        and int(coverage.get("delivery_dates") or 0) >= 60
    )
    return {
        "production_ready": production_ready,
        "standard_shadow_ready": standard_shadow_ready,
        "first_mode_shadow_ready": first_mode_shadow_ready,
        "shadow_ready": standard_shadow_ready or first_mode_shadow_ready,
    }


def _run_training(scratch_db: Path, layout: StorageLayout, horizon=10, min_dates=315, first_mode=False):
    """Run one governed, incremental Delivery training cycle.

    The raw Parquet lake is queried only when the source watermark changed. A
    versioned feature store reopens only the label-maturation tail. Unchanged
    purged folds are reused from a durable OOF cache, and an identical labelled
    dataset/model specification skips training entirely.
    """
    import joblib
    from sklearn.ensemble import HistGradientBoostingClassifier, HistGradientBoostingRegressor

    conn = sqlite3.connect(str(scratch_db), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    try:
        store = Store(conn)
        governance = AIGovernanceService(store)
        model_id = FIRST_MODE_MODEL_ID if first_mode else MODEL_ID
        feature_hash = hashlib.sha256(json.dumps({
            "features": FEATURES,
            "builder": "equilibrium-nse-official-feature-builder-6.1.0-pl42-row-scoped-corporate-actions",
        }, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        model_spec = {
            "model_id": model_id,
            "model_family": "hist_gradient_boosting",
            "horizon": int(horizon),
            "first_mode": bool(first_mode),
            "features": FEATURES,
            "primary_estimator": {
                "type": "HistGradientBoostingRegressor",
                "max_iter": 220,
                "learning_rate": .04,
                "max_leaf_nodes": 15,
                "l2_regularization": 1.0,
                "random_state": 4510,
            },
            "oof_estimator": {
                "max_iter": 180,
                "learning_rate": .045,
                "max_leaf_nodes": 15,
                "l2_regularization": 1.0,
                "random_state": 4510,
            },
            "cost_model": "india-cash-cost-model-1.0.0",
            "supervised_target_policy": SUPERVISED_TARGET_POLICY,
            "walk_forward_authority": "purged-embargoed-capital-profile",
            "historical_train_reference_days": int(policy_for_mode("delivery").reference_days) if not first_mode else None,
            "historical_train_minimum_days": int(policy_for_mode("delivery").minimum_days) if not first_mode else None,
            "historical_train_maximum_days": int(policy_for_mode("delivery").maximum_days) if not first_mode else None,
            "historical_symbol_minimum_days": int(policy_for_mode("delivery").per_symbol_minimum_days) if not first_mode else None,
            "historical_recency_half_life_days": int(policy_for_mode("delivery").recency_half_life_days) if not first_mode else None,
            "historical_train_window_policy": "ADAPTIVE_ALL_ELIGIBLE_HISTORY_BY_SYMBOL_AND_MODE" if not first_mode else "BOUNDED_FIRST_USE_SHADOW",
            "evidence_publication_contract": "capital-wfa-postgres-1.0.0-pl24",
            "fold_local_artifact_contract": FOLD_LOCAL_ARTIFACT_CONTRACT if not first_mode else None,
        }
        model_spec_hash = hashlib.sha256(json.dumps(model_spec, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        source_dates, source_watermark = lake_source_state(layout)
        if not source_dates:
            raise RuntimeError("Curated Parquet/DuckDB daily source dates are unavailable. Refresh the production-authority research catalogue first.")
        quality_authority = data_quality_authority(layout)

        # Fast no-op path: no full feature Parquet read and no ML imports/fits
        # when both the source watermark and governed model specification match.
        manifest_file = feature_manifest_path(layout, mode="delivery", horizon=horizon)
        feature_manifest = read_manifest(manifest_file)
        latest_run = read_manifest(layout.manifests_dir / "latest-training-run.json")
        store_file = feature_store_path(layout, mode="delivery", horizon=horizon)
        manifest_current = bool(
            store_file.is_file()
            and feature_manifest.get("source_watermark") == source_watermark
            and feature_manifest.get("feature_definition_hash") == feature_hash
            and int(feature_manifest.get("horizon") or -1) == int(horizon)
        )
        if manifest_current and training_is_current(
            latest_run,
            dataset_fingerprint=str(feature_manifest.get("dataset_fingerprint") or ""),
            model_spec_hash=model_spec_hash,
            labelled_through=str(feature_manifest.get("labelled_through") or ""),
        ):
            quality_authority = reconcile_panel_quality_authority(quality_authority, feature_manifest)
            return {
                "ok": True,
                "skipped": True,
                "state": "TRAINING_NOT_REQUIRED",
                "created_at": _now(),
                "model_id": model_id,
                "dataset_fingerprint": feature_manifest.get("dataset_fingerprint"),
                "model_spec_hash": model_spec_hash,
                "labelled_through": feature_manifest.get("labelled_through"),
                "feature_store": dict(feature_manifest, state="FEATURE_STORE_CURRENT_NO_READ"),
                "data_quality_authority": quality_authority,
                "reason": "Source watermark, feature definition, labelled-through date and model specification are unchanged.",
            }

        featured, feature_store = materialize_feature_store(
            layout,
            mode="delivery",
            horizon=horizon,
            feature_names=FEATURES,
            feature_definition_hash=feature_hash,
            source_dates=source_dates,
            source_watermark=source_watermark,
            load_panel=lambda start_date: load_panel_from_lake(layout, start_date),
            build_features=lambda panel, h: attach_historical_regime_labels(build_features(panel, h)),
        )
        data_source = TRAINING_PIPELINE_SOURCE
        quality_authority = reconcile_panel_quality_authority(quality_authority, feature_store)
        # PL27 may already have persisted a feature store containing non-finite
        # targets. Sanitize after every store read so that store can be reused
        # without rebuilding the 4.69M-row research catalogue.
        featured, target_sanitation = sanitize_supervised_targets(featured)
        labelled = featured.dropna(subset=["forward_return"]).copy()
        corporate_action_filter = {"required": not first_mode, "eligible_rows": int(len(labelled)), "excluded_rows": 0}
        if not first_mode:
            if "corporate_action_adjusted" not in labelled.columns:
                raise RuntimeError("Row-scoped corporate-action authority is absent from the historical training panel.")
            before = len(labelled)
            labelled = labelled[labelled["corporate_action_adjusted"].fillna(False).astype(bool)].copy()
            corporate_action_filter.update({"eligible_rows": int(len(labelled)), "excluded_rows": int(before - len(labelled))})
        if labelled.empty:
            raise RuntimeError("Incremental feature store contains no corporate-action-qualified completed forward-return labels.")
        coverage = {
            "symbols": int(labelled.symbol.nunique()),
            "dates": int(labelled.date.nunique()),
            "rows": int(len(labelled)),
            "start": labelled.date.min().strftime("%Y-%m-%d"),
            "end": labelled.date.max().strftime("%Y-%m-%d"),
            "delivery_dates": int(featured.loc[featured.delivery_pct.notna(), "date"].nunique()),
        }
        readiness = coverage_readiness(coverage, min_dates=min_dates, first_mode=first_mode)
        production_coverage_ready = readiness["production_ready"]
        shadow_coverage_ready = readiness["shadow_ready"]
        coverage.update(readiness)
        coverage["first_mode"] = bool(first_mode)
        if not shadow_coverage_ready:
            requirement = (
                ">=126 daily dates, 60 delivery dates, 800 rows and 10 symbols"
                if first_mode else
                ">=126 daily dates, 100 delivery dates, 1500 rows and 15 symbols"
            )
            raise RuntimeError(f"Insufficient even for shadow training: {coverage}. Need {requirement}.")

        fingerprint = str(feature_store.get("dataset_fingerprint") or "")
        basis = dict(feature_store.get("dataset_basis") or {})
        labelled_through = str(feature_store.get("labelled_through") or coverage["end"])
        # R20 deep-evidence policy: the standard model starts only after a
        # substantial multi-year training history when available and then
        # consumes every remaining chronological fold. It no longer truncates
        # evidence to six folds / ~252 test sessions while much deeper retained
        # PIT history exists. First-use mode stays intentionally bounded/shadow.
        if first_mode:
            adaptive_floor, adaptive_cap = 126, 252
            adaptive_train_dates = max(adaptive_floor, min(adaptive_cap, int(coverage["dates"] * .50)))
            oof_test_dates, oof_max_folds = 42, 6
        else:
            # PL42: use all eligible history. 500 is a Delivery reference for the
            # first stable OOF fold, not a cap. Later folds expand through all prior
            # eligible sessions; only an explicit operator resource ceiling bounds it.
            training_policy = resolve_historical_training_policy(
                int(coverage.get("dates") or 0), horizon_days=horizon, mode="delivery"
            )
            if not training_policy["ready"]:
                raise RuntimeError(f"Historical ML training policy is not ready: {training_policy}")
            adaptive_train_dates = int(training_policy["initial_wfa_train_days"])
            oof_test_dates, oof_max_folds = 63, 0  # 0 = every eligible fold
        fold_cache_path = layout.prediction_lake_dir / "walk_forward" / "delivery" / f"{model_id}-h{horizon}.joblib"
        oof_start_dates = adaptive_train_dates if first_mode else adaptive_train_dates + int(horizon)
        oof = out_of_sample_predictions(
            labelled,
            horizon,
            min_train_dates=oof_start_dates,
            test_dates=oof_test_dates,
            max_folds=oof_max_folds,
            cache_path=fold_cache_path,
            model_spec_hash=model_spec_hash,
            train_window_days=(None if first_mode or int(training_policy.get("maximum_days") or 0) <= 0 else int(training_policy["maximum_days"])),
            adaptive_history_mode=(None if first_mode else "delivery"),
        )
        fold_cache = dict(oof.attrs.get("fold_cache") or {})
        fold_cache.update({
            "history_dates_available": int(coverage.get("dates") or 0),
            "initial_train_dates": int(adaptive_train_dates),
            "first_test_start_offset_dates": int(oof_start_dates),
            "purge_days_before_first_test": int(horizon),
            "oof_test_dates_per_fold": int(oof_test_dates),
            "oof_fold_count": int(oof["fold"].nunique()) if "fold" in oof.columns else 0,
            "evidence_depth_policy": "EXPANDING_ALL_ELIGIBLE_HISTORY_BY_SYMBOL_MODE_RECENCY_WEIGHTED" if not first_mode else "BOUNDED_FIRST_USE_SHADOW",
            "historical_training_policy": training_policy if not first_mode else None,
            "historical_training_policy_days": int(adaptive_train_dates) if not first_mode else None,
        })
        observations = validation_observations(
            oof,
            dataset_fingerprint=fingerprint,
            feature_manifest_hash=feature_hash,
            quality_authority=quality_authority,
            horizon=horizon,
        )
        validator = WalkForwardValidationService(store)
        fold_local_trainer = None if first_mode else make_delivery_fold_local_trainer(
            labelled, model_spec_hash=model_spec_hash,
            artifact_dir=layout.prediction_lake_dir / "walk_forward" / "delivery" / "fold_local_models",
        )
        validation = validator.validate(
            model_id,
            observations,
            horizon_days=horizon,
            min_train_days=42 if first_mode else 60,
            test_days=21,
            purge_days=horizon,
            embargo_days=horizon,
            max_folds=4 if first_mode else 6,
            min_samples=60 if first_mode else 100,
            trial_count=1,
            persist=True,
            fold_trainer=fold_local_trainer,
        )
        capital_validation = validator.validate_capital(
            model_id,
            observations,
            horizon_days=horizon,
            min_train_days=42 if first_mode else 126,
            test_days=21 if first_mode else 63,
            purge_days=horizon,
            embargo_days=horizon,
            max_folds=4 if first_mode else 40,
            min_samples=300,
            trial_count=1,
            portfolio_simulator=delivery_capital_portfolio_simulator(featured),
            persist=True,
            fold_trainer=fold_local_trainer,
        )

        final_training_source = labelled
        final_history_summary = None
        final_weights = None
        if not first_mode:
            ceiling = int(training_policy.get("maximum_days") or 0)
            if ceiling > 0:
                final_training_source, _ = recent_date_window(labelled, ceiling)
            final_training, final_weights, final_history_summary = training_frame_and_weights(
                final_training_source, mode="delivery"
            )
            if len(final_training) < 1000:
                raise RuntimeError(f"Adaptive all-history final training set is too small after per-stock eligibility: {final_history_summary}")
        else:
            final_training, final_training_dates = recent_date_window(labelled, adaptive_train_dates)
        final_training_dates = sorted(final_training.date.dt.strftime("%Y-%m-%d").unique())
        final_training_symbols = set(final_training["symbol"].astype(str).str.upper().unique())
        final_model = HistGradientBoostingRegressor(
            max_iter=220, learning_rate=.04, max_leaf_nodes=15,
            l2_regularization=1.0, random_state=4510,
        )
        if final_weights is not None:
            final_model.fit(final_training[FEATURES], final_training["forward_return"], sample_weight=final_weights.loc[final_training.index])
        else:
            final_model.fit(final_training[FEATURES], final_training["forward_return"])

        equilibrium_labelled = featured.dropna(subset=["forward_equilibrium_atr20"]).copy()
        equilibrium_weights = None
        if not first_mode:
            final_training_date_set = set(final_training_dates)
            equilibrium_labelled = equilibrium_labelled[
                equilibrium_labelled.date.dt.strftime("%Y-%m-%d").isin(final_training_date_set)
                & equilibrium_labelled["symbol"].astype(str).str.upper().isin(final_training_symbols)
                & equilibrium_labelled["corporate_action_adjusted"].fillna(False).astype(bool)
            ].copy()
            equilibrium_labelled, equilibrium_weights, _ = training_frame_and_weights(
                equilibrium_labelled, mode="delivery", eligible_symbols=final_training_symbols
            )
        equilibrium_model = HistGradientBoostingRegressor(
            max_iter=180, learning_rate=.04, max_leaf_nodes=15,
            l2_regularization=1.0, random_state=4511,
        )
        if equilibrium_weights is not None:
            equilibrium_model.fit(equilibrium_labelled[FEATURES], equilibrium_labelled["forward_equilibrium_atr20"], sample_weight=equilibrium_weights.loc[equilibrium_labelled.index])
        else:
            equilibrium_model.fit(equilibrium_labelled[FEATURES], equilibrium_labelled["forward_equilibrium_atr20"])

        reversion_labelled = featured.dropna(subset=["reverted_to_equilibrium"]).copy()
        reversion_weights = None
        if not first_mode:
            reversion_labelled = reversion_labelled[
                reversion_labelled.date.dt.strftime("%Y-%m-%d").isin(final_training_date_set)
                & reversion_labelled["symbol"].astype(str).str.upper().isin(final_training_symbols)
                & reversion_labelled["corporate_action_adjusted"].fillna(False).astype(bool)
            ].copy()
            reversion_labelled, reversion_weights, _ = training_frame_and_weights(
                reversion_labelled, mode="delivery", eligible_symbols=final_training_symbols
            )
        reversion_model = None
        if reversion_labelled["reverted_to_equilibrium"].nunique() >= 2:
            reversion_model = HistGradientBoostingClassifier(
                max_iter=180, learning_rate=.04, max_leaf_nodes=15,
                l2_regularization=1.0, random_state=4512,
            )
            if reversion_weights is not None:
                reversion_model.fit(reversion_labelled[FEATURES], reversion_labelled["reverted_to_equilibrium"].astype(int), sample_weight=reversion_weights.loc[reversion_labelled.index])
            else:
                reversion_model.fit(reversion_labelled[FEATURES], reversion_labelled["reverted_to_equilibrium"].astype(int))

        artifact = layout.models_dir / "delivery" / f"{model_id}.joblib"
        _atomic_dump_model(joblib, {
            "model": final_model,
            "equilibrium_distance_model": equilibrium_model,
            "equilibrium_reversion_model": reversion_model,
            "features": FEATURES,
            "feature_groups": {
                "momentum_liquidity": MOMENTUM_LIQUIDITY_FEATURES,
                "price_equilibrium": EQUILIBRIUM_FEATURES,
            },
            "dataset_fingerprint": fingerprint,
            "feature_manifest_hash": feature_hash,
            "model_spec_hash": model_spec_hash,
            "model_family": "hist_gradient_boosting",
            "feature_store": feature_store,
            "trained_at": _now(),
            "trained_through": coverage["end"],
            "labelled_through": labelled_through,
            "historical_training_policy": training_policy if not first_mode else None,
            "training_history_summary": final_history_summary if not first_mode else None,
            "corporate_action_filter": corporate_action_filter,
            "final_training_dates": int(len(final_training_dates)),
            "final_training_symbols": int(len(final_training_symbols)),
            "final_training_start": final_training_dates[0] if final_training_dates else None,
            "final_training_end": final_training_dates[-1] if final_training_dates else None,
        }, artifact)
        decay_reports, factor_registry_evidence, factor_redundancy_audit = persist_factor_governance(
            featured, conn, horizon, fingerprint
        )
        factor_authority = FactorAuthorityService(store).authorize(FEATURES)
        approved = (
            bool(capital_validation.get("approved"))
            and production_coverage_ready
            and factor_authority.get("eligible", False)
            and quality_authority.get("eligible", False)
        )
        model_record = {
            "model_id": model_id,
            "model_version": "4.2.0-pl26-factor-governance-first-mode" if first_mode else "4.4.0-pl44-fold-local-capital-wfa",
            "framework": "Incremental PIT Parquet + adaptive all-history fold-local purged/embargoed WFA + official NSE authority + equilibrium-aware HistGradientBoosting",
            "model_family": "hist_gradient_boosting",
            "horizon_days": horizon,
            "lifecycle_state": "SHADOW",
            "requested_lifecycle_state": "SHADOW" if first_mode else ("BACKTEST_VALIDATED" if approved else "SHADOW"),
            "approval_id": capital_validation.get("approval_id") if approved else None,
            "evaluation_paper_weight": .05 if first_mode else (.10 if approved else .05),
            "production_weight": 0.0,
            "feature_manifest_hash": feature_hash,
            "model_spec_hash": model_spec_hash,
            "production_weight_policy": PRODUCTION_WEIGHT_POLICY,
            "first_mode": bool(first_mode),
            "publication_authority": PUBLICATION_AUTHORITY,
            "broker_authority": "NONE",
            "dataset_fingerprint": fingerprint,
            "trained_through": coverage["end"],
            "labelled_through": labelled_through,
            "artifact_uri": str(artifact),
            "coverage": coverage,
            "supervised_target_sanitation": target_sanitation,
            "dataset_basis": basis,
            "training_data_source": TRAINING_DATA_AUTHORITY,
            "training_pipeline_source": data_source,
            "price_basis": quality_authority["price_basis"],
            "data_quality_authority": quality_authority,
            "feature_store": feature_store,
            "fold_cache": fold_cache,
            "factor_names": FEATURES,
            "feature_groups": {
                "momentum_liquidity": MOMENTUM_LIQUIDITY_FEATURES,
                "price_equilibrium": EQUILIBRIUM_FEATURES,
            },
            "auxiliary_outputs": {
                "expected_equilibrium_distance_atr20": "SHADOW_DIAGNOSTIC",
                "reversion_to_equilibrium_probability": "UNCALIBRATED_SHADOW",
            },
            "factor_authority_at_registration": factor_authority,
            "factor_registry_evidence": factor_registry_evidence,
            "factor_redundancy_audit": factor_redundancy_audit,
            "evidence": {
                "samples": int(coverage.get("rows") or 0),
                "dates": int(coverage.get("dates") or 0),
                "symbols": int(coverage.get("symbols") or 0),
                "point_in_time": bool(quality_authority.get("point_in_time_universe")),
                "purged_walk_forward": int(capital_validation.get("purge_days") or 0) > 0,
                "embargo": int(capital_validation.get("embargo_days") or 0) > 0,
                "costs_included": float(capital_validation.get("cost_coverage") or 0.0) >= 1.0,
                "holdout_untouched": False,
                "baseline_comparison": bool(capital_validation.get("complete_baselines")),
                "trial_count_recorded": int(capital_validation.get("trial_count") or 0) >= 1,
                "multiple_testing_control": capital_validation.get("multiple_test_adjusted_pvalue") is not None,
                "feature_redundancy_audited": bool(factor_redundancy_audit.get("ok")),
            },
            "validation_summary": {k: capital_validation.get(k) for k in (
                "status", "n_test", "mean_net_return", "mean_excess_return", "win_rate",
                "fold_stability", "max_drawdown", "corporate_action_coverage",
                "survivorship_control_coverage", "lineage_coverage",
                "official_nse_lineage_coverage", "official_nse_complete_coverage",
                "fold_local_training_requested", "fold_local_training_proven",
                "capital_model_training_proven", "validation_kind",
            )},
        }
        governance.register_model(model_record)
        latest_date = featured.date.max()
        latest = featured[featured.date == latest_date].copy()
        latest["prediction"] = final_model.predict(latest[FEATURES])
        latest["expected_equilibrium_atr20"] = equilibrium_model.predict(latest[FEATURES])
        if reversion_model is not None:
            positive_index = list(reversion_model.classes_).index(1)
            latest["raw_reversion_probability"] = reversion_model.predict_proba(latest[FEATURES])[:, positive_index]
        else:
            latest["raw_reversion_probability"] = float("nan")
        latest["rank"] = latest.prediction.rank(pct=True) * 100
        predictions = []
        for _, row in latest.iterrows():
            rank = float(row["rank"])
            confidence = min(.85, .50 + abs(rank - 50) / 100)
            prediction = {
                "model_id": model_id,
                "symbol": row.symbol,
                "mode": "delivery",
                "as_of": _now(),
                "horizon_days": horizon,
                "rank_score": rank,
                "expected_excess_return": float(row.prediction),
                "confidence": confidence,
                "equilibrium": equilibrium_diagnostics(
                    row,
                    expected_distance_atr20=float(row.expected_equilibrium_atr20),
                    reversion_probability=(
                        float(row.raw_reversion_probability)
                        if row.raw_reversion_probability == row.raw_reversion_probability else None
                    ),
                ),
                "feature_manifest_hash": feature_hash,
                "dataset_fingerprint": fingerprint,
                "model_spec_hash": model_spec_hash,
            }
            governance.record_prediction(prediction)
            predictions.append(prediction)
        publication_basis = {
            "model_id": model_id,
            "dataset_fingerprint": fingerprint,
            "model_spec_hash": model_spec_hash,
            "trained_through": coverage["end"],
            "labelled_through": labelled_through,
            "prediction_count": len(predictions),
        }
        publication_id = hashlib.sha256(json.dumps(publication_basis, sort_keys=True).encode()).hexdigest()[:24]
        return {
            "ok": True,
            "publication_id": publication_id,
            "publication_version": "ai-training-publication-3.1.0-pl26-factor-governance",
            "created_at": _now(),
            "dataset_fingerprint": fingerprint,
            "model_spec_hash": model_spec_hash,
            "labelled_through": labelled_through,
            "model": model_record,
            "training_data_source": TRAINING_DATA_AUTHORITY,
            "training_pipeline_source": data_source,
            "validation": validation,
            "capital_validation": capital_validation,
            "factor_decay": decay_reports,
            "factor_registry": factor_registry_evidence,
            "factor_redundancy": factor_redundancy_audit,
            "predictions": predictions,
            "feature_store": feature_store,
            "fold_cache": fold_cache,
            "summary": {
                "coverage": coverage,
                "supervised_target_sanitation": target_sanitation,
                "oof_predictions": int(len(oof)),
                "latest_predictions": int(len(latest)),
                "factor_authority": factor_authority,
                "factor_registry_count": len(factor_registry_evidence),
                "factor_redundancy": factor_redundancy_audit,
                "data_quality_authority": quality_authority,
                "final_model_training_rows": int(len(final_training)),
                "final_model_training_dates": int(len(final_training_dates)),
                "equilibrium_distance_samples": int(len(equilibrium_labelled)),
                "equilibrium_reversion_samples": int(len(reversion_labelled)),
                "feature_store": feature_store,
                "fold_cache": fold_cache,
            },
        }
    finally:
        conn.close()


def run(data_dir: Path, api_url: str, horizon=10, min_dates=315, first_mode=False):
    layout = StorageLayout.from_data_dir(Path(data_dir))
    layout.ensure()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    scratch = layout.training_scratch_dir / f"governance-{stamp}-{os.getpid()}.sqlite3"
    cleanup_abandoned_sqlite_artifacts(layout.training_scratch_dir, older_than_seconds=3600)
    with interprocess_lock(layout.locks_dir / "ai-training.lock", timeout_seconds=1.0):
        with interprocess_lock(layout.locks_dir / "analytical-pipeline.lock", timeout_seconds=3600.0):
            try:
                if not lake_training_available(layout):
                    raise RuntimeError("Curated Parquet/DuckDB training panel is unavailable. Run the production-authority research catalogue refresh before training.")
                bundle = _run_training(scratch, layout, horizon=horizon, min_dates=min_dates, first_mode=first_mode)
                source = "R46_MATERIALIZED_RESEARCH_PANEL_TO_INCREMENTAL_FEATURE_STORE"
                if bundle.get("skipped"):
                    publication = {
                        "ok": True,
                        "state": "TRAINING_NOT_REQUIRED",
                        "publication_transport": "NONE",
                        "reason": bundle.get("reason"),
                    }
                else:
                    publication = _publish_bundle(bundle, api_url, layout.publication_outbox_dir)
            finally:
                remove_sqlite_family(scratch)

    if bundle.get("skipped"):
        result = {
            "ok": True,
            "state": "TRAINING_NOT_REQUIRED",
            "model_id": bundle["model_id"],
            "dataset_fingerprint": bundle.get("dataset_fingerprint"),
            "model_spec_hash": bundle.get("model_spec_hash"),
            "labelled_through": bundle.get("labelled_through"),
            "feature_store": bundle.get("feature_store"),
            "data_quality_authority": bundle.get("data_quality_authority"),
            "training_data_source": source,
            "publication": publication,
            "training_source_policy": TRAINING_SOURCE_POLICY,
            "publication_authority": PUBLICATION_AUTHORITY,
            "production_weight_policy": PRODUCTION_WEIGHT_POLICY,
            "first_mode": bool(first_mode),
            "temporary_operational_snapshot_retained": False,
        }
    else:
        summary = bundle["summary"]
        result = {
            "ok": True,
            "state": publication.get("state") or "TRAINED_AND_PUBLISHED",
            "model_id": bundle["model"]["model_id"],
            "lifecycle": bundle["model"]["lifecycle_state"],
            "coverage": summary["coverage"],
            "dataset_fingerprint": bundle.get("dataset_fingerprint"),
            "model_spec_hash": bundle.get("model_spec_hash"),
            "labelled_through": bundle.get("labelled_through"),
            "oof_predictions": summary["oof_predictions"],
            "validation": bundle["validation"],
            "capital_validation": bundle["capital_validation"],
            "factor_decay": bundle["factor_decay"],
            "factor_authority": summary["factor_authority"],
            "data_quality_authority": summary["data_quality_authority"],
            "feature_store": summary["feature_store"],
            "fold_cache": summary["fold_cache"],
            "equilibrium_distance_samples": summary["equilibrium_distance_samples"],
            "equilibrium_reversion_samples": summary["equilibrium_reversion_samples"],
            "latest_predictions": summary["latest_predictions"],
            "artifact": bundle["model"]["artifact_uri"],
            "training_data_source": source,
            "temporary_operational_snapshot_retained": False,
            "publication": publication,
            "training_source_policy": TRAINING_SOURCE_POLICY,
            "publication_authority": PUBLICATION_AUTHORITY,
            "production_weight_policy": PRODUCTION_WEIGHT_POLICY,
            "first_mode": bool(first_mode),
        }
    run_dir = layout.manifests_dir / "training_runs"
    run_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(run_dir / f"training-{stamp}.json", result)
    # A no-op check must not erase the previous successful evidence fields.
    if result["state"] != "TRAINING_NOT_REQUIRED":
        atomic_write_json(layout.manifests_dir / "latest-training-run.json", result)
    else:
        atomic_write_json(layout.manifests_dir / "latest-training-check.json", result)
    print(json.dumps(result, indent=2, default=str))
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Train the governed NSE SmartAI delivery model")
    parser.add_argument("--data-dir", type=Path, default=Path(r"C:\ProgramData\ProjectLaddu\data"))
    parser.add_argument("--api-url", default="http://127.0.0.1:8086")
    parser.add_argument("--horizon", type=int, default=10)
    parser.add_argument("--min-dates", type=int, default=315)
    parser.add_argument("--first-mode", action="store_true", help="Train a bounded diagnostic Shadow model from the governed first-useful cohort; effective influence is zero only during deterministic model fallback")
    args = parser.parse_args()
    try:
        run(args.data_dir, args.api_url, max(5, args.horizon), max(126 if args.first_mode else 252, args.min_dates), first_mode=args.first_mode)
    except TimeoutError as exc:
        reason = str(exc)
        duplicate = "ai-training.lock" in reason
        print(json.dumps({
            "ok": False,
            "state": "TRAINING_ALREADY_RUNNING" if duplicate else "ANALYTICAL_PIPELINE_BUSY",
            "reason": reason,
            "next": "Wait for the existing trainer to finish." if duplicate else "Lake sync or analytical maintenance is still running; training will retry on the next schedule or can be started after it finishes."
        }, indent=2))
        raise SystemExit(3)
    except Exception as exc:
        reason = str(exc)
        if "database is locked" in reason.lower():
            next_step = "Operational snapshot could not be created within 30 seconds; retry after the current critical write completes."
        elif isinstance(exc, ResearchPanelStageError):
            next_step = "Research-panel stage failed explicitly; inspect the reported stage. Do not infer that historical data is empty."
        else:
            next_step = "Check data coverage, the training log, and the durable publication outbox."
        print(json.dumps({"ok": False, "state": "TRAINING_BLOCKED", "reason": reason,
                          "panel_stage": getattr(exc, "stage", None),
                          "next": next_step}, indent=2))
        raise SystemExit(2)
