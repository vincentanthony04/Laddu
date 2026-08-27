"""alpha101_084 — Kakushadze Alpha #84.

Kakushadze Alpha #84.

Formula (paper appendix): SignedPower(Ts_Rank(vwap-ts_max(vwap,15), 21), delta(close,5))
Source: Kakushadze (2015), "101 Formulaic Alphas", arXiv:1601.00991, eq. 84.

Clean-room reimplementation from the published formula, using our own
core/factors/factor_ops.py operators (not copied source code).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from core.factors import factor_ops as ops

__alpha_meta__ = {
    'id': 'alpha101_084',
    'nickname': 'Kakushadze Alpha #84',
    'theme': ['momentum'],
    'formula_latex': 'SignedPower(Ts_Rank(vwap-ts_max(vwap,15), 21), delta(close,5))',
    'columns_required': ['close', 'vwap'],
    'extras_required': [],
    'requires_sector': False,
    'universe': ['equity_us', 'equity_in'],
    'frequency': ['1D'],
    'decay_horizon': 5,
    'min_warmup_bars': 35,
    'notes': "SignedPower is evaluated in a bounded log domain to avoid floating-point overflow while preserving sign and ordering.",
}


def compute(panel: dict) -> pd.DataFrame:
    """Compute the alpha on the OHLCV+ panel and return a wide DataFrame."""
    close = panel["close"]
    vwap = panel["vwap"]


    # Helper aliases (local closures keep the file standalone & purity-safe).
    base = ops.ts_rank(vwap - ops.ts_max(vwap, 15), 21)
    exponent_df = ops.delta(close, 5)
    base_arr = base.to_numpy(dtype=np.float64, na_value=np.nan)
    exp_arr = exponent_df.to_numpy(dtype=np.float64, na_value=np.nan)
    magnitude = np.abs(base_arr)
    valid = np.isfinite(magnitude) & np.isfinite(exp_arr) & (magnitude > 0)
    log_power = np.full_like(magnitude, np.nan)
    log_power[valid] = np.clip(exp_arr[valid] * np.log(magnitude[valid]), -700.0, 700.0)
    out_arr = np.sign(base_arr) * np.exp(log_power)
    out_arr[(magnitude == 0) & (exp_arr > 0)] = 0.0
    out = pd.DataFrame(out_arr, index=close.index, columns=close.columns)
    return out
