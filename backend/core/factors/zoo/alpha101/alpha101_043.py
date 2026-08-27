"""alpha101_043 — Kakushadze Alpha #43.

Kakushadze Alpha #43.

Formula (paper appendix): ts_rank(volume/adv20,20) * ts_rank(-1*delta(close,7),8)
Source: Kakushadze (2015), "101 Formulaic Alphas", arXiv:1601.00991, eq. 43.

Clean-room reimplementation from the published formula, using our own
core/factors/factor_ops.py operators (not copied source code).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from core.factors import factor_ops as ops

__alpha_meta__ = {
    'id': 'alpha101_043',
    'nickname': 'Kakushadze Alpha #43',
    'theme': ['volume', 'momentum'],
    'formula_latex': 'ts_rank(volume/adv20,20) * ts_rank(-1*delta(close,7),8)',
    'columns_required': ['close', 'volume'],
    'extras_required': [],
    'requires_sector': False,
    'universe': ['equity_us', 'equity_in'],
    'frequency': ['1D'],
    'decay_horizon': 5,
    'min_warmup_bars': 39,
    'notes': '',
}


def compute(panel: dict) -> pd.DataFrame:
    """Compute the alpha on the OHLCV+ panel and return a wide DataFrame."""
    close = panel["close"]
    volume = panel["volume"]
    adv20 = ops.ts_mean(volume, 20)

    # Helper aliases (local closures keep the file standalone & purity-safe).
    out = ops.ts_rank(ops.safe_div(volume, adv20), 20) * ops.ts_rank(-1.0 * ops.delta(close, 7), 8)
    return out
