"""alpha101_061 — Kakushadze Alpha #61.

Kakushadze Alpha #61.

Formula (paper appendix): rank(vwap - ts_min(vwap,16)) < rank(correlation(vwap, adv180, 18))
Source: Kakushadze (2015), "101 Formulaic Alphas", arXiv:1601.00991, eq. 61.

Clean-room reimplementation from the published formula, using our own
core/factors/factor_ops.py operators (not copied source code).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from core.factors import factor_ops as ops

__alpha_meta__ = {
    'id': 'alpha101_061',
    'nickname': 'Kakushadze Alpha #61',
    'theme': ['volume'],
    'formula_latex': 'rank(vwap - ts_min(vwap,16)) < rank(correlation(vwap, adv180, 18))',
    'columns_required': ['volume', 'vwap', 'close'],
    'extras_required': [],
    'requires_sector': False,
    'universe': ['equity_us', 'equity_in'],
    'frequency': ['1D'],
    'decay_horizon': 5,
    'min_warmup_bars': 197,
    'notes': '',
}


def compute(panel: dict) -> pd.DataFrame:
    """Compute the alpha on the OHLCV+ panel and return a wide DataFrame."""
    volume = panel["volume"]
    vwap = panel["vwap"]
    adv180 = ops.ts_mean(volume, 180)

    # Helper aliases (local closures keep the file standalone & purity-safe).
    lhs = ops.rank(vwap - ops.ts_min(vwap, 16))
    rhs = ops.rank(ops.ts_corr(vwap, adv180, 18))
    out = (lhs < rhs).astype(float)
    return out
