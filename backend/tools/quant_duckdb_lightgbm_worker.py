"""Isolated DuckDB/LightGBM worker for Project Laddu shadow research.

This process is launched only by QuantAnalyticsService using the persistent
research Python.  It never imports into the live web-service process and has no
production database write path: SQLite is opened read-only for projection.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import statistics
import sys
from typing import Any, Dict, Iterable, Mapping, Sequence

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from core.nse_cross_sectional_selector_service import FEATURE_MANIFEST_HASH
from core.quant_research_dataset_service import QuantResearchDatasetService
from core.walk_forward_validation_service import WalkForwardValidationService


WORKER_VERSION = "duckdb-lightgbm-worker-1.1.1-r6-revalidated"
MODEL_RULE_VERSION = "lightgbm-lambdarank-quant-paper-rule-1.1.0"
SHADOW_MIN_OBSERVATIONS = 100
SHADOW_MIN_DAYS = 20
MIN_OBSERVATIONS = 340
MIN_DAYS = 126
MIN_HOLDOUT_DAYS = 20
MIN_HOLDOUT_OBSERVATIONS = 40
MIN_REGIMES = 3
MIN_FOLDS = 3


def shadow_training_ready(*, observations: int, trading_days: int, required_days: int) -> bool:
    """Early-learning gate; never a production/promotion qualification gate."""
    return int(observations) >= SHADOW_MIN_OBSERVATIONS and int(trading_days) >= max(SHADOW_MIN_DAYS, int(required_days))


def production_validation_ready(*, observations: int, trading_days: int, regimes: int, horizon_days: int) -> bool:
    """Immutable statistical qualification evidence floor."""
    return (
        int(observations) >= MIN_OBSERVATIONS
        and int(trading_days) >= MIN_DAYS + MIN_HOLDOUT_DAYS + int(horizon_days)
        and int(regimes) >= MIN_REGIMES
    )

LIGHTGBM_FIXED_PARAMETERS = {
    "objective": "lambdarank",
    "metric": "ndcg",
    "n_estimators": 180,
    "learning_rate": 0.035,
    "num_leaves": 15,
    "max_depth": 5,
    "min_child_samples": 15,
    "subsample": 0.85,
    "colsample_bytree": 0.85,
    "reg_lambda": 1.0,
    "verbosity": -1,
    "deterministic": True,
    "force_col_wise": True,
}


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def digest(value: Any, length: int = 64) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8", errors="replace")).hexdigest()[:length]


def timestamp(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _finite(value: Any) -> float | None:
    try:
        parsed = float(value)
        return parsed if math.isfinite(parsed) else None
    except (TypeError, ValueError):
        return None


def read_sqlite_rows(path: Path, table: str, mode: str) -> list[Dict[str, Any]]:
    uri = f"{path.as_uri()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    try:
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        if not exists:
            return []
        return [
            dict(row)
            for row in connection.execute(
                f"SELECT * FROM {table} WHERE mode=? ORDER BY 1",
                (mode,),
            ).fetchall()
        ]
    finally:
        connection.close()


def row_json(row: Mapping[str, Any]) -> str:
    return canonical(dict(row))


def content_hash(snapshot_json: Sequence[str], label_json: Sequence[str]) -> str:
    return digest({
        "snapshots": sorted(snapshot_json),
        "labels": sorted(label_json),
    })


def sql_path(path: Path) -> str:
    return str(path.resolve()).replace("'", "''")


def ensure_projection_schema(connection: Any) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS quant_feature_snapshot_projection (
          snapshot_id VARCHAR PRIMARY KEY,
          candidate_id VARCHAR NOT NULL,
          population_fingerprint VARCHAR NOT NULL,
          symbol VARCHAR NOT NULL,
          mode VARCHAR NOT NULL,
          decision_ts VARCHAR NOT NULL,
          dataset_fingerprint VARCHAR NOT NULL,
          feature_manifest_hash VARCHAR NOT NULL,
          feature_hash VARCHAR NOT NULL,
          feature_json VARCHAR NOT NULL,
          compact_feature_coverage DOUBLE NOT NULL,
          regime_tag VARCHAR NOT NULL,
          snapshot_state VARCHAR NOT NULL,
          lineage_state VARCHAR NOT NULL,
          snapshot_hash VARCHAR NOT NULL,
          source_as_of VARCHAR,
          received_at VARCHAR,
          row_json VARCHAR NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS quant_label_vector_projection (
          candidate_id VARCHAR NOT NULL,
          horizon VARCHAR NOT NULL,
          snapshot_id VARCHAR NOT NULL,
          symbol VARCHAR NOT NULL,
          mode VARCHAR NOT NULL,
          observed_at VARCHAR NOT NULL,
          settled_at VARCHAR NOT NULL,
          market_regime VARCHAR NOT NULL,
          net_return_bps DOUBLE NOT NULL,
          net_return_plus_20bps DOUBLE NOT NULL,
          target_before_stop INTEGER,
          mfe_bps DOUBLE,
          mae_bps DOUBLE,
          time_to_outcome_bars INTEGER,
          record_hash VARCHAR NOT NULL,
          row_json VARCHAR NOT NULL,
          PRIMARY KEY(candidate_id,horizon)
        )
        """
    )
    connection.execute(
        """
        CREATE OR REPLACE VIEW quant_training_projection AS
        SELECT
          s.snapshot_id,s.candidate_id,s.symbol,s.mode,s.decision_ts,
          s.dataset_fingerprint,s.feature_manifest_hash,s.feature_hash,s.feature_json,
          s.compact_feature_coverage,s.regime_tag,s.snapshot_state,s.lineage_state,
          l.horizon,l.market_regime AS label_regime,l.net_return_bps,
          l.net_return_plus_20bps,l.target_before_stop,l.mfe_bps,l.mae_bps,
          l.time_to_outcome_bars,l.settled_at,l.record_hash
        FROM quant_feature_snapshot_projection s
        JOIN quant_label_vector_projection l ON l.snapshot_id=s.snapshot_id
        WHERE s.snapshot_state='COMPLETE' AND s.lineage_state='VERIFIED'
        """
    )


