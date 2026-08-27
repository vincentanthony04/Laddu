"""alpha101_068 — Kakushadze Alpha #68.

Kakushadze Alpha #68.

Formula (paper appendix): (Ts_Rank(correlation(rank(high), rank(adv15), 9), 14) < rank(delta(0.518*close+0.482*low, 1))) * -1
Source: Kakushadze (2015), "101 Formulaic Alphas", arXiv:1601.00991, eq. 68.

Clean-room reimplementation from the published formula, using our own
core/factors/factor_ops.py operators (not copied source code).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from core.factors import factor_ops as ops

__alpha_meta__ = {
    'id': 'alpha101_068',
    'nickname': 'Kakushadze Alpha #68',
    'theme': ['volume'],
    'formula_latex': '(Ts_Rank(correlation(rank(high), rank(adv15), 9), 14) < rank(delta(0.518*close+0.482*low, 1))) * -1',
    'columns_required': ['high', 'low', 'close', 'volume'],
    'extras_required': [],
    'requires_sector': False,
    'universe': ['equity_us', 'equity_in'],
    'frequency': ['1D'],
    'decay_horizon': 5,
    'min_warmup_bars': 36,
    'notes': '',
}


def compute(panel: dict) -> pd.DataFrame:
    """Compute the alpha on the OHLCV+ panel and return a wide DataFrame."""
    close = panel["close"]
    high = panel["high"]
    low = panel["low"]
    volume = panel["volume"]
    adv15 = ops.ts_mean(volume, 15)

    # Helper aliases (local closures keep the file standalone & purity-safe).
    lhs = ops.ts_rank(ops.ts_corr(ops.rank(high), ops.rank(adv15), 9), 14)
    mix = close * 0.518371 + low * (1.0 - 0.518371)
    rhs = ops.rank(ops.delta(mix, 1))
    out = (lhs < rhs).astype(float) * -1.0
    return out
