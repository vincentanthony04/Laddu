"""alpha101_075 — Kakushadze Alpha #75.

Kakushadze Alpha #75.

Formula (paper appendix): rank(correlation(vwap, volume, 4)) < rank(correlation(rank(low), rank(adv50), 12))
Source: Kakushadze (2015), "101 Formulaic Alphas", arXiv:1601.00991, eq. 75.

Clean-room reimplementation from the published formula, using our own
core/factors/factor_ops.py operators (not copied source code).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from core.factors import factor_ops as ops

__alpha_meta__ = {
    'id': 'alpha101_075',
    'nickname': 'Kakushadze Alpha #75',
    'theme': ['volume'],
    'formula_latex': 'rank(correlation(vwap, volume, 4)) < rank(correlation(rank(low), rank(adv50), 12))',
    'columns_required': ['low', 'volume', 'vwap', 'close'],
    'extras_required': [],
    'requires_sector': False,
    'universe': ['equity_us', 'equity_in'],
    'frequency': ['1D'],
    'decay_horizon': 5,
    'min_warmup_bars': 61,
    'notes': '',
}


def compute(panel: dict) -> pd.DataFrame:
    """Compute the alpha on the OHLCV+ panel and return a wide DataFrame."""
    low = panel["low"]
    volume = panel["volume"]
    vwap = panel["vwap"]
    adv5 = ops.ts_mean(volume, 5)
    adv50 = ops.ts_mean(volume, 50)

    # Helper aliases (local closures keep the file standalone & purity-safe).
    lhs = ops.rank(ops.ts_corr(vwap, volume, 4))
    rhs = ops.rank(ops.ts_corr(ops.rank(low), ops.rank(adv50), 12))
    out = (lhs < rhs).astype(float)
    return out
