from __future__ import annotations
from typing import List, Dict, Any, Optional
import math

from core.indicator_snapshot_authority import DEFAULT_INDICATOR_SNAPSHOT_AUTHORITY, ema as canonical_ema, rsi_series as canonical_rsi_series


def _num(x):
    try:
        if x is None or isinstance(x, bool):
            return None
        if isinstance(x, str) and not x.strip():
            return None
        out = float(x)
        return out if math.isfinite(out) else None
    except Exception:
        return None

def _finite_series(values: List[float]) -> Optional[List[float]]:
    out: List[float] = []
    for value in values or []:
        parsed = _num(value)
        if parsed is None:
            return None
        out.append(parsed)
    return out


def closes(candles: List[Dict[str, Any]]) -> List[float]:
    out = []
    for c in candles:
        v = _num(c.get("close"))
        if v is not None:
            out.append(v)
    return out


def ema(values: List[float], period: int) -> Optional[float]:
    clean = _finite_series(values)
    return canonical_ema(clean, period) if clean is not None else None

def sma(values: List[float], period: int) -> Optional[float]:
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def rsi(values: List[float], period: int = 14) -> Optional[float]:
    clean = _finite_series(values)
    if clean is None:
        return None
    series = canonical_rsi_series(clean, period)
    value = series[-1] if series else None
    return round(float(value), 2) if value is not None else None

def macd(values: List[float]) -> Dict[str, Optional[float]]:
    # Canonical snapshot owns MACD seed/smoothing semantics.
    rows = [{"open": v, "high": v, "low": v, "close": v, "volume": 1.0} for v in values]
    metrics = (DEFAULT_INDICATOR_SNAPSHOT_AUTHORITY.calculate(rows).get("metrics") or {})
    m, sig, hist = metrics.get("macd"), metrics.get("macd_signal"), metrics.get("macd_hist")
    return {"macd": round(float(m), 4) if m is not None else None, "signal": round(float(sig), 4) if sig is not None else None, "hist": round(float(hist), 4) if hist is not None else None}

def wilder_directional_series(candles: List[Dict[str, Any]], period: int = 14) -> List[Dict[str, Optional[float]]]:
    # Compatibility-only view generated from the canonical authority. There is
    # no independent DMI/ADX implementation here.
    out: List[Dict[str, Optional[float]]] = []
    for end in range(1, len(candles) + 1):
        metrics = (DEFAULT_INDICATOR_SNAPSHOT_AUTHORITY.calculate(candles[:end]).get("metrics") or {})
        if metrics.get("atr14") is None and metrics.get("adx14") is None:
            continue
        plus_di, minus_di, adx_value = metrics.get("plus_di14"), metrics.get("minus_di14"), metrics.get("adx14")
        dx = None
        if plus_di is not None and minus_di is not None:
            denom = float(plus_di) + float(minus_di)
            dx = 100.0 * abs(float(plus_di) - float(minus_di)) / denom if denom else 0.0
        out.append({
            "atr": metrics.get("atr14"), "plus_di": plus_di, "minus_di": minus_di,
            "dx": dx, "adx": adx_value,
        })
    return out


def wilder_directional_metrics(candles: List[Dict[str, Any]], period: int = 14) -> Dict[str, Optional[float]]:
    # Production is standardized on period=14. Other periods are not silently
    # relabelled as ADX14/ATR14.
    if period != 14:
        return {"atr": None, "adx": None, "plus_di": None, "minus_di": None, "adx_change": None, "regime": "UNAVAILABLE"}
    latest = (DEFAULT_INDICATOR_SNAPSHOT_AUTHORITY.calculate(candles).get("metrics") or {})
    previous = (DEFAULT_INDICATOR_SNAPSHOT_AUTHORITY.calculate(candles[:-1]).get("metrics") or {}) if len(candles) > 1 else {}
    atr_value, adx_value = latest.get("atr14"), latest.get("adx14")
    plus_di, minus_di = latest.get("plus_di14"), latest.get("minus_di14")
    previous_adx = previous.get("adx14")
    adx_change = (float(adx_value) - float(previous_adx)) if adx_value is not None and previous_adx is not None else None
    if adx_value is None:
        regime = "FORMING" if atr_value is not None else "UNAVAILABLE"
    elif float(adx_value) < 20:
        regime = "RANGE"
    elif plus_di is not None and minus_di is not None and float(plus_di) > float(minus_di):
        regime = "TRENDING_BULLISH" if float(adx_value) >= 25 else "EMERGING_BULLISH"
    elif plus_di is not None and minus_di is not None and float(minus_di) > float(plus_di):
        regime = "TRENDING_BEARISH" if float(adx_value) >= 25 else "EMERGING_BEARISH"
    else:
        regime = "DIRECTIONALLY_NEUTRAL"
    return {
        "atr": round(float(atr_value), 4) if atr_value is not None else None,
        "adx": round(float(adx_value), 2) if adx_value is not None else None,
        "plus_di": round(float(plus_di), 2) if plus_di is not None else None,
        "minus_di": round(float(minus_di), 2) if minus_di is not None else None,
        "adx_change": round(float(adx_change), 2) if adx_change is not None else None,
        "regime": regime,
    }


def atr(candles: List[Dict[str, Any]], period: int = 14) -> Optional[float]:
    return wilder_directional_metrics(candles, period).get("atr")


def adx(candles: List[Dict[str, Any]], period: int = 14) -> Optional[float]:
    return wilder_directional_metrics(candles, period).get("adx")


