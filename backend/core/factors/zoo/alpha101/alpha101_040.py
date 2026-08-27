"""alpha101_040 — Kakushadze Alpha #40.

Kakushadze Alpha #40.

Formula (paper appendix): (-1*rank(stddev(high,10))) * correlation(high,volume,10)
Source: Kakushadze (2015), "101 Formulaic Alphas", arXiv:1601.00991, eq. 40.

Clean-room reimplementation from the published formula, using our own
core/factors/factor_ops.py operators (not copied source code).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from core.factors import factor_ops as ops

__alpha_meta__ = {
    'id': 'alpha101_040',
    'nickname': 'Kakushadze Alpha #40',
    'theme': ['volatility', 'volume'],
    'formula_latex': '(-1*rank(stddev(high,10))) * correlation(high,volume,10)',
    'columns_required': ['high', 'volume', 'close'],
    'extras_required': [],
    'requires_sector': False,
    'universe': ['equity_us', 'equity_in'],
    'frequency': ['1D'],
    'decay_horizon': 5,
    'min_warmup_bars': 10,
    'notes': '',
}


def compute(panel: dict) -> pd.DataFrame:
    """Compute the alpha on the OHLCV+ panel and return a wide DataFrame."""
    high = panel["high"]
    volume = panel["volume"]


    # Helper aliases (local closures keep the file standalone & purity-safe).
    out = (-1.0 * ops.rank(ops.ts_std(high, 10))) * ops.ts_corr(high, volume, 10)
    return out
