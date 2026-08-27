"""alpha101_078 — Kakushadze Alpha #78.

Kakushadze Alpha #78.

Formula (paper appendix): rank(correlation(sum(0.352*low+0.648*vwap, 20), sum(adv40,20), 7))^rank(correlation(rank(vwap), rank(volume), 6))
Source: Kakushadze (2015), "101 Formulaic Alphas", arXiv:1601.00991, eq. 78.

Clean-room reimplementation from the published formula, using our own
core/factors/factor_ops.py operators (not copied source code).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from core.factors import factor_ops as ops

__alpha_meta__ = {
    'id': 'alpha101_078',
    'nickname': 'Kakushadze Alpha #78',
    'theme': ['volume'],
    'formula_latex': 'rank(correlation(sum(0.352*low+0.648*vwap, 20), sum(adv40,20), 7))^rank(correlation(rank(vwap), rank(volume), 6))',
    'columns_required': ['low', 'volume', 'vwap', 'close'],
    'extras_required': [],
    'requires_sector': False,
    'universe': ['equity_us', 'equity_in'],
    'frequency': ['1D'],
    'decay_horizon': 5,
    'min_warmup_bars': 46,
    'notes': '',
}


def _rolling_sum(df: pd.DataFrame, n: int) -> pd.DataFrame:
    """Rolling window sum; warmup -> NaN."""
    return df.rolling(window=n, min_periods=n).sum()


def compute(panel: dict) -> pd.DataFrame:
    """Compute the alpha on the OHLCV+ panel and return a wide DataFrame."""
    low = panel["low"]
    volume = panel["volume"]
    vwap = panel["vwap"]
    adv40 = ops.ts_mean(volume, 40)

    # Helper aliases (local closures keep the file standalone & purity-safe).
    rolling_sum = _rolling_sum
    mix = low * 0.352233 + vwap * (1.0 - 0.352233)
    lhs = ops.rank(ops.ts_corr(rolling_sum(mix, 20), rolling_sum(adv40, 20), 7))
    rhs = ops.rank(ops.ts_corr(ops.rank(vwap), ops.rank(volume), 6))
    out = lhs * rhs
    return out
