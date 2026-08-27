"""academic_high52w — George & Hwang (2004) 52-week-high momentum.

Formula: close / trailing 252-day max(close), cross-sectionally
z-scored. Stocks trading near their own 52-week high score highest
(the "nearness to 52-week high" anomaly is a stronger momentum
predictor than trailing return itself, per the paper).

Source: George, T.J. & Hwang, C.Y. (2004), "The 52-Week High and
Momentum Investing", Journal of Finance. Public academic literature —
formula reimplemented from the published definition.
"""
from __future__ import annotations

import pandas as pd

from core.factors import factor_ops as ops

__alpha_meta__ = {
    "id": "academic_high52w",
    "family": "academic",
    "theme": "momentum",
    "formula_latex": r"z\left(\frac{close_t}{ts\_max(close, 252)}\right)",
    "columns_required": ["close"],
    "universe": "equity_in",
    "frequency": "daily",
    "decay_horizon_days": 252,
    "min_warmup_bars": 253,
    "notes": "Proximity to 52-week high, cross-sectional z-score. Higher score = closer to 52wk high.",
}


def compute(panel: pd.DataFrame) -> pd.DataFrame:
    close = panel["close"]
    high_52w = ops.ts_max(close, 252)
    proximity = ops.safe_div(close, high_52w)
    return ops.zscore(proximity)
