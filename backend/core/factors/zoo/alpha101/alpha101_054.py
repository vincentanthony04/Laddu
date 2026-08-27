"""alpha101_054 — Kakushadze Alpha #54.

Kakushadze Alpha #54.

Formula (paper appendix): -1 * ((low-close)*(open^5)) / ((low-high)*(close^5))
Source: Kakushadze (2015), "101 Formulaic Alphas", arXiv:1601.00991, eq. 54.

Clean-room reimplementation from the published formula, using our own
core/factors/factor_ops.py operators (not copied source code).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from core.factors import factor_ops as ops

__alpha_meta__ = {
    'id': 'alpha101_054',
    'nickname': 'Kakushadze Alpha #54',
    'theme': ['reversal'],
    'formula_latex': '-1 * ((low-close)*(open^5)) / ((low-high)*(close^5))',
    'columns_required': ['open', 'high', 'low', 'close'],
    'extras_required': [],
    'requires_sector': False,
    'universe': ['equity_us', 'equity_in'],
    'frequency': ['1D'],
    'decay_horizon': 5,
    'min_warmup_bars': 1,
    'notes': '',
}


def compute(panel: dict) -> pd.DataFrame:
    """Compute the alpha on the OHLCV+ panel and return a wide DataFrame."""
    close = panel["close"]
    open_ = panel["open"]
    high = panel["high"]
    low = panel["low"]


    # Helper aliases (local closures keep the file standalone & purity-safe).
    num = (low - close) * open_.pow(5)
    denom = (low - high) * close.pow(5)
    out = -1.0 * ops.safe_div(num, denom)
    return out
