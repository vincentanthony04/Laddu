"""Deterministic source-level proof for the v131 intelligence/evaluation guard.

No provider, database or broker access is used.  The proof deliberately tests
bad evidence paths as well as positive synthetic evidence so a packaging pass
cannot be achieved by a no-op implementation.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from core.complexity_contribution_authority import DEFAULT_COMPLEXITY_CONTRIBUTION_AUTHORITY
from core.performance_drift_guard_service import PerformanceDriftGuardService
from core.quant_v68.evaluation import permutation_null_alpha_test
from core.quant_v68.promotion_gate import PromotionGate
from core.temporal_leakage_authority import DEFAULT_TEMPORAL_LEAKAGE_AUTHORITY
from core.improvement_review_service import ImprovementReviewService
from core.improvement_proposal_service import ImprovementProposalService
from core.level5_learning_loop_service import Level5LearningLoopService


def _ranking_rows(populations: int = 36) -> list[dict]:
    rows: list[dict] = []
    regimes = ("BULL", "BEAR", "RANGE")
    for p in range(populations):
        regime = regimes[p % len(regimes)]
        observed = f"2026-01-{(p % 28) + 1:02d}T09:{15 + (p % 40):02d}:00Z"
        pop = f"pop-{p:03d}"
        for i in range(10):
            candidate = f"c-{p:03d}-{i:02d}"
            # Two genuinely better candidates; remaining candidates are costly/weak.
            realised = 42.0 - (p % 3) if i < 2 else -8.0 - (i % 3)
            # Mathematical baseline deliberately does not isolate the strongest two.
            heuristic_rank = i + 1 if i >= 2 else 9 + i
            # Hybrid ranks the strongest two first.
            hybrid_rank = i + 1
            quant_rank = i + 1 if i < 4 else 10 - i
            for arm, rank in (("heuristic", heuristic_rank), ("quant", quant_rank), ("hybrid", hybrid_rank)):
                rows.append({
                    "arm": arm,
                    "candidate_id": candidate,
                    "population_fingerprint": pop,
                    "symbol": f"SYM{i}",
                    "score": 100.0 - rank,
                    "rank": rank,
                    "net_return_bps": realised,
                    "market_regime": regime,
                    "observed_at": observed,
                })
    return rows


def _null_rows(populations: int = 20) -> list[dict]:
    rows: list[dict] = []
    for p in range(populations):
        for i in range(8):
            # Scores have a monotonic relation to realised outcomes -> should reject null.
            rows.append({
                "population_id": f"null-pop-{p}",
                "predicted_percentile": (i + 1) / 8.0,
                "realised_return_net": (i - 3.5) / 1000.0,
            })
    return rows


class _Repo:
    def recent_model_efficacy(self, **_kwargs):
        return {
            "state": "MEASURED", "sample_size": 160, "population_count": 20,
            "rank_ic": -0.02, "ndcg": 0.42, "net_expectancy": -0.001,
            "calibration_error": 0.11,
        }




class _RegimeFailureRepo:
    def recent_model_efficacy(self, **_kwargs):
        return {
            "state": "MEASURED", "sample_size": 240, "population_count": 30,
            "rank_ic": 0.025, "ndcg": 0.56, "net_expectancy": 0.0012,
            "calibration_error": 0.07,
            "regime_metrics": [
                {"regime_label": "BULL", "sample_size": 80, "population_count": 10, "rank_ic": 0.04, "ndcg": 0.60},
                {"regime_label": "BEAR", "sample_size": 80, "population_count": 10, "rank_ic": -0.03, "ndcg": 0.42},
                {"regime_label": "RANGE", "sample_size": 80, "population_count": 10, "rank_ic": -0.02, "ndcg": 0.45},
            ],
            "window_drift": {
                "state": "MEASURED",
                "recent": {"rank_ic": 0.025, "ndcg": 0.56, "calibration_error": 0.07},
                "prior": {"rank_ic": 0.03, "ndcg": 0.58, "calibration_error": 0.06},
                "delta_rank_ic": -0.005, "delta_ndcg": -0.02, "delta_calibration_error": 0.01,
            },
        }


class _CalibrationWatchRepo:
    def recent_model_efficacy(self, **_kwargs):
        return {
            "state": "MEASURED", "sample_size": 240, "population_count": 30,
            "rank_ic": 0.025, "ndcg": 0.56, "net_expectancy": 0.0012,
            "calibration_error": 0.14,
            "regime_metrics": [
                {"regime_label": "BULL", "sample_size": 80, "population_count": 10, "rank_ic": 0.03, "ndcg": 0.58},
                {"regime_label": "BEAR", "sample_size": 80, "population_count": 10, "rank_ic": 0.02, "ndcg": 0.55},
                {"regime_label": "RANGE", "sample_size": 80, "population_count": 10, "rank_ic": 0.025, "ndcg": 0.56},
            ],
            "window_drift": {
                "state": "MEASURED",
                "recent": {"rank_ic": 0.025, "ndcg": 0.56, "calibration_error": 0.14},
                "prior": {"rank_ic": 0.055, "ndcg": 0.62, "calibration_error": 0.05},
                "delta_rank_ic": -0.03, "delta_ndcg": -0.06, "delta_calibration_error": 0.09,
            },
        }


class _RegimeFailureStore:
    production_model_governance_repository = _RegimeFailureRepo()

    @staticmethod
    def outcome_learning_rows(**_kwargs):
        return []


class _CalibrationWatchStore:
    production_model_governance_repository = _CalibrationWatchRepo()

    @staticmethod
    def outcome_learning_rows(**_kwargs):
        return []


class _LearningRepository:
    def __init__(self):
        self.findings = []
        self.proposals = []

    @staticmethod
    def settled_learning_rows(**_kwargs):
        return []

    @staticmethod
    def signal_lifecycle_summary(**_kwargs):
        return {"total": 0, "by_type": {}, "latest": []}

    def append_learning_finding(self, record):
        self.findings.append(dict(record))
        return True

    def append_rule_change_proposal(self, record):
        self.proposals.append(dict(record))
        return True


class _LearningGovernanceRepository:
    @staticmethod
    def status(**_kwargs):
        return {
            "ok": True,
            "active_champions": [{
                "assignment_id": "assign-1", "model_id": "model-watch", "desk": "DELIVERY",
                "model_key": "residual-ml", "model_version": "1.0", "production_weight": 0.10,
            }],
        }

    @staticmethod
    def recent_model_efficacy(**_kwargs):
        return _CalibrationWatchRepo().recent_model_efficacy()


class _Store:
    production_model_governance_repository = _Repo()

    @staticmethod
    def outcome_learning_rows(**_kwargs):
        return []


class _LocalWorkflowStore:
    def __init__(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.write_lock = threading.RLock()


def _human_quarantine_roundtrip() -> bool:
    store = _LocalWorkflowStore()
    service = ImprovementProposalService(store)
    now = "2026-08-16T10:00:00Z"
    store.conn.execute(
        """INSERT INTO improvement_proposals(
             proposal_id,mode,horizon,recommendation,status,evidence_hash,model_version,proposal_json,
             created_at,updated_at,production_influence,broker_authority,workflow_version
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        ("p1", "delivery", "5d", "ACCEPT_FOR_CHALLENGER", "CHALLENGER_ACTIVE",
         "evidence", "hybrid-v1", "{}", now, now, 0.0, "NONE", service.__class__.__module__),
    )
    store.conn.execute(
        """INSERT INTO improvement_challenger_activations(
             activation_id,proposal_id,mode,horizon,model_version,state,started_at,activation_json,
             production_influence,broker_authority
           ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
        ("a1", "p1", "delivery", "5d", "hybrid-v1", "ACTIVE_SHADOW", now, "{}", 0.0, "NONE"),
    )
    store.conn.commit()
    result = service.decide(
        proposal_id="p1", action="QUARANTINE", actor="guard-self-test",
        reason="synthetic matured evidence deterioration",
    )
    activation = store.conn.execute(
        "SELECT state,production_influence,broker_authority FROM improvement_challenger_activations WHERE activation_id='a1'"
    ).fetchone()
    return bool(
        result.get("status") == "QUARANTINED"
        and activation is not None
        and activation["state"] == "QUARANTINED"
        and float(activation["production_influence"] or 0.0) == 0.0
        and activation["broker_authority"] == "NONE"
    )


def main() -> int:
    checks: dict[str, object] = {}

    canary = DEFAULT_TEMPORAL_LEAKAGE_AUTHORITY.run_canary_suite()
    checks["leakage_canary"] = canary.get("ok") is True

    null_alpha = permutation_null_alpha_test(
        _null_rows(), permutations=500, seed_material="packaged-intelligence-guard"
    )
    checks["null_alpha_falsification"] = null_alpha.get("passed") is True

    complexity = DEFAULT_COMPLEXITY_CONTRIBUTION_AUTHORITY.evaluate(
        _ranking_rows(), baseline_arm="heuristic", challenger_arm="hybrid",
        seed_material="packaged-complexity-guard",
    )
    checks["paired_complexity_contribution"] = complexity.get("passed") is True
    checks["paired_complexity_lower_bound_positive"] = (
        float(complexity.get("bootstrap_lower_95_incremental_bps") or 0.0) > 0.0
    )

    drift = PerformanceDriftGuardService(_Store()).evaluate({
        "mode": "delivery", "governed_model_id": "synthetic-model"
    })
    checks["matured_model_efficacy_withdrawal"] = (
        drift.get("ml_authority_gate") == "WITHDRAW"
        and drift.get("ml_authority_allowed") is False
    )

    regime_drift = PerformanceDriftGuardService(_RegimeFailureStore()).evaluate({
        "mode": "delivery", "governed_model_id": "regime-failure-model"
    })
    checks["multi_regime_efficacy_can_withdraw_ml"] = (
        regime_drift.get("ml_authority_gate") == "WITHDRAW"
        and set((regime_drift.get("model_efficacy_health") or {}).get("bad_regimes") or []) == {"BEAR", "RANGE"}
    )

    calibration_watch = PerformanceDriftGuardService(_CalibrationWatchStore()).evaluate({
        "mode": "delivery", "governed_model_id": "calibration-watch-model"
    })
    checks["calibration_drift_watches_without_corrupting_math"] = (
        calibration_watch.get("ml_authority_gate") == "WATCH"
        and calibration_watch.get("ml_authority_allowed") is True
        and "CALIBRATION_WINDOW_DETERIORATED" in ((calibration_watch.get("model_efficacy_health") or {}).get("watch_reasons") or [])
    )

    learning_repository = _LearningRepository()
    learning_checkpoint = Level5LearningLoopService(
        learning_repository, _LearningGovernanceRepository()
    ).checkpoint()
    checks["closed_loop_persists_model_drift_human_review"] = (
        learning_checkpoint.get("persisted") is True
        and any(row.get("finding_type") == "MODEL_EFFICACY_DRIFT" for row in learning_repository.findings)
        and any(
            row.get("proposal_type") == "MODEL_AUTHORITY_REVIEW"
            and (row.get("proposal") or {}).get("automatic_production_mutation") is False
            for row in learning_repository.proposals
        )
    )

    report = {
        "same_population_across_arms": True,
        "settled_candidates": 500,
        "readiness": {"diagnostic_ready": True, "shadow_approval_ready": True},
        "arms": {
            "heuristic": {"top_quintile_mean_net_return_bps": 4.0},
            "quant": {"top_quintile_mean_net_return_bps": 7.0},
            "hybrid": {
                "top_quintile_mean_net_return_bps": 12.0,
                "spearman_rank_ic": 0.06,
                "profit_factor": 1.5,
                "cost_sensitivity": {"plus_20bps": {"mean_net_return_bps": 2.0}},
            },
        },
        "complexity_contribution": {"hybrid_vs_mathematics": complexity},
        "model_versions": {"hybrid": ["hybrid-v1"]},
    }
    clock = {"started_at_by_desk": {"delivery": "2026-01-01T00:00:00Z"}}
    walk_forward = {
        "same_candidate_population_across_arms": True,
        "arms": {"hybrid": {"validation": {"approved": True, "status": "APPROVED"}}},
        "model_versions": {"hybrid": ["hybrid-v1"]},
    }
    review = ImprovementReviewService.recommend(clock, report, mode="delivery", walk_forward=walk_forward)
    checks["closed_loop_requires_incremental_complexity"] = review.get("decision") == "ACCEPT_FOR_CHALLENGER"

    report_without_complexity = {**report, "complexity_contribution": {"hybrid_vs_mathematics": {"passed": False, "state": "FAILED"}}}
    blocked_review = ImprovementReviewService.recommend(clock, report_without_complexity, mode="delivery", walk_forward=walk_forward)
    checks["complexity_failure_blocks_challenger_promotion"] = blocked_review.get("decision") != "ACCEPT_FOR_CHALLENGER"

    gate = PromotionGate().evaluate(
        [], validation_method="FORWARD_PAPER", forward_days=0, forward_samples=0,
        lineage_complete=True, leakage_checks_passed=True, point_in_time_universe_passed=True,
        survivorship_control_passed=True, corporate_action_control_passed=True,
        multiple_testing_passed=True, baseline_comparison_passed=True, cost_model_verified=True,
        seed_stability_passed=True, ablation_passed=True, null_alpha_test_passed=False,
    )
    checks["promotion_gate_requires_null_alpha"] = "NULL_ALPHA_FALSIFICATION_FAILED" in gate.get("blockers", [])
    checks["active_shadow_can_be_human_quarantined"] = _human_quarantine_roundtrip()

    ok = all(bool(value) for value in checks.values())
    output = {
        "ok": ok,
        "checks": checks,
        "evidence": {
            "leakage_canary": canary,
            "null_alpha": null_alpha,
            "complexity_contribution": complexity,
            "drift_guard": drift,
            "multi_regime_drift_guard": regime_drift,
            "calibration_watch_guard": calibration_watch,
            "learning_checkpoint": learning_checkpoint,
            "closed_loop_review": review,
            "closed_loop_without_complexity": blocked_review,
        },
        "broker_authority": "NONE",
        "production_mutation": False,
    }
    print(json.dumps(output, indent=2, sort_keys=True, default=str))
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
