"""alpha101_072 — Kakushadze Alpha #72.

Kakushadze Alpha #72.

Formula (paper appendix): rank(decay_linear(correlation((high+low)/2, adv40, 9), 10)) / rank(decay_linear(correlation(Ts_Rank(vwap,4), Ts_Rank(volume,19), 7), 3))
Source: Kakushadze (2015), "101 Formulaic Alphas", arXiv:1601.00991, eq. 72.

Clean-room reimplementation from the published formula, using our own
core/factors/factor_ops.py operators (not copied source code).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from core.factors import factor_ops as ops

__alpha_meta__ = {
    'id': 'alpha101_072',
    'nickname': 'Kakushadze Alpha #72',
    'theme': ['volume'],
    'formula_latex': 'rank(decay_linear(correlation((high+low)/2, adv40, 9), 10)) / rank(decay_linear(correlation(Ts_Rank(vwap,4), Ts_Rank(volume,19), 7), 3))',
    'columns_required': ['high', 'low', 'volume', 'vwap', 'close'],
    'extras_required': [],
    'requires_sector': False,
    'universe': ['equity_us', 'equity_in'],
    'frequency': ['1D'],
    'decay_horizon': 5,
    'min_warmup_bars': 57,
    'notes': '',
}


def compute(panel: dict) -> pd.DataFrame:
    """Compute the alpha on the OHLCV+ panel and return a wide DataFrame."""
    high = panel["high"]
    low = panel["low"]
    volume = panel["volume"]
    vwap = panel["vwap"]
    adv40 = ops.ts_mean(volume, 40)

    # Helper aliases (local closures keep the file standalone & purity-safe).
    num = ops.rank(ops.decay_linear(ops.ts_corr((high + low) / 2.0, adv40, 9), 10))
    denom = ops.rank(ops.decay_linear(ops.ts_corr(ops.ts_rank(vwap, 4), ops.ts_rank(volume, 19), 7), 3))
    out = ops.safe_div(num, denom)
    return out
