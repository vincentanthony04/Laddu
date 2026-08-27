"""Canonical production rank applied before a decision can be persisted."""
from __future__ import annotations
import hashlib
import json
from typing import Any, Dict, Optional
from core.evidence_engine_service import EvidenceEngineService, model_version_for_mode
from core.numeric_semantics import finite_number
from core.ai_governance_service import AIGovernanceService
from core.production_risk_authority_service import ProductionRiskAuthorityService
from core.production_mode_policy import POLICY_VERSION, require_production_mode
from core.calibrated_edge_service import CalibratedEdgeService
from core.execution_quality_service import ExecutionQualityService
from core.event_risk_policy_service import EventRiskPolicyService
from core.performance_drift_guard_service import PerformanceDriftGuardService
from core.evidence_score_validation_service import EvidenceScoreValidationService
from core.quant_paper_activation_service import QuantPaperActivationService
from models import now_iso

RANKING_VERSION = "desk-aware-production-ranker-10.3.0-strict-finite-model-authority"
RANKING_CONTRACT_VERSION = "canonical-ranking-trace-1.1.0"
MODEL_LEARNING_CONTRACT_VERSION = "governed-learning-observation-2.0.0"


class ProductionRankingService:
    def __init__(self, store: Any = None, runtime_status: Optional[Dict[str, Any]] = None, evidence_validation: Any = None):
        self.evidence = EvidenceEngineService(store)
        self.ai = AIGovernanceService(store)
        self.calibrated_edge = CalibratedEdgeService(store)
        self.execution_quality = ExecutionQualityService()
        self.event_risk = EventRiskPolicyService()
        self.drift_guard = PerformanceDriftGuardService(store)
        self.risk = ProductionRiskAuthorityService(store, runtime_status=runtime_status)
        self.evidence_validation = evidence_validation or (EvidenceScoreValidationService(store) if store is not None else None)
        self.quant_paper = QuantPaperActivationService(store) if store is not None else None

    def apply(self, candidate: Dict[str, Any], delivery: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        out = dict(candidate)
        mode = require_production_mode(out.get("mode"))
        out["mode"] = mode
        delivery = delivery or out.get("institutional_signal") or {}
        ranked = self.evidence.score_candidate(out, delivery=delivery, regime={"state": out.get("index_context")}).to_dict()
        out["source_engine_score"] = out.get("score")
        evidence_score = ranked["evidence_score"]
        out["evidence_score"] = evidence_score
        out["evidence_model_id"] = model_version_for_mode(mode)
        # Active prediction is read here as a precomputed, immutable score and
        # materially influences the automatic paper decision.
        # This path never trains, reconciles or places a broker order.
        quant_paper = getattr(self, "quant_paper", None)
        if quant_paper is not None:
            try:
                paper = quant_paper.precomputed_candidate(out)
            except Exception as exc:
                paper = {
                    "ok": False,
                    "state": "UNAVAILABLE",
                    "reason": str(exc)[:200],
                    "paper_rank_score": evidence_score,
                    "prediction_state": "MODEL_UNAVAILABLE",
                    "decision_weight": 0.0,
                    "broker_execution_weight": 0.0,
                }
        else:
            paper = {
                "ok": True,
                "state": "NO_STORE",
                "paper_rank_score": evidence_score,
                "prediction_state": "MODEL_UNAVAILABLE",
                "decision_weight": 0.0,
                "broker_execution_weight": 0.0,
            }
        if not isinstance(paper, dict):
            paper = {
                "ok": False, "state": "INVALID_PREDICTION_CONTRACT",
                "paper_rank_score": evidence_score, "prediction_state": "MODEL_UNAVAILABLE",
                "decision_weight": 0.0, "broker_execution_weight": 0.0,
            }
        out["quant_paper"] = paper
        out["evidence_composite_score"] = evidence_score
        out["model_paper_rank_score"] = paper.get("paper_rank_score", evidence_score)
        out["model_decision_weight"] = paper.get(
            "decision_weight", paper.get("paper_weight", 0.0)
        )
        out["model_paper_model_id"] = paper.get("active_model_id")
        quant_state = str(paper.get("prediction_state") or paper.get("state") or "").upper()
        quant_weight = finite_number(paper.get("decision_weight"))
        quant_model_id = str(paper.get("active_model_id") or "").strip()
        quant_active = (
            quant_state in {"PREDICTION_ACTIVE", "PAPER_ACTIVE", "ACTIVE_PRODUCTION"}
            and quant_weight is not None and 0.0 < quant_weight <= 0.15
            and bool(quant_model_id)
        )
        symbol = str(out.get("symbol") or "")
        production_mode = str(out.get("mode") or "delivery")
        instrument_key = str(out.get("instrument_key") or out.get("instrument_token") or "") or None
        try:
            shadow = self.ai.shadow_signal(
                symbol, production_mode, instrument_key=instrument_key,
            )
        except TypeError:
            shadow = self.ai.shadow_signal(symbol, production_mode)
        except Exception as exc:
            shadow = {
                "available": False, "eligible": False, "state": "SHADOW_UNAVAILABLE",
                "reason": str(exc)[:200], "weight": 0.0,
            }
        try:
            ai = self.ai.production_signal(
                symbol, production_mode, instrument_key=instrument_key,
            )
        except TypeError as exc:
            # Compatibility for injected test/dry-run governance adapters that
            # implement the pre-v68 two-argument interface. Do not mask a
            # TypeError raised inside a current adapter.
            message = str(exc)
            if "instrument_key" not in message or "unexpected keyword" not in message:
                raise
            ai = self.ai.production_signal(symbol, production_mode)

        if not isinstance(shadow, dict):
            shadow = {"available": False, "eligible": False, "state": "SHADOW_INVALID", "weight": 0.0}
        if not isinstance(ai, dict):
            ai = {"eligible": False, "state": "MODEL_CONTRACT_INVALID", "weight": 0.0}

        # Matured efficacy is checked before any model contribution is blended.
        # Failure or unverifiable evidence removes only model authority; the
        # deterministic mathematical score remains available and unchanged.
        ai_eligible = ai.get("eligible") is True
        drift_model_id = (
            ai.get("model_id") if ai_eligible else
            paper.get("active_model_id") if quant_active else None
        )
        drift_candidate = dict(out)
        drift_candidate["governed_model_id"] = drift_model_id
        performance_drift = self.drift_guard.evaluate(drift_candidate)
        if not isinstance(performance_drift, dict):
            performance_drift = {"gate": "BLOCK", "ml_authority_allowed": False, "reason": "invalid drift-guard contract"}
        ml_authority_allowed = performance_drift.get("ml_authority_allowed") is True

        # Exactly one governed model channel may alter ranking. PostgreSQL
        # production assignment is authoritative when present. The older
        # Quant-Paper channel is a compatibility authority only when PostgreSQL
        # has not granted an assignment. This prevents double-counting.
        base_score = float(evidence_score)
        final_score = base_score
        model_score = None
        model_confidence = None
        model_id = None
        model_state = "MODEL_UNAVAILABLE"
        model_channel = "NONE"
        effective_weight = 0.0
        calibrated_model_score = None

        if ai_eligible and ml_authority_allowed:
            ai_score = finite_number(ai.get("rank_score"))
            ai_confidence = finite_number(ai.get("confidence"))
            ai_weight = finite_number(ai.get("weight"))
            if (
                ai_score is not None and 0.0 <= ai_score <= 100.0
                and ai_confidence is not None and 0.0 <= ai_confidence <= 1.0
                and ai_weight is not None and 0.0 < ai_weight <= 0.15
                and str(ai.get("model_key") or ai.get("model_id") or "").strip()
            ):
                model_score = ai_score
                model_confidence = ai_confidence
                model_id = ai.get("model_key") or ai.get("model_id")
                model_state = str(ai.get("state") or "APPROVED_PRODUCTION_INFERENCE")
                model_channel = "GOVERNANCE_POSTGRESQL_ASSIGNMENT"
                effective_weight = ai_weight
                calibrated_model_score = 50.0 + (model_score - 50.0) * model_confidence
                final_score = (1.0 - effective_weight) * base_score + effective_weight * calibrated_model_score
            else:
                model_state = "MODEL_CONTRACT_INVALID_ZERO_INFLUENCE"
                model_channel = "GOVERNANCE_POSTGRESQL_ASSIGNMENT_REJECTED"
        elif quant_active and ml_authority_allowed:
            raw_model_score = (
                paper.get("active_model_score") if paper.get("active_model_score") is not None
                else paper.get("shadow_model_score")
            )
            quant_score = finite_number(raw_model_score)
            quant_rank_score = finite_number(paper.get("paper_rank_score"))
            quant_confidence = finite_number(paper.get("active_model_confidence"))
            if (
                quant_score is not None and 0.0 <= quant_score <= 100.0
                and quant_rank_score is not None and 0.0 <= quant_rank_score <= 100.0
                and (quant_confidence is None or 0.0 <= quant_confidence <= 1.0)
            ):
                model_score = quant_score
                model_confidence = quant_confidence
                model_id = quant_model_id
                model_state = "PREDICTION_ACTIVE"
                model_channel = "QUANT_PAPER_COMPATIBILITY_ASSIGNMENT"
                effective_weight = quant_weight or 0.0
                final_score = quant_rank_score
            else:
                model_state = "QUANT_MODEL_CONTRACT_INVALID_ZERO_INFLUENCE"
                model_channel = "QUANT_PAPER_COMPATIBILITY_REJECTED"
        else:
            if not ml_authority_allowed and (ai_eligible or quant_active):
                source = ai if ai_eligible else paper
                raw_score = source.get("rank_score") if ai_eligible else source.get("active_model_score")
                if raw_score is None and not ai_eligible:
                    raw_score = source.get("shadow_model_score")
                candidate_score = finite_number(raw_score)
                model_score = candidate_score if candidate_score is not None and 0.0 <= candidate_score <= 100.0 else None
                raw_confidence = source.get("confidence") if ai_eligible else source.get("active_model_confidence")
                candidate_confidence = finite_number(raw_confidence)
                model_confidence = candidate_confidence if candidate_confidence is not None and 0.0 <= candidate_confidence <= 1.0 else None
                model_id = drift_model_id
                model_state = "ML_AUTHORITY_WITHDRAWN_DRIFT"
                model_channel = "GOVERNED_MODEL_EFFICACY_WITHDRAWAL"
            else:
                shadow_score = shadow.get("rank_score") if shadow.get("available") is True else paper.get("shadow_model_score")
                candidate_score = finite_number(shadow_score)
                if candidate_score is not None and 0.0 <= candidate_score <= 100.0:
                    model_score = candidate_score
                    raw_confidence = (
                        shadow.get("confidence") if shadow.get("available") is True
                        else paper.get("shadow_model_confidence")
                    )
                    candidate_confidence = finite_number(raw_confidence)
                    model_confidence = candidate_confidence if candidate_confidence is not None and 0.0 <= candidate_confidence <= 1.0 else None
                    model_id = shadow.get("model_id") or paper.get("shadow_model_id")
                    model_state = str(shadow.get("state") or paper.get("shadow_prediction_state") or "SHADOW_CALCULATING")
                    model_channel = str(shadow.get("authority") or "SHADOW_EVIDENCE")

        safe_final = finite_number(final_score)
        if safe_final is None:
            safe_final = base_score
            effective_weight = 0.0
            model_state = "MODEL_NUMERIC_INVALID_ZERO_INFLUENCE"
            model_channel = "NONE"
        final_score = round(max(0.0, min(100.0, safe_final)), 4)
        out["prediction_state"] = model_state
        out["decision_weight"] = effective_weight
        out["quant_production_contribution"] = round(final_score - base_score, 4) if model_channel.startswith("QUANT_") else 0.0
        out["quant_broker_authority"] = "NONE"
        paper["prediction_active"] = bool(quant_active and not ai_eligible and ml_authority_allowed)
        paper["decision_weight"] = effective_weight if model_channel.startswith("QUANT_") else 0.0
        paper["broker_execution_weight"] = 0.0

        out["research_factor_state"] = (
            "APPROVED_PRODUCTION_INFERENCE" if effective_weight > 0.0
            else model_state if model_score is not None
            else "NOT_APPROVED_FOR_PRODUCTION"
        )
        out["research_factor_points"] = round(final_score - base_score, 2)
        out["model_score"] = model_score
        out["model_confidence"] = model_confidence
        out["model_calibrated_score"] = calibrated_model_score
        out["model_state"] = model_state
        out["model_id"] = model_id
        out["model_channel"] = model_channel
        out["model_ranking_weight"] = effective_weight
        out["model_ranking_authority_pct"] = round(effective_weight * 100.0, 2)
        out["model_influence_applied"] = effective_weight > 0.0
        out["model_rank_contribution"] = round(final_score - base_score, 4)
        out["model_ranking_stage"] = (
            "GOVERNED_PRODUCTION" if effective_weight > 0.0
            else "AUTHORITY_WITHDRAWN" if model_channel == "GOVERNED_MODEL_EFFICACY_WITHDRAWAL"
            else "SHADOW_CALCULATING" if model_score is not None
            else "AWAITING_MODEL_PUBLICATION"
        )
        out["model_ranking_policy"] = (
            "CALCULATE_AND_RECORD_ALWAYS; ZERO_INFLUENCE_UNTIL_FORWARD_PAPER_PROMOTION; "
            "THEN_SINGLE_CHANNEL_CAPPED_BLEND_MAX_15_PERCENT; MATURED_AGGREGATE_AND_REGIME_EFFICACY_CAN_WITHDRAW_MODEL_AUTHORITY; CALIBRATION_DRIFT_CAN_TRIGGER_WATCH"
        )
        out["model_ranking_consumers"] = [
            "TODAY_ENTRY_SCANNER", "REASSESSMENT_SCANNER", "MANUAL_ANALYSIS"
        ]
        out["model_shadow_evidence"] = shadow
        out["performance_drift_guard"] = performance_drift
        out["model_authority_withdrawn_by_drift"] = bool(not ml_authority_allowed and drift_model_id)
        out["score"] = out["rank_score"] = final_score
        out["rank_readiness"] = ranked["readiness"]
        out["rank_components"] = ranked["components"]
        out["rank_conflicts"] = ranked["conflicts"]
        out["rank_raw_score"] = ranked.get("raw_score")
        out["rank_effective_max_score"] = ranked.get("effective_max_score")
        out["rank_normalized_score"] = ranked.get("normalized_score")
        out["rank_scoring_state"] = ranked.get("scoring_state")
        out["rank_degraded_components"] = ranked.get("degraded_components") or []
        out["rank_missing_inputs"] = ranked.get("missing_inputs") or []
        out["rank_gate_failures"] = ranked.get("gate_failures") or []
        out["rank_veto_reasons"] = ranked.get("veto_reasons") or []
        out["ranking_version"] = RANKING_VERSION
        out["policy_version"] = POLICY_VERSION
        out["ai_governance"] = ai
        out["model_ranking_contract"] = {
            "calculation_active": model_score is not None,
            "model_score": model_score,
            "ranking_authority_pct": out["model_ranking_authority_pct"],
            "influence_applied": out["model_influence_applied"],
            "stage": out["model_ranking_stage"],
            "policy": out["model_ranking_policy"],
            "consumers": list(out["model_ranking_consumers"]),
        }
        # Level-4 reconciliation: every canonical evaluation carries a stable
        # input and result fingerprint. Re-reading or projecting a DecisionRecord
        # cannot silently change the rank, model authority or consumer contract.
        ranking_input = {
            "symbol": str(out.get("symbol") or "").upper(),
            "instrument_key": str(out.get("instrument_key") or out.get("instrument_token") or ""),
            "mode": mode,
            "evidence_score": round(float(base_score), 6),
            "evidence_model_id": out.get("evidence_model_id"),
            "rank_components": ranked.get("components") or [],
            "rank_conflicts": ranked.get("conflicts") or [],
            "model_id": model_id,
            "model_state": model_state,
            "model_channel": model_channel,
            "model_score": model_score,
            "model_confidence": model_confidence,
            "model_ranking_weight": effective_weight,
            "ml_authority_gate": performance_drift.get("ml_authority_gate"),
            "ml_authority_allowed": ml_authority_allowed,
            "ml_efficacy_health_gate": (performance_drift.get("model_efficacy_health") or {}).get("gate"),
            "ml_efficacy_health_authority": (performance_drift.get("model_efficacy_health") or {}).get("authority_version"),
            "policy_version": POLICY_VERSION,
            "ranking_version": RANKING_VERSION,
        }
        input_json = json.dumps(ranking_input, sort_keys=True, separators=(",", ":"), default=str)
        ranking_input_hash = hashlib.sha256(input_json.encode("utf-8")).hexdigest()
        ranking_result = {
            "ranking_input_hash": ranking_input_hash,
            "rank_score": final_score,
            "readiness": ranked.get("readiness"),
            "scoring_state": ranked.get("scoring_state"),
            "model_rank_contribution": out["model_rank_contribution"],
            "model_ranking_authority_pct": out["model_ranking_authority_pct"],
            "model_influence_applied": out["model_influence_applied"],
        }
        result_json = json.dumps(ranking_result, sort_keys=True, separators=(",", ":"), default=str)
        ranking_result_hash = hashlib.sha256(result_json.encode("utf-8")).hexdigest()
        out["ranking_contract_version"] = RANKING_CONTRACT_VERSION
        out["ranking_input_hash"] = ranking_input_hash
        out["ranking_result_hash"] = ranking_result_hash
        out["ranking_trace_id"] = f"rank:{ranking_input_hash[:16]}:{ranking_result_hash[:16]}"
        out["ranking_reconciliation"] = {
            "contract_version": RANKING_CONTRACT_VERSION,
            "ranking_version": RANKING_VERSION,
            "input_hash": ranking_input_hash,
            "result_hash": ranking_result_hash,
            "trace_id": out["ranking_trace_id"],
            "consumers": list(out["model_ranking_consumers"]),
            "single_canonical_ranker": True,
        }
        # v69.9.12: every calculated model score receives a deterministic
        # learning-observation identity. This links the prediction to a future
        # settled outcome without granting it ranking authority. The identity
        # is derived only from immutable ranking/model inputs, so projections
        # across Today Entries, reassessment and manual analysis reconcile.
        if model_score is not None:
            learning_seed = {
                "ranking_input_hash": ranking_input_hash,
                "symbol": str(out.get("symbol") or "").upper(),
                "mode": mode,
                "model_id": model_id,
                "model_channel": model_channel,
                "model_score": model_score,
                "model_confidence": model_confidence,
                "ranking_version": RANKING_VERSION,
                "contract_version": MODEL_LEARNING_CONTRACT_VERSION,
            }
            learning_json = json.dumps(learning_seed, sort_keys=True, separators=(",", ":"), default=str)
            learning_hash = hashlib.sha256(learning_json.encode("utf-8")).hexdigest()
            observation_id = f"learn:{learning_hash[:24]}"
            out["model_learning_observation_id"] = observation_id
            out["model_learning_contract"] = {
                "contract_version": MODEL_LEARNING_CONTRACT_VERSION,
                "observation_id": observation_id,
                "observed_at": str(out.get("evaluated_at") or out.get("generated_at") or now_iso()),
                "outcome_link_key": str(out.get("decision_id") or out.get("signal_id") or out["ranking_trace_id"]),
                "prediction_target": (
                    "same_session_post_cost_outcome" if mode == "intraday"
                    else "delivery_thesis_post_cost_outcome"
                ),
                "model_score": model_score,
                "model_confidence": model_confidence,
                "ranking_authority_pct": out["model_ranking_authority_pct"],
                "rank_contribution": out["model_rank_contribution"],
                "stage": out["model_ranking_stage"],
                "ranking_input_hash": ranking_input_hash,
                "ranking_result_hash": ranking_result_hash,
                "settlement_state": "PENDING_OUTCOME",
                "production_change_allowed": False,
            }
        else:
            out["model_learning_observation_id"] = None
            out["model_learning_contract"] = {
                "contract_version": MODEL_LEARNING_CONTRACT_VERSION,
                "observation_id": None,
                "settlement_state": "AWAITING_MODEL_SCORE",
                "production_change_allowed": False,
            }
        out["factor_authority"] = ai.get("factor_authority") or {"eligible": False, "state": ai.get("state"), "weight_multiplier": 0.0}
        out["ranking_explanation"] = "; ".join(
            f"{c['name']} {c['points']}/{c['max_points']}: {c['reason']}" for c in ranked["components"]
        )
        if str(out.get("status") or "").upper() == "PROMOTED" and (
            ranked["readiness"] != "READY" or ranked.get("scoring_state") != "NORMAL"
        ):
            out["status"] = "WATCH" if ranked["readiness"] in ("WATCH", "EXTENDED") else "BLOCKED"
            out["decision"] = "WATCH" if out["status"] == "WATCH" else "WAIT"
            out["promotion_blocked_by"] = (
                ranked.get("veto_reasons") or ranked["conflicts"]
                or [f"canonical readiness is {ranked['readiness']}"]
            )
            out["reason"] = (out.get("reason") or "") + "; Production ranker blocked legacy promotion: " + ", ".join(out["promotion_blocked_by"])
        # v65.26.33: calibrated edge and execution/event/drift gates sit after
        # canonical scoring but before portfolio risk. They may veto a proposed
        # promotion; none may add evidence points, create direction or grant
        # capital authority. Insufficient calibration remains shadow-only and
        # preserves the deterministic champion.
        out["calibrated_edge"] = self.calibrated_edge.evaluate(out)
        out["execution_quality"] = self.execution_quality.evaluate(out)
        out["event_risk_policy"] = self.event_risk.evaluate(out)
        out["performance_drift_guard"] = performance_drift
        governed_blocks = []
        for label, report in (
            ("calibrated edge", out["calibrated_edge"]),
            ("execution quality", out["execution_quality"]),
            ("event risk", out["event_risk_policy"]),
            ("performance drift", out["performance_drift_guard"]),
        ):
            if not isinstance(report, dict):
                governed_blocks.append(f"{label}: invalid authority contract")
                continue
            if str(report.get("gate") or "").upper() == "BLOCK":
                blockers = report.get("blockers") if isinstance(report.get("blockers"), list) else []
                governed_blocks.append(f"{label}: {report.get('reason') or ', '.join(str(x) for x in blockers) or report.get('state')}")
        out["governed_edge_gates"] = {
            "passed": not governed_blocks,
            "blocks": governed_blocks,
            "policy": "post-score gates may only veto; score and direction are immutable",
        }
        if str(out.get("status") or "").upper() == "PROMOTED" and governed_blocks:
            out["status"] = "WATCH"
            out["decision"] = "WATCH"
            out["promotion_blocked_by"] = list(dict.fromkeys(list(out.get("promotion_blocked_by") or []) + governed_blocks))
            out["reason"] = (str(out.get("reason") or "") + "; Governed edge admission blocked promotion: " + ", ".join(governed_blocks)).strip("; ")
        # Risk admission is deliberately last: it cannot improve evidence or AI
        # scores, and it sees the final canonical promotion state.
        out = self.risk.apply(out)
        # Record the actual heuristic selector for every finalized candidate,
        # including WATCH/BLOCKED rows. This observation happens after final
        # status is known but preserves the pre-AI evidence score explicitly.
        if self.evidence_validation is not None:
            try:
                out["selection_validation_observation"] = self.evidence_validation.record(out)
            except Exception as exc:
                out["selection_validation_observation"] = {
                    "ok": False, "state": "RECORD_FAILED", "error": str(exc)[:160]
                }
        return out