def project(args: argparse.Namespace) -> Dict[str, Any]:
    import duckdb

    sqlite_path = Path(args.sqlite).resolve()
    duckdb_path = Path(args.duckdb).resolve()
    parquet_dir = Path(args.parquet_dir).resolve()
    snapshots = read_sqlite_rows(sqlite_path, "quant_feature_snapshots", args.mode)
    labels = read_sqlite_rows(sqlite_path, "quant_label_vectors", args.mode)
    snapshot_json = [row_json(row) for row in snapshots]
    label_json = [row_json(row) for row in labels]
    source_hash = content_hash(snapshot_json, label_json)

    duckdb_path.parent.mkdir(parents=True, exist_ok=True)
    parquet_dir.mkdir(parents=True, exist_ok=True)
    connection = duckdb.connect(str(duckdb_path))
    try:
        ensure_projection_schema(connection)
        connection.execute("BEGIN TRANSACTION")
        connection.execute("DELETE FROM quant_label_vector_projection WHERE mode=?", [args.mode])
        connection.execute("DELETE FROM quant_feature_snapshot_projection WHERE mode=?", [args.mode])
        if snapshots:
            connection.executemany(
                """INSERT INTO quant_feature_snapshot_projection VALUES(
                    ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
                )""",
                [
                    (
                        row.get("snapshot_id"), row.get("candidate_id"),
                        row.get("population_fingerprint"), row.get("symbol"), row.get("mode"),
                        row.get("decision_ts"), row.get("dataset_fingerprint"),
                        row.get("feature_manifest_hash"), row.get("feature_hash"),
                        row.get("feature_json"), row.get("compact_feature_coverage"),
                        row.get("regime_tag"), row.get("snapshot_state"),
                        row.get("lineage_state"), row.get("snapshot_hash"),
                        row.get("source_as_of"), row.get("received_at"), raw_json,
                    )
                    for row, raw_json in zip(snapshots, snapshot_json)
                ],
            )
        if labels:
            connection.executemany(
                """INSERT INTO quant_label_vector_projection VALUES(
                    ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
                )""",
                [
                    (
                        row.get("candidate_id"), row.get("horizon"), row.get("snapshot_id"),
                        row.get("symbol"), row.get("mode"), row.get("observed_at"),
                        row.get("settled_at"), row.get("market_regime"),
                        row.get("net_return_bps"), row.get("net_return_plus_20bps"),
                        row.get("target_before_stop"), row.get("mfe_bps"), row.get("mae_bps"),
                        row.get("time_to_outcome_bars"), row.get("record_hash"), raw_json,
                    )
                    for row, raw_json in zip(labels, label_json)
                ],
            )
        connection.execute("COMMIT")

        projected_snapshot_json = [
            row[0] for row in connection.execute(
                "SELECT row_json FROM quant_feature_snapshot_projection WHERE mode=? ORDER BY snapshot_id",
                [args.mode],
            ).fetchall()
        ]
        projected_label_json = [
            row[0] for row in connection.execute(
                """SELECT row_json FROM quant_label_vector_projection
                   WHERE mode=? ORDER BY candidate_id,horizon""",
                [args.mode],
            ).fetchall()
        ]
        projected_hash = content_hash(projected_snapshot_json, projected_label_json)
        snapshot_count = len(projected_snapshot_json)
        label_count = len(projected_label_json)

        snapshots_parquet = parquet_dir / f"{args.mode}_quant_feature_snapshots.parquet"
        labels_parquet = parquet_dir / f"{args.mode}_quant_label_vectors.parquet"
        connection.execute(
            f"""COPY (
                  SELECT * FROM quant_feature_snapshot_projection WHERE mode='{args.mode}'
                ) TO '{sql_path(snapshots_parquet)}' (FORMAT PARQUET, COMPRESSION ZSTD)"""
        )
        connection.execute(
            f"""COPY (
                  SELECT * FROM quant_label_vector_projection WHERE mode='{args.mode}'
                ) TO '{sql_path(labels_parquet)}' (FORMAT PARQUET, COMPRESSION ZSTD)"""
        )
    finally:
        connection.close()

    reconciled = (
        len(snapshots) == snapshot_count
        and len(labels) == label_count
        and source_hash == projected_hash
    )
    projection_id = digest({
        "mode": args.mode,
        "source_content_hash": source_hash,
        "snapshot_count": len(snapshots),
        "label_count": len(labels),
    }, 40)
    return {
        "ok": True,
        "state": "RECONCILED" if reconciled else "MISMATCH",
        "version": WORKER_VERSION,
        "projection_id": projection_id,
        "mode": args.mode,
        "sqlite_snapshot_count": len(snapshots),
        "sqlite_label_count": len(labels),
        "duckdb_snapshot_count": snapshot_count,
        "duckdb_label_count": label_count,
        "source_content_hash": source_hash,
        "projected_content_hash": projected_hash,
        "parquet_paths": {
            "snapshots": str(snapshots_parquet),
            "labels": str(labels_parquet),
        },
    }


