"""academic_illiq — Amihud (2002) illiquidity.

Formula: 21-day mean of |daily return| / dollar volume, cross-sectionally
z-scored. Higher score = less liquid (larger price impact per rupee
traded) -- illiquidity has historically earned a return premium.

Source: Amihud, Y. (2002), "Illiquidity and Stock Returns: Cross-Section
and Time-Series Effects", Journal of Financial Markets. Public academic
literature — formula reimplemented from the published definition.
"""
from __future__ import annotations

import pandas as pd

from core.factors import factor_ops as ops

__alpha_meta__ = {
    "id": "academic_illiq",
    "family": "academic",
    "theme": "liquidity",
    "formula_latex": r"z\left(ts\_mean\left(\frac{|r_t|}{close_t \cdot volume_t}, 21\right)\right)",
    "columns_required": ["close", "volume"],
    "universe": "equity_in",
    "frequency": "daily",
    "decay_horizon_days": 21,
    "min_warmup_bars": 22,
    "notes": "Amihud illiquidity ratio, cross-sectional z-score. Higher score = less liquid.",
}


def compute(panel: pd.DataFrame) -> pd.DataFrame:
    close = panel["close"]
    volume = panel["volume"]
    daily_return = ops.returns(close, 1)
    dollar_volume = close * volume
    illiq_daily = ops.safe_div(daily_return.abs(), dollar_volume)
    illiq_21d = ops.ts_mean(illiq_daily, 21)
    return ops.zscore(illiq_21d)
