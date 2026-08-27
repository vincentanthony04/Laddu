"""alpha101_015 — Kakushadze Alpha #15.

Kakushadze Alpha #15.

Formula (paper appendix): -1 * sum(rank(correlation(rank(high), rank(volume), 3)), 3)
Source: Kakushadze (2015), "101 Formulaic Alphas", arXiv:1601.00991, eq. 15.

Clean-room reimplementation from the published formula, using our own
core/factors/factor_ops.py operators (not copied source code).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from core.factors import factor_ops as ops

__alpha_meta__ = {
    'id': 'alpha101_015',
    'nickname': 'Kakushadze Alpha #15',
    'theme': ['volume'],
    'formula_latex': '-1 * sum(rank(correlation(rank(high), rank(volume), 3)), 3)',
    'columns_required': ['high', 'volume', 'close'],
    'extras_required': [],
    'requires_sector': False,
    'universe': ['equity_us', 'equity_in'],
    'frequency': ['1D'],
    'decay_horizon': 5,
    'min_warmup_bars': 6,
    'notes': '',
}


def _rolling_sum(df: pd.DataFrame, n: int) -> pd.DataFrame:
    """Rolling window sum; warmup -> NaN."""
    return df.rolling(window=n, min_periods=n).sum()


def compute(panel: dict) -> pd.DataFrame:
    """Compute the alpha on the OHLCV+ panel and return a wide DataFrame."""
    high = panel["high"]
    volume = panel["volume"]


    # Helper aliases (local closures keep the file standalone & purity-safe).
    rolling_sum = _rolling_sum
    out = -1.0 * rolling_sum(ops.rank(ops.ts_corr(ops.rank(high), ops.rank(volume), 3)), 3)
    return out
