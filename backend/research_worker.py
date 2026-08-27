from __future__ import annotations

"""Project Laddu v39.1 research worker.

Runs inside the persistent research venv, not inside the live backend process.
Input: path to JSON file with candles, price snapshots, delivery data, mode.
Output: JSON to stdout with factor rows/evidence/status.

This worker deliberately avoids broker/order APIs. It uses installed research
libraries only for calculations on Laddu's stored evidence.
"""

import json
import math
import os
import statistics
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional


def _num(v: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        if v in (None, "", "—"):
            return default
        n = float(v)
        if math.isfinite(n):
            return n
    except Exception:
        pass
    return default


def _clamp(v: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, float(v)))


def _status_imports() -> Dict[str, Any]:
    """Fast library status check.

    v39.1.0 imported heavy runtimes such as Qlib/Vibe during every adapter
    request, which can take longer than the API timeout on Windows. This status
    check is intentionally metadata/spec based: it proves installation without
    booting agent/model runtimes. Actual calculations below import only the
    small libraries they need.
    """
    out: Dict[str, Any] = {}
    specs = {
        "pandas": ("pandas", "pandas"),
        "numpy": ("numpy", "numpy"),
        "ta": ("ta", "ta"),
        "pandas_ta_classic": ("pandas_ta_classic", "pandas-ta-classic"),
        "talib": ("talib", "TA-Lib"),
        "backtesting": ("backtesting", "backtesting"),
        "qlib": ("qlib", "pyqlib"),
        "statsmodels": ("statsmodels", "statsmodels"),
        "arch": ("arch", "arch"),
        "skfolio": ("skfolio", "skfolio"),
        "smartmoneyconcepts": ("smartmoneyconcepts", "smartmoneyconcepts"),
        "duckdb": ("duckdb", "duckdb"),
        "lightgbm": ("lightgbm", "lightgbm"),
        "pyarrow": ("pyarrow", "pyarrow"),
    }
    import importlib.metadata
    import importlib.util
    for label, (mod_name, dist_name) in specs.items():
        version = None
        try:
            version = importlib.metadata.version(dist_name)
        except Exception:
            version = None
        spec_ok = True
        if mod_name:
            try:
                spec_ok = importlib.util.find_spec(mod_name) is not None
            except Exception:
                spec_ok = False
        else:
            # Some distributions (for example vibe-trading-ai) expose several
            # console/workspace modules and do not have a stable import name.
            spec_ok = version is not None
        if version or spec_ok:
            out[label] = {"available": True, "version": str(version) if version else None, "check": "metadata/spec-no-heavy-import"}
        else:
            out[label] = {"available": False, "reason": "not installed or module spec missing"}
    return out


def _make_df(payload: Dict[str, Any]):
    import pandas as pd
    rows = payload.get("candles") or []
    clean = []
    for r in rows:
        o = _num(r.get("open")); h = _num(r.get("high")); l = _num(r.get("low")); c = _num(r.get("close")); v = _num(r.get("volume"), 0.0)
        if None in (o, h, l, c):
            continue
        clean.append({
            "timestamp": r.get("timestamp") or r.get("ts") or r.get("time") or r.get("date"),
            "open": o, "high": h, "low": l, "close": c, "volume": v or 0.0,
        })
    df = pd.DataFrame(clean)
    if not df.empty:
        df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
        df = df.dropna(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)
    return df


def _factor(name: str, raw: Any, score: float, weight: float, status: str, explanation: str, library: str) -> Dict[str, Any]:
    score = _clamp(score)
    return {
        "factor_name": name,
        "raw_value": raw,
        "normalized_score": round(score, 2),
        "weight": float(weight),
        "contribution": round(score * float(weight) / 100.0, 2),
        "status": status,
        "explanation": explanation,
        "library": library,
    }


