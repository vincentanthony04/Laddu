"""alpha101_033 — Kakushadze Alpha #33.

Kakushadze Alpha #33.

Formula (paper appendix): rank(-1*(1-open/close))
Source: Kakushadze (2015), "101 Formulaic Alphas", arXiv:1601.00991, eq. 33.

Clean-room reimplementation from the published formula, using our own
core/factors/factor_ops.py operators (not copied source code).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from core.factors import factor_ops as ops

__alpha_meta__ = {
    'id': 'alpha101_033',
    'nickname': 'Kakushadze Alpha #33',
    'theme': ['reversal'],
    'formula_latex': 'rank(-1*(1-open/close))',
    'columns_required': ['open', 'close'],
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
    open_ = panel["open"]


    # Helper aliases (local closures keep the file standalone & purity-safe).
    out = ops.rank(-1.0 * (1.0 - ops.safe_div(open_, close)))
    return out
