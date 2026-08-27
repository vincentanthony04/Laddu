"""alpha101_081 — Kakushadze Alpha #81.

Kakushadze Alpha #81.

Formula (paper appendix): (rank(Log(product(rank((rank(correlation(vwap, sum(adv10,50), 8))^4)), 15))) < rank(correlation(rank(vwap), rank(volume), 5))) * -1
Source: Kakushadze (2015), "101 Formulaic Alphas", arXiv:1601.00991, eq. 81.

Clean-room reimplementation from the published formula, using our own
core/factors/factor_ops.py operators (not copied source code).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from core.factors import factor_ops as ops

__alpha_meta__ = {
    'id': 'alpha101_081',
    'nickname': 'Kakushadze Alpha #81',
    'theme': ['volume'],
    'formula_latex': '(rank(Log(product(rank((rank(correlation(vwap, sum(adv10,50), 8))^4)), 15))) < rank(correlation(rank(vwap), rank(volume), 5))) * -1',
    'columns_required': ['volume', 'vwap', 'close'],
    'extras_required': [],
    'requires_sector': False,
    'universe': ['equity_us', 'equity_in'],
    'frequency': ['1D'],
    'decay_horizon': 5,
    'min_warmup_bars': 70,
    'notes': '',
}


def _rolling_sum(df: pd.DataFrame, n: int) -> pd.DataFrame:
    """Rolling window sum; warmup -> NaN."""
    return df.rolling(window=n, min_periods=n).sum()


def _rolling_prod(df: pd.DataFrame, n: int) -> pd.DataFrame:
    """Rolling window product; warmup -> NaN."""
    return df.rolling(window=n, min_periods=n).apply(np.prod, raw=True)


def compute(panel: dict) -> pd.DataFrame:
    """Compute the alpha on the OHLCV+ panel and return a wide DataFrame."""
    volume = panel["volume"]
    vwap = panel["vwap"]
    adv10 = ops.ts_mean(volume, 10)

    # Helper aliases (local closures keep the file standalone & purity-safe).
    rolling_sum = _rolling_sum
    rolling_prod = _rolling_prod
    inner = ops.rank(ops.ts_corr(vwap, rolling_sum(adv10, 50), 8))
    inner = ops.signed_power(inner, 4.0)
    inner = ops.rank(inner)
    prod = rolling_prod(inner, 15)
    lhs = ops.rank(np.log(prod.where(prod > 0)))
    rhs = ops.rank(ops.ts_corr(ops.rank(vwap), ops.rank(volume), 5))
    out = (lhs < rhs).astype(float) * -1.0
    return out
