"""alpha101_020 — Kakushadze Alpha #20.

Kakushadze Alpha #20.

Formula (paper appendix): (((-1*rank(open-delay(high,1)))*rank(open-delay(close,1)))*rank(open-delay(low,1)))
Source: Kakushadze (2015), "101 Formulaic Alphas", arXiv:1601.00991, eq. 20.

Clean-room reimplementation from the published formula, using our own
core/factors/factor_ops.py operators (not copied source code).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from core.factors import factor_ops as ops

__alpha_meta__ = {
    'id': 'alpha101_020',
    'nickname': 'Kakushadze Alpha #20',
    'theme': ['reversal'],
    'formula_latex': '(((-1*rank(open-delay(high,1)))*rank(open-delay(close,1)))*rank(open-delay(low,1)))',
    'columns_required': ['open', 'high', 'low', 'close'],
    'extras_required': [],
    'requires_sector': False,
    'universe': ['equity_us', 'equity_in'],
    'frequency': ['1D'],
    'decay_horizon': 5,
    'min_warmup_bars': 2,
    'notes': '',
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
    high = panel["high"]
    low = panel["low"]


    # Helper aliases (local closures keep the file standalone & purity-safe).
    delay = _delay
    out = ((-1.0 * ops.rank(open_ - delay(high, 1))) * ops.rank(open_ - delay(close, 1))) * ops.rank(open_ - delay(low, 1))
    return out
