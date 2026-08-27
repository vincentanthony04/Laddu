"""alpha101_029 — Kakushadze Alpha #29.

Kakushadze Alpha #29.

Formula (paper appendix): min(product(rank(rank(scale(log(sum(ts_min(rank(rank(-1*rank(delta(close-1,5)))),2),1))))),1),5) + ts_rank(delay(-1*returns,6),5)
Source: Kakushadze (2015), "101 Formulaic Alphas", arXiv:1601.00991, eq. 29.

Clean-room reimplementation from the published formula, using our own
core/factors/factor_ops.py operators (not copied source code).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from core.factors import factor_ops as ops

__alpha_meta__ = {
    'id': 'alpha101_029',
    'nickname': 'Kakushadze Alpha #29',
    'theme': ['reversal', 'volume'],
    'formula_latex': 'min(product(rank(rank(scale(log(sum(ts_min(rank(rank(-1*rank(delta(close-1,5)))),2),1))))),1),5) + ts_rank(delay(-1*returns,6),5)',
    'columns_required': ['close', 'volume'],
    'extras_required': [],
    'requires_sector': False,
    'universe': ['equity_us', 'equity_in'],
    'frequency': ['1D'],
    'decay_horizon': 5,
    'min_warmup_bars': 12,
    'notes': '',
}


def _rolling_sum(df: pd.DataFrame, n: int) -> pd.DataFrame:
    """Rolling window sum; warmup -> NaN."""
    return df.rolling(window=n, min_periods=n).sum()


def _rolling_prod(df: pd.DataFrame, n: int) -> pd.DataFrame:
    """Rolling window product; warmup -> NaN."""
    return df.rolling(window=n, min_periods=n).apply(np.prod, raw=True)


def _delay(df: pd.DataFrame, n: int) -> pd.DataFrame:
    """Backward shift by n (lookahead-safe; n>=1 required)."""
    if n < 1:
        raise ValueError("delay requires n >= 1 (lookahead ban)")
    return df.shift(n)


def compute(panel: dict) -> pd.DataFrame:
    """Compute the alpha on the OHLCV+ panel and return a wide DataFrame."""
    close = panel["close"]
    returns = close.pct_change()
    # Helper aliases (local closures keep the file standalone & purity-safe).
    rolling_sum = _rolling_sum
    rolling_prod = _rolling_prod
    delay = _delay
    inner = ops.rank(ops.rank(-1.0 * ops.rank(ops.delta(close - 1.0, 5))))
    inner = ops.ts_min(inner, 2)
    inner = rolling_sum(inner, 1)
    inner = np.log(inner.where(inner > 0))
    inner = ops.scale(inner)
    inner = ops.rank(ops.rank(inner))
    inner = rolling_prod(inner, 1)
    term1 = ops.ts_min(inner, 5)
    term2 = ops.ts_rank(delay(-1.0 * returns, 6), 5)
    out = term1 + term2
    return out
