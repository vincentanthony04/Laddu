"""alpha101_001 — Kakushadze Alpha #1.

Kakushadze Alpha #1.

Formula (paper appendix): rank(ts_argmax(SignedPower((returns<0)?stddev(returns,20):close, 2.), 5)) - 0.5
Source: Kakushadze (2015), "101 Formulaic Alphas", arXiv:1601.00991, eq. 1.

Clean-room reimplementation from the published formula, using our own
core/factors/factor_ops.py operators (not copied source code).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from core.factors import factor_ops as ops

__alpha_meta__ = {
    'id': 'alpha101_001',
    'nickname': 'Kakushadze Alpha #1',
    'theme': ['reversal', 'volatility'],
    'formula_latex': 'rank(ts_argmax(SignedPower((returns<0)?stddev(returns,20):close, 2.), 5)) - 0.5',
    'columns_required': ['close'],
    'extras_required': [],
    'requires_sector': False,
    'universe': ['equity_us', 'equity_in'],
    'frequency': ['1D'],
    'decay_horizon': 5,
    'min_warmup_bars': 25,
    'notes': '',
}


def compute(panel: dict) -> pd.DataFrame:
    """Compute the alpha on the OHLCV+ panel and return a wide DataFrame."""
    close = panel["close"]


    returns = close.pct_change()
    # Helper aliases (local closures keep the file standalone & purity-safe).
    cond = (returns < 0).astype(float)
    x = ops.ts_std(returns, 20) * cond + close * (1.0 - cond)
    out = ops.rank(ops.ts_argmax(ops.signed_power(x, 2.0), 5)) - 0.5
    return out
