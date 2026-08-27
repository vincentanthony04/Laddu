"""academic_rmw — Fama-French (2015) RMW, price-proxy form.

The canonical RMW (Robust Minus Weak) sorts on operating profitability
from income statements. This is a documented PROXY: negative trailing
60-day realized return volatility — historically, higher-quality/more
profitable firms tend to exhibit lower idiosyncratic volatility (the
"low-vol anomaly" overlaps with the quality/profitability effect).
Higher score = lower volatility (quality proxy).

Source: Fama, E. F., & French, K. R. (2015), "A Five-Factor Asset
Pricing Model", Journal of Financial Economics. Formula reimplemented
as a price-only proxy from the published definition.

Caveat: this is explicitly a proxy, not true operating profitability.
"""
from __future__ import annotations

import pandas as pd

from core.factors import factor_ops as ops

__alpha_meta__ = {
    "id": "academic_rmw",
    "family": "academic",
    "theme": "quality",
    "formula_latex": r"z\left(-ts\_std(r_1, 60)\right)",
    "columns_required": ["close"],
    "universe": "equity_in",
    "frequency": "daily",
    "decay_horizon_days": 60,
    "min_warmup_bars": 60,
    "notes": "[PRICE PROXY, not true RMW] inverse 60d return-volatility z-score.",
}


def compute(panel: pd.DataFrame) -> pd.DataFrame:
    close = panel["close"]
    ret_1d = ops.returns(close, 1)
    vol_60 = ops.ts_std(ret_1d, 60)
    return ops.zscore(-vol_60)