def duckdb_rows(path: Path, mode: str, horizon: str) -> list[Dict[str, Any]]:
    import duckdb

    connection = duckdb.connect(str(path), read_only=True)
    try:
        cursor = connection.execute(
            """SELECT * FROM quant_training_projection
               WHERE mode=? AND horizon=? ORDER BY decision_ts,symbol""",
            [mode, horizon],
        )
        names = [item[0] for item in cursor.description]
        return [dict(zip(names, row)) for row in cursor.fetchall()]
    finally:
        connection.close()


def impute(
    train: Sequence[Mapping[str, Any]],
    test: Sequence[Mapping[str, Any]],
) -> tuple[list[list[float]], list[list[float]], list[float]]:
    width = len(train[0]["values"])
    medians = []
    for column in range(width):
        available = [
            float(row["values"][column])
            for row in train
            if row["values"][column] is not None
        ]
        medians.append(statistics.median(available) if available else 0.0)

    def matrix(rows: Sequence[Mapping[str, Any]]) -> list[list[float]]:
        return [
            [
                float(value) if value is not None else medians[column]
                for column, value in enumerate(row["values"])
            ]
            for row in rows
        ]

    return matrix(train), matrix(test), medians


def average_ranks(values: Sequence[float]) -> list[float]:
    ordered = sorted(enumerate(values), key=lambda item: item[1])
    ranks = [0.0] * len(values)
    cursor = 0
    while cursor < len(ordered):
        end = cursor + 1
        while end < len(ordered) and ordered[end][1] == ordered[cursor][1]:
            end += 1
        rank = (cursor + 1 + end) / 2.0
        for index in range(cursor, end):
            ranks[ordered[index][0]] = rank
        cursor = end
    return ranks


def correlation(left: Sequence[float], right: Sequence[float]) -> float | None:
    if len(left) < 2 or len(left) != len(right):
        return None
    left_mean = statistics.fmean(left)
    right_mean = statistics.fmean(right)
    numerator = sum((a - left_mean) * (b - right_mean) for a, b in zip(left, right))
    denominator = math.sqrt(
        sum((a - left_mean) ** 2 for a in left)
        * sum((b - right_mean) ** 2 for b in right)
    )
    return numerator / denominator if denominator > 1e-12 else None


def ranking_metrics(
    rows: Sequence[Mapping[str, Any]],
    predictions: Sequence[float],
) -> Dict[str, Any]:
    by_date: Dict[str, list[tuple[float, float, str]]] = {}
    for row, predicted in zip(rows, predictions):
        by_date.setdefault(str(row["date"]), []).append(
            (float(predicted), float(row["net_return_plus_20bps"]), str(row["regime"]))
        )
    ics = []
    lifts = []
    regime_lifts: Dict[str, list[float]] = {}
    for pairs in by_date.values():
        if len(pairs) < 2:
            continue
        predicted = [item[0] for item in pairs]
        actual = [item[1] for item in pairs]
        rank_ic = correlation(average_ranks(predicted), average_ranks(actual))
        if rank_ic is not None:
            ics.append(rank_ic)
        top_n = max(1, math.ceil(len(pairs) * 0.20))
        ordered = sorted(pairs, key=lambda item: item[0], reverse=True)
        lift = statistics.fmean(item[1] for item in ordered[:top_n]) - statistics.fmean(actual)
        lifts.append(lift)
        for regime in {item[2] for item in pairs}:
            regime_pairs = [item for item in pairs if item[2] == regime]
            if not regime_pairs:
                continue
            regime_top = max(1, math.ceil(len(regime_pairs) * 0.20))
            regime_ordered = sorted(regime_pairs, key=lambda item: item[0], reverse=True)
            regime_lifts.setdefault(regime, []).append(
                statistics.fmean(item[1] for item in regime_ordered[:regime_top])
                - statistics.fmean(item[1] for item in regime_pairs)
            )
    regime_summary = {
        regime: round(statistics.fmean(values), 6)
        for regime, values in regime_lifts.items()
        if values
    }
    return {
        "cross_section_dates": len(ics),
        "mean_rank_ic": round(statistics.fmean(ics), 8) if ics else None,
        "positive_rank_ic_fraction": round(sum(value > 0 for value in ics) / len(ics), 8) if ics else None,
        "mean_top_quintile_lift_after_20bps": round(statistics.fmean(lifts), 6) if lifts else None,
        "regime_top_quintile_lift_after_20bps": regime_summary,
        "positive_regime_fraction": (
            round(sum(value > 0 for value in regime_summary.values()) / len(regime_summary), 8)
            if regime_summary else None
        ),
    }