def _infer_bar_seconds(candles: List[Dict[str, Any]]) -> Optional[float]:
    """Infer the selected candle interval without trusting UI labels."""
    try:
        from market_layers import candle_datetime
        stamps = []
        for row in candles[-80:]:
            dt = candle_datetime(row.get("timestamp") or row.get("time") or row.get("date"))
            if dt is not None:
                stamps.append(dt)
        diffs = sorted((b-a).total_seconds() for a,b in zip(stamps, stamps[1:]) if (b-a).total_seconds() > 0)
        return diffs[len(diffs)//2] if diffs else None
    except Exception:
        return None


def _sr_lookback_for_selected_timeframe(candles: List[Dict[str, Any]], requested: int) -> int:
    """Use enough selected-timeframe history to make S/R structurally meaningful.

    A fixed 120 bars is only ~2 hours on 1m but ~6 months on 1D.  That was
    the source of visibly inconsistent S/R across timeframe changes.
    """
    sec = _infer_bar_seconds(candles)
    if sec is None:
        return max(120, requested)
    if sec <= 90: return max(390, requested)       # 1m: a full NSE cash session
    if sec <= 240: return max(260, requested)      # 3m
    if sec <= 420: return max(220, requested)      # 5m
    if sec <= 1200: return max(180, requested)     # 15m
    if sec <= 2400: return max(150, requested)     # 30m
    if sec <= 5400: return max(140, requested)     # 1H
    if sec <= 18000: return max(110, requested)    # 4H
    if sec <= 129600: return max(180, requested)   # 1D
    if sec <= 900000: return max(104, requested)   # 1W
    return max(60, requested)                      # 1M


def support_resistance(candles: List[Dict[str, Any]], lookback: int = 40, interval: str | None = None) -> Dict[str, Optional[float]]:
    """Canonical selected-timeframe S/R using one ranked structural authority.

    The window is now timeframe-aware.  If the ranked authority cannot validate
    a level, the nearest provisional structural pivot is returned as provisional
    evidence; raw period min/max is no longer promoted as if it were canonical S/R.
    """
    if len(candles) < 5:
        return {"support": None, "resistance": None, "method": "insufficient history"}
    if interval is not None:
        try:
            from core.market_level_service import compute_levels_from_candles
            canonical = compute_levels_from_candles(candles, interval=interval)
            return {
                "support": canonical.get("support"),
                "resistance": canonical.get("resistance"),
                "support_validated": canonical.get("support") is not None,
                "resistance_validated": canonical.get("resistance") is not None,
                "support_levels": list(canonical.get("support_levels") or []),
                "resistance_levels": list(canonical.get("resistance_levels") or []),
                "provisional_support": list(canonical.get("provisional_support") or []),
                "provisional_resistance": list(canonical.get("provisional_resistance") or []),
                "major_support": canonical.get("major_support_evidence"),
                "major_resistance": canonical.get("major_resistance_evidence"),
                "level_report": canonical.get("level_report") or {},
                "method": canonical.get("method") or "canonical market level authority",
                "authority_version": canonical.get("version"),
            }
        except Exception:
            return {
                "support": None, "resistance": None,
                "support_validated": False, "resistance_validated": False,
                "support_levels": [], "resistance_levels": [],
                "provisional_support": [], "provisional_resistance": [],
                "major_support": None, "major_resistance": None,
                "level_report": {"ok": False, "reason": "canonical S/R unavailable"},
                "method": "canonical S/R unavailable",
            }
    try:
        from market_layers import support_resistance_levels
        effective = min(len(candles), _sr_lookback_for_selected_timeframe(candles, lookback))
        sr = support_resistance_levels(candles, lookback=effective)
        if sr.get("ok"):
            sup = sr.get("nearest_support")
            res = sr.get("nearest_resistance")
            provisional_sup = list(sr.get("provisional_support") or [])
            provisional_res = list(sr.get("provisional_resistance") or [])
            if not sup and provisional_sup:
                sup = max((x for x in provisional_sup if _num(x.get("price")) is not None), key=lambda x: _num(x.get("price")), default=None)
            if not res and provisional_res:
                res = min((x for x in provisional_res if _num(x.get("price")) is not None), key=lambda x: _num(x.get("price")), default=None)
            return {
                "support": sup["price"] if sup else None,
                "resistance": res["price"] if res else None,
                "support_validated": bool(sr.get("nearest_support")),
                "resistance_validated": bool(sr.get("nearest_resistance")),
                "support_levels": list(sr.get("support") or []),
                "resistance_levels": list(sr.get("resistance") or []),
                "provisional_support": provisional_sup,
                "provisional_resistance": provisional_res,
                "major_support": sr.get("major_support"),
                "major_resistance": sr.get("major_resistance"),
                "level_report": sr,
                "method": f"{sr.get('method') or 'ranked structural'}; timeframe-aware {effective} bars",
            }
    except Exception:
        pass
    return {
        "support": None, "resistance": None,
        "support_validated": False, "resistance_validated": False,
        "support_levels": [], "resistance_levels": [],
        "provisional_support": [], "provisional_resistance": [],
        "major_support": None, "major_resistance": None,
        "level_report": {"ok": False, "reason": "ranked S/R unavailable"},
        "method": "ranked S/R unavailable",
    }


def vwap(candles: List[Dict[str, Any]]) -> Optional[float]:
    # Compatibility projection only; SessionVWAPAuthority owns the mathematics.
    from core.session_vwap_authority import DEFAULT_SESSION_VWAP_AUTHORITY
    result = DEFAULT_SESSION_VWAP_AUTHORITY.calculate(candles)
    value = result.get("value") if result.get("state") == "READY" else None
    return round(float(value), 2) if value is not None else None
