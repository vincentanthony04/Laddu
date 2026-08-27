"""academic_strev — Jegadeesh (1990) short-term reversal.

Formula: inverse of trailing 21-trading-day return, cross-sectionally
z-scored. Stocks with the worst recent 1-month return score highest
(reversal thesis: recent losers tend to bounce, recent winners tend to
cool off, over a ~1 month horizon).

Source: Jegadeesh, N. (1990), "Evidence of Predictable Behavior of
Security Returns", Journal of Finance. Public academic literature —
formula reimplemented from the published definition, not copied from
any third-party codebase.
"""
from __future__ import annotations

import pandas as pd

from core.factors import factor_ops as ops

__alpha_meta__ = {
    "id": "academic_strev",
    "family": "academic",
    "theme": "reversal",
    "formula_latex": r"z\left(-\frac{close_t - close_{t-21}}{close_{t-21}}\right)",
    "columns_required": ["close"],
    "universe": "equity_in",
    "frequency": "daily",
    "decay_horizon_days": 21,
    "min_warmup_bars": 22,
    "notes": "Inverse 21-day return, cross-sectional z-score. Higher score = more oversold.",
}


def compute(panel: pd.DataFrame) -> pd.DataFrame:
    """panel must be a MultiIndex-free DataFrame with a 'close' sub-panel
    accessible as panel['close'] (columns = symbols)."""
    close = panel["close"]
    r21 = ops.returns(close, 21)
    return ops.zscore(-r21)
