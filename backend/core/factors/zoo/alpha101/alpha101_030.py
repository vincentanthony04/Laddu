"""alpha101_030 — Kakushadze Alpha #30.

Kakushadze Alpha #30.

Formula (paper appendix): ((1-rank(sign(d1)+sign(d2)+sign(d3))) * sum(volume,5)) / sum(volume,20)
Source: Kakushadze (2015), "101 Formulaic Alphas", arXiv:1601.00991, eq. 30.

Clean-room reimplementation from the published formula, using our own
core/factors/factor_ops.py operators (not copied source code).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from core.factors import factor_ops as ops

__alpha_meta__ = {
    'id': 'alpha101_030',
    'nickname': 'Kakushadze Alpha #30',
    'theme': ['momentum', 'volume'],
    'formula_latex': '((1-rank(sign(d1)+sign(d2)+sign(d3))) * sum(volume,5)) / sum(volume,20)',
    'columns_required': ['close', 'volume'],
    'extras_required': [],
    'requires_sector': False,
    'universe': ['equity_us', 'equity_in'],
    'frequency': ['1D'],
    'decay_horizon': 5,
    'min_warmup_bars': 20,
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
    volume = panel["volume"]


    # Helper aliases (local closures keep the file standalone & purity-safe).
    rolling_sum = _rolling_sum
    delay = _delay
    s = np.sign(close - delay(close, 1)) + np.sign(delay(close, 1) - delay(close, 2)) + np.sign(delay(close, 2) - delay(close, 3))
    out = ops.safe_div((1.0 - ops.rank(s)) * rolling_sum(volume, 5), rolling_sum(volume, 20))
    return out
