"""academic_hml — Fama-French (1993) HML, price-proxy form.

The canonical HML (High Minus Low) sorts on book-to-market, which needs
fundamental book equity we don't carry in the OHLCV panel. This is a
documented PROXY: negative trailing 252-day return (long-term reversal
proxy) — deep long-term underperformers approximate "value" names.
Higher score = larger long-term drawdown (deeper value proxy).

Source: Fama, E. F., & French, K. R. (1993), "Common Risk Factors in
the Returns on Stocks and Bonds", Journal of Financial Economics.
Formula reimplemented as a price-only proxy from the published
definition.

Caveat: this is explicitly a proxy, not true book-to-market value. Must
be IC/IR tested on NSE data — long-term price reversal and book-market
value are correlated but distinct effects.
"""
from __future__ import annotations

import pandas as pd

from core.factors import factor_ops as ops

__alpha_meta__ = {
    "id": "academic_hml",
    "family": "academic",
    "theme": "value",
    "formula_latex": r"z\left(-r_{252}\right)",
    "columns_required": ["close"],
    "universe": "equity_in",
    "frequency": "daily",
    "decay_horizon_days": 60,
    "min_warmup_bars": 252,
    "notes": "[PRICE PROXY, not true HML] inverse 252d return z-score. Top = deep value proxy.",
}


def compute(panel: pd.DataFrame) -> pd.DataFrame:
    close = panel["close"]
    ret_252 = ops.returns(close, 252)
    return ops.zscore(-ret_252)
