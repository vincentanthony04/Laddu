"""alpha101_022 — Kakushadze Alpha #22.

Kakushadze Alpha #22.

Formula (paper appendix): -1 * (delta(correlation(high,volume,5),5) * rank(stddev(close,20)))
Source: Kakushadze (2015), "101 Formulaic Alphas", arXiv:1601.00991, eq. 22.

Clean-room reimplementation from the published formula, using our own
core/factors/factor_ops.py operators (not copied source code).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from core.factors import factor_ops as ops

__alpha_meta__ = {
    'id': 'alpha101_022',
    'nickname': 'Kakushadze Alpha #22',
    'theme': ['volume', 'volatility'],
    'formula_latex': '-1 * (delta(correlation(high,volume,5),5) * rank(stddev(close,20)))',
    'columns_required': ['high', 'close', 'volume'],
    'extras_required': [],
    'requires_sector': False,
    'universe': ['equity_us', 'equity_in'],
    'frequency': ['1D'],
    'decay_horizon': 5,
    'min_warmup_bars': 25,
    'notes': '',
}


def compute(panel: dict) -> pd.DataFrame:
    """Compute the alpha on the OHLCV+ panel and return a wide DataFrame."""
    close = panel["close"]
    high = panel["high"]
    volume = panel["volume"]


    # Helper aliases (local closures keep the file standalone & purity-safe).
    out = -1.0 * (ops.delta(ops.ts_corr(high, volume, 5), 5) * ops.rank(ops.ts_std(close, 20)))
    return out
