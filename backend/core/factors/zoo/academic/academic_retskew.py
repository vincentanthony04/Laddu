"""academic_retskew — Harvey & Siddique (2000) skewness premium.

Formula: inverse of trailing 60-day return skewness, cross-sectionally
z-scored. Stocks with more negatively-skewed recent returns (occasional
sharp drops) have historically demanded a higher expected return as
compensation for that tail risk — so this factor scores negative-skew
names highest.

Source: Harvey, C.R. & Siddique, A. (2000), "Conditional Skewness in
Asset Pricing Tests", Journal of Finance. Public academic literature —
formula reimplemented from the published definition.
"""
from __future__ import annotations

import pandas as pd

from core.factors import factor_ops as ops

__alpha_meta__ = {
    "id": "academic_retskew",
    "family": "academic",
    "theme": "volatility",
    "formula_latex": r"z\left(-ts\_skew(r_t, 60)\right)",
    "columns_required": ["close"],
    "universe": "equity_in",
    "frequency": "daily",
    "decay_horizon_days": 60,
    "min_warmup_bars": 61,
    "notes": "Inverse 60-day return skewness, cross-sectional z-score. Higher score = more negatively skewed (riskier) recent returns.",
}


def compute(panel: pd.DataFrame) -> pd.DataFrame:
    close = panel["close"]
    daily_return = ops.returns(close, 1)
    skew_60d = ops.ts_skew(daily_return, 60)
    return ops.zscore(-skew_60d)
