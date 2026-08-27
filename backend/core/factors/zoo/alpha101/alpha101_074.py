"""alpha101_074 — Kakushadze Alpha #74.

Kakushadze Alpha #74.

Formula (paper appendix): (rank(correlation(close, sum(adv30,37), 15)) < rank(correlation(rank(0.026*high+0.974*vwap), rank(volume), 11))) * -1
Source: Kakushadze (2015), "101 Formulaic Alphas", arXiv:1601.00991, eq. 74.

Clean-room reimplementation from the published formula, using our own
core/factors/factor_ops.py operators (not copied source code).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from core.factors import factor_ops as ops

__alpha_meta__ = {
    'id': 'alpha101_074',
    'nickname': 'Kakushadze Alpha #74',
    'theme': ['volume'],
    'formula_latex': '(rank(correlation(close, sum(adv30,37), 15)) < rank(correlation(rank(0.026*high+0.974*vwap), rank(volume), 11))) * -1',
    'columns_required': ['high', 'close', 'volume', 'vwap'],
    'extras_required': [],
    'requires_sector': False,
    'universe': ['equity_us', 'equity_in'],
    'frequency': ['1D'],
    'decay_horizon': 5,
    'min_warmup_bars': 60,
    'notes': '',
}


def _rolling_sum(df: pd.DataFrame, n: int) -> pd.DataFrame:
    """Rolling window sum; warmup -> NaN."""
    return df.rolling(window=n, min_periods=n).sum()


def compute(panel: dict) -> pd.DataFrame:
    """Compute the alpha on the OHLCV+ panel and return a wide DataFrame."""
    close = panel["close"]
    high = panel["high"]
    volume = panel["volume"]
    vwap = panel["vwap"]
    adv30 = ops.ts_mean(volume, 30)

    # Helper aliases (local closures keep the file standalone & purity-safe).
    rolling_sum = _rolling_sum
    lhs = ops.rank(ops.ts_corr(close, rolling_sum(adv30, 37), 15))
    mix = high * 0.0261661 + vwap * (1.0 - 0.0261661)
    rhs = ops.rank(ops.ts_corr(ops.rank(mix), ops.rank(volume), 11))
    out = (lhs < rhs).astype(float) * -1.0
    return out
