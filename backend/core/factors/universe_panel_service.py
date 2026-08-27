"""UniversePanelService -- fix_to_be_done #3: real cross-sectional computation.

Problem this closes: research_worker._factor_zoo_panel builds a single-symbol
(one-column) OHLCV panel per analyze_one call. factor_ops.rank/scale/zscore
are correctly implemented as axis=1 (across-symbol) operators, but fed a
1-column frame they always return a constant (rank of the only value in its
row is always 1.0; zscore of one value is always NaN via /0 std). Any zoo
factor built from rank/scale/zscore was therefore mathematically inert when
run through the per-symbol path, not merely unvalidated.

Fix: build one true multi-symbol panel per scan pass (all instruments in the
current batch, same trading calendar, one column per symbol) and run the
cross-sectional-dependent factors against that shared panel exactly once per
pass, instead of once per symbol against a fake 1-column panel.

This module is additive and self-contained -- it does not change analyze_one
or research_worker's existing per-symbol time-series factor path (ts_rank,
ts_mean, etc. are correct with 1 column and are untouched). It gives the
scan-orchestration layer a batch entry point:

    from core.factors.universe_panel_service import UniversePanelService
    svc = UniversePanelService(store)
    cs_scores = svc.compute_cross_sectional(symbol_to_instrument_key, interval="day")
    # cs_scores: {symbol: {factor_id: latest_value}}

research_worker consumes cs_scores via the `cross_sectional_scores` param
added to _factor_zoo_family (see research_worker.py) -- when absent (no
batch context wired in yet for a given call site), cross-sectional-dependent
factors are zero-weighted rather than silently emitting the old constant,
mirroring the project's existing v61.7 zero-weight convention for fake
evidence instead of inventing a new pattern.
"""
from __future__ import annotations

import ast
import glob
import importlib
import os
import time
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Iterable, List

import pandas as pd

CROSS_SECTIONAL_OPS = {"rank", "scale", "zscore"}
ZOO_ROOT = Path(__file__).resolve().parent / "zoo"


@lru_cache(maxsize=1)
def cross_sectional_factor_ids() -> frozenset[str]:
    """Every zoo module (family/name) that calls rank/scale/zscore.

    Detected once via AST (not a text substring match, so e.g. a factor
    named `frankfurter` or a comment mentioning "rank" can't false-positive)
    and cached for process lifetime -- the zoo is static on disk per release.
    """
    hits: set[str] = set()
    for path in glob.glob(str(ZOO_ROOT / "*" / "*.py")):
        family = Path(path).parent.name
        name = Path(path).stem
        try:
            tree = ast.parse(Path(path).read_text(encoding="utf-8"))
        except Exception:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            # covers `rank(x)` (direct import) and `ops.rank(x)` / `factor_ops.rank(x)`
            # (module-qualified call) -- both patterns appear across the zoo.
            called_name = fn.id if isinstance(fn, ast.Name) else fn.attr if isinstance(fn, ast.Attribute) else None
            if called_name in CROSS_SECTIONAL_OPS:
                hits.add(f"{family}.{name}")
                break
    return frozenset(hits)


def build_universe_panel(
    symbol_candles: Dict[str, List[Dict[str, Any]]],
) -> pd.DataFrame:
    """One OHLCV panel across every symbol, aligned on a shared date index.

    symbol_candles: {symbol: [{"timestamp", "open", "high", "low", "close",
    "volume"}, ...]} as already returned by storage.get_candles per symbol.
    Symbols with no candles are skipped (can't contribute a cross-sectional
    column); this is not an error, just a smaller cross-section for that pass.
    """
    frames: Dict[str, pd.DataFrame] = {}
    for symbol, candles in symbol_candles.items():
        if not candles:
            continue
        df = pd.DataFrame(candles)
        if df.empty or "timestamp" not in df.columns:
            continue
        df = df.dropna(subset=["timestamp"]).sort_values("timestamp")
        idx = pd.to_datetime(df["timestamp"], errors="coerce")
        for col in ("open", "high", "low", "close", "volume"):
            if col not in df.columns:
                continue
        frames[symbol.upper()] = df.set_index(idx)

    if not frames:
        return pd.DataFrame()

    panel_cols: Dict[tuple, Any] = {}
    for field in ("open", "high", "low", "close", "volume"):
        for symbol, df in frames.items():
            if field not in df.columns:
                continue
            panel_cols[(field, symbol)] = pd.to_numeric(df[field], errors="coerce")

    if not panel_cols:
        return pd.DataFrame()

    panel = pd.DataFrame(panel_cols)
    panel.columns = pd.MultiIndex.from_tuples(panel.columns)
    panel = panel.sort_index()
    # vwap approximation, same convention as research_worker._factor_zoo_panel
    for symbol in frames:
        needed = [(c, symbol) for c in ("open", "high", "low", "close")]
        if all(c in panel.columns for c in needed):
            panel[("vwap", symbol)] = panel[needed].mean(axis=1)
    return panel.replace([float("inf"), float("-inf")], float("nan"))


class UniversePanelService:
    """Batch cross-sectional factor runner, backed by the existing store."""

    def __init__(self, store):
        self.store = store

    def _fetch_symbol_candles(self, instrument_key: str, interval: str, limit: int) -> List[Dict[str, Any]]:
        try:
            return self.store.get_candles(instrument_key, interval, limit=limit) or []
        except Exception:
            return []

    def compute_cross_sectional(
        self,
        symbol_to_instrument_key: Dict[str, str],
        interval: str = "day",
        limit: int = 400,
        time_budget_sec: float = 45.0,
    ) -> Dict[str, Dict[str, float]]:
        """Returns {symbol: {"family.name": latest_finite_value}} for every
        rank/scale/zscore-based factor, computed once across the whole
        universe passed in -- not per symbol. Bails out (returning whatever
        was computed so far, or {} if none) once time_budget_sec has elapsed,
        so a slow/contended DB can't turn this into an unbounded stall even
        though it now always runs on a background thread (see
        ResearchAdapter.refresh_cross_sectional).
        """
        start = time.monotonic()
        symbol_candles: Dict[str, List[Dict[str, Any]]] = {}
        for sym, key in symbol_to_instrument_key.items():
            if time.monotonic() - start > time_budget_sec:
                break
            symbol_candles[sym] = self._fetch_symbol_candles(key, interval, limit)
        panel = build_universe_panel(symbol_candles)
        if panel.empty or len(panel.columns.get_level_values(1).unique()) < 3:
            # A cross-section needs a real cross-section. Fewer than 3 names
            # isn't enough for rank/zscore to mean anything either -- return
            # empty so callers zero-weight exactly like the no-context case.
            return {}

        out: Dict[str, Dict[str, float]] = {sym: {} for sym in panel.columns.get_level_values(1).unique()}
        for factor_id in cross_sectional_factor_ids():
            if time.monotonic() - start > time_budget_sec:
                break
            family, name = factor_id.split(".", 1)
            try:
                mod = importlib.import_module(f"core.factors.zoo.{family}.{name}")
                result = mod.compute(panel)
            except Exception:
                continue
            if result is None or getattr(result, "empty", True):
                continue
            last_row = result.iloc[-1]
            for sym in out:
                if sym in last_row.index:
                    val = last_row[sym]
                    try:
                        fval = float(val)
                    except (TypeError, ValueError):
                        continue
                    if fval == fval and abs(fval) != float("inf"):  # finite check w/o importing math
                        out[sym][factor_id] = round(fval, 6)
        return out
