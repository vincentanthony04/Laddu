"""alpha101_098 — Kakushadze Alpha #98.

Kakushadze Alpha #98.

Formula (paper appendix): rank(decay_linear(correlation(vwap, sum(adv5,26), 5), 7)) - rank(decay_linear(Ts_Rank(Ts_ArgMin(correlation(rank(open), rank(adv15), 21), 9), 7), 8))
Source: Kakushadze (2015), "101 Formulaic Alphas", arXiv:1601.00991, eq. 98.

Clean-room reimplementation from the published formula, using our own
core/factors/factor_ops.py operators (not copied source code).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from core.factors import factor_ops as ops

__alpha_meta__ = {
    'id': 'alpha101_098',
    'nickname': 'Kakushadze Alpha #98',
    'theme': ['volume'],
    'formula_latex': 'rank(decay_linear(correlation(vwap, sum(adv5,26), 5), 7)) - rank(decay_linear(Ts_Rank(Ts_ArgMin(correlation(rank(open), rank(adv15), 21), 9), 7), 8))',
    'columns_required': ['open', 'volume', 'vwap', 'close'],
    'extras_required': [],
    'requires_sector': False,
    'universe': ['equity_us', 'equity_in'],
    'frequency': ['1D'],
    'decay_horizon': 5,
    'min_warmup_bars': 56,
    'notes': '',
}


def _rolling_sum(df: pd.DataFrame, n: int) -> pd.DataFrame:
    """Rolling window sum; warmup -> NaN."""
    return df.rolling(window=n, min_periods=n).sum()


def compute(panel: dict) -> pd.DataFrame:
    """Compute the alpha on the OHLCV+ panel and return a wide DataFrame."""
    open_ = panel["open"]
    volume = panel["volume"]
    vwap = panel["vwap"]
    adv5 = ops.ts_mean(volume, 5)
    adv15 = ops.ts_mean(volume, 15)

    # Helper aliases (local closures keep the file standalone & purity-safe).
    rolling_sum = _rolling_sum
    a = ops.rank(ops.decay_linear(ops.ts_corr(vwap, rolling_sum(adv5, 26), 5), 7))
    b = ops.rank(ops.decay_linear(ops.ts_rank(ops.ts_argmin(ops.ts_corr(ops.rank(open_), ops.rank(adv15), 21), 9), 7), 8))
    out = a - b
    return out
