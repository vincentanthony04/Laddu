"""alpha101_019 — Kakushadze Alpha #19.

Kakushadze Alpha #19.

Formula (paper appendix): (-1*sign((close-delay(close,7))+delta(close,7))) * (1+rank(1+sum(returns,250)))
Source: Kakushadze (2015), "101 Formulaic Alphas", arXiv:1601.00991, eq. 19.

Clean-room reimplementation from the published formula, using our own
core/factors/factor_ops.py operators (not copied source code).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from core.factors import factor_ops as ops

__alpha_meta__ = {
    'id': 'alpha101_019',
    'nickname': 'Kakushadze Alpha #19',
    'theme': ['momentum'],
    'formula_latex': '(-1*sign((close-delay(close,7))+delta(close,7))) * (1+rank(1+sum(returns,250)))',
    'columns_required': ['close'],
    'extras_required': [],
    'requires_sector': False,
    'universe': ['equity_us', 'equity_in'],
    'frequency': ['1D'],
    'decay_horizon': 5,
    'min_warmup_bars': 250,
    'notes': 'Very long lookback (>= ~100 bars); produces NaN warmup on short panels which may trigger the >95% NaN registry guard.',
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


    returns = close.pct_change()
    # Helper aliases (local closures keep the file standalone & purity-safe).
    rolling_sum = _rolling_sum
    delay = _delay
    out = (-1.0 * np.sign((close - delay(close, 7)) + ops.delta(close, 7))) * (1.0 + ops.rank(1.0 + rolling_sum(returns, 250)))
    return out
