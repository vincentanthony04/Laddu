"""alpha101_077 — Kakushadze Alpha #77.

Kakushadze Alpha #77.

Formula (paper appendix): min(rank(decay_linear((high+low)/2 + high - (vwap+high), 20)), rank(decay_linear(correlation((high+low)/2, adv40, 3), 6)))
Source: Kakushadze (2015), "101 Formulaic Alphas", arXiv:1601.00991, eq. 77.

Clean-room reimplementation from the published formula, using our own
core/factors/factor_ops.py operators (not copied source code).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from core.factors import factor_ops as ops

__alpha_meta__ = {
    'id': 'alpha101_077',
    'nickname': 'Kakushadze Alpha #77',
    'theme': ['volume'],
    'formula_latex': 'min(rank(decay_linear((high+low)/2 + high - (vwap+high), 20)), rank(decay_linear(correlation((high+low)/2, adv40, 3), 6)))',
    'columns_required': ['high', 'low', 'volume', 'vwap', 'close'],
    'extras_required': [],
    'requires_sector': False,
    'universe': ['equity_us', 'equity_in'],
    'frequency': ['1D'],
    'decay_horizon': 5,
    'min_warmup_bars': 47,
    'notes': '',
}


def compute(panel: dict) -> pd.DataFrame:
    """Compute the alpha on the OHLCV+ panel and return a wide DataFrame."""
    close = panel["close"]
    high = panel["high"]
    low = panel["low"]
    volume = panel["volume"]
    vwap = panel["vwap"]
    adv40 = ops.ts_mean(volume, 40)

    # Helper aliases (local closures keep the file standalone & purity-safe).
    a = ops.rank(ops.decay_linear(((high + low) / 2.0) + high - (vwap + high), 20))
    b = ops.rank(ops.decay_linear(ops.ts_corr((high + low) / 2.0, adv40, 3), 6))
    arr_a = a.to_numpy(dtype=np.float64, na_value=np.nan)
    arr_b = b.to_numpy(dtype=np.float64, na_value=np.nan)
    out = pd.DataFrame(np.fmin(arr_a, arr_b), index=close.index, columns=close.columns)
    return out
