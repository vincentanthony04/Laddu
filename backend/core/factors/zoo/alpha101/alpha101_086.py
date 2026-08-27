"""alpha101_086 — Kakushadze Alpha #86.

Kakushadze Alpha #86.

Formula (paper appendix): (Ts_Rank(correlation(close, sum(adv20,15), 6), 20) < rank((open+close) - (vwap+open))) * -1
Source: Kakushadze (2015), "101 Formulaic Alphas", arXiv:1601.00991, eq. 86.

Clean-room reimplementation from the published formula, using our own
core/factors/factor_ops.py operators (not copied source code).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from core.factors import factor_ops as ops

__alpha_meta__ = {
    'id': 'alpha101_086',
    'nickname': 'Kakushadze Alpha #86',
    'theme': ['volume'],
    'formula_latex': '(Ts_Rank(correlation(close, sum(adv20,15), 6), 20) < rank((open+close) - (vwap+open))) * -1',
    'columns_required': ['open', 'close', 'volume', 'vwap'],
    'extras_required': [],
    'requires_sector': False,
    'universe': ['equity_us', 'equity_in'],
    'frequency': ['1D'],
    'decay_horizon': 5,
    'min_warmup_bars': 44,
    'notes': '',
}


def _rolling_sum(df: pd.DataFrame, n: int) -> pd.DataFrame:
    """Rolling window sum; warmup -> NaN."""
    return df.rolling(window=n, min_periods=n).sum()


def compute(panel: dict) -> pd.DataFrame:
    """Compute the alpha on the OHLCV+ panel and return a wide DataFrame."""
    close = panel["close"]
    open_ = panel["open"]
    volume = panel["volume"]
    vwap = panel["vwap"]
    adv20 = ops.ts_mean(volume, 20)

    # Helper aliases (local closures keep the file standalone & purity-safe).
    rolling_sum = _rolling_sum
    lhs = ops.ts_rank(ops.ts_corr(close, rolling_sum(adv20, 15), 6), 20)
    rhs = ops.rank((open_ + close) - (vwap + open_))
    out = (lhs < rhs).astype(float) * -1.0
    return out
