"""alpha101_037 — Kakushadze Alpha #37.

Kakushadze Alpha #37.

Formula (paper appendix): rank(correlation(delay(open-close,1),close,200)) + rank(open-close)
Source: Kakushadze (2015), "101 Formulaic Alphas", arXiv:1601.00991, eq. 37.

Clean-room reimplementation from the published formula, using our own
core/factors/factor_ops.py operators (not copied source code).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from core.factors import factor_ops as ops

__alpha_meta__ = {
    'id': 'alpha101_037',
    'nickname': 'Kakushadze Alpha #37',
    'theme': ['momentum'],
    'formula_latex': 'rank(correlation(delay(open-close,1),close,200)) + rank(open-close)',
    'columns_required': ['open', 'close'],
    'extras_required': [],
    'requires_sector': False,
    'universe': ['equity_us', 'equity_in'],
    'frequency': ['1D'],
    'decay_horizon': 5,
    'min_warmup_bars': 201,
    'notes': 'Very long lookback (>= ~100 bars); produces NaN warmup on short panels which may trigger the >95% NaN registry guard.',
}


def _delay(df: pd.DataFrame, n: int) -> pd.DataFrame:
    """Backward shift by n (lookahead-safe; n>=1 required)."""
    if n < 1:
        raise ValueError("delay requires n >= 1 (lookahead ban)")
    return df.shift(n)


def compute(panel: dict) -> pd.DataFrame:
    """Compute the alpha on the OHLCV+ panel and return a wide DataFrame."""
    close = panel["close"]
    open_ = panel["open"]


    # Helper aliases (local closures keep the file standalone & purity-safe).
    delay = _delay
    out = ops.rank(ops.ts_corr(delay(open_ - close, 1), close, 200)) + ops.rank(open_ - close)
    return out
