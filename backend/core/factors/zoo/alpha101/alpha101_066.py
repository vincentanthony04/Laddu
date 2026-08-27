"""alpha101_066 — Kakushadze Alpha #66.

Kakushadze Alpha #66.

Formula (paper appendix): (rank(decay_linear(delta(vwap,4), 7)) + Ts_Rank(decay_linear(((0.966*low+0.034*low - vwap)/(open-(high+low)/2)), 11), 7)) * -1
Source: Kakushadze (2015), "101 Formulaic Alphas", arXiv:1601.00991, eq. 66.

Clean-room reimplementation from the published formula, using our own
core/factors/factor_ops.py operators (not copied source code).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from core.factors import factor_ops as ops

__alpha_meta__ = {
    'id': 'alpha101_066',
    'nickname': 'Kakushadze Alpha #66',
    'theme': ['momentum'],
    'formula_latex': '(rank(decay_linear(delta(vwap,4), 7)) + Ts_Rank(decay_linear(((0.966*low+0.034*low - vwap)/(open-(high+low)/2)), 11), 7)) * -1',
    'columns_required': ['open', 'high', 'low', 'vwap', 'close'],
    'extras_required': [],
    'requires_sector': False,
    'universe': ['equity_us', 'equity_in'],
    'frequency': ['1D'],
    'decay_horizon': 5,
    'min_warmup_bars': 18,
    'notes': '',
}


def compute(panel: dict) -> pd.DataFrame:
    """Compute the alpha on the OHLCV+ panel and return a wide DataFrame."""
    open_ = panel["open"]
    high = panel["high"]
    low = panel["low"]
    vwap = panel["vwap"]


    # Helper aliases (local closures keep the file standalone & purity-safe).
    t1 = ops.rank(ops.decay_linear(ops.delta(vwap, 4), 7))
    num = (low * 0.96633 + low * (1.0 - 0.96633)) - vwap
    denom = open_ - (high + low) / 2.0
    t2 = ops.ts_rank(ops.decay_linear(ops.safe_div(num, denom), 11), 7)
    out = (t1 + t2) * -1.0
    return out
