"""alpha101_062 — Kakushadze Alpha #62.

Kakushadze Alpha #62.

Formula (paper appendix): (rank(correlation(vwap, sum(adv20,22), 10)) < rank(((rank(open)+rank(open)) < (rank((high+low)/2)+rank(high))))) * -1
Source: Kakushadze (2015), "101 Formulaic Alphas", arXiv:1601.00991, eq. 62.

Clean-room reimplementation from the published formula, using our own
core/factors/factor_ops.py operators (not copied source code).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from core.factors import factor_ops as ops

__alpha_meta__ = {
    'id': 'alpha101_062',
    'nickname': 'Kakushadze Alpha #62',
    'theme': ['volume'],
    'formula_latex': '(rank(correlation(vwap, sum(adv20,22), 10)) < rank(((rank(open)+rank(open)) < (rank((high+low)/2)+rank(high))))) * -1',
    'columns_required': ['open', 'high', 'low', 'volume', 'vwap', 'close'],
    'extras_required': [],
    'requires_sector': False,
    'universe': ['equity_us', 'equity_in'],
    'frequency': ['1D'],
    'decay_horizon': 5,
    'min_warmup_bars': 35,
    'notes': '',
}


def _rolling_sum(df: pd.DataFrame, n: int) -> pd.DataFrame:
    """Rolling window sum; warmup -> NaN."""
    return df.rolling(window=n, min_periods=n).sum()


def compute(panel: dict) -> pd.DataFrame:
    """Compute the alpha on the OHLCV+ panel and return a wide DataFrame."""
    open_ = panel["open"]
    high = panel["high"]
    low = panel["low"]
    volume = panel["volume"]
    vwap = panel["vwap"]
    adv20 = ops.ts_mean(volume, 20)

    # Helper aliases (local closures keep the file standalone & purity-safe).
    rolling_sum = _rolling_sum
    lhs = ops.rank(ops.ts_corr(vwap, rolling_sum(adv20, 22), 10))
    inner = ((ops.rank(open_) + ops.rank(open_)) < (ops.rank((high + low) / 2.0) + ops.rank(high))).astype(float)
    rhs = ops.rank(inner)
    out = (lhs < rhs).astype(float) * -1.0
    return out