def _ta_factors(df, mode: str) -> List[Dict[str, Any]]:
    factors: List[Dict[str, Any]] = []
    if df.empty or len(df) < 20:
        return [_factor("TA library coverage", f"{len(df)} candles", 15, 8, "fail", "Research adapter needs at least 20 candles for indicator use.", "pandas/ta")]
    import pandas as pd
    try:
        from ta.momentum import RSIIndicator, StochasticOscillator
        from ta.trend import ADXIndicator, MACD, EMAIndicator
        from ta.volatility import AverageTrueRange, BollingerBands
    except Exception as exc:
        # Research libraries are optional. A missing TA package must not block
        # the local factor zoo / evidence ledger from running.
        close_fb = df["close"].astype(float)
        ret20 = None
        try:
            if len(close_fb) >= 21 and float(close_fb.iloc[-21]) != 0:
                ret20 = (float(close_fb.iloc[-1]) - float(close_fb.iloc[-21])) / float(close_fb.iloc[-21]) * 100.0
        except Exception:
            ret20 = None
        score = 65 if ret20 is not None and ret20 > 0 else 45 if ret20 is not None else 20
        return [_factor("TA library coverage", {"ta_import": "missing", "fallback_ret20_pct": None if ret20 is None else round(ret20, 3), "error": str(exc).splitlines()[0][:120]}, score, 4, "fallback", "ta package missing; used a small pandas fallback and continued local factor-zoo evidence instead of failing Stock Intelligence.", "pandas fallback")]

    close = df["close"].astype(float)
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    volume = df["volume"].astype(float)
    latest_close = float(close.iloc[-1])

    def last(series, default=None):
        try:
            x = series.dropna().iloc[-1]
            return float(x)
        except Exception:
            return default

    rsi = last(RSIIndicator(close=close, window=14).rsi())
    rsi_score = 80 if rsi is not None and 45 <= rsi <= 65 else 62 if rsi is not None and 35 <= rsi <= 72 else 35 if rsi is not None else 20
    factors.append(_factor("TA: RSI(14)", None if rsi is None else round(rsi, 2), rsi_score, 7, "ok" if rsi_score >= 70 else "watch", "RSI from ta library; healthy momentum scores higher without exhaustion.", "ta"))

    ema20 = last(EMAIndicator(close=close, window=20).ema_indicator())
    ema50 = last(EMAIndicator(close=close, window=50).ema_indicator()) if len(close) >= 50 else None
    ema_score = 78 if ema20 and ema50 and latest_close > ema20 > ema50 else 62 if ema20 and latest_close > ema20 else 35 if ema20 else 20
    factors.append(_factor("TA: EMA trend", {"close": round(latest_close, 2), "ema20": None if ema20 is None else round(ema20, 2), "ema50": None if ema50 is None else round(ema50, 2)}, ema_score, 9, "ok" if ema_score >= 70 else "watch", "EMA alignment from ta library; close above EMA20/50 supports long bias.", "ta"))

    macd_obj = MACD(close=close)
    macd_hist = last(macd_obj.macd_diff())
    macd_score = 76 if macd_hist is not None and macd_hist > 0 else 42 if macd_hist is not None else 20
    factors.append(_factor("TA: MACD histogram", None if macd_hist is None else round(macd_hist, 4), macd_score, 7, "ok" if macd_score >= 70 else "watch", "MACD histogram from ta library confirms or weakens direction.", "ta"))

    adx = last(ADXIndicator(high=high, low=low, close=close, window=14).adx())
    adx_score = 80 if adx is not None and adx >= 22 else 56 if adx is not None and adx >= 15 else 35 if adx is not None else 20
    factors.append(_factor("TA: ADX trend strength", None if adx is None else round(adx, 2), adx_score, 7, "ok" if adx_score >= 70 else "watch", "ADX from ta library; weak trend strength blocks high conviction.", "ta"))

    atr = last(AverageTrueRange(high=high, low=low, close=close, window=14).average_true_range())
    atr_pct = (atr / latest_close * 100.0) if atr and latest_close else None
    atr_score = 72 if atr_pct is not None and 0.4 <= atr_pct <= 4.5 else 52 if atr_pct is not None else 20
    factors.append(_factor("TA: ATR volatility", None if atr_pct is None else round(atr_pct, 2), atr_score, 5, "ok" if atr_score >= 65 else "watch", "ATR percent from ta library; too low or too high volatility reduces setup quality.", "ta"))

    bb = BollingerBands(close=close, window=20, window_dev=2)
    bb_hi = last(bb.bollinger_hband()); bb_lo = last(bb.bollinger_lband())
    bb_pos = None
    if bb_hi and bb_lo and bb_hi != bb_lo:
        bb_pos = (latest_close - bb_lo) / (bb_hi - bb_lo)
    bb_score = 74 if bb_pos is not None and 0.35 <= bb_pos <= 0.82 else 55 if bb_pos is not None else 20
    factors.append(_factor("TA: Bollinger position", None if bb_pos is None else round(bb_pos, 3), bb_score, 4, "ok" if bb_score >= 70 else "watch", "Bollinger band location from ta library; avoids chasing extremes.", "ta"))

    vol_ma = float(volume.tail(20).mean()) if len(volume) >= 20 else None
    vol_ratio = float(volume.iloc[-1] / vol_ma) if vol_ma and vol_ma > 0 else None
    vol_score = 84 if vol_ratio is not None and vol_ratio >= 1.5 else 68 if vol_ratio is not None and vol_ratio >= 1.0 else 42 if vol_ratio is not None else 20
    factors.append(_factor("TA: Volume expansion", None if vol_ratio is None else round(vol_ratio, 2), vol_score, 8, "ok" if vol_score >= 70 else "watch", "Volume ratio from stored candles; confirms or weakens breakout quality.", "pandas"))
    return factors


