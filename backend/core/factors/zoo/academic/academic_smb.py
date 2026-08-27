"""academic_smb — Fama-French (1993) SMB, price-proxy form.

The canonical SMB (Small Minus Big) sorts on market capitalization,
which is not carried in the OHLCV panel. This is a documented PROXY:
negative log of 60-day average dollar volume (close * volume) as a
liquidity-weighted size proxy — small caps typically trade with low
dollar volume. Higher score = smaller/less liquid names.

Source: Fama, E. F., & French, K. R. (1993), "Common Risk Factors in
the Returns on Stocks and Bonds", Journal of Financial Economics.
Formula reimplemented as a price/volume proxy from the published
definition.

Caveat: this is explicitly a proxy, not true market cap. Should be
replaced with a real market-cap-based SMB once shares-outstanding data
is available per symbol (see fundamental family).
"""
from __future__ import annotations

import pandas as pd

from core.factors import factor_ops as ops

__alpha_meta__ = {
    "id": "academic_smb",
    "family": "academic",
    "theme": "quality",
    "formula_latex": r"z\left(-\log(ts\_mean(close \cdot volume, 60) + 1)\right)",
    "columns_required": ["close", "volume"],
    "universe": "equity_in",
    "frequency": "daily",
    "decay_horizon_days": 60,
    "min_warmup_bars": 60,
    "notes": "[PRICE/VOLUME PROXY, not true SMB] inverse log 60d dollar-volume z-score.",
}


def compute(panel: pd.DataFrame) -> pd.DataFrame:
    close = panel["close"]
    volume = panel["volume"]
    dollar_volume = close * volume
    avg = ops.ts_mean(dollar_volume, 60)
    log_size = ops.log(avg + 1.0)
    return ops.zscore(-log_size)
