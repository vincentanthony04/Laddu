"""alpha101_002 — Kakushadze Alpha #2.

Kakushadze Alpha #2.

Formula (paper appendix): -1 * correlation(rank(delta(log(volume), 2)), rank(((close-open)/open)), 6)
Source: Kakushadze (2015), "101 Formulaic Alphas", arXiv:1601.00991, eq. 2.

Clean-room reimplementation from the published formula, using our own
core/factors/factor_ops.py operators (not copied source code).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from core.factors import factor_ops as ops

__alpha_meta__ = {
    'id': 'alpha101_002',
    'nickname': 'Kakushadze Alpha #2',
    'theme': ['volume', 'reversal'],
    'formula_latex': '-1 * correlation(rank(delta(log(volume), 2)), rank(((close-open)/open)), 6)',
    'columns_required': ['open', 'close', 'volume'],
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
    close = panel["close"]
    open_ = panel["open"]
    volume = panel["volume"]


    # Helper aliases (local closures keep the file standalone & purity-safe).
    out = -1.0 * ops.ts_corr(ops.rank(ops.delta(np.log(volume), 2)), ops.rank((close - open_) / open_), 6)
    return out
