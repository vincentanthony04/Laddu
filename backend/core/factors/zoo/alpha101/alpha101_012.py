"""alpha101_012 — Kakushadze Alpha #12.

Kakushadze Alpha #12.

Formula (paper appendix): sign(delta(volume,1)) * (-1 * delta(close,1))
Source: Kakushadze (2015), "101 Formulaic Alphas", arXiv:1601.00991, eq. 12.

Clean-room reimplementation from the published formula, using our own
core/factors/factor_ops.py operators (not copied source code).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from core.factors import factor_ops as ops

__alpha_meta__ = {
    'id': 'alpha101_012',
    'nickname': 'Kakushadze Alpha #12',
    'theme': ['volume', 'reversal'],
    'formula_latex': 'sign(delta(volume,1)) * (-1 * delta(close,1))',
    'columns_required': ['close', 'volume'],
    'extras_required': [],
    'requires_sector': False,
    'universe': ['equity_us', 'equity_in'],
    'frequency': ['1D'],
    'decay_horizon': 5,
    'min_warmup_bars': 2,
    'notes': '',
}


def compute(panel: dict) -> pd.DataFrame:
    """Compute the alpha on the OHLCV+ panel and return a wide DataFrame."""
    close = panel["close"]
    volume = panel["volume"]


    # Helper aliases (local closures keep the file standalone & purity-safe).
    out = np.sign(ops.delta(volume, 1)) * (-1.0 * ops.delta(close, 1))
    return out
