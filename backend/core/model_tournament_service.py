"""Finite, evidence-led model tournament for Delivery and Intraday.

Candidates are not decorative factors and never contribute zero-weight
'evidence'.  They either perform an active validation responsibility, are
promoted with positive production weight, or are rejected and removed from the
production candidate set.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import threading
from typing import Any, Dict, Iterable, Mapping, Optional

from core.dual_desk_architecture_service import architecture_for
from core.production_mode_policy import require_production_mode


SERVICE_VERSION = "dual-desk-model-tournament-1.0.0"
LIFECYCLE_STATES = ("EXPERIMENT", "ACTIVE_VALIDATION", "ACTIVE_PRODUCTION", "REJECTED")


_SCHEMA = """
CREATE TABLE IF NOT EXISTS model_experiments (
  experiment_id TEXT PRIMARY KEY,
  model_key TEXT NOT NULL,
  library_key TEXT NOT NULL,
  mode TEXT NOT NULL CHECK(mode IN ('intraday','delivery')),
  horizon TEXT NOT NULL,
  lifecycle_state TEXT NOT NULL CHECK(lifecycle_state IN ('EXPERIMENT','ACTIVE_VALIDATION','ACTIVE_PRODUCTION','REJECTED')),
  benchmark_model_key TEXT NOT NULL,
  dataset_fingerprint TEXT NOT NULL,
  feature_manifest_hash TEXT NOT NULL,
  target_name TEXT NOT NULL,
  required_observations INTEGER NOT NULL,
  required_regimes INTEGER NOT NULL,
  validation_deadline TEXT NOT NULL,
  production_weight REAL,
  rejection_reason TEXT,
  config_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(model_key, mode, horizon, dataset_fingerprint)
);
CREATE INDEX IF NOT EXISTS ix_model_experiments_desk_state
  ON model_experiments(mode, lifecycle_state, horizon, updated_at);

CREATE TABLE IF NOT EXISTS model_predictions (
  prediction_id TEXT PRIMARY KEY,
  experiment_id TEXT NOT NULL,
  candidate_id TEXT NOT NULL,
  symbol TEXT NOT NULL,
  mode TEXT NOT NULL CHECK(mode IN ('intraday','delivery')),
  horizon TEXT NOT NULL,
  observed_at TEXT NOT NULL,
  probability_positive REAL,
  expected_net_return_bps REAL,
  target_before_stop_probability REAL,
  downside_quantile_bps REAL,
  prediction_json TEXT NOT NULL,
  prediction_hash TEXT NOT NULL UNIQUE,
  created_at TEXT NOT NULL,
  FOREIGN KEY(experiment_id) REFERENCES model_experiments(experiment_id)
);
CREATE INDEX IF NOT EXISTS ix_model_predictions_eval
  ON model_predictions(experiment_id, observed_at, symbol);

CREATE TABLE IF NOT EXISTS model_evaluations (
  evaluation_id TEXT PRIMARY KEY,
  experiment_id TEXT NOT NULL,
  evaluation_as_of TEXT NOT NULL,
  observation_count INTEGER NOT NULL,
  trading_days INTEGER NOT NULL,
  regime_count INTEGER NOT NULL,
  post_cost_expectancy_bps REAL,
  lower_confidence_bound_bps REAL,
  incremental_lcb_bps REAL,
  brier_score REAL,
  log_loss REAL,
  rank_ic REAL,
  top_bucket_lift_bps REAL,
  maximum_drawdown_bps REAL,
  turnover_bps REAL,
  multiple_testing_adjusted_pvalue REAL,
  latency_p95_ms REAL,
  evaluation_json TEXT NOT NULL,
  record_hash TEXT NOT NULL UNIQUE,
  created_at TEXT NOT NULL,
  FOREIGN KEY(experiment_id) REFERENCES model_experiments(experiment_id)
);
CREATE INDEX IF NOT EXISTS ix_model_evaluations_experiment
  ON model_evaluations(experiment_id, evaluation_as_of);

