"""alpha101_092 — Kakushadze Alpha #92.

Kakushadze Alpha #92.

Formula (paper appendix): min(Ts_Rank(decay_linear(((high+low)/2 + close < low+open), 15), 19), Ts_Rank(decay_linear(correlation(rank(low), rank(adv30), 8), 7), 7))
Source: Kakushadze (2015), "101 Formulaic Alphas", arXiv:1601.00991, eq. 92.

Clean-room reimplementation from the published formula, using our own
core/factors/factor_ops.py operators (not copied source code).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from core.factors import factor_ops as ops

__alpha_meta__ = {
    'id': 'alpha101_092',
    'nickname': 'Kakushadze Alpha #92',
    'theme': ['volume'],
    'formula_latex': 'min(Ts_Rank(decay_linear(((high+low)/2 + close < low+open), 15), 19), Ts_Rank(decay_linear(correlation(rank(low), rank(adv30), 8), 7), 7))',
    'columns_required': ['open', 'high', 'low', 'close', 'volume'],
    'extras_required': [],
    'requires_sector': False,
    'universe': ['equity_us', 'equity_in'],
    'frequency': ['1D'],
    'decay_horizon': 5,
    'min_warmup_bars': 49,
    'notes': '',
}


def compute(panel: dict) -> pd.DataFrame:
    """Compute the alpha on the OHLCV+ panel and return a wide DataFrame."""
    close = panel["close"]
    open_ = panel["open"]
    high = panel["high"]
    low = panel["low"]
    volume = panel["volume"]
    adv30 = ops.ts_mean(volume, 30)

    # Helper aliases (local closures keep the file standalone & purity-safe).
    cond = (((high + low) / 2.0 + close) < (low + open_)).astype(float)
    a = ops.ts_rank(ops.decay_linear(cond, 15), 19)
    b = ops.ts_rank(ops.decay_linear(ops.ts_corr(ops.rank(low), ops.rank(adv30), 8), 7), 7)
    arr_a = a.to_numpy(dtype=np.float64, na_value=np.nan)
    arr_b = b.to_numpy(dtype=np.float64, na_value=np.nan)
    out = pd.DataFrame(np.fmin(arr_a, arr_b), index=close.index, columns=close.columns)
    return out
