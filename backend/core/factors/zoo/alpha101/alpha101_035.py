"""alpha101_035 — Kakushadze Alpha #35.

Kakushadze Alpha #35.

Formula (paper appendix): ts_rank(volume,32) * (1 - ts_rank((close+high-low),16)) * (1 - ts_rank(returns,32))
Source: Kakushadze (2015), "101 Formulaic Alphas", arXiv:1601.00991, eq. 35.

Clean-room reimplementation from the published formula, using our own
core/factors/factor_ops.py operators (not copied source code).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from core.factors import factor_ops as ops

__alpha_meta__ = {
    'id': 'alpha101_035',
    'nickname': 'Kakushadze Alpha #35',
    'theme': ['volume', 'momentum'],
    'formula_latex': 'ts_rank(volume,32) * (1 - ts_rank((close+high-low),16)) * (1 - ts_rank(returns,32))',
    'columns_required': ['high', 'low', 'close', 'volume'],
    'extras_required': [],
    'requires_sector': False,
    'universe': ['equity_us', 'equity_in'],
    'frequency': ['1D'],
    'decay_horizon': 5,
    'min_warmup_bars': 33,
    'notes': '',
}


def compute(panel: dict) -> pd.DataFrame:
    """Compute the alpha on the OHLCV+ panel and return a wide DataFrame."""
    close = panel["close"]
    high = panel["high"]
    low = panel["low"]
    volume = panel["volume"]

    returns = close.pct_change()
    # Helper aliases (local closures keep the file standalone & purity-safe).
    out = ops.ts_rank(volume, 32) * (1.0 - ops.ts_rank((close + high - low), 16)) * (1.0 - ops.ts_rank(returns, 32))
    return out
