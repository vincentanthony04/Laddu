from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Iterable, Mapping

from core.temporal_leakage_authority import DEFAULT_TEMPORAL_LEAKAGE_AUTHORITY


@dataclass(frozen=True)
class PromotionPolicy:
    required_regimes: tuple[str, ...] = ("BULL", "BEAR", "VOLATILE", "RANGE", "SECTOR_ROTATION")
    minimum_samples_per_regime: int = 100
    minimum_total_samples: int = 600
    minimum_lower_confidence_expectancy: float = 0.0
    minimum_rank_ic: float = 0.01
    minimum_ndcg: float = 0.50
    maximum_calibration_error: float = 0.08
    maximum_drawdown: float = 0.20
    maximum_cvar_95: float = 0.08
    require_forward_paper: bool = True
    minimum_forward_days: int = 63
    minimum_forward_samples: int = 300


class PromotionGate:
    """Regime-stratified, fail-closed champion/challenger gate."""

    def __init__(self, policy: PromotionPolicy | None = None):
        self.policy = policy or PromotionPolicy()

    @staticmethod
    def _rows(metrics: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
        return [dict(row) for row in metrics]

    def evaluate(
        self,
        metrics: Iterable[Mapping[str, Any]],
        *,
        validation_method: str,
        forward_days: int = 0,
        forward_samples: int = 0,
        lineage_complete: bool,
        leakage_checks_passed: bool,
        point_in_time_universe_passed: bool,
        survivorship_control_passed: bool,
        corporate_action_control_passed: bool,
        multiple_testing_passed: bool,
        baseline_comparison_passed: bool,
        cost_model_verified: bool,
        seed_stability_passed: bool,
        ablation_passed: bool,
        null_alpha_test_passed: bool = False,
    ) -> dict[str, Any]:
        rows = self._rows(metrics)
        blockers: list[str] = []
        p = self.policy
        leakage_canary = DEFAULT_TEMPORAL_LEAKAGE_AUTHORITY.run_canary_suite()
        if leakage_canary.get("ok") is not True:
            blockers.append("LEAKAGE_CANARY_SUITE_FAILED")
        if not lineage_complete:
            blockers.append("LINEAGE_INCOMPLETE")
        if not leakage_checks_passed:
            blockers.append("LEAKAGE_CHECK_FAILED")
        if not point_in_time_universe_passed:
            blockers.append("POINT_IN_TIME_UNIVERSE_FAILED")
        if not survivorship_control_passed:
            blockers.append("SURVIVORSHIP_CONTROL_FAILED")
        if not corporate_action_control_passed:
            blockers.append("CORPORATE_ACTION_CONTROL_FAILED")
        if not multiple_testing_passed:
            blockers.append("MULTIPLE_TESTING_CONTROL_FAILED")
        if not baseline_comparison_passed:
            blockers.append("BASELINE_COMPARISON_FAILED")
        if not cost_model_verified:
            blockers.append("COST_MODEL_NOT_VERIFIED")
        if not seed_stability_passed:
            blockers.append("SEED_STABILITY_FAILED")
        if not ablation_passed:
            blockers.append("FEATURE_ABLATION_FAILED")
        if not null_alpha_test_passed:
            blockers.append("NULL_ALPHA_FALSIFICATION_FAILED")
        total_samples = sum(int(row.get("sample_size") or 0) for row in rows if str(row.get("regime_label") or "").upper() != "ALL")
        if total_samples < p.minimum_total_samples:
            blockers.append("TOTAL_SAMPLE_INSUFFICIENT")
        by_regime = {str(row.get("regime_label") or "").upper(): row for row in rows if str(row.get("liquidity_band") or "ALL").upper() == "ALL" and str(row.get("market_cap_band") or "ALL").upper() == "ALL"}
        for regime in p.required_regimes:
            row = by_regime.get(regime)
            if not row:
                blockers.append(f"REGIME_MISSING:{regime}")
                continue
            sample = int(row.get("sample_size") or 0)
            if sample < p.minimum_samples_per_regime:
                blockers.append(f"REGIME_SAMPLE_INSUFFICIENT:{regime}")
            lower = float(row.get("lower_confidence_net_expectancy") or 0.0)
            if lower <= p.minimum_lower_confidence_expectancy:
                blockers.append(f"REGIME_EXPECTANCY_FAILED:{regime}")
            rank_ic = float(row.get("rank_ic") or 0.0)
            if rank_ic < p.minimum_rank_ic:
                blockers.append(f"REGIME_RANK_IC_FAILED:{regime}")
            ndcg = float(row.get("ndcg") or 0.0)
            if ndcg < p.minimum_ndcg:
                blockers.append(f"REGIME_NDCG_FAILED:{regime}")
            cal = float(row.get("calibration_error") or 1.0)
            if cal > p.maximum_calibration_error:
                blockers.append(f"REGIME_CALIBRATION_FAILED:{regime}")
            drawdown = abs(float(row.get("max_drawdown") or 1.0))
            if drawdown > p.maximum_drawdown:
                blockers.append(f"REGIME_DRAWDOWN_FAILED:{regime}")
            cvar = abs(float(row.get("cvar_95") or 1.0))
            if cvar > p.maximum_cvar_95:
                blockers.append(f"REGIME_CVAR_FAILED:{regime}")
        method = validation_method.strip().upper()
        if p.require_forward_paper:
            if method != "FORWARD_PAPER":
                blockers.append("FORWARD_PAPER_REQUIRED")
            if forward_days < p.minimum_forward_days:
                blockers.append("FORWARD_DURATION_INSUFFICIENT")
            if forward_samples < p.minimum_forward_samples:
                blockers.append("FORWARD_SAMPLE_INSUFFICIENT")
        decision = "PROMOTED_CHALLENGER" if not blockers else "REJECTED"
        return {
            "decision": decision,
            "eligible": not blockers,
            "blockers": sorted(set(blockers)),
            "policy": asdict(p),
            "evidence": {
                "validation_method": method,
                "forward_days": int(forward_days),
                "forward_samples": int(forward_samples),
                "total_regime_samples": total_samples,
                "regimes_present": sorted(by_regime),
                "null_alpha_test_passed": bool(null_alpha_test_passed),
                "leakage_canary_suite_passed": leakage_canary.get("ok") is True,
                "leakage_canary_authority_version": leakage_canary.get("authority_version"),
            },
        }
