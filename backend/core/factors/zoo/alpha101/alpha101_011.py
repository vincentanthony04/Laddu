"""alpha101_011 — Kakushadze Alpha #11.

Kakushadze Alpha #11.

Formula (paper appendix): (rank(ts_max(vwap-close,3))+rank(ts_min(vwap-close,3)))*rank(delta(volume,3))
Source: Kakushadze (2015), "101 Formulaic Alphas", arXiv:1601.00991, eq. 11.

Clean-room reimplementation from the published formula, using our own
core/factors/factor_ops.py operators (not copied source code).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from core.factors import factor_ops as ops

__alpha_meta__ = {
    'id': 'alpha101_011',
    'nickname': 'Kakushadze Alpha #11',
    'theme': ['volume', 'reversal'],
    'formula_latex': '(rank(ts_max(vwap-close,3))+rank(ts_min(vwap-close,3)))*rank(delta(volume,3))',
    'columns_required': ['close', 'volume', 'vwap'],
    'extras_required': [],
    'requires_sector': False,
    'universe': ['equity_us', 'equity_in'],
    'frequency': ['1D'],
    'decay_horizon': 5,
    'min_warmup_bars': 5,
    'notes': '',
}


def compute(panel: dict) -> pd.DataFrame:
    """Compute the alpha on the OHLCV+ panel and return a wide DataFrame."""
    close = panel["close"]
    volume = panel["volume"]
    vwap = panel["vwap"]


    # Helper aliases (local closures keep the file standalone & purity-safe).
    diff = vwap - close
    out = (ops.rank(ops.ts_max(diff, 3)) + ops.rank(ops.ts_min(diff, 3))) * ops.rank(ops.delta(volume, 3))
    return out
