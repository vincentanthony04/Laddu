"""Layer 2 — IC/IR benchmark runner (the third and final validation gate).

Computes the Information Coefficient (IC) of a factor's cross-sectional
score against forward returns, on OUR OWN NSE OHLCV history. This is
what decides alive/reversed/dead for our universe — never trust an
upstream repo's categorization (a factor "alive" on CSI300/US universes
may be dead or reversed on NIFTY).

Method (Spearman IC via rank-then-Pearson, vectorized):
    1. Rank factor values and forward returns cross-sectionally, per date.
    2. Pearson-correlate the two rank panels per date -> daily IC series.
    3. IR = mean(IC) / std(IC) -- risk-adjusted consistency of the signal.
    4. Classify status from mean IC magnitude and sign:
         |mean IC| < alive_threshold           -> "dead"
         mean IC >= alive_threshold             -> "alive"
         mean IC <= -alive_threshold            -> "reversed"
       "reversed" is not an error -- it means the factor is real but
       flipped for our universe (e.g. gtja191 tuned to Chinese
       microstructure/circuit-band rules that don't hold on NSE). The
       registry can choose to negate reversed factors before use, but
       that decision is explicit, not automatic here.

A date is dropped if fewer than min_names_per_date symbols have both a
factor and a forward-return value that day (mirrors the reference
methodology used for this kind of daily cross-sectional IC).
"""
from __future__ import annotations

from dataclasses import dataclass

from core.factors.factor_thresholds import (
    DEFAULT_ALIVE_IC_THRESHOLD,
    DEFAULT_MIN_NAMES_PER_DATE,
)

import numpy as np
import pandas as pd



@dataclass(frozen=True)
class ICResult:
    factor_id: str
    forward_horizon_days: int
    n_dates: int
    mean_ic: float
    std_ic: float
    ir: float
    hit_rate: float  # fraction of dates with IC same sign as mean_ic
    status: str  # "alive" | "reversed" | "dead" | "insufficient_data"


def compute_ic_series(
    factor_panel: pd.DataFrame,
    forward_return_panel: pd.DataFrame,
    min_names_per_date: int = DEFAULT_MIN_NAMES_PER_DATE,
) -> pd.Series:
    """Daily Spearman rank-IC between factor_panel and forward_return_panel.

    Both panels: index=date, columns=symbol. Only cells present (non-NaN)
    on both sides, on a date with at least `min_names_per_date` such
    symbols, contribute to that date's IC.
    """
    common_dates = factor_panel.index.intersection(forward_return_panel.index)
    common_cols = factor_panel.columns.intersection(forward_return_panel.columns)
    if len(common_dates) == 0 or len(common_cols) == 0:
        return pd.Series(dtype=float)

    f = factor_panel.loc[common_dates, common_cols]
    r = forward_return_panel.loc[common_dates, common_cols]

    pair_mask = f.notna() & r.notna()
    n_valid = pair_mask.sum(axis=1)

    f_aligned = f.where(pair_mask)
    r_aligned = r.where(pair_mask)

    # Spearman = Pearson on per-row ranks.
    f_ranks = f_aligned.rank(axis=1, method="average")
    r_ranks = r_aligned.rank(axis=1, method="average")
    ic = f_ranks.corrwith(r_ranks, axis=1, method="pearson")

    ic = ic[n_valid >= min_names_per_date]
    ic = ic.dropna()
    return ic.astype(float)


def forward_returns(close: pd.DataFrame, horizon_days: int) -> pd.DataFrame:
    """Forward `horizon_days` simple return, aligned so row t holds the
    return realized from t to t+horizon_days (NaN for the last
    `horizon_days` rows, which have no future to look at yet -- this is
    the forward-return target, not a factor input, so it is fine for it
    to peek forward; factors themselves must never do this)."""
    if horizon_days < 1:
        raise ValueError(f"horizon_days must be >= 1, got {horizon_days}")
    shifted = close.shift(-horizon_days)
    return (shifted - close) / close.replace(0.0, np.nan)


def evaluate_factor(
    factor_id: str,
    factor_panel: pd.DataFrame,
    close_panel: pd.DataFrame,
    horizon_days: int = 5,
    min_names_per_date: int = DEFAULT_MIN_NAMES_PER_DATE,
    alive_threshold: float = DEFAULT_ALIVE_IC_THRESHOLD,
) -> ICResult:
    """Full IC/IR benchmark for one factor against our own NSE close panel.

    horizon_days=5 (roughly one trading week) is the default forward
    window; callers validating longer-horizon factors should pass
    a longer horizon (e.g. 21) to match the mode the factor targets.
    """
    fwd_ret = forward_returns(close_panel, horizon_days)
    ic_series = compute_ic_series(factor_panel, fwd_ret, min_names_per_date)

    if len(ic_series) < 10:
        return ICResult(
            factor_id=factor_id,
            forward_horizon_days=horizon_days,
            n_dates=len(ic_series),
            mean_ic=float("nan"),
            std_ic=float("nan"),
            ir=float("nan"),
            hit_rate=float("nan"),
            status="insufficient_data",
        )

    mean_ic = float(ic_series.mean())
    std_ic = float(ic_series.std(ddof=1))
    ir = mean_ic / std_ic if std_ic > 0 else float("nan")
    hit_rate = float((np.sign(ic_series) == np.sign(mean_ic)).mean()) if mean_ic != 0 else float("nan")

    if abs(mean_ic) < alive_threshold or not np.isfinite(mean_ic):
        status = "dead"
    elif mean_ic > 0:
        status = "alive"
    else:
        status = "reversed"

    return ICResult(
        factor_id=factor_id,
        forward_horizon_days=horizon_days,
        n_dates=len(ic_series),
        mean_ic=mean_ic,
        std_ic=std_ic,
        ir=ir,
        hit_rate=hit_rate,
        status=status,
    )
