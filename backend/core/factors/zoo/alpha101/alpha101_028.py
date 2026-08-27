"""alpha101_028 — Kakushadze Alpha #28.

Kakushadze Alpha #28.

Formula (paper appendix): scale((correlation(adv20,low,5) + (high+low)/2) - close)
Source: Kakushadze (2015), "101 Formulaic Alphas", arXiv:1601.00991, eq. 28.

Clean-room reimplementation from the published formula, using our own
core/factors/factor_ops.py operators (not copied source code).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from core.factors import factor_ops as ops

__alpha_meta__ = {
    'id': 'alpha101_028',
    'nickname': 'Kakushadze Alpha #28',
    'theme': ['volume'],
    'formula_latex': 'scale((correlation(adv20,low,5) + (high+low)/2) - close)',
    'columns_required': ['high', 'low', 'close', 'volume'],
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
    high = panel["high"]
    low = panel["low"]
    volume = panel["volume"]
    adv20 = ops.ts_mean(volume, 20)

    # Helper aliases (local closures keep the file standalone & purity-safe).
    out = ops.scale(ops.ts_corr(adv20, low, 5) + (high + low) / 2.0 - close)
    return out
