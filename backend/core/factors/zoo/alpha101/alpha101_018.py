"""alpha101_018 — Kakushadze Alpha #18.

Kakushadze Alpha #18.

Formula (paper appendix): -1 * rank(stddev(abs(close-open),5) + (close-open) + correlation(close,open,10))
Source: Kakushadze (2015), "101 Formulaic Alphas", arXiv:1601.00991, eq. 18.

Clean-room reimplementation from the published formula, using our own
core/factors/factor_ops.py operators (not copied source code).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from core.factors import factor_ops as ops

__alpha_meta__ = {
    'id': 'alpha101_018',
    'nickname': 'Kakushadze Alpha #18',
    'theme': ['volatility'],
    'formula_latex': '-1 * rank(stddev(abs(close-open),5) + (close-open) + correlation(close,open,10))',
    'columns_required': ['open', 'close'],
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


    # Helper aliases (local closures keep the file standalone & purity-safe).
    diff = (close - open_)
    out = -1.0 * ops.rank(ops.ts_std(diff.abs(), 5) + diff + ops.ts_corr(close, open_, 10))
    return out