def _pandas_ta_classic_factors(df) -> List[Dict[str, Any]]:
    factors: List[Dict[str, Any]] = []
    if df.empty or len(df) < 20:
        return factors
    try:
        import pandas_ta_classic as pta
        close = df["close"].astype(float)
        mom = None; sma = None
        try:
            mom_series = pta.mom(close, length=10)
            mom = _num(mom_series.dropna().iloc[-1]) if mom_series is not None and not mom_series.dropna().empty else None
        except Exception:
            mom = _num(close.iloc[-1] - close.iloc[-11]) if len(close) > 11 else None
        try:
            sma_series = pta.sma(close, length=20)
            sma = _num(sma_series.dropna().iloc[-1]) if sma_series is not None and not sma_series.dropna().empty else None
        except Exception:
            sma = _num(close.tail(20).mean())
        last_close = _num(close.iloc[-1])
        score = 76 if mom is not None and mom > 0 and last_close and sma and last_close > sma else 55 if mom is not None else 20
        factors.append(_factor("pandas-ta-classic: momentum/SMA", {"mom10": None if mom is None else round(mom, 3), "sma20": None if sma is None else round(sma, 2)}, score, 6, "ok" if score >= 70 else "watch", "pandas-ta-classic imported and calculated momentum/SMA on Laddu stored candles.", "pandas-ta-classic"))
    except Exception as exc:
        factors.append(_factor("pandas-ta-classic: unavailable", str(exc).splitlines()[0][:120], 20, 2, "missing", "pandas-ta-classic adapter failed; visible as a calculation warning.", "pandas-ta-classic"))
    return factors


