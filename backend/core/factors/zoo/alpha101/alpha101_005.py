"""alpha101_005 — Kakushadze Alpha #5.

Kakushadze Alpha #5.

Formula (paper appendix): rank((open - sum(vwap,10)/10)) * (-1 * abs(rank((close - vwap))))
Source: Kakushadze (2015), "101 Formulaic Alphas", arXiv:1601.00991, eq. 5.

Clean-room reimplementation from the published formula, using our own
core/factors/factor_ops.py operators (not copied source code).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from core.factors import factor_ops as ops

__alpha_meta__ = {
    'id': 'alpha101_005',
    'nickname': 'Kakushadze Alpha #5',
    'theme': ['reversal'],
    'formula_latex': 'rank((open - sum(vwap,10)/10)) * (-1 * abs(rank((close - vwap))))',
    'columns_required': ['open', 'close', 'vwap'],
    'extras_required': [],
    'requires_sector': False,
    'universe': ['equity_us', 'equity_in'],
    'frequency': ['1D'],
    'decay_horizon': 5,
    'min_warmup_bars': 10,
    'notes': '',
}


def _rolling_sum(df: pd.DataFrame, n: int) -> pd.DataFrame:
    """Rolling window sum; warmup -> NaN."""
    return df.rolling(window=n, min_periods=n).sum()


def compute(panel: dict) -> pd.DataFrame:
    """Compute the alpha on the OHLCV+ panel and return a wide DataFrame."""
    close = panel["close"]
    open_ = panel["open"]
    vwap = panel["vwap"]


    # Helper aliases (local closures keep the file standalone & purity-safe).
    rolling_sum = _rolling_sum
    out = ops.rank(open_ - rolling_sum(vwap, 10) / 10.0) * (-1.0 * ops.rank(close - vwap).abs())
    return out
