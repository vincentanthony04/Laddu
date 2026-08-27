"""alpha101_073 — Kakushadze Alpha #73.

Kakushadze Alpha #73.

Formula (paper appendix): max(rank(decay_linear(delta(vwap,5), 3)), Ts_Rank(decay_linear(-1*(delta(0.147*open+0.853*low,2)/(0.147*open+0.853*low)), 3), 17)) * -1
Source: Kakushadze (2015), "101 Formulaic Alphas", arXiv:1601.00991, eq. 73.

Clean-room reimplementation from the published formula, using our own
core/factors/factor_ops.py operators (not copied source code).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from core.factors import factor_ops as ops

__alpha_meta__ = {
    'id': 'alpha101_073',
    'nickname': 'Kakushadze Alpha #73',
    'theme': ['volume'],
    'formula_latex': 'max(rank(decay_linear(delta(vwap,5), 3)), Ts_Rank(decay_linear(-1*(delta(0.147*open+0.853*low,2)/(0.147*open+0.853*low)), 3), 17)) * -1',
    'columns_required': ['open', 'low', 'vwap', 'close'],
    'extras_required': [],
    'requires_sector': False,
    'universe': ['equity_us', 'equity_in'],
    'frequency': ['1D'],
    'decay_horizon': 5,
    'min_warmup_bars': 21,
    'notes': '',
}


def compute(panel: dict) -> pd.DataFrame:
    """Compute the alpha on the OHLCV+ panel and return a wide DataFrame."""
    close = panel["close"]
    open_ = panel["open"]
    low = panel["low"]
    vwap = panel["vwap"]


    # Helper aliases (local closures keep the file standalone & purity-safe).
    a = ops.rank(ops.decay_linear(ops.delta(vwap, 5), 3))
    mix = open_ * 0.147155 + low * (1.0 - 0.147155)
    b_inner = ops.safe_div(ops.delta(mix, 2), mix) * -1.0
    b = ops.ts_rank(ops.decay_linear(b_inner, 3), 17)
    arr_a = a.to_numpy(dtype=np.float64, na_value=np.nan)
    arr_b = b.to_numpy(dtype=np.float64, na_value=np.nan)
    out = pd.DataFrame(np.fmax(arr_a, arr_b), index=close.index, columns=close.columns) * -1.0
    return out
