"""alpha101_006 — Kakushadze Alpha #6.

Kakushadze Alpha #6.

Formula (paper appendix): -1 * correlation(open, volume, 10)
Source: Kakushadze (2015), "101 Formulaic Alphas", arXiv:1601.00991, eq. 6.

Clean-room reimplementation from the published formula, using our own
core/factors/factor_ops.py operators (not copied source code).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from core.factors import factor_ops as ops

__alpha_meta__ = {
    'id': 'alpha101_006',
    'nickname': 'Kakushadze Alpha #6',
    'theme': ['volume', 'reversal'],
    'formula_latex': '-1 * correlation(open, volume, 10)',
    'columns_required': ['open', 'volume', 'close'],
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
    open_ = panel["open"]
    volume = panel["volume"]


    # Helper aliases (local closures keep the file standalone & purity-safe).
    out = -1.0 * ops.ts_corr(open_, volume, 10)
    return out
