"""alpha101_026 — Kakushadze Alpha #26.

Kakushadze Alpha #26.

Formula (paper appendix): -1 * ts_max(correlation(ts_rank(volume,5),ts_rank(high,5),5),3)
Source: Kakushadze (2015), "101 Formulaic Alphas", arXiv:1601.00991, eq. 26.

Clean-room reimplementation from the published formula, using our own
core/factors/factor_ops.py operators (not copied source code).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from core.factors import factor_ops as ops

__alpha_meta__ = {
    'id': 'alpha101_026',
    'nickname': 'Kakushadze Alpha #26',
    'theme': ['volume'],
    'formula_latex': '-1 * ts_max(correlation(ts_rank(volume,5),ts_rank(high,5),5),3)',
    'columns_required': ['high', 'volume', 'close'],
    'extras_required': [],
    'requires_sector': False,
    'universe': ['equity_us', 'equity_in'],
    'frequency': ['1D'],
    'decay_horizon': 5,
    'min_warmup_bars': 13,
    'notes': '',
}


def compute(panel: dict) -> pd.DataFrame:
    """Compute the alpha on the OHLCV+ panel and return a wide DataFrame."""
    high = panel["high"]
    volume = panel["volume"]


    # Helper aliases (local closures keep the file standalone & purity-safe).
    out = -1.0 * ops.ts_max(ops.ts_corr(ops.ts_rank(volume, 5), ops.ts_rank(high, 5), 5), 3)
    return out
