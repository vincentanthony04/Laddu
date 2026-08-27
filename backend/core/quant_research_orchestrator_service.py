"""Governed finite model-tournament orchestration for both production desks.

The orchestrator turns newly settled immutable labels into calibrated research
candidates, records them in the finite model tournament and produces validation
reports. It never changes production weights, risk limits, trade levels or
broker state; promotion remains an explicit evidence decision owned by
ModelTournamentService.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import threading
from typing import Any, Dict, Optional

from core.nse_calibrated_challenger_service import (
    FORECAST_HORIZONS,
    NseCalibratedChallengerService,
)
from core.production_mode_policy import require_production_mode
from core.quant_analytics_service import QuantAnalyticsService
from core.quant_edge_data_service import QuantEdgeDataService
from core.quant_paper_activation_service import QuantPaperActivationService
from core.model_tournament_service import ModelTournamentService
from core.dual_desk_architecture_service import DualDeskArchitectureService
from core.selection_research_validation_service import SelectionResearchValidationService
from core.selection_platform_service import SelectionPlatformService


ORCHESTRATOR_VERSION = "dual-desk-quant-tournament-orchestrator-3.3.0-runtime-revalidation"
CADENCE_HOURS = {"intraday": 6, "delivery": 20}
SHADOW_MIN_TRAIN_DAYS = 20


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse(value: Any) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _cycle_id(payload: Any) -> str:
    return hashlib.sha256(_canonical(payload).encode("utf-8", errors="replace")).hexdigest()[:40]


class QuantResearchOrchestratorService:
    """Run equal-priority Delivery and Intraday model tournaments."""

    def __init__(self, store: Any):
        self.store = store
        if not hasattr(store, "write_lock"):
            store.write_lock = threading.RLock()
        if not hasattr(store, "_quant_research_cycle_locks"):
            store._quant_research_cycle_locks = {
                "intraday": threading.Lock(),
                "delivery": threading.Lock(),
            }
        self._cycle_locks = store._quant_research_cycle_locks
        self.data = QuantEdgeDataService(store)
        self.models = NseCalibratedChallengerService(store)
        self.analytics = QuantAnalyticsService(store)
        self.validation = SelectionResearchValidationService(store)
        self.selection = SelectionPlatformService(store)
        self.tournament = ModelTournamentService(store)
        self.architecture = DualDeskArchitectureService()
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        with self.store.write_lock:
            self.store.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS quant_research_cycles (
                  cycle_id TEXT PRIMARY KEY,
                  mode TEXT NOT NULL,
                  trigger_name TEXT NOT NULL,
                  started_at TEXT NOT NULL,
                  completed_at TEXT NOT NULL,
                  label_count INTEGER NOT NULL,
                  snapshot_count INTEGER NOT NULL,
                  state TEXT NOT NULL,
                  result_json TEXT NOT NULL,
                  result_hash TEXT NOT NULL UNIQUE,
                  orchestrator_version TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_quant_research_cycles_latest
                  ON quant_research_cycles(mode, completed_at);
                """
            )
            self.store.conn.commit()


    def _runtime_fingerprint(self) -> str:
        """Fingerprint executable research code so a source fix invalidates stale failures.

        Evidence cadence must suppress only unchanged data *and* unchanged code.
        A corrected worker must be re-run once after install even when label/snapshot
        counts have not advanced; otherwise a persisted WORKER_FAILED result can
        survive indefinitely.
        """
        material = {"orchestrator_version": ORCHESTRATOR_VERSION}
        for label, path in (("lightgbm_worker", self.analytics.worker),):
            try:
                material[label] = hashlib.sha256(Path(path).read_bytes()).hexdigest()
            except Exception:
                material[label] = "UNAVAILABLE"
        return hashlib.sha256(_canonical(material).encode("utf-8", errors="replace")).hexdigest()

    def _latest_cycle(self, mode: str) -> Optional[Dict[str, Any]]:
        raw = self.store.conn.execute(
            """SELECT * FROM quant_research_cycles
               WHERE mode=? ORDER BY completed_at DESC LIMIT 1""",
            (mode,),
        ).fetchone()
        if not raw:
            return None
        row = dict(raw)
        try:
            result = json.loads(row.get("result_json") or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            result = {}
        return {
            "cycle_id": row["cycle_id"],
            "mode": row["mode"],
            "trigger": row["trigger_name"],
            "started_at": row["started_at"],
            "completed_at": row["completed_at"],
            "label_count": int(row["label_count"]),
            "snapshot_count": int(row["snapshot_count"]),
            "state": row["state"],
            "result": result,
        }

    def _latest_selection(self, mode: str) -> Dict[str, Any]:
        """Return rank research without fabricating model evidence or weights."""
        latest = self.store.conn.execute(
            """SELECT population_fingerprint,MAX(created_at) AS created_at
               FROM shadow_selector_predictions WHERE mode=?
               GROUP BY population_fingerprint
               ORDER BY created_at DESC,population_fingerprint DESC LIMIT 1""",
            (mode,),
        ).fetchone()
        if not latest:
            return {
                "state": "NO_POPULATION_RECORDED",
                "population_fingerprint": None,
                "top_quant": [],
                "production_influence": False,
            }
        population = str(latest["population_fingerprint"])
        rows = self.store.conn.execute(
            """SELECT symbol,rank,score,feature_coverage,prediction_json
               FROM shadow_selector_predictions
               WHERE population_fingerprint=? AND arm='quant'
               ORDER BY rank,symbol LIMIT 5""",
            (population,),
        ).fetchall()
        leaders = []
        for raw in rows:
            row = dict(raw)
            try:
                prediction = json.loads(row.pop("prediction_json") or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                prediction = {}
            leaders.append({
                "symbol": row["symbol"],
                "rank": int(row["rank"]),
                "rank_tier": str(prediction.get("rank_tier") or "UNRATED"),
                "score": round(float(row["score"]), 4),
                "feature_coverage": round(float(row["feature_coverage"]), 4),
                "setup_family": prediction.get("setup_family"),
                "responsibility": "RESEARCH_RANKING_INPUT",
                "production_influence": False,
            })
        return {
            "state": "RESEARCH_RANK_READY",
            "population_fingerprint": population,
            "created_at": str(latest["created_at"]),
            "top_quant": leaders,
            "production_influence": False,
        }

    def _register_calibrated_candidate(self, mode: str, horizon: str, training: Dict[str, Any]) -> Dict[str, Any]:
        model_id = str(training.get("model_id") or "").strip()
        if not model_id:
            return {"ok": True, "state": "NO_CANDIDATE"}
        validation = dict(training.get("validation") or {})
        readiness = dict(training.get("readiness") or {})
        observations = int(
            validation.get("out_of_sample_observations")
            or readiness.get("observations")
            or training.get("observations")
            or 0
        )
        regimes = int(readiness.get("regimes") or training.get("regimes") or 0)
        material = {
            "model_id": model_id,
            "mode": mode,
            "horizon": horizon,
            "readiness": readiness,
            "validation": validation,
        }
        digest = hashlib.sha256(_canonical(material).encode("utf-8", errors="replace")).hexdigest()
        experiment = self.tournament.register_candidate(
            model_key=model_id,
            library_key="scikit-learn",
            mode=mode,
            horizon=horizon,
            benchmark_model_key="laddu_current_production",
            dataset_fingerprint=digest,
            feature_manifest_hash=hashlib.sha256(
                _canonical((validation.get("feature_redundancy_audit") or {})).encode("utf-8")
            ).hexdigest(),
            target_name="positive_net_return_and_return_distribution",
            validation_deadline=(datetime.now(timezone.utc) + timedelta(days=180)).isoformat(timespec="seconds").replace("+00:00", "Z"),
            required_observations=max(300, observations),
            required_regimes=max(3, regimes),
            config={
                "model_family": training.get("model_family") or "CALIBRATED_LOGISTIC_RIDGE",
                "historical_validation": validation,
                "training_state": training.get("state"),
            },
        )
        if experiment.get("lifecycle_state") == "EXPERIMENT":
            experiment = self.tournament.start_validation(experiment["experiment_id"])
        return {"ok": True, **experiment}

    def status(self) -> Dict[str, Any]:
        # Legacy paper activation remains readable for existing installed data,
        # but the finite tournament is the only authority for new promotion.
        paper_status = QuantPaperActivationService(self.store).status()
        tournament_all = self.tournament.status()
        tournament_by_model = {
            str(row.get("model_key")): row
            for row in tournament_all.get("experiments", [])
            if row.get("model_key")
        }
        desks: Dict[str, Any] = {}
        for mode in ("intraday", "delivery"):
            data_status = self.data.status(mode)
            analytics_status = self.analytics.status(mode)
            models: Dict[str, Any] = {}
            for horizon in FORECAST_HORIZONS[mode]:
                model_status = self.models.status(mode=mode, horizon=horizon)
                model_id = str(model_status.get("model_id") or "")
                lifecycle = tournament_by_model.get(model_id) or {}
                models[horizon] = {
                    "state": model_status.get("state"),
                    "model_id": model_status.get("model_id"),
                    "library": "scikit-learn",
                    "observations": model_status.get("observations")
                    or (model_status.get("readiness") or {}).get("observations", 0),
                    "trading_days": model_status.get("trading_days")
                    or (model_status.get("readiness") or {}).get("trading_days", 0),
                    "regimes": model_status.get("regimes")
                    or (model_status.get("readiness") or {}).get("regimes", 0),
                    "lifecycle_state": lifecycle.get("lifecycle_state") or "NOT_REGISTERED",
                    "production_weight": lifecycle.get("production_weight"),
                    "production_influence": lifecycle.get("lifecycle_state") == "ACTIVE_PRODUCTION",
                }
                nonlinear = (analytics_status.get("lightgbm_models") or {}).get(horizon) or {}
                nonlinear_id = str(nonlinear.get("model_id") or "")
                nonlinear_lifecycle = tournament_by_model.get(nonlinear_id) or {}
                models[f"lightgbm:{horizon}"] = {
                    "state": nonlinear.get("state") or "NOT_TRAINED",
                    "model_id": nonlinear.get("model_id"),
                    "library": "lightgbm",
                    "observations": nonlinear.get("observations", 0),
                    "trading_days": nonlinear.get("trading_days", 0),
                    "regimes": nonlinear.get("regimes", 0),
                    "lifecycle_state": nonlinear_lifecycle.get("lifecycle_state") or "NOT_REGISTERED",
                    "production_weight": nonlinear_lifecycle.get("production_weight"),
                    "production_influence": nonlinear_lifecycle.get("lifecycle_state") == "ACTIVE_PRODUCTION",
                }
            latest = self._latest_cycle(mode)
            latest_at = _parse(latest.get("completed_at")) if latest else None
            due_at = (
                (latest_at + timedelta(hours=CADENCE_HOURS[mode])).isoformat().replace("+00:00", "Z")
                if latest_at else None
            )
            desks[mode] = {
                "architecture": self.architecture.status()["desks"][mode],
                "data": data_status,
                "analytics": analytics_status,
                "models": models,
                "model_tournament": self.tournament.status(mode=mode),
                "latest_selection": self._latest_selection(mode),
                "range_compression": (
                    self.selection.latest_range_compression(mode)
                    if mode == "delivery"
                    else {"state": "NOT_APPLICABLE", "production_influence": False}
                ),
                "latest_cycle": latest,
                "cadence_hours": CADENCE_HOURS[mode],
                "next_cycle_due_at": due_at,
            }
        active = [
            row for row in tournament_all.get("experiments", [])
            if row.get("lifecycle_state") == "ACTIVE_PRODUCTION"
        ]
        return {
            "ok": True,
            "version": ORCHESTRATOR_VERSION,
            "selection_edge": "REQUIRES_TOURNAMENT_EVIDENCE",
            "model_tournament": tournament_all,
            "prediction_state": "ACTIVE_PRODUCTION" if active else "ACTIVE_VALIDATION_OR_UNAVAILABLE",
            "production_models": active,
            "production_influence": bool(active),
            "broker_order_authority": "NONE",
            "automatic_promotion": False,
            "legacy_paper_activation": paper_status,
            "desks": desks,
            "gates": {
                "settled_observations": 300,
                "trading_days": 126,
                "regimes": 3,
                "positive_post_cost_expectancy": True,
                "positive_incremental_lower_confidence_bound": True,
                "multiple_testing_adjustment": True,
                "calibration_required": True,
                "latency_budget_required": True,
                "finite_validation_deadline": True,
            },
        }

    def run_cycle(
        self,
        *,
        mode: str,
        trigger: str = "manual",
        trial_count: int = 1,
        min_train_days: int = 126,
        test_days: int = 21,
        max_folds: int = 8,
        embargo_days: int = 1,
    ) -> Dict[str, Any]:
        desk = require_production_mode(mode)
        cycle_lock = self._cycle_locks[desk]
        if not cycle_lock.acquire(blocking=False):
            return {
                "ok": True,
                "state": "ALREADY_RUNNING",
                "mode": desk,
                "selection_edge": "NOT_YET_STATISTICALLY_VALIDATED",
                "prediction_state": "MODEL_UNAVAILABLE",
                "decision_weight": 0.0,
                "production_change_allowed": False,
            }
        try:
            return self._run_locked_cycle(
                mode=desk,
                trigger=trigger,
                trial_count=trial_count,
                min_train_days=min_train_days,
                test_days=test_days,
                max_folds=max_folds,
                embargo_days=embargo_days,
            )
        finally:
            cycle_lock.release()

    def _run_locked_cycle(
        self,
        *,
        mode: str,
        trigger: str,
        trial_count: int,
        min_train_days: int,
        test_days: int,
        max_folds: int,
        embargo_days: int,
    ) -> Dict[str, Any]:
        desk = require_production_mode(mode)
        started = _now()
        backfill = self.data.backfill_existing()
        data_status = self.data.status(desk)
        projection = self.analytics.project(mode=desk)
        horizons: Dict[str, Any] = {}
        review_candidates = 0
        for horizon in FORECAST_HORIZONS[desk]:
            training = self.models.train(
                mode=desk,
                horizon=horizon,
                min_train_days=min_train_days,
                test_days=test_days,
                max_folds=max_folds,
                embargo_days=embargo_days,
                trial_count=max(1, int(trial_count)),
            )
            report = self.validation.report(mode=desk, horizon=horizon)
            nonlinear = self.analytics.train_lightgbm(
                mode=desk,
                horizon=horizon,
                # Shadow evaluation is deliberately allowed to begin earlier
                # than statistical qualification. The worker still enforces
                # the immutable 126-day/holdout/regime production gates.
                min_train_days=SHADOW_MIN_TRAIN_DAYS,
                test_days=test_days,
                max_folds=max_folds,
                embargo_days=embargo_days,
                trial_count=max(1, int(trial_count)),
                projection_result=projection,
            )
            calibrated_tournament = self._register_calibrated_candidate(desk, horizon, training)
            candidate_states = {
                str(calibrated_tournament.get("lifecycle_state") or calibrated_tournament.get("state") or ""),
                str(((nonlinear.get("model_tournament") or {}).get("lifecycle_state")) or ""),
            }
            review_candidates += int("ACTIVE_VALIDATION" in candidate_states)
            horizons[horizon] = {
                "calibrated_training": training,
                "calibrated_tournament": calibrated_tournament,
                "lightgbm_training": nonlinear,
                "validation": report,
                "production_change_allowed": False,
            }
        state = "ACTIVE_VALIDATION" if review_candidates else "NO_VALIDATION_CANDIDATE"
        completed = _now()
        result = {
            "mode": desk,
            "runtime_fingerprint": self._runtime_fingerprint(),
            "trigger": str(trigger or "manual"),
            "started_at": started,
            "completed_at": completed,
            "backfill": backfill,
            "data": data_status,
            "analytics_projection": projection,
            "horizons": horizons,
            "state": state,
            "selection_edge": "NOT_YET_STATISTICALLY_VALIDATED",
            "prediction_state": state,
            "production_influence": False,
            "broker_execution_weight": 0.0,
            "promotion_authority": "MODEL_TOURNAMENT_SERVICE",
            "orchestrator_version": ORCHESTRATOR_VERSION,
        }
        result_hash = hashlib.sha256(_canonical(result).encode("utf-8", errors="replace")).hexdigest()
        cycle = _cycle_id({"result_hash": result_hash, "completed_at": completed})
        with self.store.write_lock:
            self.store.conn.execute(
                """INSERT INTO quant_research_cycles(
                    cycle_id,mode,trigger_name,started_at,completed_at,label_count,
                    snapshot_count,state,result_json,result_hash,orchestrator_version
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    cycle, desk, str(trigger or "manual"), started, completed,
                    int(data_status["labels"]), int(data_status["snapshots"]), state,
                    _canonical(result), result_hash, ORCHESTRATOR_VERSION,
                ),
            )
            self.store.conn.commit()
        return {"ok": True, "cycle_id": cycle, **result}

    def maybe_run_cycle(self, *, mode: str, trigger: str = "scheduled-tournament") -> Dict[str, Any]:
        desk = require_production_mode(mode)
        latest = self._latest_cycle(desk)
        status = dict(self.data.status(desk) or {})
        current_labels = int(status.get("labels") or 0)
        current_snapshots = int(status.get("snapshots") or 0)
        if latest:
            completed = _parse(latest["completed_at"])
            not_due = completed is not None and datetime.now(timezone.utc) < (
                completed + timedelta(hours=CADENCE_HOURS[desk])
            )
            labels_advanced = current_labels > int(latest.get("label_count") or 0)
            snapshots_advanced = current_snapshots > int(latest.get("snapshot_count") or 0)
            evidence_advanced = labels_advanced or snapshots_advanced
            latest_runtime = str((latest.get("result") or {}).get("runtime_fingerprint") or "")
            current_runtime = self._runtime_fingerprint()
            runtime_changed = latest_runtime != current_runtime
            # New point-in-time evidence is a convergence event and must be evaluated
            # immediately.  R6 also treats executable research-code change as an
            # evidence event so a persisted worker exception cannot remain authoritative
            # after the corrected worker has been installed.
            if not evidence_advanced and not runtime_changed:
                return {
                    "ok": True,
                    "state": "NOT_DUE" if not_due else "NO_NEW_EVIDENCE",
                    "mode": desk,
                    "latest_cycle": latest,
                    "current_labels": current_labels,
                    "current_snapshots": current_snapshots,
                    "runtime_fingerprint": current_runtime,
                    "production_change_allowed": False,
                }
            if runtime_changed and not evidence_advanced:
                trigger = f"{trigger}:runtime-source-revalidation"
        return self.run_cycle(mode=desk, trigger=trigger, trial_count=1)
