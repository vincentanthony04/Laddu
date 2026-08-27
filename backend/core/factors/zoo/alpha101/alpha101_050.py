"""alpha101_050 — Kakushadze Alpha #50.

Kakushadze Alpha #50.

Formula (paper appendix): -1 * ts_max(rank(correlation(rank(volume), rank(vwap), 5)), 5)
Source: Kakushadze (2015), "101 Formulaic Alphas", arXiv:1601.00991, eq. 50.

Clean-room reimplementation from the published formula, using our own
core/factors/factor_ops.py operators (not copied source code).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from core.factors import factor_ops as ops

__alpha_meta__ = {
    'id': 'alpha101_050',
    'nickname': 'Kakushadze Alpha #50',
    'theme': ['volume'],
    'formula_latex': '-1 * ts_max(rank(correlation(rank(volume), rank(vwap), 5)), 5)',
    'columns_required': ['volume', 'vwap', 'close'],
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
    volume = panel["volume"]
    vwap = panel["vwap"]


    # Helper aliases (local closures keep the file standalone & purity-safe).
    out = -1.0 * ops.ts_max(ops.rank(ops.ts_corr(ops.rank(volume), ops.rank(vwap), 5)), 5)
    return out
