"""alpha101_094 — Kakushadze Alpha #94.

Kakushadze Alpha #94.

Formula (paper appendix): (rank(vwap-ts_min(vwap,12))^Ts_Rank(correlation(Ts_Rank(vwap,20), Ts_Rank(adv60,4), 18), 3)) * -1
Source: Kakushadze (2015), "101 Formulaic Alphas", arXiv:1601.00991, eq. 94.

Clean-room reimplementation from the published formula, using our own
core/factors/factor_ops.py operators (not copied source code).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from core.factors import factor_ops as ops

__alpha_meta__ = {
    'id': 'alpha101_094',
    'nickname': 'Kakushadze Alpha #94',
    'theme': ['volume'],
    'formula_latex': '(rank(vwap-ts_min(vwap,12))^Ts_Rank(correlation(Ts_Rank(vwap,20), Ts_Rank(adv60,4), 18), 3)) * -1',
    'columns_required': ['volume', 'vwap', 'close'],
    'extras_required': [],
    'requires_sector': False,
    'universe': ['equity_us', 'equity_in'],
    'frequency': ['1D'],
    'decay_horizon': 5,
    'min_warmup_bars': 82,
    'notes': '',
}


def compute(panel: dict) -> pd.DataFrame:
    """Compute the alpha on the OHLCV+ panel and return a wide DataFrame."""
    volume = panel["volume"]
    vwap = panel["vwap"]
    adv60 = ops.ts_mean(volume, 60)

    # Helper aliases (local closures keep the file standalone & purity-safe).
    lhs = ops.rank(vwap - ops.ts_min(vwap, 12))
    rhs = ops.ts_rank(ops.ts_corr(ops.ts_rank(vwap, 20), ops.ts_rank(adv60, 4), 18), 3)
    out = (lhs * rhs) * -1.0
    return out
