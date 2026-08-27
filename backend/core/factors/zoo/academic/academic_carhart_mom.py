"""academic_carhart_mom — Carhart (1997) UMD momentum.

Formula: 252-day return minus 21-day return, cross-sectionally z-scored.
Captures intermediate-term momentum (12m) while netting out the most
recent month, which is dominated by short-term reversal, not momentum.

Source: Carhart, M. M. (1997), "On Persistence in Mutual Fund
Performance", Journal of Finance. Public academic literature — formula
reimplemented from the published definition, not copied from any
third-party codebase.

Note: canonical window is 252 trading days; this needs ~1 year of NSE
history per symbol before it produces a non-NaN value. Do not silently
shrink the window on short panels — an all-NaN result here is a signal
of insufficient history, not a bug to be "fixed" by shrinking.
"""
from __future__ import annotations

import pandas as pd

from core.factors import factor_ops as ops

__alpha_meta__ = {
    "id": "academic_carhart_mom",
    "family": "academic",
    "theme": "momentum",
    "formula_latex": r"z\left(r_{252} - r_{21}\right)",
    "columns_required": ["close"],
    "universe": "equity_in",
    "frequency": "daily",
    "decay_horizon_days": 60,
    "min_warmup_bars": 252,
    "notes": "12-month minus 1-month return, cross-sectional z-score. Top = winners.",
}


def compute(panel: pd.DataFrame) -> pd.DataFrame:
    close = panel["close"]
    ret_long = ops.returns(close, 252)
    ret_short = ops.returns(close, 21)
    return ops.zscore(ret_long - ret_short)
