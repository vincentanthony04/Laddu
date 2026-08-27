"""alpha101_039 — Kakushadze Alpha #39.

Kakushadze Alpha #39.

Formula (paper appendix): (-1*rank(delta(close,7)*(1-rank(decay_linear(volume/adv20,9))))) * (1+rank(sum(returns,250)))
Source: Kakushadze (2015), "101 Formulaic Alphas", arXiv:1601.00991, eq. 39.

Clean-room reimplementation from the published formula, using our own
core/factors/factor_ops.py operators (not copied source code).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from core.factors import factor_ops as ops

__alpha_meta__ = {
    'id': 'alpha101_039',
    'nickname': 'Kakushadze Alpha #39',
    'theme': ['momentum', 'volume'],
    'formula_latex': '(-1*rank(delta(close,7)*(1-rank(decay_linear(volume/adv20,9))))) * (1+rank(sum(returns,250)))',
    'columns_required': ['close', 'volume'],
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


def compute(panel: dict) -> pd.DataFrame:
    """Compute the alpha on the OHLCV+ panel and return a wide DataFrame."""
    close = panel["close"]
    volume = panel["volume"]
    adv20 = ops.ts_mean(volume, 20)
    returns = close.pct_change()
    # Helper aliases (local closures keep the file standalone & purity-safe).
    rolling_sum = _rolling_sum
    out = (-1.0 * ops.rank(ops.delta(close, 7) * (1.0 - ops.rank(ops.decay_linear(ops.safe_div(volume, adv20), 9))))) * (1.0 + ops.rank(rolling_sum(returns, 250)))
    return out
