"""alpha101_085 — Kakushadze Alpha #85.

Kakushadze Alpha #85.

Formula (paper appendix): rank(correlation(0.877*high+0.123*close, adv30, 10))^rank(correlation(Ts_Rank((high+low)/2,4), Ts_Rank(volume,10), 7))
Source: Kakushadze (2015), "101 Formulaic Alphas", arXiv:1601.00991, eq. 85.

Clean-room reimplementation from the published formula, using our own
core/factors/factor_ops.py operators (not copied source code).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from core.factors import factor_ops as ops

__alpha_meta__ = {
    'id': 'alpha101_085',
    'nickname': 'Kakushadze Alpha #85',
    'theme': ['volume'],
    'formula_latex': 'rank(correlation(0.877*high+0.123*close, adv30, 10))^rank(correlation(Ts_Rank((high+low)/2,4), Ts_Rank(volume,10), 7))',
    'columns_required': ['high', 'low', 'close', 'volume'],
    'extras_required': [],
    'requires_sector': False,
    'universe': ['equity_us', 'equity_in'],
    'frequency': ['1D'],
    'decay_horizon': 5,
    'min_warmup_bars': 39,
    'notes': '',
}


def compute(panel: dict) -> pd.DataFrame:
    """Compute the alpha on the OHLCV+ panel and return a wide DataFrame."""
    close = panel["close"]
    high = panel["high"]
    low = panel["low"]
    volume = panel["volume"]
    adv30 = ops.ts_mean(volume, 30)

    # Helper aliases (local closures keep the file standalone & purity-safe).
    mix = high * 0.876703 + close * (1.0 - 0.876703)
    lhs = ops.rank(ops.ts_corr(mix, adv30, 10))
    rhs = ops.rank(ops.ts_corr(ops.ts_rank((high + low) / 2.0, 4), ops.ts_rank(volume, 10), 7))
    out = lhs * rhs
    return out
