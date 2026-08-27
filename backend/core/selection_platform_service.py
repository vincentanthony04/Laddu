"""Canonical three-arm selection platform orchestration.

One immutable candidate population is evaluated by the existing heuristic
baseline, an NSE cross-sectional quantitative challenger and a hybrid
challenger.  Challenger outputs are persisted for comparison only.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import threading
from typing import Any, Dict, Iterable, List, Mapping, Optional

from core.candidate_population_service import CandidatePopulationService
from core.nse_cross_sectional_selector_service import NseCrossSectionalSelectorService
from core.nse_calibrated_challenger_service import NseCalibratedChallengerService, DEFAULT_HORIZON, FORECAST_HORIZONS
from core.quant_paper_activation_service import QuantPaperActivationService
from core.range_compression_rule_service import RangeCompressionRuleService

SELECTION_PLATFORM_VERSION = "selection-platform-1.3.0"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class SelectionPlatformService:
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
                "record_selector_predictions", "selector_predictions",
                "latest_selector_population", "latest_selector_challenger_model",
            )
            if (
                self.governance_repository is None
                or getattr(self.governance_repository, "authority", None) is None
                or any(not callable(getattr(self.governance_repository, name, None)) for name in required)
            ):
                raise RuntimeError("PRODUCTION_SELECTION_PLATFORM_REQUIRES_POSTGRES_GOVERNANCE_REPOSITORY")
        if not hasattr(store, "write_lock"):
            store.write_lock = threading.RLock()
        self.populations = CandidatePopulationService(store)
        self.selector = NseCrossSectionalSelectorService()
        if self.production_governance_required:
            # Prediction math is stateless; construction is deliberately
            # bypassed because that compatibility class creates SQLite tables.
            self.calibrated = object.__new__(NseCalibratedChallengerService)
        else:
            self.calibrated = NseCalibratedChallengerService(store)
            self._ensure_schema()

    def _latest_model(self, *, mode: str, horizon: str) -> Optional[Dict[str, Any]]:
        if self.production_governance_required:
            return self.governance_repository.latest_selector_challenger_model(
                mode=mode, horizon=horizon
            )
        return self.calibrated.latest_model(mode=mode, horizon=horizon, eligible_only=True)

    def _ensure_schema(self) -> None:
        with self.store.write_lock:
            self.store.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS shadow_selector_predictions (
                  population_fingerprint TEXT NOT NULL,
                  candidate_id TEXT NOT NULL,
                  arm TEXT NOT NULL,
                  symbol TEXT NOT NULL,
                  mode TEXT NOT NULL,
                  model_version TEXT NOT NULL,
                  score REAL NOT NULL,
                  rank INTEGER NOT NULL,
                  percentile REAL NOT NULL,
                  feature_coverage REAL NOT NULL,
                  setup_family TEXT,
                  meta_label TEXT,
                  probability_positive REAL,
                  expected_net_return REAL,
                  calibration_state TEXT NOT NULL,
                  authority TEXT NOT NULL,
                  prediction_json TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  PRIMARY KEY(population_fingerprint, candidate_id, arm)
                );
                CREATE INDEX IF NOT EXISTS ix_shadow_selector_predictions_latest
                  ON shadow_selector_predictions(mode, arm, created_at, rank);
                CREATE TABLE IF NOT EXISTS range_compression_population_runs (
                  population_fingerprint TEXT PRIMARY KEY,
                  mode TEXT NOT NULL,
                  rule_id TEXT NOT NULL,
                  state TEXT NOT NULL,
                  universe_size INTEGER NOT NULL,
                  qualified_count INTEGER NOT NULL,
                  top_1_count INTEGER NOT NULL,
                  top_5_count INTEGER NOT NULL,
                  summary_json TEXT NOT NULL,
                  created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_range_compression_runs_latest
                  ON range_compression_population_runs(mode,created_at);
                """
            )
            self.store.conn.commit()

    def evaluate_population(
        self,
        rows: Iterable[Mapping[str, Any]],
        *,
        mode: str,
        observed_at: Optional[str],
        universe_id: str,
        dataset_fingerprint: str,
        feature_manifest_hash: str,
    ) -> Dict[str, Any]:
        population = self.populations.record_population(
            rows, mode=mode, observed_at=observed_at, universe_id=universe_id,
            dataset_fingerprint=dataset_fingerprint,
            feature_manifest_hash=feature_manifest_hash,
        )
        stored_rows = self.populations.rows(population["population_fingerprint"])
        result = self.selector.evaluate(
            stored_rows, mode=mode,
            population_fingerprint=population["population_fingerprint"],
        )
        range_research = (
            RangeCompressionRuleService.rank_population(stored_rows)
            if str(mode).lower() == "delivery"
            else {
                "ok": True, "state": "NOT_APPLICABLE",
                "rule": RangeCompressionRuleService.rule_card(),
                "universe_size": len(stored_rows), "qualified_count": 0,
                "primary_top_1_count": 0, "secondary_top_5_count": 0,
                "predictions": [], "top_1_percent": [], "top_5_percent": [],
                "prediction_state": "MODEL_UNAVAILABLE", "decision_weight": 0.0,
            }
        )
        result["arms"]["range_compression"] = range_research["predictions"]
        result["range_compression"] = range_research
        horizons = FORECAST_HORIZONS[str(mode).lower()]
        models = {
            horizon: self._latest_model(mode=mode, horizon=horizon)
            for horizon in horizons
        }
        features_by_id = {str(row.get("candidate_id")): row for row in stored_rows}
        primary_horizon = DEFAULT_HORIZON[str(mode).lower()]
        for arm in ("quant", "hybrid"):
            for item in result["arms"][arm]:
                forecasts = {}
                features = features_by_id.get(str(item.get("candidate_id")), {})
                for horizon in horizons:
                    model = models[horizon]
                    if model is None:
                        forecasts[horizon] = {
                            "state": "MODEL_UNAVAILABLE", "probability_positive": None,
                            "expected_net_return_bps": None, "expected_net_profit_100": None,
                            "probability_support_interval_95": None, "model_id": None,
                            "forecast_display_eligible": False, "probability_kind": "UNAVAILABLE",
                            "prediction_state": "MODEL_UNAVAILABLE",
                            "authority": "MODEL_UNAVAILABLE",
                            "decision_weight": 0.0,
                        }
                    else:
                        forecasts[horizon] = self.calibrated.predict_with_model(model, features=features)
                        entry = features.get("entry") if features.get("entry") is not None else features.get("planned_entry")
                        try:
                            entry_value = float(entry)
                            expected_bps = forecasts[horizon].get("expected_net_return_bps")
                            forecasts[horizon]["expected_net_profit_100"] = (
                                round(entry_value * 100.0 * float(expected_bps) / 10000.0, 2)
                                if expected_bps is not None and entry_value > 0 else None
                            )
                        except (TypeError, ValueError):
                            forecasts[horizon]["expected_net_profit_100"] = None
                item["horizon_forecasts"] = forecasts
                prediction = forecasts[primary_horizon]
                probability = prediction.get("probability_positive")
                item["probability_positive"] = probability
                item["expected_net_return_bps"] = prediction.get("expected_net_return_bps")
                item["expected_net_return"] = prediction.get("expected_net_return_bps")
                item["calibration_state"] = prediction.get("state")
                item["calibrated_model_id"] = prediction.get("model_id")
                item["prediction_horizon"] = primary_horizon
                item["forecast_display_eligible"] = bool(prediction.get("forecast_display_eligible"))
                item["probability_kind"] = prediction.get("probability_kind")
                # A shadow probability must never be translated into a trade
                # action by an unvalidated hard-coded threshold.
                item["calibrated_meta_label"] = None
        created = _now()
        authoritative_predictions = [
            item for arm in ("heuristic", "quant", "hybrid") for item in result["arms"][arm]
        ]
        predictions = authoritative_predictions + list(result["arms"]["range_compression"])
        if self.production_governance_required:
            self.governance_repository.record_selector_predictions(
                population["population_fingerprint"], authoritative_predictions,
                prediction_at=created,
            )
        else:
            with self.store.write_lock:
                for item in predictions:
                    self.store.conn.execute(
                        """INSERT OR IGNORE INTO shadow_selector_predictions(
                            population_fingerprint,candidate_id,arm,symbol,mode,model_version,
                            score,rank,percentile,feature_coverage,setup_family,meta_label,
                            probability_positive,expected_net_return,calibration_state,authority,
                            prediction_json,created_at
                        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            population["population_fingerprint"], item.get("candidate_id"), item.get("arm"),
                            item.get("symbol"), item.get("mode"), item.get("model_version"), item.get("score"),
                            item.get("rank"), item.get("percentile"), item.get("feature_coverage"),
                            item.get("setup_family"), item.get("meta_label"), item.get("probability_positive"),
                            item.get("expected_net_return"), item.get("calibration_state"), item.get("authority"),
                            json.dumps(item, sort_keys=True, default=str), created,
                        ),
                    )
                self.store.conn.execute(
                    """INSERT OR REPLACE INTO range_compression_population_runs(
                       population_fingerprint,mode,rule_id,state,universe_size,qualified_count,
                       top_1_count,top_5_count,summary_json,created_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (
                        population["population_fingerprint"], str(mode).lower(),
                        str((range_research.get("rule") or {}).get("rule_id") or ""),
                        str(range_research.get("state") or "UNAVAILABLE"),
                        int(range_research.get("universe_size") or 0),
                        int(range_research.get("qualified_count") or 0),
                        int(range_research.get("primary_top_1_count") or 0),
                        int(range_research.get("secondary_top_5_count") or 0),
                        json.dumps(range_research, sort_keys=True, default=str), created,
                    ),
                )
                self.store.conn.commit()
        try:
            quant_paper = QuantPaperActivationService(self.store).process_selection_population(
                mode=mode,
                population_fingerprint=population["population_fingerprint"],
                candidates=stored_rows,
                quant_predictions=result["arms"]["quant"],
                range_predictions=result["arms"]["range_compression"],
            )
            paper_by_candidate = {
                str(item.get("candidate_id")): item
                for item in (quant_paper.get("results") or [])
            }
            for arm in ("quant", "hybrid"):
                for item in result["arms"][arm]:
                    paper = paper_by_candidate.get(str(item.get("candidate_id")))
                    if paper:
                        item["quant_paper"] = paper
                        item["model_paper_rank_score"] = paper.get("paper_rank_score")
                        item["model_decision_weight"] = paper.get(
                            "decision_weight", paper.get("paper_weight", 0.0)
                        )
        except Exception as exc:
            quant_paper = {
                "ok": False,
                "state": "UNAVAILABLE",
                "reason": str(exc)[:240],
                "processed": 0,
                "decision_weight": 0.0,
                "broker_execution_weight": 0.0,
                "broker_order_authority": "NONE",
            }
        return {
            **result,
            "selection_platform_version": SELECTION_PLATFORM_VERSION,
            "candidate_count": len(stored_rows),
            "prediction_count": len(predictions),
            "recorded_population": population,
            "quant_paper": quant_paper,
            "prediction_state": quant_paper.get("prediction_state", "MODEL_UNAVAILABLE"),
            "decision_weight": quant_paper.get("decision_weight", 0.0),
            "broker_execution_weight": 0.0,
            "production_authority": "UNCHANGED_HEURISTIC_BASELINE",
        }

    def latest_range_compression(self, mode: str = "delivery") -> Dict[str, Any]:
        if self.production_governance_required:
            return {
                "ok": True, "state": "NON_AUTHORITATIVE_LENS_NOT_PERSISTED",
                "rule": RangeCompressionRuleService.rule_card(), "universe_size": 0,
                "qualified_count": 0, "top_1_percent": [], "top_5_percent": [],
                "prediction_state": "MODEL_UNAVAILABLE", "authority": "GOVERNANCE_POSTGRESQL",
                "decision_weight": 0.0,
            }
        row = self.store.conn.execute(
            """SELECT * FROM range_compression_population_runs
               WHERE mode=? ORDER BY created_at DESC,population_fingerprint DESC LIMIT 1""",
            (str(mode).lower(),),
        ).fetchone()
        if not row:
            return {
                "ok": True, "state": "NO_POPULATION_RECORDED",
                "rule": RangeCompressionRuleService.rule_card(),
                "universe_size": 0, "qualified_count": 0,
                "top_1_percent": [], "top_5_percent": [],
                "prediction_state": "MODEL_UNAVAILABLE",
                "authority": "MODEL_UNAVAILABLE",
                "decision_weight": 0.0,
            }
        record = dict(row)
        try:
            summary = json.loads(record.get("summary_json") or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            summary = {}
        result = dict(summary if isinstance(summary, dict) else {})
        result.update({
            "population_fingerprint": record.get("population_fingerprint"),
            "created_at": record.get("created_at"),
            "state": record.get("state"),
            "universe_size": int(record.get("universe_size") or 0),
            "qualified_count": int(record.get("qualified_count") or 0),
            "primary_top_1_count": int(record.get("top_1_count") or 0),
            "secondary_top_5_count": int(record.get("top_5_count") or 0),
            "prediction_state": result.get("prediction_state", "QUANT_EVALUATION_PAPER"),
            "authority": result.get("authority", "QUANT_EVALUATION_PAPER"),
            "decision_weight": result.get("decision_weight", 0.0),
        })
        return result

    def predictions(self, population_fingerprint: str) -> List[Dict[str, Any]]:
        if self.production_governance_required:
            return self.governance_repository.selector_predictions(str(population_fingerprint))
        rows = self.store.conn.execute(
            """SELECT * FROM shadow_selector_predictions
               WHERE population_fingerprint=? ORDER BY arm,rank,symbol""",
            (str(population_fingerprint),),
        ).fetchall()
        result = []
        for raw in rows:
            row = dict(raw)
            try:
                prediction = json.loads(row.get("prediction_json") or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                prediction = {}
            item = dict(prediction if isinstance(prediction, dict) else {})
            item.update({
                "population_fingerprint": row.get("population_fingerprint"),
                "candidate_id": row.get("candidate_id"),
                "arm": row.get("arm"),
                "symbol": row.get("symbol"),
                "mode": row.get("mode"),
                "score": row.get("score"),
                "rank": row.get("rank"),
                "percentile": row.get("percentile"),
                "created_at": row.get("created_at"),
            })
            result.append(item)
        return result

    def latest_summary(self, mode: Optional[str] = None) -> Dict[str, Any]:
        if self.production_governance_required:
            latest = self.governance_repository.latest_selector_population(mode)
            if not latest:
                return {
                    "ok": True, "version": SELECTION_PLATFORM_VERSION,
                    "state": "NO_POPULATION_RECORDED", "arms": {},
                    "production_authority": "UNCHANGED_HEURISTIC_BASELINE",
                    "prediction_state": "MODEL_UNAVAILABLE", "decision_weight": 0.0,
                }
            population = str(latest["population_fingerprint"])
            predictions = self.predictions(population)
            arms: Dict[str, List[Dict[str, Any]]] = {}
            for item in predictions:
                arms.setdefault(str(item.get("arm")), []).append(item)
            return {
                "ok": True, "version": SELECTION_PLATFORM_VERSION,
                "state": "SHADOW_ACTIVE", "population_fingerprint": population,
                "created_at": str(latest.get("observed_at")), "arms": arms,
                "calibrated_models": {}, "prediction_state": "MODEL_UNAVAILABLE",
                "decision_weight": 0.0, "broker_execution_weight": 0.0,
                "production_authority": "UNCHANGED_HEURISTIC_BASELINE",
                "selection_edge": "NOT_YET_STATISTICALLY_VALIDATED",
                "authority": "GOVERNANCE_POSTGRESQL",
            }
        params: List[Any] = []
        where = ""
        if mode:
            where = "WHERE mode=?"
            params.append(str(mode))
        latest = self.store.conn.execute(
            f"""SELECT population_fingerprint,created_at
                FROM shadow_selector_predictions {where}
                ORDER BY created_at DESC,population_fingerprint DESC LIMIT 1""",
            tuple(params),
        ).fetchone()
        if not latest or not latest[0]:
            return {
                "ok": True, "version": SELECTION_PLATFORM_VERSION,
                "state": "NO_POPULATION_RECORDED", "arms": {},
                "production_authority": "UNCHANGED_HEURISTIC_BASELINE",
                "prediction_state": "MODEL_UNAVAILABLE",
                "decision_weight": 0.0,
            }
        population = str(latest[0])
        predictions = self.predictions(population)
        arms: Dict[str, List[Dict[str, Any]]] = {}
        for item in predictions:
            arms.setdefault(str(item.get("arm")), []).append(item)
        calibrated = {}
        for desk in ({str(item.get("mode")) for item in predictions} or ({str(mode)} if mode else set())):
            if desk in {"intraday", "delivery"}:
                calibrated[desk] = {}
                for horizon in FORECAST_HORIZONS[desk]:
                    model = self._latest_model(mode=desk, horizon=horizon)
                    calibrated[desk][horizon] = {
                        "state": "MODEL_UNAVAILABLE",
                        "model_id": model.get("model_id") if model else None,
                        "horizon": horizon,
                        "prediction_state": "MODEL_UNAVAILABLE",
                        "authority": "MODEL_UNAVAILABLE",
                        "decision_weight": 0.0,
                    }
        return {
            "ok": True, "version": SELECTION_PLATFORM_VERSION,
            "state": "SHADOW_ACTIVE", "population_fingerprint": population,
            "created_at": latest[1], "arms": arms, "calibrated_models": calibrated,
            "prediction_state": "MODEL_UNAVAILABLE",
            "decision_weight": 0.0,
            "broker_execution_weight": 0.0,
            "production_authority": "UNCHANGED_HEURISTIC_BASELINE",
            "selection_edge": "NOT_YET_STATISTICALLY_VALIDATED",
        }