def same_population_baselines(
    rows: Sequence[Mapping[str, Any]],
    matrix: Sequence[Sequence[float]],
    feature_names: Sequence[str],
    model_predictions: Sequence[float],
) -> Dict[str, Any]:
    """Compare the model with deterministic, same-date/same-cost baselines.

    Every arm receives the exact same point-in-time population and the already
    20bps-stressed return label.  This prevents a model from "winning" merely
    because its benchmark saw different symbols or cheaper execution.
    """
    if not rows or len(rows) != len(matrix) or len(rows) != len(model_predictions):
        return {
            "all_required_baselines_implemented": False,
            "state": "POPULATION_ALIGNMENT_FAILED",
            "model_beats_strongest_baseline_by_20bps": False,
        }
    preferred_momentum = (
        "momentum_20d" if "momentum_20d" in feature_names
        else "intraday_relative_strength"
        if "intraday_relative_strength" in feature_names
        else None
    )
    liquidity_name = "liquidity" if "liquidity" in feature_names else None
    if preferred_momentum is None or liquidity_name is None:
        return {
            "all_required_baselines_implemented": False,
            "state": "REQUIRED_BASELINE_FEATURE_MISSING",
            "missing": [
                name for name, present in (
                    ("momentum_or_intraday_relative_strength", preferred_momentum),
                    ("liquidity", liquidity_name),
                ) if present is None
            ],
            "model_beats_strongest_baseline_by_20bps": False,
        }
    momentum_index = list(feature_names).index(preferred_momentum)
    liquidity_index = list(feature_names).index(liquidity_name)
    random_scores = [
        int(
            hashlib.sha256(
                f"quant-baseline-seed-17|{row.get('date')}|{row.get('symbol')}|{row.get('candidate_id')}".encode()
            ).hexdigest()[:16],
            16,
        ) / float(0xFFFFFFFFFFFFFFFF)
        for row in rows
    ]
    momentum_scores = [float(values[momentum_index]) for values in matrix]
    liquidity_scores = [float(values[liquidity_index]) for values in matrix]
    model_metrics = ranking_metrics(rows, model_predictions)
    baselines = {
        "random_matched_frequency_seed_17": ranking_metrics(rows, random_scores),
        "all_eligible_equal_weight": {
            "mean_top_quintile_lift_after_20bps": 0.0,
            "definition": "all eligible observations; no ex-post sorting",
        },
        f"first_retained_{preferred_momentum}": ranking_metrics(rows, momentum_scores),
        "liquidity_only": ranking_metrics(rows, liquidity_scores),
    }
    lifts = []
    for report in baselines.values():
        lift = _finite(report.get("mean_top_quintile_lift_after_20bps"))
        if lift is not None:
            lifts.append(lift)
    strongest = max(lifts) if lifts else 0.0
    model_lift = _finite(model_metrics.get("mean_top_quintile_lift_after_20bps"))
    advantage = model_lift - strongest if model_lift is not None else None
    all_eligible_mean = statistics.fmean(
        float(row["net_return_plus_20bps"]) for row in rows
    )
    return {
        "state": "MEASURED",
        "same_population_count": len(rows),
        "cost_label": "net_return_plus_20bps",
        "all_required_baselines_implemented": True,
        "model": model_metrics,
        "baselines": baselines,
        "all_eligible_mean_net_return_after_20bps": round(all_eligible_mean, 6),
        "strongest_baseline_top_quintile_lift_after_20bps": round(strongest, 6),
        "model_advantage_over_strongest_baseline_bps": (
            round(advantage, 6) if advantage is not None else None
        ),
        "required_material_advantage_bps": 20.0,
        "model_beats_strongest_baseline_by_20bps": (
            advantage is not None and advantage >= 20.0
        ),
    }


def score_adapter_evidence(scores: Sequence[float]) -> Dict[str, Any]:
    """Persist deterministic development-only raw-score normalization."""
    ordered = sorted(float(value) for value in scores if math.isfinite(float(value)))
    if not ordered:
        return {"state": "NOT_AVAILABLE", "quantile_knots": []}
    percentiles = [index / 20.0 for index in range(21)]
    knots = []
    for percentile in percentiles:
        position = percentile * (len(ordered) - 1)
        lower = int(math.floor(position))
        upper = int(math.ceil(position))
        fraction = position - lower
        raw = ordered[lower] + (ordered[upper] - ordered[lower]) * fraction
        knots.append({"percentile": percentile, "raw_score": round(raw, 12)})
    scale = statistics.pstdev(ordered) if len(ordered) > 1 else 0.0
    return {
        "state": "DEVELOPMENT_ONLY_EMPIRICAL_QUANTILES",
        "version": "lightgbm-raw-score-quantile-adapter-1.0.0",
        "observations": len(ordered),
        "center": round(statistics.fmean(ordered), 12),
        "scale": round(scale if scale > 1e-12 else 1.0, 12),
        "quantile_knots": knots,
        "holdout_used_for_normalization": False,
    }


