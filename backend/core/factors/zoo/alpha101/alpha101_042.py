"""alpha101_042 — Kakushadze Alpha #42.

Kakushadze Alpha #42.

Formula (paper appendix): rank(vwap-close) / rank(vwap+close)
Source: Kakushadze (2015), "101 Formulaic Alphas", arXiv:1601.00991, eq. 42.

Clean-room reimplementation from the published formula, using our own
core/factors/factor_ops.py operators (not copied source code).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from core.factors import factor_ops as ops

__alpha_meta__ = {
    'id': 'alpha101_042',
    'nickname': 'Kakushadze Alpha #42',
    'theme': ['reversal'],
    'formula_latex': 'rank(vwap-close) / rank(vwap+close)',
    'columns_required': ['close', 'vwap'],
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
    vwap = panel["vwap"]


    # Helper aliases (local closures keep the file standalone & purity-safe).
    out = ops.safe_div(ops.rank(vwap - close), ops.rank(vwap + close))
    return out
