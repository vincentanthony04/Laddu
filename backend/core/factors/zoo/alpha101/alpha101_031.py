"""alpha101_031 — Kakushadze Alpha #31.

Kakushadze Alpha #31.

Formula (paper appendix): rank(rank(rank(decay_linear(-1*rank(rank(delta(close,10))),10)))) + rank(-1*delta(close,3)) + sign(scale(correlation(adv20,low,12)))
Source: Kakushadze (2015), "101 Formulaic Alphas", arXiv:1601.00991, eq. 31.

Clean-room reimplementation from the published formula, using our own
core/factors/factor_ops.py operators (not copied source code).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from core.factors import factor_ops as ops

__alpha_meta__ = {
    'id': 'alpha101_031',
    'nickname': 'Kakushadze Alpha #31',
    'theme': ['momentum'],
    'formula_latex': 'rank(rank(rank(decay_linear(-1*rank(rank(delta(close,10))),10)))) + rank(-1*delta(close,3)) + sign(scale(correlation(adv20,low,12)))',
    'columns_required': ['low', 'close', 'volume'],
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
    low = panel["low"]
    volume = panel["volume"]
    adv20 = ops.ts_mean(volume, 20)

    # Helper aliases (local closures keep the file standalone & purity-safe).
    t1 = ops.rank(ops.rank(ops.rank(ops.decay_linear(-1.0 * ops.rank(ops.rank(ops.delta(close, 10))), 10))))
    t2 = ops.rank(-1.0 * ops.delta(close, 3))
    t3 = pd.DataFrame(np.sign(ops.scale(ops.ts_corr(adv20, low, 12)).to_numpy(dtype=np.float64, na_value=np.nan)), index=close.index, columns=close.columns)
    out = t1 + t2 + t3
    return out
