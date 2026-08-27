"""alpha101_055 — Kakushadze Alpha #55.

Kakushadze Alpha #55.

Formula (paper appendix): -1 * correlation(rank((close-ts_min(low,12))/(ts_max(high,12)-ts_min(low,12))), rank(volume), 6)
Source: Kakushadze (2015), "101 Formulaic Alphas", arXiv:1601.00991, eq. 55.

Clean-room reimplementation from the published formula, using our own
core/factors/factor_ops.py operators (not copied source code).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from core.factors import factor_ops as ops

__alpha_meta__ = {
    'id': 'alpha101_055',
    'nickname': 'Kakushadze Alpha #55',
    'theme': ['volume', 'reversal'],
    'formula_latex': '-1 * correlation(rank((close-ts_min(low,12))/(ts_max(high,12)-ts_min(low,12))), rank(volume), 6)',
    'columns_required': ['high', 'low', 'close', 'volume'],
    'extras_required': [],
    'requires_sector': False,
    'universe': ['equity_us', 'equity_in'],
    'frequency': ['1D'],
    'decay_horizon': 5,
    'min_warmup_bars': 17,
    'notes': '',
}


def compute(panel: dict) -> pd.DataFrame:
    """Compute the alpha on the OHLCV+ panel and return a wide DataFrame."""
    close = panel["close"]
    high = panel["high"]
    low = panel["low"]
    volume = panel["volume"]


    # Helper aliases (local closures keep the file standalone & purity-safe).
    x = ops.safe_div(close - ops.ts_min(low, 12), ops.ts_max(high, 12) - ops.ts_min(low, 12))
    out = -1.0 * ops.ts_corr(ops.rank(x), ops.rank(volume), 6)
    return out
