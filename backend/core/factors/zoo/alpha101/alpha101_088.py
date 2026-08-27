"""alpha101_088 — Kakushadze Alpha #88.

Kakushadze Alpha #88.

Formula (paper appendix): min(rank(decay_linear((rank(open)+rank(low))-(rank(high)+rank(close)),8)), Ts_Rank(decay_linear(correlation(Ts_Rank(close,8),Ts_Rank(adv60,20),8),7),3))
Source: Kakushadze (2015), "101 Formulaic Alphas", arXiv:1601.00991, eq. 88.

Clean-room reimplementation from the published formula, using our own
core/factors/factor_ops.py operators (not copied source code).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from core.factors import factor_ops as ops

__alpha_meta__ = {
    'id': 'alpha101_088',
    'nickname': 'Kakushadze Alpha #88',
    'theme': ['volume'],
    'formula_latex': 'min(rank(decay_linear((rank(open)+rank(low))-(rank(high)+rank(close)),8)), Ts_Rank(decay_linear(correlation(Ts_Rank(close,8),Ts_Rank(adv60,20),8),7),3))',
    'columns_required': ['open', 'high', 'low', 'close', 'volume'],
    'extras_required': [],
    'requires_sector': False,
    'universe': ['equity_us', 'equity_in'],
    'frequency': ['1D'],
    'decay_horizon': 5,
    'min_warmup_bars': 94,
    'notes': '',
}


def compute(panel: dict) -> pd.DataFrame:
    """Compute the alpha on the OHLCV+ panel and return a wide DataFrame."""
    close = panel["close"]
    open_ = panel["open"]
    high = panel["high"]
    low = panel["low"]
    volume = panel["volume"]
    adv60 = ops.ts_mean(volume, 60)

    # Helper aliases (local closures keep the file standalone & purity-safe).
    a = ops.rank(ops.decay_linear((ops.rank(open_) + ops.rank(low)) - (ops.rank(high) + ops.rank(close)), 8))
    b = ops.ts_rank(ops.decay_linear(ops.ts_corr(ops.ts_rank(close, 8), ops.ts_rank(adv60, 20), 8), 7), 3)
    arr_a = a.to_numpy(dtype=np.float64, na_value=np.nan)
    arr_b = b.to_numpy(dtype=np.float64, na_value=np.nan)
    out = pd.DataFrame(np.fmin(arr_a, arr_b), index=close.index, columns=close.columns)
    return out
