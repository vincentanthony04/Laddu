"""fund_earnings_yield — net income over market cap, cross-sectionally z-scored.

Hybrid factor: uses PIT-safe net income and diluted shares from our
FundamentalStore, combined with the daily close panel to derive market
cap. Division by zero/NaN market cap becomes NaN before scoring (via
safe_div), never a crash or silent inf.

Source: standard value-investing definition (earnings yield = E/P).
Not vendored from any codebase; formula is a market-cap division plus
our own safe_div/zscore operators.
"""
from __future__ import annotations

import pandas as pd

from core.factors import factor_ops as ops

__alpha_meta__ = {
    "id": "fund_earnings_yield",
    "family": "fundamental",
    "theme": "value",
    "formula_latex": r"z\left(\frac{net\_income}{close \cdot shares\_diluted}\right)",
    "columns_required": ["close", "fund:net_income", "fund:shares_diluted"],
    "universe": "equity_in",
    "frequency": "daily",
    "decay_horizon_days": 60,
    "min_warmup_bars": 1,
    "notes": (
        "Hybrid value factor: PIT-safe net income / shares_diluted from "
        "FundamentalStore, aligned to the daily close panel."
    ),
}


def compute(panel: pd.DataFrame) -> pd.DataFrame:
    available = set(panel.columns.get_level_values(0)) if isinstance(panel.columns, pd.MultiIndex) else set(panel.columns)
    if not {"close", "fund:net_income", "fund:shares_diluted"}.issubset(available):
        return ops.missing_output(panel)
    market_cap = panel["close"] * panel["fund:shares_diluted"]
    earnings_yield = ops.safe_div(panel["fund:net_income"], market_cap)
    return ops.zscore(earnings_yield)
