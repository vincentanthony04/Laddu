"""fund_roe — return on equity, cross-sectionally z-scored.

PIT-safe quality factor. Expects panel["fund:roe"] to already be
point-in-time aligned by our FundamentalStore (i.e. no data from a
quarter that hadn't actually been reported as of that date) — this
factor does no PIT enforcement itself, it trusts the panel.

Source: standard fundamental-analysis definition (net income / equity).
Not vendored from any codebase; the formula is a single division plus
our own zscore operator.
"""
from __future__ import annotations

import pandas as pd

from core.factors import factor_ops as ops

__alpha_meta__ = {
    "id": "fund_roe",
    "family": "fundamental",
    "theme": "quality",
    "formula_latex": r"z\left(ROE\right)",
    "columns_required": ["fund:roe"],
    "universe": "equity_in",
    "frequency": "daily",
    "decay_horizon_days": 60,
    "min_warmup_bars": 1,
    "notes": (
        "PIT-safe ROE from FundamentalStore, cross-sectional z-score. "
        "Missing statements stay NaN and are excluded from that date's cross-section."
    ),
}


def compute(panel: pd.DataFrame) -> pd.DataFrame:
    if "fund:roe" not in panel.columns.get_level_values(0):
        return ops.missing_output(panel)
    return ops.zscore(panel["fund:roe"])
