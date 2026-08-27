"""alpha101_053 — Kakushadze Alpha #53.

Kakushadze Alpha #53.

Formula (paper appendix): -1 * delta(((close-low) - (high-close))/(close-low), 9)
Source: Kakushadze (2015), "101 Formulaic Alphas", arXiv:1601.00991, eq. 53.

Clean-room reimplementation from the published formula, using our own
core/factors/factor_ops.py operators (not copied source code).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from core.factors import factor_ops as ops

__alpha_meta__ = {
    'id': 'alpha101_053',
    'nickname': 'Kakushadze Alpha #53',
    'theme': ['reversal'],
    'formula_latex': '-1 * delta(((close-low) - (high-close))/(close-low), 9)',
    'columns_required': ['high', 'low', 'close'],
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
    high = panel["high"]
    low = panel["low"]


    # Helper aliases (local closures keep the file standalone & purity-safe).
    x = ops.safe_div(((close - low) - (high - close)), (close - low))
    out = -1.0 * ops.delta(x, 9)
    return out
