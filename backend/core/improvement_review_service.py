"""Governed, read-only improvement recommendation for Project Laddu.

The review diagnoses settled successes and failures and returns an explicit
research/challenger recommendation.  It cannot edit mathematics, factor
weights, model artefacts, canonical decisions, or production authority.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, Mapping, Optional

from core.forward_evidence_clock_service import ForwardEvidenceClockService
from core.selection_research_validation_service import SelectionResearchValidationService
from core.selection_walk_forward_replay_service import SelectionWalkForwardReplayService
from core.forward_horizon_policy import canonical_horizon, normalise_desk


SERVICE_VERSION = "improvement-review-1.1.0-complexity-gated"
VALID_DECISIONS = {
    "BLOCKED",
    "RETAIN_CURRENT_VERSION",
    "ACCEPT_FOR_RESEARCH",
    "ACCEPT_FOR_CHALLENGER",
    "REJECT",
    "QUARANTINE",
}


def _number(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _arm(report: Mapping[str, Any], name: str) -> Dict[str, Any]:
    value = (report.get("arms") or {}).get(name) or {}
    return dict(value) if isinstance(value, Mapping) else {}


class ImprovementReviewService:
    def __init__(self, store: Any):
        self.store = store

    @staticmethod
    def recommend(
        clock: Mapping[str, Any], report: Mapping[str, Any], *, mode: Optional[str] = None,
        walk_forward: Optional[Mapping[str, Any]] = None
    ) -> Dict[str, Any]:
        readiness = dict(report.get("readiness") or {})
        same_population = report.get("same_population_across_arms") is True
        diagnostic_ready = readiness.get("diagnostic_ready") is True
        shadow_ready = readiness.get("shadow_approval_ready") is True
        settled = int(report.get("settled_candidates") or 0)
        heuristic = _arm(report, "heuristic")
        quant = _arm(report, "quant")
        hybrid = _arm(report, "hybrid")

        h_mean = _number(heuristic.get("top_quintile_mean_net_return_bps"))
        q_mean = _number(quant.get("top_quintile_mean_net_return_bps"))
        y_mean = _number(hybrid.get("top_quintile_mean_net_return_bps"))
        y_ic = _number(hybrid.get("spearman_rank_ic"))
        y_pf = _number(hybrid.get("profit_factor"))
        y_stress = _number(
            ((hybrid.get("cost_sensitivity") or {}).get("plus_20bps") or {}).get(
                "mean_net_return_bps"
            )
        )
        beats_baseline = (
            y_mean is not None and h_mean is not None and y_mean > h_mean
        )
        beats_challenger = (
            y_mean is not None and q_mean is not None and y_mean >= q_mean
        )
        robust_positive = bool(
            y_mean is not None
            and y_mean > 0
            and y_stress is not None
            and y_stress > 0
            and y_ic is not None
            and y_ic > 0
            and y_pf is not None
            and y_pf > 1.0
        )
        complexity = dict(
            ((report.get("complexity_contribution") or {}).get("hybrid_vs_mathematics") or {})
        )
        complexity_passed = complexity.get("passed") is True

        walk_forward_report = dict(walk_forward or {})
        walk_forward_same_population = (
            walk_forward_report.get("same_candidate_population_across_arms") is True
        )
        walk_forward_hybrid = dict(
            ((walk_forward_report.get("arms") or {}).get("hybrid") or {}).get("validation") or {}
        )
        walk_forward_hybrid_approved = walk_forward_hybrid.get("approved") is True
        walk_forward_status = str(walk_forward_hybrid.get("status") or "NOT_RUN")
        validation_hybrid_versions = {
            str(value).strip() for value in ((report.get("model_versions") or {}).get("hybrid") or [])
            if str(value).strip()
        }
        walk_forward_hybrid_versions = {
            str(value).strip() for value in ((walk_forward_report.get("model_versions") or {}).get("hybrid") or [])
            if str(value).strip()
        }
        governed_hybrid_versions = validation_hybrid_versions & walk_forward_hybrid_versions
        hybrid_model_version_consistent = bool(
            len(validation_hybrid_versions) == 1
            and validation_hybrid_versions == walk_forward_hybrid_versions
        )
        governed_hybrid_model_version = (
            next(iter(governed_hybrid_versions))
            if hybrid_model_version_consistent and len(governed_hybrid_versions) == 1
            else None
        )

        blockers = []
        desk = str(mode or report.get("mode") or "").lower().strip()
        desk_started_at = (clock.get("started_at_by_desk") or {}).get(desk) if desk else None
        clock_started = bool(desk_started_at or (not desk and clock.get("started_at")))
        if not clock_started:
            blockers.append(
                f"{desk} forward evidence clock has not started"
                if desk else "forward evidence clock has not started"
            )
        if not same_population:
            blockers.append("Baseline, ML Challenger and Hybrid do not share one settled population")
        if settled <= 0:
            blockers.append("no settled post-cost candidate outcomes")

        if blockers:
            decision = "BLOCKED"
            reason = "; ".join(blockers)
        elif not diagnostic_ready:
            decision = "RETAIN_CURRENT_VERSION"
            reason = "forward evidence exists but the diagnostic sample gate is incomplete"
        elif not shadow_ready:
            decision = "ACCEPT_FOR_RESEARCH"
            reason = "diagnostic evidence may support a versioned hypothesis; forward challenger gate is incomplete"
        elif (robust_positive and beats_baseline and beats_challenger and complexity_passed
              and walk_forward_hybrid_approved and walk_forward_same_population
              and hybrid_model_version_consistent):
            decision = "ACCEPT_FOR_CHALLENGER"
            reason = "one exact Hybrid model version passed paired incremental-complexity proof, same-population diagnostics, the purged capital-profile walk-forward gate, and the 20 bps stress"
        elif robust_positive and beats_baseline and beats_challenger:
            decision = "ACCEPT_FOR_RESEARCH"
            reason = "Hybrid diagnostics are positive, but paired incremental-complexity proof, purged walk-forward approval, or exact model-version lineage is incomplete"
        elif y_mean is not None and y_mean < 0 and y_stress is not None and y_stress < 0:
            decision = "QUARANTINE"
            reason = "Hybrid evidence is negative before and after additional cost stress"
        else:
            decision = "RETAIN_CURRENT_VERSION"
            reason = "no governed evidence that the proposed Hybrid path improves the current baseline"

        return {
            "decision": decision,
            "reason": reason,
            "blockers": blockers,
            "checks": {
                "forward_clock_started": clock_started,
                "forward_clock_started_at": desk_started_at or clock.get("started_at"),
                "same_population_across_arms": same_population,
                "settled_candidates": settled,
                "diagnostic_ready": diagnostic_ready,
                "shadow_approval_ready": shadow_ready,
                "hybrid_beats_mathematical_baseline": beats_baseline,
                "hybrid_matches_or_beats_ml_challenger": beats_challenger,
                "hybrid_positive_after_20bps_stress": bool(y_stress is not None and y_stress > 0),
                "hybrid_positive_rank_ic": bool(y_ic is not None and y_ic > 0),
                "hybrid_profit_factor_above_one": bool(y_pf is not None and y_pf > 1.0),
                "hybrid_incremental_complexity_gate_passed": complexity_passed,
                "hybrid_incremental_complexity_state": complexity.get("state"),
                "hybrid_incremental_lower95_bps": complexity.get("bootstrap_lower_95_incremental_bps"),
                "hybrid_incremental_mean_bps": complexity.get("mean_incremental_bps"),
                "walk_forward_same_population_across_arms": walk_forward_same_population,
                "walk_forward_hybrid_status": walk_forward_status,
                "walk_forward_hybrid_approved": walk_forward_hybrid_approved,
                "hybrid_model_version_consistent": hybrid_model_version_consistent,
                "governed_hybrid_model_version": governed_hybrid_model_version,
                "validation_hybrid_model_versions": sorted(validation_hybrid_versions),
                "walk_forward_hybrid_model_versions": sorted(walk_forward_hybrid_versions),
            },
            "metrics": {
                "heuristic_top_quintile_mean_net_return_bps": h_mean,
                "quant_top_quintile_mean_net_return_bps": q_mean,
                "hybrid_top_quintile_mean_net_return_bps": y_mean,
                "hybrid_plus_20bps_mean_net_return_bps": y_stress,
                "hybrid_spearman_rank_ic": y_ic,
                "hybrid_profit_factor": y_pf,
                "hybrid_incremental_complexity": complexity,
            },
        }

    def review(self, *, mode: str, horizon: str) -> Dict[str, Any]:
        desk = normalise_desk(mode)
        horizon_key = canonical_horizon(desk, horizon)

        clock = ForwardEvidenceClockService(self.store).status()
        try:
            report = SelectionResearchValidationService(self.store).report(
                mode=desk, horizon=horizon_key
            )
        except Exception as exc:
            report = {
                "ok": False,
                "mode": desk,
                "horizon": horizon_key,
                "same_population_across_arms": False,
                "settled_candidates": 0,
                "readiness": {},
                "arms": {},
                "error": str(exc),
            }
        try:
            walk_forward = SelectionWalkForwardReplayService(self.store).replay(
                mode=desk, horizon=horizon_key, top_fraction=0.20,
                min_train_days=252, test_days=63, max_folds=8,
                embargo_days=1, min_samples=300, profile="capital",
            )
        except Exception as exc:
            walk_forward = {
                "ok": False,
                "mode": desk,
                "horizon": horizon_key,
                "state": "UNAVAILABLE",
                "error": str(exc),
                "same_candidate_population_across_arms": False,
                "arms": {},
            }
        recommendation = self.recommend(
            clock, report, mode=desk, walk_forward=walk_forward
        )
        material = {
            "service_version": SERVICE_VERSION,
            "mode": desk,
            "horizon": horizon_key,
            "clock_version": clock.get("version"),
            "clock_started_at": (clock.get("started_at_by_desk") or {}).get(desk),
            "report_version": report.get("version"),
            "settled_candidates": report.get("settled_candidates"),
            "recommendation": recommendation,
            "walk_forward_version": walk_forward.get("version"),
            "walk_forward_hybrid_status": recommendation.get("checks", {}).get("walk_forward_hybrid_status"),
            "governed_hybrid_model_version": recommendation.get("checks", {}).get("governed_hybrid_model_version"),
            "validation_model_versions": report.get("model_versions"),
            "walk_forward_model_versions": walk_forward.get("model_versions"),
        }
        proposal_hash = hashlib.sha256(
            json.dumps(material, sort_keys=True, separators=(",", ":"), default=str).encode()
        ).hexdigest()
        decision = recommendation["decision"]
        if decision not in VALID_DECISIONS:
            raise RuntimeError("invalid improvement-review decision")
        return {
            "ok": True,
            "version": SERVICE_VERSION,
            "mode": desk,
            "horizon": horizon_key,
            "proposal_id": f"improvement:{proposal_hash[:24]}",
            "recommendation": recommendation,
            "forward_evidence": clock,
            "validation": report,
            "walk_forward": walk_forward,
            "governed_challenger_model_version": recommendation.get("checks", {}).get("governed_hybrid_model_version"),
            "model_versions": {
                "validation": report.get("model_versions") or {},
                "walk_forward": walk_forward.get("model_versions") or {},
            },
            "authority": {
                "read_only_analysis": True,
                "automatic_research_acceptance": False,
                "automatic_challenger_activation": False,
                "automatic_production_mutation": False,
                "human_approval_required": True,
                "production_ml_influence": 0.0,
                "broker_authority": "NONE",
            },
            "next_action": {
                "BLOCKED": "repair the named evidence gap and rerun",
                "RETAIN_CURRENT_VERSION": "keep current mathematics/model version and collect more evidence",
                "ACCEPT_FOR_RESEARCH": "create a versioned research hypothesis; do not alter production",
                "ACCEPT_FOR_CHALLENGER": "human may approve an isolated Model Paper challenger version",
                "REJECT": "discard the proposal",
                "QUARANTINE": "remove the candidate factor/model from governed influence pending diagnosis",
            }[decision],
        }
