"""alpha101_071 — Kakushadze Alpha #71.

Kakushadze Alpha #71.

Formula (paper appendix): max(Ts_Rank(decay_linear(correlation(Ts_Rank(close,3), Ts_Rank(adv180,12), 18), 4), 16), Ts_Rank(decay_linear((rank((low+open)-(2*vwap))^2, 16), 4))
Source: Kakushadze (2015), "101 Formulaic Alphas", arXiv:1601.00991, eq. 71.

Clean-room reimplementation from the published formula, using our own
core/factors/factor_ops.py operators (not copied source code).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from core.factors import factor_ops as ops

__alpha_meta__ = {
    'id': 'alpha101_071',
    'nickname': 'Kakushadze Alpha #71',
    'theme': ['volume', 'reversal'],
    'formula_latex': 'max(Ts_Rank(decay_linear(correlation(Ts_Rank(close,3), Ts_Rank(adv180,12), 18), 4), 16), Ts_Rank(decay_linear((rank((low+open)-(2*vwap))^2, 16), 4))',
    'columns_required': ['open', 'low', 'close', 'volume', 'vwap'],
    'extras_required': [],
    'requires_sector': False,
    'universe': ['equity_us', 'equity_in'],
    'frequency': ['1D'],
    'decay_horizon': 5,
    'min_warmup_bars': 226,
    'notes': '',
}


def compute(panel: dict) -> pd.DataFrame:
    """Compute the alpha on the OHLCV+ panel and return a wide DataFrame."""
    close = panel["close"]
    open_ = panel["open"]
    low = panel["low"]
    volume = panel["volume"]
    vwap = panel["vwap"]
    adv180 = ops.ts_mean(volume, 180)

    # Helper aliases (local closures keep the file standalone & purity-safe).
    a = ops.ts_rank(ops.decay_linear(ops.ts_corr(ops.ts_rank(close, 3), ops.ts_rank(adv180, 12), 18), 4), 16)
    inner = ops.signed_power(ops.rank((low + open_) - (vwap + vwap)), 2.0)
    b = ops.ts_rank(ops.decay_linear(inner, 16), 4)
    arr_a = a.to_numpy(dtype=np.float64, na_value=np.nan)
    arr_b = b.to_numpy(dtype=np.float64, na_value=np.nan)
    out = pd.DataFrame(np.fmax(arr_a, arr_b), index=close.index, columns=close.columns)
    return out
