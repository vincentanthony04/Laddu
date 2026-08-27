"""fund_asset_growth — inverse asset-growth investment factor.

PIT-safe factor: negative year-over-year total-asset growth, cross-
sectionally z-scored. High asset growth (aggressive investment) scores
lower; low/negative growth (conservative) scores higher. Expects
panel["fund:asset_growth"] pre-computed and PIT-aligned by our
FundamentalStore.

Source: Cooper, M. J., Gulen, H., & Schill, M. J. (2008), "Asset
Growth and the Cross-Section of Stock Returns", Journal of Finance.
Formula reimplemented from the published definition.
"""
from __future__ import annotations

import pandas as pd

from core.factors import factor_ops as ops

__alpha_meta__ = {
    "id": "fund_asset_growth",
    "family": "fundamental",
    "theme": "growth",
    "formula_latex": r"-z\left(\Delta total\_assets_{YoY}\right)",
    "columns_required": ["fund:asset_growth"],
    "universe": "equity_in",
    "frequency": "daily",
    "decay_horizon_days": 60,
    "min_warmup_bars": 1,
    "notes": "PIT-safe inverse asset growth from FundamentalStore, cross-sectional z-score.",
}


def compute(panel: pd.DataFrame) -> pd.DataFrame:
    if "fund:asset_growth" not in panel.columns.get_level_values(0):
        return ops.missing_output(panel)
    return -ops.zscore(panel["fund:asset_growth"])
