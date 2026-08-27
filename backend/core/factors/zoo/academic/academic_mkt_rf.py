"""academic_mkt_rf — market factor (Sharpe 1964 / Fama-French), price-proxy form.

The canonical MKT-RF is a value-weighted market-portfolio excess return
(a single market-wide series, not a per-symbol factor). We approximate
per-symbol market-factor exposure as the 21-day total return,
cross-sectionally z-scored — suitable for long-short ranking, not as a
literal CAPM beta.

Source: Sharpe, W. F. (1964), "Capital Asset Prices: A Theory of Market
Equilibrium under Conditions of Risk", Journal of Finance. Formula
reimplemented as a price-only proxy from the published definition.
"""
from __future__ import annotations

import pandas as pd

from core.factors import factor_ops as ops

__alpha_meta__ = {
    "id": "academic_mkt_rf",
    "family": "academic",
    "theme": "momentum",
    "formula_latex": r"z\left(r_{21}\right)",
    "columns_required": ["close"],
    "universe": "equity_in",
    "frequency": "daily",
    "decay_horizon_days": 21,
    "min_warmup_bars": 21,
    "notes": "[PRICE PROXY, not true Mkt-RF] 21d return z-score. Top = recent winners.",
}


def compute(panel: pd.DataFrame) -> pd.DataFrame:
    close = panel["close"]
    ret_21 = ops.returns(close, 21)
    return ops.zscore(ret_21)
