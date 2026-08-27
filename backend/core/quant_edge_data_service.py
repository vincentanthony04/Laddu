"""Immutable point-in-time feature snapshots and linked research labels.

This is the canonical Phase-0 data contract for the quant research loop.  It
normalises the existing candidate population ledger into explicit, queryable
feature snapshots and stores one immutable multi-metric label vector per
candidate/horizon.  It has no production-selection or broker authority.
"""
from __future__ import annotations

from dataclasses import asdict, fields
from datetime import datetime, timezone
import hashlib
import json
import math
import threading
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence

from core.india_cost_model import IndiaCashCostConfig, IndiaCashCostModel
from core.nse_cross_sectional_selector_service import DELIVERY_FEATURES, INTRADAY_FEATURES
from core.production_mode_policy import require_production_mode
from core.strict_json import json_safe


QUANT_EDGE_DATA_VERSION = "quant-edge-data-1.0.0"
COST_AUTHORITY_ID_VERSION = "quant-cost-authority-v1"
COST_STRESS_BPS = (0.0, 5.0, 10.0, 20.0)
MIN_TRAINING_FEATURE_COVERAGE = 0.60
TRAINING_FRESHNESS = {
    "intraday": {"LIVE", "FRESH"},
    "delivery": {"LIVE", "FRESH", "CLOSED_MARKET", "VERIFIED_CLOSE"},
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _sha(value: Any, length: int = 64) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8", errors="replace")).hexdigest()[:length]


def _finite(value: Any) -> Optional[float]:
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _parse_timestamp(value: Any) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _first_text(row: Mapping[str, Any], names: Sequence[str], default: str = "") -> str:
    for name in names:
        value = str(row.get(name) or "").strip()
        if value:
            return value
    return default


def _resolved_cost_authority(mode: str, features: Mapping[str, Any]) -> tuple[IndiaCashCostConfig, str]:
    """Freeze the exact venue-aware cost config and its immutable identity."""
    desk = require_production_mode(mode)
    feature_payload = dict(features or {})
    cost_model = IndiaCashCostModel.for_evidence(desk, feature_payload)
    supplied_costs = feature_payload.get("cost_assumptions")
    if isinstance(supplied_costs, dict):
        allowed = {item.name for item in fields(IndiaCashCostConfig)}
        supplied = {key: value for key, value in supplied_costs.items() if key in allowed}
        try:
            frozen = IndiaCashCostConfig(**supplied)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid snapshot cost assumptions: {exc}") from exc
    else:
        frozen = cost_model.config
    identity = _sha({
        "identity_version": COST_AUTHORITY_ID_VERSION,
        "mode": desk,
        "config": asdict(frozen),
    }, 40)
    return frozen, identity


_QUANT_EDGE_SCHEMA = """
CREATE TABLE IF NOT EXISTS quant_feature_snapshots (
  snapshot_id TEXT PRIMARY KEY,
  candidate_id TEXT NOT NULL UNIQUE,
  population_fingerprint TEXT NOT NULL,
  symbol TEXT NOT NULL,
  instrument_key TEXT NOT NULL,
  mode TEXT NOT NULL,
  side TEXT,
  decision_ts TEXT NOT NULL,
  feature_as_of TEXT NOT NULL,
  source_as_of TEXT NOT NULL,
  received_at TEXT NOT NULL,
  universe_id TEXT NOT NULL,
  universe_membership_as_of TEXT NOT NULL,
  dataset_fingerprint TEXT NOT NULL,
  feature_manifest_hash TEXT NOT NULL,
  feature_hash TEXT NOT NULL,
  candidate_identity_version TEXT NOT NULL DEFAULT 'candidate-identity-v1',
  cost_authority_id TEXT NOT NULL DEFAULT '',
  feature_json TEXT NOT NULL,
  cost_model_version TEXT NOT NULL,
  cost_assumption_json TEXT NOT NULL,
  regime_tag TEXT NOT NULL,
  freshness_state TEXT NOT NULL,
  compact_feature_coverage REAL NOT NULL,
  missing_features_json TEXT NOT NULL,
  lineage_state TEXT NOT NULL,
  lineage_missing_json TEXT NOT NULL,
  snapshot_state TEXT NOT NULL,
  snapshot_hash TEXT NOT NULL UNIQUE,
  created_at TEXT NOT NULL,
  data_version TEXT NOT NULL,
  FOREIGN KEY(candidate_id) REFERENCES candidate_population_observations(candidate_id)
);
CREATE INDEX IF NOT EXISTS ix_quant_feature_snapshots_mode_time
  ON quant_feature_snapshots(mode, decision_ts, symbol);
CREATE INDEX IF NOT EXISTS ix_quant_feature_snapshots_population
  ON quant_feature_snapshots(population_fingerprint, symbol);

CREATE TABLE IF NOT EXISTS quant_label_vectors (
  candidate_id TEXT NOT NULL,
  horizon TEXT NOT NULL,
  snapshot_id TEXT NOT NULL,
  snapshot_hash TEXT NOT NULL,
  feature_hash TEXT NOT NULL,
  symbol TEXT NOT NULL,
  mode TEXT NOT NULL,
  observed_at TEXT NOT NULL,
  settled_at TEXT NOT NULL,
  market_regime TEXT NOT NULL,
  result TEXT NOT NULL,
  gross_return_bps REAL,
  strategy_net_return_bps REAL NOT NULL,
  net_return_bps REAL NOT NULL,
  net_return_plus_5bps REAL NOT NULL,
  net_return_plus_10bps REAL NOT NULL,
  net_return_plus_20bps REAL NOT NULL,
  target_before_stop INTEGER,
  mfe_bps REAL,
  mae_bps REAL,
  time_to_outcome_bars INTEGER,
  same_bar_stop_first INTEGER NOT NULL,
  cost_model_version TEXT NOT NULL,
  label_json TEXT NOT NULL,
  record_hash TEXT NOT NULL UNIQUE,
  created_at TEXT NOT NULL,
  data_version TEXT NOT NULL,
  PRIMARY KEY(candidate_id, horizon),
  FOREIGN KEY(snapshot_id) REFERENCES quant_feature_snapshots(snapshot_id)
);
CREATE INDEX IF NOT EXISTS ix_quant_label_vectors_training
  ON quant_label_vectors(mode, horizon, observed_at, market_regime);
"""


def ensure_quant_edge_tables(conn: Any) -> None:
    """Apply the idempotent Phase-0 schema on the canonical Store connection."""
    conn.executescript(_QUANT_EDGE_SCHEMA)
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(quant_feature_snapshots)").fetchall()}
    if "candidate_identity_version" not in columns:
        conn.execute(
            "ALTER TABLE quant_feature_snapshots ADD COLUMN "
            "candidate_identity_version TEXT NOT NULL DEFAULT 'candidate-identity-v1'"
        )
    if "cost_authority_id" not in columns:
        conn.execute(
            "ALTER TABLE quant_feature_snapshots ADD COLUMN cost_authority_id TEXT NOT NULL DEFAULT ''"
        )
    # Legacy snapshots retain their original hashes and IDs. Attach a stable
    # cost-authority identity derived from their already-frozen configuration;
    # new content is always appended under candidate-identity-v2.
    legacy = conn.execute(
        """SELECT snapshot_id,cost_model_version,cost_assumption_json
           FROM quant_feature_snapshots WHERE COALESCE(cost_authority_id,'')=''"""
    ).fetchall()
    for row in legacy:
        try:
            assumptions = json.loads(row[2] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            assumptions = {}
        cost_authority_id = _sha({
            "identity_version": COST_AUTHORITY_ID_VERSION,
            "cost_model_version": str(row[1] or ""),
            "cost_assumptions": assumptions,
        }, 40)
        conn.execute(
            """UPDATE quant_feature_snapshots
               SET candidate_identity_version='candidate-identity-v1',cost_authority_id=?
               WHERE snapshot_id=?""",
            (cost_authority_id, row[0]),
        )
    conn.commit()


class QuantEdgeDataService:
    """Persist immutable feature and label vectors for shadow research."""

    def __init__(self, store: Any):
        self.store = store
        self.production_governance_required = bool(
            getattr(store, "production_model_governance_required", False)
        )
        self.governance_repository = getattr(
            store, "production_model_governance_repository", None
        )
        if self.production_governance_required:
            required = (
                "record_selector_feature_snapshot", "selector_feature_snapshot",
                "record_selector_label", "selector_evidence_status", "quant_training_rows",
            )
            if (
                self.governance_repository is None
                or getattr(self.governance_repository, "authority", None) is None
                or any(not callable(getattr(self.governance_repository, name, None)) for name in required)
            ):
                raise RuntimeError("PRODUCTION_QUANT_EDGE_REQUIRES_POSTGRES_GOVERNANCE_REPOSITORY")
        if not hasattr(store, "write_lock"):
            store.write_lock = threading.RLock()
        if not self.production_governance_required:
            self._ensure_schema()

    def _ensure_schema(self) -> None:
        with self.store.write_lock:
            ensure_quant_edge_tables(self.store.conn)

    @staticmethod
    def cost_authority_identity(mode: str, features: Mapping[str, Any]) -> str:
        return _resolved_cost_authority(mode, features)[1]

    @staticmethod
    def _feature_completeness(mode: str, features: Mapping[str, Any]) -> tuple[float, list[str]]:
        specs = DELIVERY_FEATURES if mode == "delivery" else INTRADAY_FEATURES
        missing = []
        for name, aliases, _weight, _higher in specs:
            if not any(_finite(features.get(alias)) is not None for alias in aliases):
                missing.append(name)
        coverage = (len(specs) - len(missing)) / len(specs) if specs else 0.0
        return round(coverage, 6), missing

    def record_snapshot(
        self,
        *,
        candidate_id: str,
        population_fingerprint: str,
        symbol: str,
        instrument_key: str,
        mode: str,
        side: str,
        decision_ts: str,
        universe_id: str,
        dataset_fingerprint: str,
        feature_manifest_hash: str,
        feature_hash: str,
        features: Mapping[str, Any],
        candidate_identity_version: str = "candidate-identity-v2",
        cost_authority_id: str = "",
        _persist: bool = True,
    ) -> Dict[str, Any]:
        desk = require_production_mode(mode)
        candidate_key = str(candidate_id or "").strip()
        population_key = str(population_fingerprint or "").strip()
        instrument = str(instrument_key or "").strip()
        # PL30: governed snapshots must hash the exact JSON-safe material that
        # PostgreSQL JSONB will retain. Python json.dumps permits NaN/Infinity,
        # while strict governance persistence normalises them to null. Hashing
        # before that normalisation caused an immediate false immutable-hash
        # conflict when the just-written snapshot was read back from JSONB.
        safe_features = json_safe(features or {})
        feature_payload = dict(safe_features) if isinstance(safe_features, Mapping) else {}
        if not candidate_key or not population_key or not instrument:
            raise ValueError("candidate_id, population_fingerprint and instrument_key are required")
        if feature_payload.get("identity_verified") not in (True, 1):
            raise ValueError("quant feature snapshot requires verified instrument identity")

        stamp = str(decision_ts or "").strip() or _now()
        feature_as_of = _first_text(
            feature_payload,
            ("feature_as_of", "decision_as_of", "observed_at", "last_refresh"),
        )
        source_as_of = _first_text(
            feature_payload,
            (
                "source_as_of", "quote_as_of", "quote_timestamp",
                "provider_timestamp", "provider_ts", "provider_time",
                "source_time", "quote_time", "candle_as_of", "timestamp",
            ),
        )
        received_at = _first_text(feature_payload, ("received_at", "received_time", "fetched_at", "updated_at"))
        membership_as_of = _first_text(
            feature_payload,
            ("universe_membership_as_of", "constituent_as_of"),
        )
        regime = _first_text(feature_payload, ("market_regime", "regime", "regime_tag"), "UNKNOWN").upper()
        freshness = _first_text(
            feature_payload,
            ("freshness_state", "evidence_freshness_state", "quote_freshness_state", "price_freshness_state"),
            "UNKNOWN",
        ).upper()
        # Cost assumptions are frozen from the canonical listing identity.
        # Genuine BSE-only candidates must carry their BSE group; otherwise
        # snapshot creation fails closed instead of inheriting NSE charges.
        frozen_cost_config, resolved_cost_authority_id = _resolved_cost_authority(desk, feature_payload)
        supplied_cost_authority_id = str(cost_authority_id or "").strip()
        if supplied_cost_authority_id and supplied_cost_authority_id != resolved_cost_authority_id:
            raise ValueError("candidate cost authority conflicts with immutable feature snapshot")
        cost_authority_key = supplied_cost_authority_id or resolved_cost_authority_id
        identity_version = str(candidate_identity_version or "").strip()
        if not identity_version:
            raise ValueError("candidate identity version is required")
        # Store the complete resolved configuration, not a partial override, so
        # later settlement cannot inherit changed process defaults.
        cost_assumptions = asdict(frozen_cost_config)
        coverage, missing = self._feature_completeness(desk, feature_payload)
        lineage_values = {
            "feature_as_of": feature_as_of,
            "source_as_of": source_as_of,
            "received_at": received_at,
            "universe_membership_as_of": membership_as_of,
        }
        lineage_missing = [name for name, value in lineage_values.items() if not value]
        lineage_state = "INCOMPLETE" if lineage_missing else "VERIFIED"
        parsed_decision = _parse_timestamp(stamp)
        parsed_lineage = {name: _parse_timestamp(value) for name, value in lineage_values.items() if value}
        if parsed_decision is None or any(value is None for value in parsed_lineage.values()):
            lineage_state = "INVALID_TIMESTAMP"
        elif any(
            parsed_lineage[name] > parsed_decision
            for name in (
                "feature_as_of",
                "source_as_of",
                "received_at",
                "universe_membership_as_of",
            )
            if name in parsed_lineage
        ):
            lineage_state = "LOOKAHEAD_BLOCKED"
        elif (
            "source_as_of" in parsed_lineage
            and "received_at" in parsed_lineage
            and parsed_lineage["source_as_of"] > parsed_lineage["received_at"]
        ):
            lineage_state = "INVALID_TIMESTAMP_ORDER"
        freshness_eligible = freshness in TRAINING_FRESHNESS[desk]
        snapshot_state = (
            "COMPLETE"
            if (
                coverage >= MIN_TRAINING_FEATURE_COVERAGE
                and lineage_state == "VERIFIED"
                and regime != "UNKNOWN"
                and freshness_eligible
            )
            else "PARTIAL"
        )
        snapshot_id = _sha({"candidate_id": candidate_key, "version": QUANT_EDGE_DATA_VERSION}, 40)
        payload = {
            "snapshot_id": snapshot_id,
            "candidate_id": candidate_key,
            "population_fingerprint": population_key,
            "symbol": str(symbol or "").upper().strip(),
            "instrument_key": instrument,
            "mode": desk,
            "side": str(side or "").upper().strip(),
            "decision_ts": stamp,
            "feature_as_of": feature_as_of,
            "source_as_of": source_as_of,
            "received_at": received_at,
            "universe_id": str(universe_id or "").strip(),
            "universe_membership_as_of": membership_as_of,
            "dataset_fingerprint": str(dataset_fingerprint or "").strip(),
            "feature_manifest_hash": str(feature_manifest_hash or "").strip(),
            "feature_hash": str(feature_hash or "").strip(),
            "candidate_identity_version": identity_version,
            "cost_authority_id": cost_authority_key,
            "features": feature_payload,
            "cost_model_version": str(frozen_cost_config.version),
            "cost_assumptions": cost_assumptions,
            "regime_tag": regime,
            "freshness_state": freshness,
            "freshness_eligible_for_training": freshness_eligible,
            "compact_feature_coverage": coverage,
            "missing_features": missing,
            "lineage_state": lineage_state,
            "lineage_missing": lineage_missing,
            "snapshot_state": snapshot_state,
            "data_version": QUANT_EDGE_DATA_VERSION,
        }
        snapshot_hash = _sha(payload)
        prepared = {"ok": True, "inserted": False, **payload, "snapshot_hash": snapshot_hash}
        if not _persist:
            return prepared
        if self.production_governance_required:
            return self.governance_repository.record_selector_feature_snapshot(prepared)
        with self.store.write_lock:
            with self.store.conn:
                existing = self.store.conn.execute(
                    "SELECT snapshot_hash FROM quant_feature_snapshots WHERE candidate_id=?",
                    (candidate_key,),
                ).fetchone()
                if existing:
                    if str(existing[0]) != snapshot_hash:
                        raise ValueError(
                            "feature snapshot is immutable; conflicting candidate snapshot rejected"
                        )
                    return {"ok": True, "inserted": False, **payload, "snapshot_hash": snapshot_hash}
                self.store.conn.execute(
                    """INSERT INTO quant_feature_snapshots(
                        snapshot_id,candidate_id,population_fingerprint,symbol,instrument_key,mode,side,
                        decision_ts,feature_as_of,source_as_of,received_at,universe_id,
                        universe_membership_as_of,dataset_fingerprint,feature_manifest_hash,feature_hash,
                        candidate_identity_version,cost_authority_id,
                        feature_json,cost_model_version,cost_assumption_json,regime_tag,freshness_state,
                        compact_feature_coverage,missing_features_json,lineage_state,lineage_missing_json,
                        snapshot_state,snapshot_hash,created_at,data_version
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        snapshot_id, candidate_key, population_key, payload["symbol"], instrument, desk,
                        payload["side"], stamp, feature_as_of, source_as_of, received_at,
                        payload["universe_id"], membership_as_of, payload["dataset_fingerprint"],
                        payload["feature_manifest_hash"], payload["feature_hash"], identity_version,
                        cost_authority_key, _canonical(feature_payload), payload["cost_model_version"],
                        _canonical(cost_assumptions), regime, freshness, coverage, _canonical(missing),
                        lineage_state, _canonical(lineage_missing), snapshot_state, snapshot_hash,
                        _now(), QUANT_EDGE_DATA_VERSION,
                    ),
                )
        return {"ok": True, "inserted": True, **payload, "snapshot_hash": snapshot_hash}

    def record_population_snapshots(
        self,
        rows: Iterable[Mapping[str, Any]],
        *,
        population_fingerprint: str,
        universe_id: str,
        dataset_fingerprint: str,
        feature_manifest_hash: str,
    ) -> Dict[str, Any]:
        inserted = existing = 0
        states: Dict[str, int] = {}
        for raw in rows:
            row = dict(raw or {})
            result = self.record_snapshot(
                candidate_id=str(row.get("candidate_id") or ""),
                population_fingerprint=population_fingerprint,
                symbol=str(row.get("symbol") or ""),
                instrument_key=str(row.get("instrument_key") or ""),
                mode=str(row.get("mode") or ""),
                side=str(row.get("side") or ""),
                decision_ts=str(row.get("observed_at") or ""),
                universe_id=universe_id,
                dataset_fingerprint=dataset_fingerprint,
                feature_manifest_hash=feature_manifest_hash,
                feature_hash=str(row.get("feature_hash") or ""),
                features=dict(row.get("feature_snapshot") or row.get("features") or {}),
                candidate_identity_version=str(row.get("candidate_identity_version") or "candidate-identity-v2"),
                cost_authority_id=str(row.get("cost_authority_id") or ""),
            )
            inserted += int(bool(result["inserted"]))
            existing += int(not result["inserted"])
            state = str(result["snapshot_state"])
            states[state] = states.get(state, 0) + 1
        return {
            "ok": True,
            "version": QUANT_EDGE_DATA_VERSION,
            "inserted": inserted,
            "existing": existing,
            "states": states,
            "production_change_allowed": False,
        }

    def record_label(
        self,
        *,
        candidate_id: str,
        horizon: str,
        result: str,
        gross_return_bps: Optional[float],
        net_return_bps: float,
        settled_at: str,
        market_regime: str,
        same_bar_stop_first: bool,
        proof: Mapping[str, Any],
    ) -> Dict[str, Any]:
        candidate_key = str(candidate_id or "").strip()
        horizon_key = str(horizon or "").strip().lower()
        strategy_net_bps = _finite(net_return_bps)
        if not candidate_key or not horizon_key or strategy_net_bps is None:
            raise ValueError("candidate_id, horizon and finite net_return_bps are required")
        if self.production_governance_required:
            snapshot = self.governance_repository.selector_feature_snapshot(candidate_key)
        else:
            snapshot = self.store.conn.execute(
                """SELECT snapshot_id,snapshot_hash,feature_hash,symbol,mode,decision_ts,
                          cost_model_version,regime_tag
                   FROM quant_feature_snapshots WHERE candidate_id=?""",
                (candidate_key,),
            ).fetchone()
        if not snapshot:
            raise ValueError("label requires an immutable quant feature snapshot")
        row = dict(snapshot)
        proof_payload = dict(proof or {})
        snapshot_cost_version = str(row["cost_model_version"] or "").strip()
        proof_cost_version = str(proof_payload.get("cost_version") or "").strip()
        if not proof_cost_version:
            raise ValueError("quant label requires the frozen snapshot cost version")
        if proof_cost_version != snapshot_cost_version:
            raise ValueError("quant label cost version conflicts with immutable feature snapshot")
        fixed_horizon_net_bps = _finite(proof_payload.get("fixed_horizon_net_return_bps"))
        fixed_horizon_gross_bps = _finite(proof_payload.get("fixed_horizon_gross_return_bps"))
        fixed_horizon_result = str(proof_payload.get("fixed_horizon_result") or "").upper().strip()
        fixed_horizon_settled_at = str(proof_payload.get("fixed_horizon_settled_at") or "").strip()
        if (
            fixed_horizon_net_bps is None
            or fixed_horizon_gross_bps is None
            or fixed_horizon_result not in {"SUCCESS", "FAIL", "BREAKEVEN"}
            or not fixed_horizon_settled_at
        ):
            raise ValueError("quant label requires a complete fixed-horizon return proof")
        supplied_settled_at = str(settled_at or "").strip()
        if supplied_settled_at and supplied_settled_at != fixed_horizon_settled_at:
            raise ValueError("quant label settlement timestamp conflicts with fixed-horizon proof")
        label_net_bps = fixed_horizon_net_bps
        target_before_stop = proof_payload.get("target_before_stop")
        target_value = None if target_before_stop is None else int(bool(target_before_stop))
        stresses = {f"plus_{int(extra)}bps": round(label_net_bps - extra, 6) for extra in COST_STRESS_BPS}
        payload = {
            "candidate_id": candidate_key,
            "horizon": horizon_key,
            "snapshot_id": row["snapshot_id"],
            "snapshot_hash": row["snapshot_hash"],
            "feature_hash": row["feature_hash"],
            "symbol": row["symbol"],
            "mode": row["mode"],
            "observed_at": row["decision_ts"],
            "settled_at": fixed_horizon_settled_at,
            "market_regime": str(market_regime or row["regime_tag"] or "UNKNOWN").upper(),
            "result": fixed_horizon_result,
            "gross_return_bps": fixed_horizon_gross_bps,
            "strategy_net_return_bps": strategy_net_bps,
            "net_return_bps": label_net_bps,
            "cost_stress_net_return_bps": stresses,
            "target_before_stop": None if target_value is None else bool(target_value),
            "mfe_bps": _finite(proof_payload.get("mfe_bps")),
            "mae_bps": _finite(proof_payload.get("mae_bps")),
            "time_to_outcome_bars": int(proof_payload.get("time_to_outcome_bars") or 0) or None,
            "same_bar_stop_first": bool(same_bar_stop_first),
            "cost_model_version": snapshot_cost_version,
            "proof": proof_payload,
            "data_version": QUANT_EDGE_DATA_VERSION,
        }
        record_hash = _sha(payload)
        if self.production_governance_required:
            return self.governance_repository.record_selector_label(
                {"ok": True, "inserted": False, **payload, "record_hash": record_hash}
            )
        existing = self.store.conn.execute(
            "SELECT record_hash FROM quant_label_vectors WHERE candidate_id=? AND horizon=?",
            (candidate_key, horizon_key),
        ).fetchone()
        if existing:
            if str(existing[0]) != record_hash:
                raise ValueError("quant label is immutable; conflicting settlement rejected")
            return {"ok": True, "inserted": False, **payload, "record_hash": record_hash}

        with self.store.write_lock:
            self.store.conn.execute(
                """INSERT INTO quant_label_vectors(
                    candidate_id,horizon,snapshot_id,snapshot_hash,feature_hash,symbol,mode,
                    observed_at,settled_at,market_regime,result,gross_return_bps,
                    strategy_net_return_bps,net_return_bps,
                    net_return_plus_5bps,net_return_plus_10bps,net_return_plus_20bps,
                    target_before_stop,mfe_bps,mae_bps,time_to_outcome_bars,
                    same_bar_stop_first,cost_model_version,label_json,record_hash,created_at,data_version
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    candidate_key, horizon_key, row["snapshot_id"], row["snapshot_hash"],
                    row["feature_hash"], row["symbol"], row["mode"], row["decision_ts"],
                    payload["settled_at"], payload["market_regime"], payload["result"],
                    payload["gross_return_bps"], strategy_net_bps, label_net_bps, stresses["plus_5bps"],
                    stresses["plus_10bps"], stresses["plus_20bps"], target_value,
                    payload["mfe_bps"], payload["mae_bps"], payload["time_to_outcome_bars"],
                    int(bool(same_bar_stop_first)), payload["cost_model_version"],
                    _canonical(payload), record_hash, _now(), QUANT_EDGE_DATA_VERSION,
                ),
            )
            self.store.conn.commit()
        return {"ok": True, "inserted": True, **payload, "record_hash": record_hash}

    def backfill_existing(self, *, limit: int = 5000) -> Dict[str, int]:
        if self.production_governance_required:
            return {
                "snapshots": 0, "labels": 0,
                "state": "POSTGRES_AUTHORITY_REQUIRES_STARTUP_MIGRATION_ONLY",
            }
        snapshot_rows = self.store.conn.execute(
            """SELECT o.*,p.universe_id,p.dataset_fingerprint,p.feature_manifest_hash
               FROM candidate_population_observations o
               JOIN candidate_populations p
                 ON p.population_fingerprint=o.population_fingerprint
               LEFT JOIN quant_feature_snapshots q ON q.candidate_id=o.candidate_id
               WHERE q.candidate_id IS NULL ORDER BY o.observed_at LIMIT ?""",
            (max(1, int(limit)),),
        ).fetchall()
        snapshot_inserted = 0
        for raw in snapshot_rows:
            row = dict(raw)
            try:
                features = json.loads(row.get("feature_json") or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                features = {}
            result = self.record_snapshot(
                candidate_id=row["candidate_id"],
                population_fingerprint=row["population_fingerprint"],
                symbol=row["symbol"],
                instrument_key=row["instrument_key"],
                mode=row["mode"],
                side=row.get("side") or "",
                decision_ts=row["observed_at"],
                universe_id=row["universe_id"],
                dataset_fingerprint=row["dataset_fingerprint"],
                feature_manifest_hash=row["feature_manifest_hash"],
                feature_hash=row["feature_hash"],
                features=features,
            )
            snapshot_inserted += int(bool(result["inserted"]))

        outcome_rows = self.store.conn.execute(
            """SELECT o.* FROM selector_candidate_outcomes o
               JOIN quant_feature_snapshots q ON q.candidate_id=o.candidate_id
               LEFT JOIN quant_label_vectors l
                 ON l.candidate_id=o.candidate_id AND l.horizon=o.horizon
               WHERE l.candidate_id IS NULL ORDER BY o.settled_at LIMIT ?""",
            (max(1, int(limit)),),
        ).fetchall()
        label_inserted = 0
        for raw in outcome_rows:
            row = dict(raw)
            try:
                proof = json.loads(row.get("proof_json") or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                proof = {}
            # Older selector outcomes may be target/stop returns only. They are
            # useful operational records but cannot be silently reinterpreted
            # as unbiased fixed-horizon training labels.
            if not isinstance(proof, dict) or any(
                proof.get(name) in (None, "")
                for name in (
                    "cost_version",
                    "fixed_horizon_net_return_bps",
                    "fixed_horizon_gross_return_bps",
                    "fixed_horizon_result",
                    "fixed_horizon_settled_at",
                )
            ):
                continue
            result = self.record_label(
                candidate_id=row["candidate_id"],
                horizon=row["horizon"],
                result=row["result"],
                gross_return_bps=row.get("gross_return_bps"),
                net_return_bps=row["net_return_bps"],
                settled_at=row["settled_at"],
                market_regime=row.get("market_regime") or "UNKNOWN",
                same_bar_stop_first=bool(row.get("same_bar_ambiguous")),
                proof=proof,
            )
            label_inserted += int(bool(result["inserted"]))
        return {"snapshots": snapshot_inserted, "labels": label_inserted}

    def status(self, mode: Optional[str] = None) -> Dict[str, Any]:
        if self.production_governance_required:
            evidence = self.governance_repository.selector_evidence_status(mode)
            return {
                "ok": True, "version": QUANT_EDGE_DATA_VERSION, "mode": mode or "all",
                **evidence, "selection_edge": "NOT_YET_STATISTICALLY_VALIDATED",
                "production_change_allowed": False, "authority": "GOVERNANCE_POSTGRESQL",
            }
        params: list[Any] = []
        where = ""
        label_where = ""
        if mode:
            desk = require_production_mode(mode)
            where = "WHERE mode=?"
            label_where = "WHERE mode=?"
            params.append(desk)
        snapshot = self.store.conn.execute(
            f"""SELECT COUNT(*) count,
                       SUM(CASE WHEN snapshot_state='COMPLETE' THEN 1 ELSE 0 END) complete,
                       AVG(compact_feature_coverage) coverage,
                       COUNT(DISTINCT SUBSTR(decision_ts,1,10)) days,
                       COUNT(DISTINCT regime_tag) regimes
                FROM quant_feature_snapshots {where}""",
            tuple(params),
        ).fetchone()
        labels = self.store.conn.execute(
            f"""SELECT COUNT(*) count,
                       COUNT(DISTINCT SUBSTR(observed_at,1,10)) days,
                       COUNT(DISTINCT market_regime) regimes
                FROM quant_label_vectors {label_where}""",
            tuple(params),
        ).fetchone()
        snap = dict(snapshot) if snapshot else {}
        lab = dict(labels) if labels else {}
        return {
            "ok": True,
            "version": QUANT_EDGE_DATA_VERSION,
            "mode": mode or "all",
            "snapshots": int(snap.get("count") or 0),
            "complete_snapshots": int(snap.get("complete") or 0),
            "average_compact_feature_coverage": round(float(snap.get("coverage") or 0.0), 6),
            "snapshot_days": int(snap.get("days") or 0),
            "snapshot_regimes": int(snap.get("regimes") or 0),
            "labels": int(lab.get("count") or 0),
            "label_days": int(lab.get("days") or 0),
            "label_regimes": int(lab.get("regimes") or 0),
            "selection_edge": "NOT_YET_STATISTICALLY_VALIDATED",
            "production_change_allowed": False,
        }

    def training_rows(self, *, mode: str, horizon: str) -> list[Dict[str, Any]]:
        desk = require_production_mode(mode)
        if self.production_governance_required:
            return self.governance_repository.quant_training_rows(
                mode=desk, horizon=str(horizon or "").lower().strip()
            )
        rows = self.store.conn.execute(
            """SELECT s.snapshot_id,s.candidate_id,s.symbol,s.decision_ts,s.feature_as_of,
                      s.source_as_of,s.received_at,s.dataset_fingerprint,s.feature_manifest_hash,
                      s.feature_hash,s.feature_json,s.compact_feature_coverage,s.regime_tag,
                      s.snapshot_state,s.lineage_state,
                      l.horizon,l.market_regime AS label_regime,l.result,
                      l.strategy_net_return_bps,l.net_return_bps,l.net_return_plus_5bps,
                      l.net_return_plus_10bps,l.net_return_plus_20bps,l.target_before_stop,
                      l.mfe_bps,l.mae_bps,l.time_to_outcome_bars,l.settled_at,l.record_hash
               FROM quant_feature_snapshots s
               JOIN quant_label_vectors l ON l.snapshot_id=s.snapshot_id
               WHERE s.mode=? AND l.horizon=?
                 AND s.snapshot_state='COMPLETE' AND s.lineage_state='VERIFIED'
               ORDER BY s.decision_ts,s.symbol""",
            (desk, str(horizon or "").lower().strip()),
        ).fetchall()
        result = []
        for raw in rows:
            item = dict(raw)
            try:
                item["features"] = json.loads(item.pop("feature_json") or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                item["features"] = {}
            result.append(item)
        return result
