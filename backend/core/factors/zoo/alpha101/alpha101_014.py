"""alpha101_014 — Kakushadze Alpha #14.

Kakushadze Alpha #14.

Formula (paper appendix): (-1*rank(delta(returns,3))) * correlation(open, volume, 10)
Source: Kakushadze (2015), "101 Formulaic Alphas", arXiv:1601.00991, eq. 14.

Clean-room reimplementation from the published formula, using our own
core/factors/factor_ops.py operators (not copied source code).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from core.factors import factor_ops as ops

__alpha_meta__ = {
    'id': 'alpha101_014',
    'nickname': 'Kakushadze Alpha #14',
    'theme': ['volume', 'momentum'],
    'formula_latex': '(-1*rank(delta(returns,3))) * correlation(open, volume, 10)',
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

    returns = close.pct_change()
    # Helper aliases (local closures keep the file standalone & purity-safe).
    out = (-1.0 * ops.rank(ops.delta(returns, 3))) * ops.ts_corr(open_, volume, 10)
    return out
