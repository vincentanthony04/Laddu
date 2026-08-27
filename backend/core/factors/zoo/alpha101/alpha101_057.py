"""alpha101_057 — Kakushadze Alpha #57.

Kakushadze Alpha #57.

Formula (paper appendix): 0 - 1 * ((close-vwap) / decay_linear(rank(ts_argmax(close,30)), 2))
Source: Kakushadze (2015), "101 Formulaic Alphas", arXiv:1601.00991, eq. 57.

Clean-room reimplementation from the published formula, using our own
core/factors/factor_ops.py operators (not copied source code).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from core.factors import factor_ops as ops

__alpha_meta__ = {
    'id': 'alpha101_057',
    'nickname': 'Kakushadze Alpha #57',
    'theme': ['reversal'],
    'formula_latex': '0 - 1 * ((close-vwap) / decay_linear(rank(ts_argmax(close,30)), 2))',
    'columns_required': ['close', 'vwap'],
    'extras_required': [],
    'requires_sector': False,
    'universe': ['equity_us', 'equity_in'],
    'frequency': ['1D'],
    'decay_horizon': 5,
    'min_warmup_bars': 32,
    'notes': '',
}


def compute(panel: dict) -> pd.DataFrame:
    """Compute the alpha on the OHLCV+ panel and return a wide DataFrame."""
    close = panel["close"]
    vwap = panel["vwap"]


    # Helper aliases (local closures keep the file standalone & purity-safe).
    out = 0.0 - ops.safe_div((close - vwap), ops.decay_linear(ops.rank(ops.ts_argmax(close, 30)), 2))
    return out