def grouped_training(
    rows: Sequence[Mapping[str, Any]],
    matrix: Sequence[Sequence[float]],
) -> tuple[list[list[float]], list[int], list[int], list[Mapping[str, Any]]]:
    paired = sorted(zip(rows, matrix), key=lambda item: (str(item[0]["date"]), str(item[0]["symbol"])))
    usable: list[tuple[Mapping[str, Any], Sequence[float]]] = []
    groups: list[int] = []
    labels: list[int] = []
    cursor = 0
    while cursor < len(paired):
        date = str(paired[cursor][0]["date"])
        end = cursor + 1
        while end < len(paired) and str(paired[end][0]["date"]) == date:
            end += 1
        chunk = paired[cursor:end]
        if len(chunk) >= 2:
            groups.append(len(chunk))
            returns = [float(item[0]["net_return_plus_20bps"]) for item in chunk]
            order = sorted(range(len(chunk)), key=lambda index: returns[index])
            grades = [0] * len(chunk)
            for rank, index in enumerate(order):
                grades[index] = min(4, int(rank * 5 / len(chunk)))
            labels.extend(grades)
            usable.extend(chunk)
        cursor = end
    return (
        [list(item[1]) for item in usable],
        labels,
        groups,
        [item[0] for item in usable],
    )


def fit_ranker(
    rows: Sequence[Mapping[str, Any]],
    matrix: Sequence[Sequence[float]],
    *,
    seed: int,
) -> tuple[Any, list[Mapping[str, Any]]]:
    import lightgbm as lgb

    train_x, relevance, groups, usable_rows = grouped_training(rows, matrix)
    if len(groups) < 2 or len(train_x) < 20:
        raise ValueError("LightGBM ranker requires at least two usable cross-sectional dates and 20 rows")
    model = lgb.LGBMRanker(
        **LIGHTGBM_FIXED_PARAMETERS,
        random_state=seed,
    )
    model.fit(train_x, relevance, group=groups)
    return model, usable_rows


