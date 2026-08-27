"""Governed adaptive historical-depth policy for Project Laddu ML/WFA.

500 sessions is a reference depth for Delivery, never a hard cap.  Each desk owns
its own configurable safety floor/reference/optional ceiling/recency decay, and
pooled models consume every eligible observation available before the fold cutoff.
Per-symbol eligibility and balancing prevent long-listed symbols from dominating
while still preserving their older regime evidence.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any, Iterable

from config import (
    ML_DELIVERY_TRAIN_MIN_DAYS, ML_DELIVERY_TRAIN_REFERENCE_DAYS,
    ML_DELIVERY_TRAIN_MAX_DAYS, ML_DELIVERY_SYMBOL_MIN_DAYS,
    ML_DELIVERY_RECENCY_HALF_LIFE_DAYS,
    ML_INTRADAY_TRAIN_MIN_DAYS, ML_INTRADAY_TRAIN_REFERENCE_DAYS,
    ML_INTRADAY_TRAIN_MAX_DAYS, ML_INTRADAY_SYMBOL_MIN_DAYS,
    ML_INTRADAY_RECENCY_HALF_LIFE_DAYS,
)

POLICY_VERSION = "adaptive-history-by-symbol-mode-1.0.0-pl42"


@dataclass(frozen=True)
class ModeHistoryPolicy:
    mode: str
    minimum_days: int
    reference_days: int
    maximum_days: int
    per_symbol_minimum_days: int
    recency_half_life_days: int

    @property
    def maximum_policy(self) -> str:
        return "UNBOUNDED" if self.maximum_days <= 0 else "CONFIGURED_RESOURCE_CEILING"

    def as_dict(self) -> dict[str, Any]:
        return {**asdict(self), "maximum_policy": self.maximum_policy, "policy_version": POLICY_VERSION}


def policy_for_mode(mode: str) -> ModeHistoryPolicy:
    key = str(mode or "").strip().lower()
    if key == "delivery":
        return ModeHistoryPolicy(
            mode="delivery",
            minimum_days=int(ML_DELIVERY_TRAIN_MIN_DAYS),
            reference_days=int(ML_DELIVERY_TRAIN_REFERENCE_DAYS),
            maximum_days=int(ML_DELIVERY_TRAIN_MAX_DAYS),
            per_symbol_minimum_days=int(ML_DELIVERY_SYMBOL_MIN_DAYS),
            recency_half_life_days=int(ML_DELIVERY_RECENCY_HALF_LIFE_DAYS),
        )
    if key == "intraday":
        return ModeHistoryPolicy(
            mode="intraday",
            minimum_days=int(ML_INTRADAY_TRAIN_MIN_DAYS),
            reference_days=int(ML_INTRADAY_TRAIN_REFERENCE_DAYS),
            maximum_days=int(ML_INTRADAY_TRAIN_MAX_DAYS),
            per_symbol_minimum_days=int(ML_INTRADAY_SYMBOL_MIN_DAYS),
            recency_half_life_days=int(ML_INTRADAY_RECENCY_HALF_LIFE_DAYS),
        )
    raise ValueError(f"unsupported trade mode for ML history policy: {mode!r}")


def resolve_mode_history_policy(mode: str, available_dates: int, *, horizon_days: int = 1) -> dict[str, Any]:
    policy = policy_for_mode(mode)
    available = max(0, int(available_dates))
    reserve = max(1, int(horizon_days)) + 1
    capacity = max(0, available - reserve)
    if policy.maximum_days > 0:
        capacity = min(capacity, policy.maximum_days)
    ready = capacity >= policy.minimum_days
    reference_satisfied = capacity >= policy.reference_days
    initial_wfa_days = min(capacity, max(policy.minimum_days, policy.reference_days)) if ready else capacity
    return {
        **policy.as_dict(),
        "available_dates": available,
        "horizon_reserve_days": reserve,
        "eligible_capacity_days": capacity,
        "resolved_train_days": capacity,
        "initial_wfa_train_days": int(initial_wfa_days),
        "reference_satisfied": bool(reference_satisfied),
        "ready": bool(ready),
        "state": (
            "ALL_ELIGIBLE_HISTORY_READY" if ready and reference_satisfied
            else "AVAILABLE_HISTORY_ABOVE_SAFETY_FLOOR" if ready
            else "INSUFFICIENT_HISTORY_FOR_SAFETY_FLOOR"
        ),
        "history_policy": "EXPANDING_ALL_ELIGIBLE_HISTORY_WITH_OPTIONAL_RESOURCE_CEILING",
        "reference_semantics": "STABILITY_REFERENCE_NOT_CAP",
    }


def training_frame_and_weights(frame, *, mode: str, eligible_symbols: Iterable[str] | None = None):
    """Return eligible rows, normalized sample weights, and auditable depth summary.

    Weighting uses trading-date age (not guessed calendar sessions), exponential
    recency decay, and bounded inverse-sqrt symbol balancing.  This preserves old
    regimes without allowing a very long-listed stock to dominate the pooled model.
    """
    import numpy as np
    import pandas as pd

    policy = policy_for_mode(mode)
    if frame is None or len(frame) == 0:
        return frame, pd.Series(dtype=float), {
            "policy": policy.as_dict(), "eligible_symbols": 0, "excluded_symbols": 0,
            "rows": 0, "date_count": 0,
        }
    work = frame.copy()
    work["date"] = pd.to_datetime(work["date"], errors="coerce")
    work["symbol"] = work["symbol"].astype(str).str.upper()
    work = work.dropna(subset=["date", "symbol"])
    dates_by_symbol = work.groupby("symbol")["date"].nunique().astype(int)
    if eligible_symbols is None:
        allowed = set(dates_by_symbol[dates_by_symbol >= policy.per_symbol_minimum_days].index)
    else:
        allowed = {str(value).upper() for value in eligible_symbols}
    filtered = work[work["symbol"].isin(allowed)].copy()
    excluded = sorted(set(dates_by_symbol.index) - allowed)
    if filtered.empty:
        return filtered, pd.Series(dtype=float), {
            "policy": policy.as_dict(), "eligible_symbols": 0,
            "excluded_symbols": len(excluded), "excluded_symbol_sample": excluded[:20],
            "rows": 0, "date_count": 0,
        }
    unique_dates = sorted(filtered["date"].dropna().unique())
    rank = {pd.Timestamp(day): i for i, day in enumerate(unique_dates)}
    latest_rank = max(rank.values()) if rank else 0
    age = filtered["date"].map(lambda value: latest_rank - rank.get(pd.Timestamp(value), latest_rank)).astype(float)
    half_life = max(1.0, float(policy.recency_half_life_days))
    recency = np.power(0.5, age.to_numpy(dtype=float) / half_life)

    symbol_days = filtered.groupby("symbol")["date"].nunique().astype(float)
    median_days = max(1.0, float(symbol_days.median()))
    balance_by_symbol = (median_days / symbol_days).pow(0.5).clip(lower=0.5, upper=2.0)
    balance = filtered["symbol"].map(balance_by_symbol).to_numpy(dtype=float)
    weights = recency * balance
    mean_weight = float(np.mean(weights)) if len(weights) else 1.0
    if not np.isfinite(mean_weight) or mean_weight <= 0:
        mean_weight = 1.0
    weights = weights / mean_weight
    weight_series = pd.Series(weights, index=filtered.index, dtype=float)

    counts = sorted(int(value) for value in dates_by_symbol.loc[list(allowed)].tolist()) if allowed else []
    summary = {
        "policy": policy.as_dict(),
        "history_policy": "ALL_ELIGIBLE_PRE_FOLD_HISTORY_RECENCY_WEIGHTED_SYMBOL_BALANCED",
        "eligible_symbols": len(allowed),
        "excluded_symbols": len(excluded),
        "excluded_symbol_sample": excluded[:20],
        "rows": int(len(filtered)),
        "date_count": int(filtered["date"].nunique()),
        "start": filtered["date"].min().strftime("%Y-%m-%d"),
        "end": filtered["date"].max().strftime("%Y-%m-%d"),
        "symbol_history_min": min(counts) if counts else 0,
        "symbol_history_median": int(np.median(counts)) if counts else 0,
        "symbol_history_max": max(counts) if counts else 0,
        "sample_weight_min": float(weight_series.min()),
        "sample_weight_max": float(weight_series.max()),
        "sample_weight_mean": float(weight_series.mean()),
    }
    return filtered, weight_series, summary
