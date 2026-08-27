"""alpha101_034 — Kakushadze Alpha #34.

Kakushadze Alpha #34.

Formula (paper appendix): rank((1-rank(stddev(returns,2)/stddev(returns,5))) + (1-rank(delta(close,1))))
Source: Kakushadze (2015), "101 Formulaic Alphas", arXiv:1601.00991, eq. 34.

Clean-room reimplementation from the published formula, using our own
core/factors/factor_ops.py operators (not copied source code).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from core.factors import factor_ops as ops

__alpha_meta__ = {
    'id': 'alpha101_034',
    'nickname': 'Kakushadze Alpha #34',
    'theme': ['volatility'],
    'formula_latex': 'rank((1-rank(stddev(returns,2)/stddev(returns,5))) + (1-rank(delta(close,1))))',
    'columns_required': ['close'],
    'extras_required': [],
    'requires_sector': False,
    'universe': ['equity_us', 'equity_in'],
    'frequency': ['1D'],
    'decay_horizon': 5,
    'min_warmup_bars': 6,
    'notes': '',
}


def compute(panel: dict) -> pd.DataFrame:
    """Compute the alpha on the OHLCV+ panel and return a wide DataFrame."""
    close = panel["close"]


    returns = close.pct_change()
    # Helper aliases (local closures keep the file standalone & purity-safe).
    out = ops.rank((1.0 - ops.rank(ops.safe_div(ops.ts_std(returns, 2), ops.ts_std(returns, 5)))) + (1.0 - ops.rank(ops.delta(close, 1))))
    return out
