"""fund_gross_profitability — Novy-Marx style gross profitability.

PIT-safe quality factor: gross profit / total assets, cross-sectionally
z-scored. Expects panel["fund:gross_profitability"] pre-computed and
PIT-aligned by our FundamentalStore.

Source: Novy-Marx, R. (2013), "The Other Side of Value: The Gross
Profitability Premium", Journal of Financial Economics. Formula
reimplemented from the published definition.
"""
from __future__ import annotations

import pandas as pd

from core.factors import factor_ops as ops

__alpha_meta__ = {
    "id": "fund_gross_profitability",
    "family": "fundamental",
    "theme": "quality",
    "formula_latex": r"z\left(gross\_profit / total\_assets\right)",
    "columns_required": ["fund:gross_profitability"],
    "universe": "equity_in",
    "frequency": "daily",
    "decay_horizon_days": 60,
    "min_warmup_bars": 1,
    "notes": "PIT-safe gross profitability from FundamentalStore, cross-sectional z-score.",
}


def compute(panel: pd.DataFrame) -> pd.DataFrame:
    if "fund:gross_profitability" not in panel.columns.get_level_values(0):
        return ops.missing_output(panel)
    return ops.zscore(panel["fund:gross_profitability"])
