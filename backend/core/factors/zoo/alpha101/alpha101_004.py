"""alpha101_004 — Kakushadze Alpha #4.

Kakushadze Alpha #4.

Formula (paper appendix): -1 * Ts_Rank(rank(low), 9)
Source: Kakushadze (2015), "101 Formulaic Alphas", arXiv:1601.00991, eq. 4.

Clean-room reimplementation from the published formula, using our own
core/factors/factor_ops.py operators (not copied source code).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from core.factors import factor_ops as ops

__alpha_meta__ = {
    'id': 'alpha101_004',
    'nickname': 'Kakushadze Alpha #4',
    'theme': ['reversal'],
    'formula_latex': '-1 * Ts_Rank(rank(low), 9)',
    'columns_required': ['low', 'close'],
    'extras_required': [],
    'requires_sector': False,
    'universe': ['equity_us', 'equity_in'],
    'frequency': ['1D'],
    'decay_horizon': 5,
    'min_warmup_bars': 9,
    'notes': '',
}


def compute(panel: dict) -> pd.DataFrame:
    """Compute the alpha on the OHLCV+ panel and return a wide DataFrame."""
    low = panel["low"]


    # Helper aliases (local closures keep the file standalone & purity-safe).
    out = -1.0 * ops.ts_rank(ops.rank(low), 9)
    return out
