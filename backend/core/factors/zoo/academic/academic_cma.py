"""academic_cma — Fama-French (2015) CMA, price-proxy form.

The canonical CMA (Conservative Minus Aggressive) sorts on balance-sheet
asset growth, which we don't have per-symbol in the OHLCV panel. This is
a documented PROXY: negative 60-day change in log average volume.
Firms scaling activity aggressively tend to show rising volume;
conservative firms show flat/shrinking volume. Higher score = volume
contraction (conservative proxy).

Source: Fama, E. F., & French, K. R. (2015), "A Five-Factor Asset
Pricing Model", Journal of Financial Economics. Formula reimplemented
as a price/volume proxy from the published factor definition.

Caveat: this is explicitly a proxy, not the real CMA. It must be IC/IR
tested against NSE data before being trusted for any promotion logic —
volume-growth and true investment-growth are not guaranteed to agree.
"""
from __future__ import annotations

import pandas as pd

from core.factors import factor_ops as ops

__alpha_meta__ = {
    "id": "academic_cma",
    "family": "academic",
    "theme": "quality",
    "formula_latex": r"z\left(-\Delta_{60}\log(ts\_mean(volume, 60) + 1)\right)",
    "columns_required": ["volume"],
    "universe": "equity_in",
    "frequency": "daily",
    "decay_horizon_days": 60,
    "min_warmup_bars": 120,
    "notes": "[PRICE/VOLUME PROXY, not true CMA] inverse 60d log-volume growth z-score.",
}


def compute(panel: pd.DataFrame) -> pd.DataFrame:
    volume = panel["volume"]
    log_avg_vol = ops.log(ops.ts_mean(volume, 60) + 1.0)
    growth = ops.delta(log_avg_vol, 60)
    return ops.zscore(-growth)
