"""alpha101_083 — Kakushadze Alpha #83.

Kakushadze Alpha #83.

Formula (paper appendix): (rank(delay((high-low)/(sum(close,5)/5), 2)) * rank(rank(volume))) / (((high-low)/(sum(close,5)/5)) / (vwap-close))
Source: Kakushadze (2015), "101 Formulaic Alphas", arXiv:1601.00991, eq. 83.

Clean-room reimplementation from the published formula, using our own
core/factors/factor_ops.py operators (not copied source code).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from core.factors import factor_ops as ops

__alpha_meta__ = {
    'id': 'alpha101_083',
    'nickname': 'Kakushadze Alpha #83',
    'theme': ['volume', 'volatility'],
    'formula_latex': '(rank(delay((high-low)/(sum(close,5)/5), 2)) * rank(rank(volume))) / (((high-low)/(sum(close,5)/5)) / (vwap-close))',
    'columns_required': ['high', 'low', 'close', 'volume', 'vwap'],
    'extras_required': [],
    'requires_sector': False,
    'universe': ['equity_us', 'equity_in'],
    'frequency': ['1D'],
    'decay_horizon': 5,
    'min_warmup_bars': 7,
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
    high = panel["high"]
    low = panel["low"]
    volume = panel["volume"]
    vwap = panel["vwap"]


    # Helper aliases (local closures keep the file standalone & purity-safe).
    rolling_sum = _rolling_sum
    delay = _delay
    rng_avg = ops.safe_div((high - low), rolling_sum(close, 5) / 5.0)
    num = ops.rank(delay(rng_avg, 2)) * ops.rank(ops.rank(volume))
    denom = ops.safe_div(rng_avg, vwap - close)
    out = ops.safe_div(num, denom)
    return out