def _price_snapshot_factors(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows = payload.get("price_snapshots") or []
    vals = []
    for r in reversed(rows):
        n = _num(r.get("ltp"))
        if n is not None:
            vals.append(n)
    if not vals:
        return [_factor("Price path: all snapshots", "0 snapshots", 0, 10, "fail", "No stored quote/price snapshots. Candle-only proof is not allowed.", "Laddu price_snapshots")]
    if len(vals) == 1:
        return [_factor("Price path: all snapshots", "1 snapshot", 45, 10, "thin", "Only one price snapshot; replay path is too thin.", "Laddu price_snapshots")]
    ret = (vals[-1] - vals[0]) / vals[0] * 100 if vals[0] else 0
    volatility = statistics.pstdev(vals) / statistics.mean(vals) * 100 if len(vals) >= 3 and statistics.mean(vals) else 0
    score = 76 if ret > 0 and volatility < 4 else 62 if abs(ret) <= 2 else 45
    return [_factor("Price path: all snapshots", {"count": len(vals), "path_return_pct": round(ret, 3), "path_volatility_pct": round(volatility, 3)}, score, 10, "ok" if len(vals) >= 5 else "thin", "Uses all stored LTP snapshots, not just candles, to prove price direction/path.", "Laddu price_snapshots")]


def _delivery_factor(payload: Dict[str, Any], mode: str) -> List[Dict[str, Any]]:
    if mode != "delivery":
        return []
    rows = payload.get("delivery_data") or []
    pcts = [_num(r.get("delivery_pct")) for r in rows]
    pcts = [x for x in pcts if x is not None]
    if not pcts:
        return [_factor("NSE delivery accumulation", "missing", 20, 8, "missing", "Delivery research needs NSE delivery data.", "Laddu delivery_data")]
    latest = pcts[0]
    avg = statistics.mean(pcts)
    score = 78 if latest >= avg else 55
    return [_factor("NSE delivery accumulation", {"latest_pct": round(latest, 2), "avg_pct": round(avg, 2), "rows": len(pcts)}, score, 8, "ok" if score >= 70 else "watch", "Delivery percentage compared with recent average from stored NSE delivery rows.", "Laddu delivery_data")]


def _structure_factor(df) -> List[Dict[str, Any]]:
    if df.empty or len(df) < 8:
        return []
    highs = list(df["high"].tail(8).astype(float))
    lows = list(df["low"].tail(8).astype(float))
    last_higher_high = highs[-1] > max(highs[:-1]) if len(highs) > 1 else False
    higher_low = lows[-1] > min(lows[:-3]) if len(lows) > 4 else False
    score = 78 if last_higher_high and higher_low else 62 if last_higher_high or higher_low else 42
    return [_factor("SMC-lite: structure", {"break_above_recent_high": last_higher_high, "higher_low_context": higher_low}, score, 6, "ok" if score >= 70 else "watch", "Smart-money-style structure check on stored candles. Uses internal SMC-lite logic; smartmoneyconcepts package availability is reported separately.", "SMC-lite")]


def _backtesting_validation_status(df, mode: str) -> Dict[str, Any]:
    """Describe the independent replay responsibility without creating a factor.

    Backtesting results belong to the finite model/strategy tournament and are
    never converted into live evidence points.
    """
    installed = False
    reason = None
    try:
        import importlib.util
        installed = importlib.util.find_spec("backtesting") is not None
    except Exception as exc:
        reason = str(exc).splitlines()[0][:120]
    candles = int(len(df))
    if not installed:
        state = "DEPENDENCY_MISSING"
    elif candles < 60:
        state = "INSUFFICIENT_REPLAY_SAMPLE"
    else:
        state = "ACTIVE_VALIDATION_READY"
    return {
        "library": "backtesting.py",
        "lifecycle_state": "ACTIVE_VALIDATION",
        "state": state,
        "mode": mode,
        "candles_available": candles,
        "responsibility": "independent signal/replay reconciliation only",
        "production_influence": False,
        "reason": reason,
    }


def _backtesting_factor(df, mode: str) -> List[Dict[str, Any]]:
    """Compatibility alias: validation dependencies are not score factors."""
    return []



def _factor_zoo_panel(df, symbol: str):
    """Build the OHLCV MultiIndex panel expected by Laddu's local factor zoo.

    The factor platform was ported from Vibe/Alpha/Qlib/GTJA families to run
    on India OHLCV-only data. This adapter keeps it inside the research worker
    so the live backend still cannot be slowed/crashed by heavy factor imports.
    """
    import pandas as pd
    if df.empty:
        return pd.DataFrame()
    sym = (symbol or "SYMBOL").upper()
    work = df.copy()
    if "timestamp" in work.columns:
        work = work.dropna(subset=["timestamp"]).sort_values("timestamp")
        idx = pd.to_datetime(work["timestamp"], errors="coerce")
    else:
        idx = work.index
    data = {}
    for col in ("open", "high", "low", "close", "volume"):
        if col not in work.columns:
            return pd.DataFrame()
        data[(col, sym)] = pd.to_numeric(work[col], errors="coerce").astype(float).to_numpy()
    # Vibe/Qlib/GTJA ports are OHLCV-safe. vwap is optional but many formulas
    # use it if present; create an India-safe typical-price approximation.
    data[("vwap", sym)] = (
        pd.to_numeric(work["open"], errors="coerce")
        + pd.to_numeric(work["high"], errors="coerce")
        + pd.to_numeric(work["low"], errors="coerce")
        + pd.to_numeric(work["close"], errors="coerce")
    ).astype(float).to_numpy() / 4.0
    panel = pd.DataFrame(data, index=idx)
    panel.columns = pd.MultiIndex.from_tuples(panel.columns)
    return panel.replace([float("inf"), float("-inf")], float("nan"))


def _latest_finite(result, symbol: str):
    import math
    import pandas as pd
    if result is None or getattr(result, "empty", True):
        return None
    try:
        sym = (symbol or "SYMBOL").upper()
        if sym in getattr(result, "columns", []):
            series = pd.to_numeric(result[sym], errors="coerce").dropna()
            if not series.empty:
                val = float(series.iloc[-1])
                return val if math.isfinite(val) else None
        # Fallback for any formula that returns unnamed/single-column output.
        row = pd.to_numeric(result.iloc[-1], errors="coerce").dropna()
        if not row.empty:
            val = float(row.iloc[-1])
            return val if math.isfinite(val) else None
    except Exception:
        return None
    return None


def _family_signal_score(values: List[float]) -> float:
    """Small, bounded score for uncalibrated factor-family evidence.

    These ported factors are useful evidence, but they are not allowed to
    dominate a live decision until IC/IR validation on our own NSE universe.
    Score therefore reflects only the latest sign breadth, not a blind raw sum.
    """
    finite = [float(v) for v in values if v is not None and math.isfinite(float(v))]
    if not finite:
        return 20.0
    signed = [1 if v > 0 else -1 if v < 0 else 0 for v in finite]
    active = [x for x in signed if x != 0]
    if not active:
        return 50.0
    pos_ratio = active.count(1) / len(active)
    return _clamp(50.0 + (pos_ratio - 0.5) * 40.0, 25.0, 75.0)


def _factor_zoo_family(df, payload: Dict[str, Any], family: str, file_prefix: str, label: str, weight: float, cross_sectional_scores: Dict[str, Dict[str, float]] | None = None) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Run one local Vibe-style factor family and return compact ledger rows.

    Returns a single compact factor row per family plus evidence metadata. Full
    per-factor rows would overload Stock Intelligence, so samples stay inside
    evidence while the visible decision remains compact.
    """
    if df.empty or len(df) < 80:
        return [
            _factor(f"{label}: local factor zoo", f"{len(df)} candles", 20, weight, "thin", "Needs at least 80 stored candles to run the local factor-zoo safely.", "Laddu factor zoo")
        ], {"available": 0, "finite_latest": 0, "status": "thin"}
    try:
        import glob
        import importlib
        import os
        import time
        import numpy as np
    except Exception as exc:
        return [_factor(f"{label}: local factor zoo", str(exc)[:120], 20, weight, "missing", "Python import support missing for local factor-zoo execution.", "Laddu factor zoo")], {"available": 0, "finite_latest": 0, "status": "missing"}

    symbol = str(payload.get("symbol") or "SYMBOL").upper()
    panel = _factor_zoo_panel(df, symbol)
    if panel.empty:
        return [_factor(f"{label}: local factor zoo", "panel empty", 20, weight, "fail", "Could not build OHLCV panel for local factor-zoo execution.", "Laddu factor zoo")], {"available": 0, "finite_latest": 0, "status": "fail"}

    from core.factors.universe_panel_service import cross_sectional_factor_ids
    cs_ids = cross_sectional_factor_ids()
    cs_scores_for_symbol = (cross_sectional_scores or {}).get(symbol, {})

    zoo_dir = Path(__file__).resolve().parent / "core" / "factors" / "zoo" / family
    files = sorted(glob.glob(str(zoo_dir / f"{file_prefix}_*.py")))
    values: List[float] = []
    samples: List[Dict[str, Any]] = []
    errors: List[str] = []
    cs_excluded = 0
    cs_used_real = 0
    start = time.perf_counter()
    for path in files:
        name = os.path.basename(path)[:-3]
        factor_id = f"{family}.{name}"
        is_cross_sectional = factor_id in cs_ids
        if is_cross_sectional:
            # fix_to_be_done #3: rank/scale/zscore-based factors are meaningless
            # against the single-symbol `panel` built above (always a constant --
            # see core/factors/universe_panel_service.py docstring). Only trust a
            # value computed by UniversePanelService against a real multi-symbol
            # universe panel for this scan pass; otherwise exclude, don't fake it.
            if factor_id in cs_scores_for_symbol:
                fval = cs_scores_for_symbol[factor_id]
                values.append(fval)
                cs_used_real += 1
                if len(samples) < 8:
                    samples.append({"id": name, "theme": "cross_sectional", "latest": round(fval, 6)})
            else:
                cs_excluded += 1
            continue
        try:
            mod = importlib.import_module(f"core.factors.zoo.{family}.{name}")
            val = _latest_finite(mod.compute(panel), symbol)
            if val is not None and math.isfinite(float(val)):
                fval = float(val)
                values.append(fval)
                if len(samples) < 8:
                    meta = getattr(mod, "__alpha_meta__", {}) or {}
                    samples.append({
                        "id": meta.get("id") or name,
                        "theme": meta.get("theme"),
                        "latest": round(fval, 6),
                    })
        except Exception as exc:
            if len(errors) < 5:
                errors.append(f"{name}: {str(exc).splitlines()[0][:120]}")
    elapsed_ms = round((time.perf_counter() - start) * 1000.0, 1)
    total = len(files)
    finite_count = len(values)
    score = _family_signal_score(values)
    status = "ok" if finite_count else "fail"
    raw = {
        "available": total,
        "finite_latest": finite_count,
        "cross_sectional_excluded": cs_excluded,
        "cross_sectional_scored_from_universe_panel": cs_used_real,
        "sample": samples,
        "errors_sample": errors,
        "elapsed_ms": elapsed_ms,
        "data": "India OHLCV-only; no sector tags; no amount dependency",
    }
    explanation = (
        f"Computed {finite_count}/{total} {label} factors on Laddu stored OHLCV. "
        "Used as research/evidence; live promotion remains gated by Laddu decision rules and future NSE IC/IR validation."
    )
    return [_factor(f"{label}: local factor zoo", raw, score, weight, status, explanation, "Laddu factor zoo")], raw


def _local_factor_zoo_factors(df, payload: Dict[str, Any], mode: str) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Run Alpha101 + Qlib158 + GTJA191 ported families inside Stock Intelligence evidence."""
    if str(os.environ.get("PROJECT_LADDU_FACTOR_ZOO_DISABLE") or "").strip() == "1":
        return [], {"disabled": True}
    # Keep the factor families low-weight in same-day modes because many are
    # daily-style alphas. They still appear in the Calculation Log as evidence.
    m = str(mode or "").lower()
    weight = 2.0 if m == "intraday" else 4.0
    out: List[Dict[str, Any]] = []
    evidence: Dict[str, Any] = {}
    cross_sectional_scores = payload.get("cross_sectional_scores")
    for family, prefix, label in (
        ("alpha101", "alpha101", "Alpha101"),
        ("qlib158", "qlib158", "Qlib158"),
        ("gtja191", "gtja191", "GTJA191"),
    ):
        rows, ev = _factor_zoo_family(df, payload, family, prefix, label, weight, cross_sectional_scores)
        out.extend(rows)
        evidence[family] = ev
    return out, evidence

def _research_tournament_runtime(imports: Dict[str, Any]) -> Dict[str, Any]:
    responsibilities = {
        "qlib": "Parquet-backed NSE dataset/experiment workflow",
        "lightgbm": "cross-sectional ranking and calibrated horizon model",
        "talib": "technical feature parity and ablation",
        "statsmodels": "regime, robust and quantile model candidates",
        "arch": "volatility, tail and multiple-testing governance",
        "skfolio": "desk-aware allocation and risk-budget candidate",
        "backtesting": "independent replay reconciliation",
        "smartmoneyconcepts": "finite structure-feature ablation",
    }
    rows = []
    for key, responsibility in responsibilities.items():
        status = imports.get(key) or {}
        rows.append({
            "library_key": key,
            "lifecycle_state": "ACTIVE_VALIDATION",
            "dependency_state": "INSTALLED" if status.get("available") else "MISSING",
            "version": status.get("version"),
            "responsibility": responsibility,
            "production_influence": False,
            "decision": "PROMOTE_WITH_POSITIVE_WEIGHT_OR_REJECT",
        })
    rows.append({
        "library_key": "vibe_trading_ai",
        "lifecycle_state": "REMOVED",
        "dependency_state": "NOT_REQUIRED",
        "responsibility": None,
        "production_influence": False,
        "decision": "REMOVED_NO_UNIQUE_MEASURABLE_ROLE",
    })
    return {"libraries": rows, "no_shadow_rule": True}


def _qlib_vibe_evidence(imports: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Compatibility alias: package status is never emitted as evidence."""
    return []



def run(payload: Dict[str, Any]) -> Dict[str, Any]:
    imports = _status_imports()
    df = _make_df(payload)
    mode = str(payload.get("mode") or "delivery").lower()
    if mode not in ("intraday", "delivery"):
        return {"ok": False, "status": "blocked", "error": "unsupported_production_mode", "mode": mode, "allowed_modes": ["intraday", "delivery"]}
    factors: List[Dict[str, Any]] = []
    factors.extend(_ta_factors(df, mode))
    factors.extend(_pandas_ta_classic_factors(df))
    factors.extend(_price_snapshot_factors(payload))
    factors.extend(_delivery_factor(payload, mode))
    factors.extend(_structure_factor(df))
    factor_zoo_rows, factor_zoo_evidence = _local_factor_zoo_factors(df, payload, mode)
    factors.extend(factor_zoo_rows)

    status = "ok" if factors else "empty"
    score = sum((_num(f.get("contribution"), 0.0) or 0.0) for f in factors)
    evidence = {
        "candles_used": int(len(df)),
        "price_snapshots_used": int(len(payload.get("price_snapshots") or [])),
        "delivery_rows_used": int(len(payload.get("delivery_data") or [])),
        "libraries": imports,
        "model_tournament_runtime": _research_tournament_runtime(imports),
        "backtesting_validation": _backtesting_validation_status(df, mode),
        "local_factor_zoo": factor_zoo_evidence,
    }
    return {
        "ok": True,
        "status": status,
        "source": "research_venv_subprocess",
        "symbol": payload.get("symbol"),
        "mode": mode,
        "score_contribution": round(score, 2),
        "factors": factors,
        "evidence": evidence,
        "notes": [
            "Research adapter used stored candles + all price snapshots + delivery data.",
            "Local Vibe-style factor zoo integrated: Alpha101 + Qlib158 + GTJA191, OHLCV-only, no sector-tags required.",
            "Qlib/Vibe are runtime adapters/evidence; live broker/agent execution is disabled.",
        ],
    }


def main() -> int:
    if len(sys.argv) < 2:
        print(json.dumps({"ok": False, "error": "missing input json path"}))
        return 2
    try:
        payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
        print(json.dumps(run(payload), default=str))
        return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc).splitlines()[0][:300]}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
