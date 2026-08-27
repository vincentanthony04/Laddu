"""alpha101_099 — Kakushadze Alpha #99.

Kakushadze Alpha #99.

Formula (paper appendix): (rank(correlation(sum((high+low)/2, 20), sum(adv60, 20), 9)) < rank(correlation(low, volume, 6))) * -1
Source: Kakushadze (2015), "101 Formulaic Alphas", arXiv:1601.00991, eq. 99.

Clean-room reimplementation from the published formula, using our own
core/factors/factor_ops.py operators (not copied source code).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from core.factors import factor_ops as ops

__alpha_meta__ = {
    'id': 'alpha101_099',
    'nickname': 'Kakushadze Alpha #99',
    'theme': ['volume'],
    'formula_latex': '(rank(correlation(sum((high+low)/2, 20), sum(adv60, 20), 9)) < rank(correlation(low, volume, 6))) * -1',
    'columns_required': ['high', 'low', 'volume', 'close'],
    'extras_required': [],
    'requires_sector': False,
    'universe': ['equity_us', 'equity_in'],
    'frequency': ['1D'],
    'decay_horizon': 5,
    'min_warmup_bars': 68,
    'notes': '',
}


def _rolling_sum(df: pd.DataFrame, n: int) -> pd.DataFrame:
    """Rolling window sum; warmup -> NaN."""
    return df.rolling(window=n, min_periods=n).sum()


def compute(panel: dict) -> pd.DataFrame:
    """Compute the alpha on the OHLCV+ panel and return a wide DataFrame."""
    high = panel["high"]
    low = panel["low"]
    volume = panel["volume"]
    adv60 = ops.ts_mean(volume, 60)

    # Helper aliases (local closures keep the file standalone & purity-safe).
    rolling_sum = _rolling_sum
    lhs = ops.rank(ops.ts_corr(rolling_sum((high + low) / 2.0, 20), rolling_sum(adv60, 20), 9))
    rhs = ops.rank(ops.ts_corr(low, volume, 6))
    out = (lhs < rhs).astype(float) * -1.0
    return out