def train_lightgbm(args: argparse.Namespace) -> Dict[str, Any]:
    import duckdb
    import lightgbm

    dependency_versions = {
        "duckdb": getattr(duckdb, "__version__", None),
        "lightgbm": getattr(lightgbm, "__version__", None),
    }
    raw = duckdb_rows(Path(args.duckdb).resolve(), args.mode, args.horizon)
    feature_names, dataset, dataset_evidence = QuantResearchDatasetService.build(raw, mode=args.mode)
    days = sorted({row["date"] for row in dataset})
    regimes = sorted({row["regime"] for row in dataset})
    horizon_days = 1 if args.mode == "intraday" else max(1, int(args.horizon.rstrip("d")))
    shadow_min_days = max(SHADOW_MIN_DAYS, int(args.min_train_days))
    readiness = {
        "observations": len(dataset),
        "shadow_required_observations": SHADOW_MIN_OBSERVATIONS,
        "required_observations": MIN_OBSERVATIONS,
        "trading_days": len(days),
        "shadow_required_trading_days": shadow_min_days,
        "required_trading_days": MIN_DAYS + MIN_HOLDOUT_DAYS + horizon_days,
        "regimes": len(regimes),
        "required_regimes": MIN_REGIMES,
        "shadow_training_ready": shadow_training_ready(
            observations=len(dataset), trading_days=len(days), required_days=shadow_min_days
        ),
        "production_validation_ready": production_validation_ready(
            observations=len(dataset), trading_days=len(days), regimes=len(regimes), horizon_days=horizon_days
        ),
    }
    base = {
        "ok": True,
        "version": WORKER_VERSION,
        "mode": args.mode,
        "horizon": args.horizon,
        "dataset_fingerprint": dataset_evidence["dataset_fingerprint"],
        "feature_manifest_hash": FEATURE_MANIFEST_HASH,
        "observations": len(dataset),
        "trading_days": len(days),
        "regimes": len(regimes),
        "trial_count": args.trial_count,
        "readiness": readiness,
        "libraries": dependency_versions,
        "prediction_state": "ACTIVE_VALIDATION",
        "lifecycle_state": "ACTIVE_VALIDATION",
        "production_influence": False,
        "broker_execution_weight": 0.0,
    }
    if args.trial_count != 1:
        return {
            **base,
            "state": "MULTIPLE_TESTING_BLOCKED",
            "reason": "Only one declared LightGBM specification is allowed until corrected multi-trial control exists.",
        }
    production_ready = bool(readiness["production_validation_ready"])
    if not production_ready:
        if not readiness["shadow_training_ready"]:
            return {
                **base,
                "state": "INSUFFICIENT_SHADOW_EVIDENCE",
                "reason": (
                    f"Shadow evaluation requires >= {SHADOW_MIN_OBSERVATIONS} settled observations "
                    f"across >= {shadow_min_days} trading days; production qualification still requires "
                    f">= {MIN_OBSERVATIONS} observations, >= {MIN_DAYS} development days, an untouched "
                    f"holdout and >= {MIN_REGIMES} regimes."
                ),
            }

        # Early shadow model: fixed specification, past-settled labels only, no
        # statistical/promotion claim.  It exists solely so future populations
        # can accumulate genuine out-of-sample Model-Paper evidence while the
        # 126-day/holdout/regime qualification clock continues to mature.
        train_x, _unused, medians = impute(dataset, [])
        try:
            shadow_model, _usable = fit_ranker(dataset, train_x, seed=53)
        except ValueError as exc:
            return {**base, "state": "INSUFFICIENT_SHADOW_CROSS_SECTION", "reason": str(exc)}
        train_predictions = [float(value) for value in shadow_model.predict(train_x)]
        validation = {
            "walk_forward_folds": [],
            "holdout": {},
            "baseline_comparison": {},
            "holdout_dates": {},
            "gates": {
                "shadow_only_fixed_specification": True,
                "production_maturity_126_days": False,
                "untouched_holdout_completed": False,
                "three_regime_qualification": len(regimes) >= MIN_REGIMES,
            },
            "all_gates_passed": False,
            "label": "net_return_plus_20bps",
            "probability_claim": "NONE",
            "shadow_training": {
                "state": "SHADOW_MODEL_ELIGIBLE",
                "observations": len(dataset),
                "trading_days": len(days),
                "regimes": len(regimes),
                "trained_through": max(row["date"] for row in dataset),
                "future_only_evaluation_required": True,
                "production_influence": False,
            },
        }
        specification = {
            "model_rule_version": MODEL_RULE_VERSION,
            "worker_version": WORKER_VERSION,
            "family": "LIGHTGBM_LAMBDARANK",
            "mode": args.mode,
            "horizon": args.horizon,
            "dataset_fingerprint": dataset_evidence["dataset_fingerprint"],
            "feature_manifest_hash": FEATURE_MANIFEST_HASH,
            "trial_count": args.trial_count,
            "dependency_versions": dependency_versions,
            "fixed_hyperparameters": LIGHTGBM_FIXED_PARAMETERS,
            "validation_parameters": {
                "shadow_min_train_days": shadow_min_days,
                "production_min_development_days": MIN_DAYS,
                "production_minimum_holdout_days": MIN_HOLDOUT_DAYS,
                "production_minimum_regimes": MIN_REGIMES,
                "mode": "FUTURE_ONLY_EVALUATION_PAPER",
            },
            "cost_stress_label": "net_return_plus_20bps",
            "baselines": [
                "random_matched_frequency_seed_17",
                "all_eligible_equal_weight",
                "first_retained_momentum_or_intraday_relative_strength",
                "liquidity_only",
            ],
        }
        specification_hash = digest(specification)
        model_id = digest({
            "specification_hash": specification_hash,
            "dataset_fingerprint": dataset_evidence["dataset_fingerprint"],
        }, 40)
        hint = Path(args.artifact).resolve()
        artifact_path = hint.parent / f"{hint.stem}_{model_id}.json"
        artifact = {
            "model_id": model_id,
            "model_rule_version": MODEL_RULE_VERSION,
            "specification": specification,
            "specification_hash": specification_hash,
            "family": "LIGHTGBM_LAMBDARANK",
            "mode": args.mode,
            "horizon": args.horizon,
            "feature_names": feature_names,
            "medians": medians,
            "booster_model": shadow_model.booster_.model_to_string(),
            "booster_dump_model": shadow_model.booster_.dump_model(),
            "dataset_fingerprint": dataset_evidence["dataset_fingerprint"],
            "feature_manifest_hash": FEATURE_MANIFEST_HASH,
            "trained_through": max(row["date"] for row in dataset),
            "validation": validation,
            "dependency_versions": dependency_versions,
            "score_adapter": score_adapter_evidence(train_predictions),
            "prediction_state": "SHADOW_EVALUATION",
            "lifecycle_state": "SHADOW_MODEL_ELIGIBLE",
            "production_influence": False,
            "broker_execution_weight": 0.0,
        }
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = artifact_path.with_suffix(artifact_path.suffix + ".tmp")
        temporary.write_text(canonical(artifact), encoding="utf-8")
        temporary.replace(artifact_path)
        return {
            **base,
            "state": "SHADOW_MODEL_ELIGIBLE",
            "prediction_state": "SHADOW_EVALUATION",
            "lifecycle_state": "SHADOW_MODEL_ELIGIBLE",
            "model_id": model_id,
            "artifact_path": str(artifact_path),
            "validation": validation,
            "production_influence": False,
            "broker_execution_weight": 0.0,
        }

    holdout_count = max(MIN_HOLDOUT_DAYS, int(args.test_days), math.ceil(len(days) * 0.15))
    maximum_holdout = len(days) - MIN_DAYS - horizon_days
    holdout_count = min(holdout_count, maximum_holdout)
    holdout_days = days[-holdout_count:]
    development_days = days[:-holdout_count]
    holdout_start = min(
        (timestamp(row["observed_at"]) for row in dataset if row["date"] in set(holdout_days)),
        default=None,
    )
    development = [
        row for row in dataset
        if row["date"] in set(development_days)
        and holdout_start is not None
        and timestamp(row.get("settled_at")) is not None
        and timestamp(row.get("settled_at")) < holdout_start
    ]
    holdout = [row for row in dataset if row["date"] in set(holdout_days)]
    if (
        len(development) < 300
        or len({row["date"] for row in development}) < MIN_DAYS
        or len(holdout) < MIN_HOLDOUT_OBSERVATIONS
    ):
        return {
            **base,
            "state": "INSUFFICIENT_UNTOUCHED_HOLDOUT_EVIDENCE",
            "readiness": {
                **readiness,
                "development_observations": len(development),
                "holdout_observations": len(holdout),
                "holdout_days": len(holdout_days),
            },
        }

    folds = WalkForwardValidationService.build_folds(
        sorted({row["date"] for row in development}),
        min_train_days=max(MIN_DAYS, int(args.min_train_days)),
        test_days=max(5, int(args.test_days)),
        purge_days=horizon_days,
        max_folds=max(1, int(args.max_folds)),
        embargo_days=max(0, int(args.embargo_days)),
    )
    fold_results = []
    for fold in folds:
        train_dates = set(fold["train_dates"])
        test_dates = set(fold["test_dates"])
        train_rows = [row for row in development if row["date"] in train_dates]
        test_rows = [row for row in development if row["date"] in test_dates]
        if len(train_rows) < 40 or len(test_rows) < 4:
            continue
        train_x, test_x, _medians = impute(train_rows, test_rows)
        try:
            model, _usable = fit_ranker(train_rows, train_x, seed=17 + len(fold_results))
        except ValueError:
            continue
        predictions = [float(value) for value in model.predict(test_x)]
        metrics = ranking_metrics(test_rows, predictions)
        if metrics["mean_rank_ic"] is None:
            continue
        fold_results.append({
            "fold": fold["fold"],
            "train_start": fold["train_dates"][0],
            "train_end": fold["train_dates"][-1],
            "test_start": fold["test_dates"][0],
            "test_end": fold["test_dates"][-1],
            "n_train": len(train_rows),
            "n_test": len(test_rows),
            **metrics,
        })
    if len(fold_results) < MIN_FOLDS:
        return {
            **base,
            "state": "INSUFFICIENT_FOLDS",
            "fold_count": len(fold_results),
            "required_folds": MIN_FOLDS,
        }

    development_ic = statistics.fmean(float(fold["mean_rank_ic"]) for fold in fold_results)
    development_lift = statistics.fmean(
        float(fold["mean_top_quintile_lift_after_20bps"]) for fold in fold_results
    )
    fold_stability = sum(
        float(fold["mean_rank_ic"]) > 0
        and float(fold["mean_top_quintile_lift_after_20bps"]) > 0
        for fold in fold_results
    ) / len(fold_results)

    development_x, holdout_x, medians = impute(development, holdout)
    final_model, _usable = fit_ranker(development, development_x, seed=71)
    development_predictions = [float(value) for value in final_model.predict(development_x)]
    holdout_predictions = [float(value) for value in final_model.predict(holdout_x)]
    holdout_metrics = ranking_metrics(holdout, holdout_predictions)
    baseline_comparison = same_population_baselines(
        holdout,
        holdout_x,
        feature_names,
        holdout_predictions,
    )
    gates = {
        "minimum_three_walk_forward_folds": len(fold_results) >= MIN_FOLDS,
        "development_mean_rank_ic_positive": development_ic > 0,
        "development_top_quintile_lift_after_20bps_positive": development_lift > 0,
        "development_fold_stability_60pct": fold_stability >= 0.60,
        "holdout_mean_rank_ic_positive": (
            holdout_metrics["mean_rank_ic"] is not None
            and float(holdout_metrics["mean_rank_ic"]) > 0
        ),
        "holdout_top_quintile_lift_after_20bps_positive": (
            holdout_metrics["mean_top_quintile_lift_after_20bps"] is not None
            and float(holdout_metrics["mean_top_quintile_lift_after_20bps"]) > 0
        ),
        "holdout_regime_coverage": len(holdout_metrics["regime_top_quintile_lift_after_20bps"]) >= 2,
        "holdout_positive_regime_fraction_50pct": (
            holdout_metrics["positive_regime_fraction"] is not None
            and float(holdout_metrics["positive_regime_fraction"]) >= 0.50
        ),
        "same_population_baselines_implemented": (
            baseline_comparison.get("all_required_baselines_implemented") is True
        ),
        "model_beats_strongest_baseline_by_20bps": (
            baseline_comparison.get("model_beats_strongest_baseline_by_20bps") is True
        ),
        "single_declared_trial": args.trial_count == 1,
    }
    eligible = all(gates.values())
    validation = {
        "walk_forward_folds": fold_results,
        "development_mean_rank_ic": round(development_ic, 8),
        "development_mean_top_quintile_lift_after_20bps": round(development_lift, 6),
        "development_fold_stability": round(fold_stability, 8),
        "holdout": holdout_metrics,
        "baseline_comparison": baseline_comparison,
        "holdout_dates": {
            "start": holdout_days[0],
            "end": holdout_days[-1],
            "days": len(holdout_days),
            "observations": len(holdout),
        },
        "gates": gates,
        "all_gates_passed": eligible,
        "label": "net_return_plus_20bps",
        "probability_claim": "NONE",
    }
    specification = {
        "model_rule_version": MODEL_RULE_VERSION,
        "worker_version": WORKER_VERSION,
        "family": "LIGHTGBM_LAMBDARANK",
        "mode": args.mode,
        "horizon": args.horizon,
        "dataset_fingerprint": dataset_evidence["dataset_fingerprint"],
        "feature_manifest_hash": FEATURE_MANIFEST_HASH,
        "trial_count": args.trial_count,
        "dependency_versions": dependency_versions,
        "fixed_hyperparameters": LIGHTGBM_FIXED_PARAMETERS,
        "validation_parameters": {
            "min_train_days": max(MIN_DAYS, int(args.min_train_days)),
            "test_days": max(5, int(args.test_days)),
            "max_folds": max(1, int(args.max_folds)),
            "purge_days": horizon_days,
            "embargo_days": max(0, int(args.embargo_days)),
            "holdout_fraction": 0.15,
            "minimum_holdout_days": MIN_HOLDOUT_DAYS,
        },
        "cost_stress_label": "net_return_plus_20bps",
        "baselines": [
            "random_matched_frequency_seed_17",
            "all_eligible_equal_weight",
            "first_retained_momentum_or_intraday_relative_strength",
            "liquidity_only",
        ],
    }
    specification_hash = digest(specification)
    model_id = digest({
        "specification_hash": specification_hash,
        "dataset_fingerprint": dataset_evidence["dataset_fingerprint"],
    }, 40)
    hint = Path(args.artifact).resolve()
    artifact_path = hint.parent / f"{hint.stem}_{model_id}.json"
    artifact = {
        "model_id": model_id,
        "model_rule_version": MODEL_RULE_VERSION,
        "specification": specification,
        "specification_hash": specification_hash,
        "family": "LIGHTGBM_LAMBDARANK",
        "mode": args.mode,
        "horizon": args.horizon,
        "feature_names": feature_names,
        "medians": medians,
        "booster_model": final_model.booster_.model_to_string(),
        "booster_dump_model": final_model.booster_.dump_model(),
        "dataset_fingerprint": dataset_evidence["dataset_fingerprint"],
        "feature_manifest_hash": FEATURE_MANIFEST_HASH,
        "trained_through": max(row["date"] for row in development),
        "validation": validation,
        "dependency_versions": dependency_versions,
        "score_adapter": score_adapter_evidence(development_predictions),
        "prediction_state": "ACTIVE_VALIDATION" if eligible else "REJECTED",
        "lifecycle_state": "ACTIVE_VALIDATION" if eligible else "REJECTED",
        "production_influence": False,
        "broker_execution_weight": 0.0,
    }
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = artifact_path.with_suffix(artifact_path.suffix + ".tmp")
    temporary.write_text(canonical(artifact), encoding="utf-8")
    temporary.replace(artifact_path)
    return {
        **base,
        "state": "ACTIVE_VALIDATION" if eligible else "REJECTED",
        "model_id": model_id,
        "artifact_path": str(artifact_path),
        "validation": validation,
    }


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser()
    value.add_argument("action", choices=("project", "train-lightgbm"))
    value.add_argument("--sqlite", required=True)
    value.add_argument("--duckdb", required=True)
    value.add_argument("--parquet-dir", required=True)
    value.add_argument("--mode", required=True, choices=("intraday", "delivery"))
    value.add_argument("--horizon", default="20d")
    value.add_argument("--artifact")
    value.add_argument("--trial-count", type=int, default=1)
    value.add_argument("--min-train-days", type=int, default=126)
    value.add_argument("--test-days", type=int, default=21)
    value.add_argument("--max-folds", type=int, default=8)
    value.add_argument("--embargo-days", type=int, default=1)
    return value


def main() -> int:
    args = parser().parse_args()
    try:
        result = project(args) if args.action == "project" else train_lightgbm(args)
    except Exception as exc:
        print(canonical({
            "ok": False,
            "state": "WORKER_EXCEPTION",
            "error": f"{type(exc).__name__}: {exc}",
            "version": WORKER_VERSION,
            "prediction_state": "MODEL_UNAVAILABLE",
            "decision_weight": 0.0,
            "broker_execution_weight": 0.0,
        }))
        return 1
    print(canonical(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
