"""alpha101_052 — Kakushadze Alpha #52.

Kakushadze Alpha #52.

Formula (paper appendix): ((-1*ts_min(low,5)+delay(ts_min(low,5),5)) * rank((sum(returns,240)-sum(returns,20))/220)) * ts_rank(volume,5)
Source: Kakushadze (2015), "101 Formulaic Alphas", arXiv:1601.00991, eq. 52.

Clean-room reimplementation from the published formula, using our own
core/factors/factor_ops.py operators (not copied source code).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from core.factors import factor_ops as ops

__alpha_meta__ = {
    'id': 'alpha101_052',
    'nickname': 'Kakushadze Alpha #52',
    'theme': ['momentum'],
    'formula_latex': '((-1*ts_min(low,5)+delay(ts_min(low,5),5)) * rank((sum(returns,240)-sum(returns,20))/220)) * ts_rank(volume,5)',
    'columns_required': ['low', 'close', 'volume'],
    'extras_required': [],
    'requires_sector': False,
    'universe': ['equity_us', 'equity_in'],
    'frequency': ['1D'],
    'decay_horizon': 5,
    'min_warmup_bars': 240,
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
    low = panel["low"]
    volume = panel["volume"]

    returns = close.pct_change()
    # Helper aliases (local closures keep the file standalone & purity-safe).
    rolling_sum = _rolling_sum
    delay = _delay
    out = ((-1.0 * ops.ts_min(low, 5)) + delay(ops.ts_min(low, 5), 5)) * ops.rank((rolling_sum(returns, 240) - rolling_sum(returns, 20)) / 220.0) * ops.ts_rank(volume, 5)
    return out
