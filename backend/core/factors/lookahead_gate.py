"""Layer 2 — lookahead gate.

Verifies, empirically, that a factor's compute(panel) output for day t
never changes when data strictly after day t is mutated. This is the
gate that would have caught the "S/R fixed-percentage buffer" and
short-horizon candle-staleness classes of bug if applied to those
functions too — worth reusing beyond just the factor zoo eventually.

Method: run compute() on the real panel, then run it again on a copy
where every row after some cutoff index has been randomly shuffled/
perturbed. If the pre-cutoff output changed, the factor is peeking
forward and fails the gate.
"""

from __future__ import annotations

from typing import Callable

import numpy as np
import pandas as pd

ComputeFn = Callable[[pd.DataFrame], pd.DataFrame]


class LookaheadViolation(Exception):
    pass


def _mutate_future(panel: pd.DataFrame, cutoff: int, seed: int = 0) -> pd.DataFrame:
    """Return a copy of panel with all rows after `cutoff` randomly perturbed."""
    rng = np.random.default_rng(seed)
    mutated = panel.copy(deep=True)
    future = mutated.iloc[cutoff + 1 :]
    if future.empty:
        return mutated
    noise = rng.normal(loc=0.0, scale=future.std(numeric_only=True).fillna(1.0) + 1.0,
                        size=future.shape)
    mutated.iloc[cutoff + 1 :] = future.values + noise
    return mutated


def check_no_lookahead(
    compute_fn: ComputeFn,
    panel: pd.DataFrame,
    cutoff_fraction: float = 0.7,
    atol: float = 1e-9,
) -> None:
    """Raise LookaheadViolation if compute_fn's pre-cutoff output changes
    when post-cutoff rows are mutated.

    Args:
        compute_fn: a factor's compute(panel) -> panel function (or any
            single-argument panel-in/panel-out function under test).
        panel: a real or fixture OHLCV-derived panel to test against.
        cutoff_fraction: where to split "past" vs "future" (0.7 = last
            30% of rows get mutated).
        atol: absolute tolerance for float comparison of the past region.
    """
    n = len(panel)
    if n < 10:
        raise ValueError("panel must have at least 10 rows for a meaningful lookahead check")
    cutoff = int(n * cutoff_fraction)

    baseline = compute_fn(panel)
    mutated_panel = _mutate_future(panel, cutoff)
    mutated_result = compute_fn(mutated_panel)

    past_baseline = baseline.iloc[: cutoff + 1]
    past_mutated = mutated_result.iloc[: cutoff + 1]

    if past_baseline.shape != past_mutated.shape:
        raise LookaheadViolation(
            "output shape changed between baseline and mutated-future run"
        )

    diff = (past_baseline - past_mutated).abs()
    # NaN - NaN = NaN, not 0 — treat matching NaNs as equal, everything else must be <= atol
    both_nan = past_baseline.isna() & past_mutated.isna()
    diff = diff.where(~both_nan, 0.0)

    max_diff = np.nanmax(diff.to_numpy()) if diff.size else 0.0
    if not np.isfinite(max_diff):
        max_diff = 0.0
    if max_diff > atol:
        raise LookaheadViolation(
            f"factor output for the pre-cutoff period changed by up to {max_diff} "
            f"when future (post-cutoff) data was mutated — this factor is peeking forward"
        )
