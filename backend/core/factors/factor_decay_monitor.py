"""Out-of-sample predictive decay checks using rolling cross-sectional IC."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from math import isnan
from typing import Any, Dict, Iterable


@dataclass(frozen=True)
class DecayReport:
    factor_id: str
    status: str
    baseline_dates: int
    recent_dates: int
    baseline_ic: float
    recent_ic: float
    ic_change: float
    recent_hit_rate: float
    reason: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def evaluate_decay(factor_id: str, daily_ic: Iterable[float], *, recent_dates: int = 20,
                   degradation_ratio: float = 0.5) -> DecayReport:
    """Compare recent OOS rank-IC with the preceding baseline window.

    A factor is degraded when its recent IC reverses sign or retains less than
    `degradation_ratio` of baseline absolute IC. Insufficient observations are
    explicitly blocked rather than interpreted as healthy.
    """
    series = [float(value) for value in daily_ic if value is not None and not isnan(float(value))]
    if len(series) < recent_dates * 2:
        nan = float("nan")
        return DecayReport(factor_id, "insufficient_data", max(0, len(series) - recent_dates), min(len(series), recent_dates), nan, nan, nan, nan, "need two complete non-overlapping IC windows")
    recent = series[-recent_dates:]
    baseline = series[-recent_dates * 2:-recent_dates]
    baseline_ic, recent_ic = sum(baseline) / len(baseline), sum(recent) / len(recent)
    change = recent_ic - baseline_ic
    hit_rate = sum(1 for value in recent if value * baseline_ic > 0) / len(recent) if baseline_ic else float("nan")
    sign_reversal = baseline_ic * recent_ic < 0
    retained = abs(recent_ic) / abs(baseline_ic) if baseline_ic else 0.0
    if sign_reversal:
        status, reason = "degraded", "recent predictive IC reversed sign"
    elif retained < degradation_ratio:
        status, reason = "degraded", f"recent absolute IC retained only {retained:.1%} of baseline"
    else:
        status, reason = "healthy", f"recent absolute IC retained {retained:.1%} of baseline"
    return DecayReport(factor_id, status, len(baseline), len(recent), baseline_ic, recent_ic, change, hit_rate, reason)
