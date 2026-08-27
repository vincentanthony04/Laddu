"""Truthful score, data, and model attribution for Stock Intelligence.

This service is diagnostic only.  It never changes a decision.  In particular,
validation and rejected models are reported with zero production contribution.
"""
from __future__ import annotations

from typing import Any, Dict, Mapping, Optional


CONTRACT_VERSION = "intelligence-attribution-1.0.0"


def _number(value: Any) -> Optional[float]:
    try:
        return round(float(value), 4)
    except (TypeError, ValueError):
        return None


def _ratio(value: Any) -> Optional[float]:
    number = _number(value)
    if number is None:
        return None
    return max(0.0, min(1.0, number))


class IntelligenceAttributionService:
    def __init__(self, store: Any):
        self.store = store

    def _latest_model(self, mode: str) -> Dict[str, Any]:
        repository = getattr(self.store, "production_model_governance_repository", None)
        if bool(getattr(self.store, "production_model_governance_required", False)):
            if repository is None:
                return {"state": "MODEL_UNAVAILABLE", "production_influence": False, "production_weight": 0.0, "reason": "Separate PostgreSQL governance authority is unavailable."}
            try:
                report = repository.status(mode)
                champions = list(report.get("active_champions") or [])
                if not champions:
                    return {
                        "state": "NO_ACTIVE_CHAMPION",
                        "production_influence": False,
                        "production_weight": 0.0,
                        "prediction_count": int((report.get("counts") or {}).get("frozen_predictions") or 0),
                        "reason": "No effective PostgreSQL champion assignment exists for this desk.",
                        "authority": "SEPARATE_GOVERNANCE_POSTGRES",
                    }
                champion = dict(champions[0])
                return {
                    **champion,
                    "state": "ACTIVE_CHAMPION",
                    "production_influence": float(champion.get("production_weight") or 0.0) > 0.0,
                    "production_weight": float(champion.get("production_weight") or 0.0),
                    "prediction_count": int((report.get("counts") or {}).get("frozen_predictions") or 0),
                    "reason": "Effective champion assignment from the separate PostgreSQL governance authority.",
                    "authority": "SEPARATE_GOVERNANCE_POSTGRES",
                }
            except Exception as exc:
                return {"state": "MODEL_UNAVAILABLE", "production_influence": False, "production_weight": 0.0, "reason": f"PostgreSQL governance status unavailable: {exc}"}
        try:
            cursor = self.store.conn.execute(
                """SELECT experiment_id,model_key,library_key,horizon,lifecycle_state,
                          production_weight,rejection_reason,required_observations,
                          required_regimes,validation_deadline,updated_at
                   FROM model_experiments
                   WHERE mode=?
                   ORDER BY CASE lifecycle_state
                              WHEN 'ACTIVE_PRODUCTION' THEN 0
                              WHEN 'ACTIVE_VALIDATION' THEN 1
                              WHEN 'EXPERIMENT' THEN 2
                              ELSE 3 END,
                            updated_at DESC
                   LIMIT 1""",
                (str(mode),),
            )
            row = cursor.fetchone()
            if row is None:
                return {
                    "state": "MODEL_UNAVAILABLE",
                    "production_influence": False,
                    "production_weight": 0.0,
                    "reason": "No governed model experiment exists for this desk.",
                }
            columns = [item[0] for item in cursor.description]
            model = dict(zip(columns, row))
            state = str(model.get("lifecycle_state") or "MODEL_UNAVAILABLE")
            weight = _number(model.get("production_weight")) or 0.0
            active = state == "ACTIVE_PRODUCTION" and weight > 0.0
            try:
                prediction_count = int(
                    self.store.conn.execute(
                        "SELECT COUNT(*) FROM model_predictions WHERE experiment_id=?",
                        (model.get("experiment_id"),),
                    ).fetchone()[0]
                )
            except Exception:
                prediction_count = 0
            try:
                evaluation = self.store.conn.execute(
                    """SELECT observation_count,trading_days,regime_count,
                              post_cost_expectancy_bps,lower_confidence_bound_bps,
                              multiple_testing_adjusted_pvalue,evaluation_as_of
                       FROM model_evaluations WHERE experiment_id=?
                       ORDER BY evaluation_as_of DESC,created_at DESC LIMIT 1""",
                    (model.get("experiment_id"),),
                ).fetchone()
                eval_columns = (
                    "observation_count", "trading_days", "regime_count",
                    "post_cost_expectancy_bps", "lower_confidence_bound_bps",
                    "multiple_testing_adjusted_pvalue", "evaluation_as_of",
                )
                latest_evaluation = dict(zip(eval_columns, evaluation)) if evaluation else None
            except Exception:
                latest_evaluation = None
            reason = model.get("rejection_reason")
            if not reason:
                if active:
                    reason = "Governed model passed production promotion gates."
                elif state == "ACTIVE_VALIDATION":
                    reason = "Validation-only model; frozen predictions may be evaluated but cannot affect production scoring."
                elif state == "EXPERIMENT":
                    reason = "Experiment-only model; production scoring is prohibited."
                else:
                    reason = "Model is rejected or unavailable and has zero production influence."
            return {
                **model,
                "state": state,
                "prediction_count": prediction_count,
                "latest_evaluation": latest_evaluation,
                "production_influence": bool(active),
                "production_weight": weight if active else 0.0,
                "reason": str(reason),
            }
        except Exception as exc:
            return {
                "state": "MODEL_UNAVAILABLE",
                "production_influence": False,
                "production_weight": 0.0,
                "reason": f"Governed model status unavailable: {exc}",
            }

    def build(
        self,
        *,
        mode: str,
        composite: Mapping[str, Any],
        actionable_score: Any,
        validation_gates: list[str],
        technical: Mapping[str, Any],
        mtf: Mapping[str, Any],
        fundamentals: Mapping[str, Any],
        quote_fresh: bool,
        quote_source: str,
        quote_age_seconds: Any,
        history_status: Any,
        history_rows: int,
        candle_stale: bool,
    ) -> Dict[str, Any]:
        weights = dict(composite.get("weights") or {})
        inputs = dict(composite.get("inputs") or {})
        score_contributions = []
        for name, weight in weights.items():
            value = _number(inputs.get(name))
            state = "READY" if value is not None else "MISSING"
            score_contributions.append({
                "component": name,
                "state": state,
                "input_score": value,
                "desk_weight": _number(weight),
                "weighted_points": round(value * float(weight), 4) if value is not None else None,
                "neutral_partial_points": round(50.0 * float(weight), 4) if value is None else None,
                "production_influence": value is not None,
            })

        technical_coverage = _ratio(technical.get("coverage"))
        mtf_coverage = _ratio(mtf.get("coverage"))
        fundamental_coverage = fundamentals.get("coverage")
        if isinstance(fundamental_coverage, Mapping):
            fundamental_coverage = fundamental_coverage.get("ratio")
        fundamental_coverage = _ratio(fundamental_coverage)

        data_inputs = {
            "quote": {
                "state": "LIVE" if quote_fresh else "STALE_OR_MISSING",
                "source": quote_source,
                "age_seconds": _number(quote_age_seconds),
                "decision_eligible": bool(quote_fresh),
            },
            "primary_history": {
                "state": "STALE" if candle_stale else "READY" if history_rows else "MISSING",
                "status": history_status,
                "rows": int(history_rows or 0),
                "decision_eligible": bool(history_rows and not candle_stale),
            },
            "technical": {
                "state": "READY" if technical.get("ready") else "PARTIAL",
                "coverage": technical_coverage,
                "decision_eligible": bool(technical.get("ready")),
            },
            "multi_timeframe": {
                "state": "READY" if mtf.get("ready") else "PARTIAL",
                "coverage": mtf_coverage,
                "resolved": list(mtf.get("resolved") or []),
                "missing": list(mtf.get("missing") or []),
                "decision_eligible": bool(mtf.get("ready")),
            },
            "fundamental": {
                "state": str(fundamentals.get("state") or ("READY" if fundamentals.get("ok") else "MISSING")),
                "coverage": fundamental_coverage,
                "decision_eligible": bool(fundamentals.get("ok")) if str(mode) == "delivery" else None,
            },
        }

        model = self._latest_model(str(mode))
        raw_final = _number(composite.get("final_score"))
        partial_model = _number(composite.get("model_score"))
        final_actionable = _number(actionable_score)
        cap_delta = None
        if raw_final is not None and final_actionable is not None:
            cap_delta = round(final_actionable - raw_final, 4)

        blockers = list(dict.fromkeys(str(item) for item in (validation_gates or [])))
        return {
            "ok": True,
            "contract_version": CONTRACT_VERSION,
            "decision_authority": "DETERMINISTIC_EVIDENCE_AND_RISK_GATES",
            "score_contributions": score_contributions,
            "data_inputs": data_inputs,
            "model": model,
            "counterfactual": {
                "raw_complete_score": raw_final,
                "labelled_partial_model_score": partial_model,
                "actionable_score_after_validation": final_actionable,
                "validation_adjustment_points": cap_delta,
                "would_be_trade_without_validation_gates": bool(raw_final is not None and raw_final >= 72.0),
                "production_model_points": 0.0 if not model.get("production_influence") else None,
            },
            "blockers": blockers,
            "ready": bool(composite.get("ready") and not blockers),
            "explanation": (
                "All mandatory evidence and validation gates passed."
                if composite.get("ready") and not blockers
                else "Production action is withheld or capped because mandatory evidence or validation gates are unresolved."
            ),
            "policy": "Rejected, experiment, and validation-only models have zero production influence.",
        }
