"""alpha101_008 — Kakushadze Alpha #8.

Kakushadze Alpha #8.

Formula (paper appendix): -1 * rank((sum(open,5)*sum(returns,5)) - delay(sum(open,5)*sum(returns,5),10))
Source: Kakushadze (2015), "101 Formulaic Alphas", arXiv:1601.00991, eq. 8.

Clean-room reimplementation from the published formula, using our own
core/factors/factor_ops.py operators (not copied source code).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from core.factors import factor_ops as ops

__alpha_meta__ = {
    'id': 'alpha101_008',
    'nickname': 'Kakushadze Alpha #8',
    'theme': ['reversal'],
    'formula_latex': '-1 * rank((sum(open,5)*sum(returns,5)) - delay(sum(open,5)*sum(returns,5),10))',
    'columns_required': ['open', 'close'],
    'extras_required': [],
    'requires_sector': False,
    'universe': ['equity_us', 'equity_in'],
    'frequency': ['1D'],
    'decay_horizon': 5,
    'min_warmup_bars': 15,
    'notes': '',
}


def _rolling_sum(df: pd.DataFrame, n: int) -> pd.DataFrame:
    """Rolling window sum; warmup -> NaN."""
    return df.rolling(window=n, min_periods=n).sum()


def _delay(df: pd.DataFrame, n: int) -> pd.DataFrame:
    """Backward shift by n (lookahead-safe; n>=1 required)."""
    if n < 1:
        raise ValueError("delay requires n >= 1 (lookahead ban)")
    return df.shift(n)


def compute(panel: dict) -> pd.DataFrame:
    """Compute the alpha on the OHLCV+ panel and return a wide DataFrame."""
    close = panel["close"]
    open_ = panel["open"]

    returns = close.pct_change()
    # Helper aliases (local closures keep the file standalone & purity-safe).
    rolling_sum = _rolling_sum
    delay = _delay
    s = rolling_sum(open_, 5) * rolling_sum(returns, 5)
    out = -1.0 * ops.rank(s - delay(s, 10))
    return out
