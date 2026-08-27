"""alpha101_047 — Kakushadze Alpha #47.

Kakushadze Alpha #47.

Formula (paper appendix): ((rank(1/close)*volume/adv20) * (high*rank(high-close)/(sum(high,5)/5))) - rank(vwap-delay(vwap,5))
Source: Kakushadze (2015), "101 Formulaic Alphas", arXiv:1601.00991, eq. 47.

Clean-room reimplementation from the published formula, using our own
core/factors/factor_ops.py operators (not copied source code).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from core.factors import factor_ops as ops

__alpha_meta__ = {
    'id': 'alpha101_047',
    'nickname': 'Kakushadze Alpha #47',
    'theme': ['volume', 'momentum'],
    'formula_latex': '((rank(1/close)*volume/adv20) * (high*rank(high-close)/(sum(high,5)/5))) - rank(vwap-delay(vwap,5))',
    'columns_required': ['high', 'close', 'volume', 'vwap'],
    'extras_required': [],
    'requires_sector': False,
    'universe': ['equity_us', 'equity_in'],
    'frequency': ['1D'],
    'decay_horizon': 5,
    'min_warmup_bars': 25,
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


def _make_one(ref: pd.DataFrame) -> pd.DataFrame:
    """A DataFrame of 1.0 with the same shape/index/columns as ``ref``."""
    return pd.DataFrame(1.0, index=ref.index, columns=ref.columns)


def compute(panel: dict) -> pd.DataFrame:
    """Compute the alpha on the OHLCV+ panel and return a wide DataFrame."""
    close = panel["close"]
    high = panel["high"]
    volume = panel["volume"]
    vwap = panel["vwap"]
    adv20 = ops.ts_mean(volume, 20)

    # Helper aliases (local closures keep the file standalone & purity-safe).
    rolling_sum = _rolling_sum
    delay = _delay
    make_one = _make_one
    t1 = ops.safe_div(ops.rank(ops.safe_div(make_one(close), close)) * volume, adv20)
    t2 = ops.safe_div(high * ops.rank(high - close), rolling_sum(high, 5) / 5.0)
    out = t1 * t2 - ops.rank(vwap - delay(vwap, 5))
    return out
