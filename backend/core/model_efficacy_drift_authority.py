"""Deterministic authority for matured ML ranking/calibration health.

This authority never trains, retunes or promotes a model.  It consumes only a
read-only efficacy report built from VERIFIED settled outcomes and classifies
whether ML ranking authority may remain active.  The deterministic mathematical
engine is outside this authority and therefore remains available when ML is
withdrawn.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Mapping


AUTHORITY_VERSION = "model-efficacy-drift-authority-1.0.0"


def _num(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class ModelEfficacyDriftPolicy:
    minimum_samples: int = 100
    minimum_populations: int = 10
    minimum_rank_ic: float = 0.0
    minimum_ndcg: float = 0.50
    maximum_calibration_error_watch: float = 0.12
    maximum_calibration_deterioration_watch: float = 0.05
    maximum_rank_ic_drop_watch: float = 0.03
    maximum_ndcg_drop_watch: float = 0.08
    minimum_regime_samples: int = 30
    minimum_regime_populations: int = 3
    bad_regimes_to_withdraw: int = 2


class ModelEfficacyDriftAuthority:
    """Fail-closed ML authority classification from matured evidence only."""

    def __init__(self, policy: ModelEfficacyDriftPolicy | None = None):
        self.policy = policy or ModelEfficacyDriftPolicy()

    def evaluate(self, efficacy: Mapping[str, Any] | None) -> dict[str, Any]:
        report = dict(efficacy or {})
        p = self.policy
        state = str(report.get("state") or "UNVERIFIED").upper()
        reasons: list[str] = []
        watch_reasons: list[str] = []

        if state != "MEASURED":
            return {
                "authority_version": AUTHORITY_VERSION,
                "gate": "WITHDRAW",
                "authority_allowed": False,
                "reason": str(report.get("reason") or "recent matured model efficacy is not verifiable"),
                "withdrawal_reasons": ["UNVERIFIED_MATURED_EFFICACY"],
                "watch_reasons": [],
                "policy": asdict(p),
            }

        samples = int(report.get("sample_size") or 0)
        populations = int(report.get("population_count") or 0)
        rank_ic = _num(report.get("rank_ic"))
        ndcg = _num(report.get("ndcg"))
        calibration_error = _num(report.get("calibration_error"))

        if samples < p.minimum_samples or populations < p.minimum_populations:
            reasons.append(
                f"INSUFFICIENT_MATURED_SAMPLE:{samples}/{populations}"
            )

        rank_bad = rank_ic is None or rank_ic <= p.minimum_rank_ic
        ndcg_bad = ndcg is None or ndcg < p.minimum_ndcg
        if rank_bad and ndcg_bad:
            reasons.append("OVERALL_RANK_EFFICACY_FAILED")
        elif rank_bad:
            watch_reasons.append("OVERALL_RANK_IC_DETERIORATING")
        elif ndcg_bad:
            watch_reasons.append("OVERALL_NDCG_DETERIORATING")

        qualified_regimes: list[dict[str, Any]] = []
        bad_regimes: list[str] = []
        for raw in list(report.get("regime_metrics") or []):
            row = dict(raw or {})
            regime_samples = int(row.get("sample_size") or 0)
            regime_populations = int(row.get("population_count") or 0)
            if regime_samples < p.minimum_regime_samples or regime_populations < p.minimum_regime_populations:
                continue
            qualified_regimes.append(row)
            r_ic = _num(row.get("rank_ic"))
            r_ndcg = _num(row.get("ndcg"))
            if (r_ic is None or r_ic <= p.minimum_rank_ic) and (r_ndcg is None or r_ndcg < p.minimum_ndcg):
                bad_regimes.append(str(row.get("regime_label") or "UNKNOWN").upper())

        if len(bad_regimes) >= p.bad_regimes_to_withdraw:
            reasons.append("MULTI_REGIME_RANK_EFFICACY_FAILED:" + ",".join(sorted(bad_regimes)))
        elif bad_regimes:
            watch_reasons.append("REGIME_RANK_EFFICACY_DETERIORATING:" + ",".join(sorted(bad_regimes)))

        if calibration_error is not None and calibration_error > p.maximum_calibration_error_watch:
            watch_reasons.append("CALIBRATION_ERROR_ABOVE_WATCH_FLOOR")

        drift = dict(report.get("window_drift") or {})
        if str(drift.get("state") or "").upper() == "MEASURED":
            rank_drop = _num(drift.get("delta_rank_ic"))
            ndcg_drop = _num(drift.get("delta_ndcg"))
            calibration_delta = _num(drift.get("delta_calibration_error"))
            if rank_drop is not None and rank_drop <= -abs(p.maximum_rank_ic_drop_watch):
                watch_reasons.append("RANK_IC_WINDOW_DROPPED")
            if ndcg_drop is not None and ndcg_drop <= -abs(p.maximum_ndcg_drop_watch):
                watch_reasons.append("NDCG_WINDOW_DROPPED")
            if (
                calibration_delta is not None
                and calibration_delta >= p.maximum_calibration_deterioration_watch
                and calibration_error is not None
                and calibration_error > p.maximum_calibration_error_watch
            ):
                watch_reasons.append("CALIBRATION_WINDOW_DETERIORATED")

            recent = dict(drift.get("recent") or {})
            prior = dict(drift.get("prior") or {})
            recent_rank = _num(recent.get("rank_ic"))
            recent_ndcg = _num(recent.get("ndcg"))
            prior_rank = _num(prior.get("rank_ic"))
            prior_ndcg = _num(prior.get("ndcg"))
            severe_rank_collapse = (
                recent_rank is not None and prior_rank is not None
                and recent_rank <= p.minimum_rank_ic and prior_rank >= 0.02
                and rank_drop is not None and rank_drop <= -0.04
            )
            severe_ndcg_collapse = (
                recent_ndcg is not None and prior_ndcg is not None
                and recent_ndcg < p.minimum_ndcg and prior_ndcg >= 0.55
                and ndcg_drop is not None and ndcg_drop <= -0.10
            )
            if severe_rank_collapse and severe_ndcg_collapse:
                reasons.append("CONFIRMED_RECENT_WINDOW_EFFICACY_COLLAPSE")

        if reasons:
            gate = "WITHDRAW"
            reason = "; ".join(reasons)
        elif watch_reasons:
            gate = "WATCH"
            reason = "; ".join(dict.fromkeys(watch_reasons))
        else:
            gate = "ALLOW"
            reason = "matured aggregate, regime and calibration/drift controls remain within governed bounds"

        return {
            "authority_version": AUTHORITY_VERSION,
            "gate": gate,
            "authority_allowed": gate != "WITHDRAW",
            "reason": reason,
            "withdrawal_reasons": reasons,
            "watch_reasons": list(dict.fromkeys(watch_reasons)),
            "qualified_regime_count": len(qualified_regimes),
            "bad_regimes": sorted(bad_regimes),
            "policy": asdict(p),
            "automatic_tuning_allowed": False,
            "mathematical_engine_authority_affected": False,
        }


DEFAULT_MODEL_EFFICACY_DRIFT_AUTHORITY = ModelEfficacyDriftAuthority()
