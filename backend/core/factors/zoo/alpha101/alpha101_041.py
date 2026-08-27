"""alpha101_041 — Kakushadze Alpha #41.

Kakushadze Alpha #41.

Formula (paper appendix): (high*low)^0.5 - vwap
Source: Kakushadze (2015), "101 Formulaic Alphas", arXiv:1601.00991, eq. 41.

Clean-room reimplementation from the published formula, using our own
core/factors/factor_ops.py operators (not copied source code).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from core.factors import factor_ops as ops

__alpha_meta__ = {
    'id': 'alpha101_041',
    'nickname': 'Kakushadze Alpha #41',
    'theme': ['reversal'],
    'formula_latex': '(high*low)^0.5 - vwap',
    'columns_required': ['high', 'low', 'vwap', 'close'],
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
    high = panel["high"]
    low = panel["low"]
    vwap = panel["vwap"]


    # Helper aliases (local closures keep the file standalone & purity-safe).
    out = (high * low).pow(0.5) - vwap
    return out
