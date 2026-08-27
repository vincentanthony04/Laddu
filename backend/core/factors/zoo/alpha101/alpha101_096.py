"""alpha101_096 — Kakushadze Alpha #96.

Kakushadze Alpha #96.

Formula (paper appendix): max(Ts_Rank(decay_linear(correlation(rank(vwap), rank(volume), 4), 4), 8), Ts_Rank(decay_linear(Ts_ArgMax(correlation(Ts_Rank(close,7), Ts_Rank(adv60,4), 4), 13), 14), 13)) * -1
Source: Kakushadze (2015), "101 Formulaic Alphas", arXiv:1601.00991, eq. 96.

Clean-room reimplementation from the published formula, using our own
core/factors/factor_ops.py operators (not copied source code).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from core.factors import factor_ops as ops

__alpha_meta__ = {
    'id': 'alpha101_096',
    'nickname': 'Kakushadze Alpha #96',
    'theme': ['volume'],
    'formula_latex': 'max(Ts_Rank(decay_linear(correlation(rank(vwap), rank(volume), 4), 4), 8), Ts_Rank(decay_linear(Ts_ArgMax(correlation(Ts_Rank(close,7), Ts_Rank(adv60,4), 4), 13), 14), 13)) * -1',
    'columns_required': ['close', 'volume', 'vwap'],
    'extras_required': [],
    'requires_sector': False,
    'universe': ['equity_us', 'equity_in'],
    'frequency': ['1D'],
    'decay_horizon': 5,
    'min_warmup_bars': 103,
    'notes': '',
}


def compute(panel: dict) -> pd.DataFrame:
    """Compute the alpha on the OHLCV+ panel and return a wide DataFrame."""
    close = panel["close"]
    volume = panel["volume"]
    vwap = panel["vwap"]
    adv60 = ops.ts_mean(volume, 60)

    # Helper aliases (local closures keep the file standalone & purity-safe).
    a = ops.ts_rank(ops.decay_linear(ops.ts_corr(ops.rank(vwap), ops.rank(volume), 4), 4), 8)
    b = ops.ts_rank(ops.decay_linear(ops.ts_argmax(ops.ts_corr(ops.ts_rank(close, 7), ops.ts_rank(adv60, 4), 4), 13), 14), 13)
    arr_a = a.to_numpy(dtype=np.float64, na_value=np.nan)
    arr_b = b.to_numpy(dtype=np.float64, na_value=np.nan)
    out = pd.DataFrame(np.fmax(arr_a, arr_b), index=close.index, columns=close.columns) * -1.0
    return out
