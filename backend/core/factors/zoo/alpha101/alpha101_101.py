"""alpha101_101 — Kakushadze Alpha #101.

Kakushadze Alpha #101.

Formula (paper appendix): (close - open) / ((high - low) + 0.001)
Source: Kakushadze (2015), "101 Formulaic Alphas", arXiv:1601.00991, eq. 101.

Clean-room reimplementation from the published formula, using our own
core/factors/factor_ops.py operators (not copied source code).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from core.factors import factor_ops as ops

__alpha_meta__ = {
    'id': 'alpha101_101',
    'nickname': 'Kakushadze Alpha #101',
    'theme': ['reversal'],
    'formula_latex': '(close - open) / ((high - low) + 0.001)',
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
    out = ops.safe_div((close - open_), (high - low + 0.001))
    return out