CREATE TABLE IF NOT EXISTS model_lifecycle_decisions (
  decision_id TEXT PRIMARY KEY,
  experiment_id TEXT NOT NULL,
  previous_state TEXT NOT NULL,
  new_state TEXT NOT NULL,
  decision_reason TEXT NOT NULL,
  production_weight REAL,
  evidence_json TEXT NOT NULL,
  decided_at TEXT NOT NULL,
  FOREIGN KEY(experiment_id) REFERENCES model_experiments(experiment_id)
);
CREATE INDEX IF NOT EXISTS ix_model_lifecycle_decisions_experiment
  ON model_lifecycle_decisions(experiment_id, decided_at);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _sha(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8", errors="replace")).hexdigest()


def _finite(value: Any) -> Optional[float]:
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def ensure_model_tournament_schema(conn: Any) -> None:
    conn.executescript(_SCHEMA)
    conn.commit()


@dataclass(frozen=True)
class PromotionRules:
    minimum_incremental_lcb_bps: float = 0.0
    maximum_adjusted_pvalue: float = 0.10
    maximum_brier_score: float = 0.30
    require_positive_expectancy: bool = True


class ModelTournamentService:
    def __init__(self, store: Any, *, rules: PromotionRules | None = None):
        self.store = store
        if not hasattr(store, "write_lock"):
            store.write_lock = threading.RLock()
        self.rules = rules or PromotionRules()
        self.production_governance = getattr(store, "production_model_governance_read_repository", None) or getattr(store, "production_model_governance_repository", None)
        self.production_governance_required = bool(getattr(store, "production_model_governance_required", False))
        with self.store.write_lock:
            ensure_model_tournament_schema(self.store.conn)

    @staticmethod
    def _validate_horizon(mode: str, horizon: str) -> str:
        desk = require_production_mode(mode)
        value = str(horizon or "").strip().lower()
        allowed = set(architecture_for(desk).horizons)
        if value not in allowed:
            raise ValueError(f"unsupported {desk} horizon {value!r}; expected {sorted(allowed)}")
        return value

    def register_candidate(
        self,
        *,
        model_key: str,
        library_key: str,
        mode: str,
        horizon: str,
        benchmark_model_key: str,
        dataset_fingerprint: str,
        feature_manifest_hash: str,
        target_name: str,
        validation_deadline: str,
        required_observations: int,
        required_regimes: int = 3,
        config: Mapping[str, Any] | None = None,
    ) -> Dict[str, Any]:
        if self.production_governance_required:
            raise RuntimeError("LEGACY_SQLITE_TOURNAMENT_READ_ONLY: use separate PostgreSQL model-governance workflow")
        desk = require_production_mode(mode)
        horizon_key = self._validate_horizon(desk, horizon)
        required = {
            "model_key": model_key,
            "library_key": library_key,
            "benchmark_model_key": benchmark_model_key,
            "dataset_fingerprint": dataset_fingerprint,
            "feature_manifest_hash": feature_manifest_hash,
            "target_name": target_name,
            "validation_deadline": validation_deadline,
        }
        missing = [name for name, value in required.items() if not str(value or "").strip()]
        if missing:
            raise ValueError("missing model candidate fields: " + ", ".join(missing))
        if int(required_observations) < 30:
            raise ValueError("required_observations must be at least 30")
        if int(required_regimes) < 1:
            raise ValueError("required_regimes must be positive")
        identity = {
            "model_key": str(model_key).strip(),
            "mode": desk,
            "horizon": horizon_key,
            "dataset_fingerprint": str(dataset_fingerprint).strip(),
        }
        experiment_id = "exp_" + _sha(identity)[:24]
        stamp = _now()
        payload = dict(config or {})
        with self.store.write_lock:
            self.store.conn.execute(
                """INSERT OR IGNORE INTO model_experiments(
                experiment_id,model_key,library_key,mode,horizon,lifecycle_state,
                benchmark_model_key,dataset_fingerprint,feature_manifest_hash,target_name,
                required_observations,required_regimes,validation_deadline,production_weight,
                rejection_reason,config_json,created_at,updated_at)
                VALUES(?,?,?,?,?,'EXPERIMENT',?,?,?,?,?,?,?,NULL,NULL,?,?,?)""",
                (
                    experiment_id,
                    str(model_key).strip(),
                    str(library_key).strip(),
                    desk,
                    horizon_key,
                    str(benchmark_model_key).strip(),
                    str(dataset_fingerprint).strip(),
                    str(feature_manifest_hash).strip(),
                    str(target_name).strip(),
                    int(required_observations),
                    int(required_regimes),
                    str(validation_deadline).strip(),
                    _canonical(payload),
                    stamp,
                    stamp,
                ),
            )
            self.store.conn.commit()
        return self.experiment(experiment_id)

    def experiment(self, experiment_id: str) -> Dict[str, Any]:
        row = self.store.conn.execute(
            "SELECT * FROM model_experiments WHERE experiment_id=?", (str(experiment_id),)
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown experiment {experiment_id}")
        columns = [item[1] for item in self.store.conn.execute("PRAGMA table_info(model_experiments)").fetchall()]
        data = dict(zip(columns, row))
        try:
            data["config"] = json.loads(data.pop("config_json") or "{}")
        except Exception:
            data["config"] = {}
        data["production_influence"] = data.get("lifecycle_state") == "ACTIVE_PRODUCTION"
        return data

    def start_validation(self, experiment_id: str, *, reason: str = "candidate admitted to finite tournament") -> Dict[str, Any]:
        if self.production_governance_required:
            raise RuntimeError("LEGACY_SQLITE_TOURNAMENT_READ_ONLY: use separate PostgreSQL model-governance workflow")
        return self._transition(
            experiment_id,
            new_state="ACTIVE_VALIDATION",
            reason=reason,
            production_weight=None,
            evidence={"responsibility": "generate frozen out-of-sample predictions; no production influence"},
            allowed_previous={"EXPERIMENT"},
        )

    def record_prediction(
        self,
        *,
        experiment_id: str,
        candidate_id: str,
        symbol: str,
        observed_at: str,
        probability_positive: Any = None,
        expected_net_return_bps: Any = None,
        target_before_stop_probability: Any = None,
        downside_quantile_bps: Any = None,
        detail: Mapping[str, Any] | None = None,
    ) -> Dict[str, Any]:
        if self.production_governance_required:
            raise RuntimeError("LEGACY_SQLITE_TOURNAMENT_READ_ONLY: use separate PostgreSQL model-governance workflow")
        experiment = self.experiment(experiment_id)
        if experiment["lifecycle_state"] != "ACTIVE_VALIDATION":
            raise ValueError("predictions may be recorded only during ACTIVE_VALIDATION")
        probability = _finite(probability_positive)
        target_probability = _finite(target_before_stop_probability)
        for name, value in (("probability_positive", probability), ("target_before_stop_probability", target_probability)):
            if value is not None and not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be between 0 and 1")
        payload = {
            "experiment_id": experiment_id,
            "candidate_id": str(candidate_id),
            "symbol": str(symbol).upper(),
            "mode": experiment["mode"],
            "horizon": experiment["horizon"],
            "observed_at": str(observed_at),
            "probability_positive": probability,
            "expected_net_return_bps": _finite(expected_net_return_bps),
            "target_before_stop_probability": target_probability,
            "downside_quantile_bps": _finite(downside_quantile_bps),
            "detail": dict(detail or {}),
        }
        prediction_hash = _sha(payload)
        prediction_id = "pred_" + prediction_hash[:24]
        with self.store.write_lock:
            self.store.conn.execute(
                """INSERT OR IGNORE INTO model_predictions(
                prediction_id,experiment_id,candidate_id,symbol,mode,horizon,observed_at,
                probability_positive,expected_net_return_bps,target_before_stop_probability,
                downside_quantile_bps,prediction_json,prediction_hash,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    prediction_id,
                    experiment_id,
                    payload["candidate_id"],
                    payload["symbol"],
                    payload["mode"],
                    payload["horizon"],
                    payload["observed_at"],
                    payload["probability_positive"],
                    payload["expected_net_return_bps"],
                    payload["target_before_stop_probability"],
                    payload["downside_quantile_bps"],
                    _canonical(payload["detail"]),
                    prediction_hash,
                    _now(),
                ),
            )
            self.store.conn.commit()
        return {"ok": True, "prediction_id": prediction_id, "prediction_hash": prediction_hash, **payload}

    def record_evaluation(self, *, experiment_id: str, metrics: Mapping[str, Any]) -> Dict[str, Any]:
        if self.production_governance_required:
            raise RuntimeError("LEGACY_SQLITE_TOURNAMENT_READ_ONLY: use separate PostgreSQL model-governance workflow")
        experiment = self.experiment(experiment_id)
        if experiment["lifecycle_state"] != "ACTIVE_VALIDATION":
            raise ValueError("evaluation may be recorded only during ACTIVE_VALIDATION")
        payload = dict(metrics or {})
        observation_count = int(payload.get("observation_count") or 0)
        trading_days = int(payload.get("trading_days") or 0)
        regime_count = int(payload.get("regime_count") or 0)
        evaluation_as_of = str(payload.get("evaluation_as_of") or _now())
        record_material = {
            "experiment_id": experiment_id,
            "evaluation_as_of": evaluation_as_of,
            "metrics": payload,
        }
        record_hash = _sha(record_material)
        evaluation_id = "eval_" + record_hash[:24]
        fields = (
            "post_cost_expectancy_bps",
            "lower_confidence_bound_bps",
            "incremental_lcb_bps",
            "brier_score",
            "log_loss",
            "rank_ic",
            "top_bucket_lift_bps",
            "maximum_drawdown_bps",
            "turnover_bps",
            "multiple_testing_adjusted_pvalue",
            "latency_p95_ms",
        )
        values = [_finite(payload.get(name)) for name in fields]
        with self.store.write_lock:
            self.store.conn.execute(
                """INSERT OR IGNORE INTO model_evaluations(
                evaluation_id,experiment_id,evaluation_as_of,observation_count,trading_days,regime_count,
                post_cost_expectancy_bps,lower_confidence_bound_bps,incremental_lcb_bps,
                brier_score,log_loss,rank_ic,top_bucket_lift_bps,maximum_drawdown_bps,
                turnover_bps,multiple_testing_adjusted_pvalue,latency_p95_ms,
                evaluation_json,record_hash,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    evaluation_id,
                    experiment_id,
                    evaluation_as_of,
                    observation_count,
                    trading_days,
                    regime_count,
                    *values,
                    _canonical(payload),
                    record_hash,
                    _now(),
                ),
            )
            self.store.conn.commit()
        return {"ok": True, "evaluation_id": evaluation_id, "record_hash": record_hash, **payload}

    def decide(self, experiment_id: str, *, production_weight: float | None = None) -> Dict[str, Any]:
        if self.production_governance_required:
            raise RuntimeError("LEGACY_SQLITE_TOURNAMENT_READ_ONLY: PostgreSQL promotion assignments are authoritative")
        experiment = self.experiment(experiment_id)
        if experiment["lifecycle_state"] != "ACTIVE_VALIDATION":
            raise ValueError("only ACTIVE_VALIDATION experiments can be decided")
        evaluation = self.latest_evaluation(experiment_id)
        if not evaluation:
            raise ValueError("no evaluation exists")
        reasons: list[str] = []
        if int(evaluation.get("observation_count") or 0) < int(experiment["required_observations"]):
            reasons.append("insufficient observations")
        if int(evaluation.get("regime_count") or 0) < int(experiment["required_regimes"]):
            reasons.append("insufficient regimes")
        expectancy = _finite(evaluation.get("post_cost_expectancy_bps"))
        incremental_lcb = _finite(evaluation.get("incremental_lcb_bps"))
        adjusted_pvalue = _finite(evaluation.get("multiple_testing_adjusted_pvalue"))
        brier = _finite(evaluation.get("brier_score"))
        if self.rules.require_positive_expectancy and (expectancy is None or expectancy <= 0.0):
            reasons.append("post-cost expectancy is not positive")
        if incremental_lcb is None or incremental_lcb <= self.rules.minimum_incremental_lcb_bps:
            reasons.append("incremental lower confidence bound does not beat benchmark")
        if adjusted_pvalue is None or adjusted_pvalue > self.rules.maximum_adjusted_pvalue:
            reasons.append("multiple-testing adjusted significance failed")
        if brier is not None and brier > self.rules.maximum_brier_score:
            reasons.append("calibration failed")
        latency = _finite(evaluation.get("latency_p95_ms"))
        latency_budget = architecture_for(experiment["mode"]).latency_budgets_ms["decision"]
        if latency is not None and latency > latency_budget:
            reasons.append("latency budget failed")
        if reasons:
            return self._transition(
                experiment_id,
                new_state="REJECTED",
                reason="; ".join(reasons),
                production_weight=None,
                evidence=evaluation,
                allowed_previous={"ACTIVE_VALIDATION"},
            )
        weight = _finite(production_weight)
        if weight is None or not 0.0 < weight <= 1.0:
            raise ValueError("a promoted model requires production_weight in (0,1]")
        return self._transition(
            experiment_id,
            new_state="ACTIVE_PRODUCTION",
            reason="validated incremental post-cost edge",
            production_weight=weight,
            evidence=evaluation,
            allowed_previous={"ACTIVE_VALIDATION"},
        )

    def reject(self, experiment_id: str, *, reason: str) -> Dict[str, Any]:
        if self.production_governance_required:
            raise RuntimeError("LEGACY_SQLITE_TOURNAMENT_READ_ONLY: PostgreSQL promotion decisions are authoritative")
        if not str(reason or "").strip():
            raise ValueError("rejection reason is required")
        return self._transition(
            experiment_id,
            new_state="REJECTED",
            reason=str(reason).strip(),
            production_weight=None,
            evidence={},
            allowed_previous={"EXPERIMENT", "ACTIVE_VALIDATION"},
        )

    def _transition(
        self,
        experiment_id: str,
        *,
        new_state: str,
        reason: str,
        production_weight: float | None,
        evidence: Mapping[str, Any],
        allowed_previous: set[str],
    ) -> Dict[str, Any]:
        if new_state not in LIFECYCLE_STATES:
            raise ValueError(f"unknown lifecycle state {new_state}")
        current = self.experiment(experiment_id)
        previous = current["lifecycle_state"]
        if previous not in allowed_previous:
            raise ValueError(f"cannot transition {previous} -> {new_state}")
        if new_state == "ACTIVE_PRODUCTION" and (production_weight is None or production_weight <= 0):
            raise ValueError("ACTIVE_PRODUCTION requires positive production weight")
        decision_material = {
            "experiment_id": experiment_id,
            "previous_state": previous,
            "new_state": new_state,
            "reason": reason,
            "evidence": dict(evidence or {}),
            "decided_at": _now(),
        }
        decision_id = "mdec_" + _sha(decision_material)[:24]
        with self.store.write_lock:
            self.store.conn.execute(
                """UPDATE model_experiments SET lifecycle_state=?,production_weight=?,
                rejection_reason=?,updated_at=? WHERE experiment_id=?""",
                (
                    new_state,
                    production_weight if new_state == "ACTIVE_PRODUCTION" else None,
                    reason if new_state == "REJECTED" else None,
                    decision_material["decided_at"],
                    experiment_id,
                ),
            )
            self.store.conn.execute(
                """INSERT INTO model_lifecycle_decisions(
                decision_id,experiment_id,previous_state,new_state,decision_reason,
                production_weight,evidence_json,decided_at) VALUES(?,?,?,?,?,?,?,?)""",
                (
                    decision_id,
                    experiment_id,
                    previous,
                    new_state,
                    reason,
                    production_weight if new_state == "ACTIVE_PRODUCTION" else None,
                    _canonical(dict(evidence or {})),
                    decision_material["decided_at"],
                ),
            )
            self.store.conn.commit()
        return {"ok": True, "decision_id": decision_id, **self.experiment(experiment_id)}

    def latest_evaluation(self, experiment_id: str) -> Dict[str, Any] | None:
        cursor = self.store.conn.execute(
            """SELECT * FROM model_evaluations WHERE experiment_id=?
            ORDER BY evaluation_as_of DESC, created_at DESC LIMIT 1""",
            (str(experiment_id),),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        columns = [item[0] for item in cursor.description]
        data = dict(zip(columns, row))
        try:
            data["detail"] = json.loads(data.pop("evaluation_json") or "{}")
        except Exception:
            data["detail"] = {}
        return data

    def status(self, *, mode: str | None = None) -> Dict[str, Any]:
        if self.production_governance_required:
            if self.production_governance is None:
                return {"ok": False, "version": SERVICE_VERSION, "state": "GOVERNANCE_AUTHORITY_MISSING", "experiments": []}
            return self.production_governance.status(mode)
        params: tuple[Any, ...] = ()
        where = ""
        if mode:
            desk = require_production_mode(mode)
            where = "WHERE mode=?"
            params = (desk,)
        rows = self.store.conn.execute(
            f"""SELECT experiment_id,model_key,library_key,mode,horizon,lifecycle_state,
            benchmark_model_key,dataset_fingerprint,target_name,required_observations,
            required_regimes,validation_deadline,production_weight,rejection_reason,updated_at
            FROM model_experiments {where}
            ORDER BY mode,horizon,lifecycle_state,model_key""",
            params,
        ).fetchall()
        columns = (
            "experiment_id", "model_key", "library_key", "mode", "horizon", "lifecycle_state",
            "benchmark_model_key", "dataset_fingerprint", "target_name", "required_observations",
            "required_regimes", "validation_deadline", "production_weight", "rejection_reason", "updated_at",
        )
        experiments = [dict(zip(columns, row)) for row in rows]
        counts: Dict[str, Dict[str, int]] = {
            desk: {state: 0 for state in LIFECYCLE_STATES} for desk in ("intraday", "delivery")
        }
        for row in experiments:
            counts[row["mode"]][row["lifecycle_state"]] += 1
        return {
            "ok": True,
            "version": SERVICE_VERSION,
            "lifecycle_states": list(LIFECYCLE_STATES),
            "desks": counts,
            "experiments": experiments,
            "production_rule": "Only ACTIVE_PRODUCTION experiments are visible to the decision ensemble and every such model has positive weight.",
            "validation_rule": "ACTIVE_VALIDATION generates frozen predictions and evaluation evidence but is absent from production scoring.",
        }
