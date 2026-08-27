"""Mechanical chart-formation evidence from completed OHLCV candles.

The service intentionally emits shadow evidence only. It defines reproducible
geometry for a bounded set of formations; it does not visually guess patterns
or award production score before point-in-time walk-forward validation.
"""
from __future__ import annotations

import math
import statistics
from typing import Any, Dict, List, Optional, Tuple

VERSION = "chart-pattern-evidence-1.0.0"
MIN_ROWS = 30


def _f(value: Any) -> Optional[float]:
    try:
        n = float(value)
        return n if math.isfinite(n) else None
    except (TypeError, ValueError):
        return None


def _trs(rows: List[Dict[str, Any]]) -> List[float]:
    out: List[float] = []
    prev: Optional[float] = None
    for row in rows:
        h, l, c = _f(row.get("high")), _f(row.get("low")), _f(row.get("close"))
        if h is None or l is None or c is None:
            continue
        out.append(max(h-l, abs(h-prev), abs(l-prev)) if prev is not None else h-l)
        prev = c
    return out


def _pivots(rows: List[Dict[str, Any]], radius: int = 2) -> Tuple[List[Tuple[int, float]], List[Tuple[int, float]]]:
    highs: List[Tuple[int, float]] = []
    lows: List[Tuple[int, float]] = []
    for i in range(radius, len(rows)-radius):
        h = _f(rows[i].get("high")); l = _f(rows[i].get("low"))
        if h is None or l is None:
            continue
        neighbor_highs = [_f(rows[j].get("high")) for j in range(i-radius, i+radius+1) if j != i]
        neighbor_lows = [_f(rows[j].get("low")) for j in range(i-radius, i+radius+1) if j != i]
        if all(v is not None and h >= v for v in neighbor_highs): highs.append((i, h))
        if all(v is not None and l <= v for v in neighbor_lows): lows.append((i, l))
    return highs, lows


def _slope(points: List[Tuple[int, float]]) -> Optional[float]:
    if len(points) < 2:
        return None
    xs = [float(x) for x, _ in points]; ys = [float(y) for _, y in points]
    xm, ym = statistics.mean(xs), statistics.mean(ys)
    denom = sum((x-xm)**2 for x in xs)
    return sum((x-xm)*(y-ym) for x, y in points)/denom if denom else 0.0


class ChartPatternEvidenceService:
    version = VERSION

    def analyze(self, candles: List[Dict[str, Any]]) -> Dict[str, Any]:
        rows = [dict(r) for r in (candles or [])][-120:]
        if len(rows) < MIN_ROWS:
            return {"ok": False, "state": "INSUFFICIENT_EVIDENCE", "patterns": [], "production_weight": 0, "version": self.version}
        close = _f(rows[-1].get("close"))
        trs = _trs(rows)
        atr = statistics.mean(trs[-14:]) if len(trs) >= 14 else None
        if close is None or atr is None or atr <= 0:
            return {"ok": False, "state": "INVALID_OHLC", "patterns": [], "production_weight": 0, "version": self.version}
        highs, lows = _pivots(rows)
        patterns: List[Dict[str, Any]] = []

        def add(name: str, direction: str, state: str, strength: float, geometry: Dict[str, Any]) -> None:
            patterns.append({
                "name": name, "direction": direction, "state": state,
                "strength": round(max(0.0, min(1.0, strength)), 3),
                "geometry": geometry,
            })

        tolerance = max(atr * 0.65, abs(close) * 0.004)
        # Double top/bottom: two separated pivots near one price with a neckline
        # between them. Confirmation requires a completed close through neckline.
        if len(highs) >= 2:
            a, b = highs[-2], highs[-1]
            if b[0] - a[0] >= 4 and abs(a[1]-b[1]) <= tolerance:
                trough = min((_f(rows[i].get("low")) for i in range(a[0]+1, b[0]) if _f(rows[i].get("low")) is not None), default=None)
                if trough is not None:
                    confirmed = close < trough
                    add("DOUBLE_TOP", "bearish", "CONFIRMED" if confirmed else "FORMING", 0.82 if confirmed else 0.60,
                        {"peak_1": a[1], "peak_2": b[1], "neckline": trough, "tolerance": tolerance})
        if len(lows) >= 2:
            a, b = lows[-2], lows[-1]
            if b[0] - a[0] >= 4 and abs(a[1]-b[1]) <= tolerance:
                peak = max((_f(rows[i].get("high")) for i in range(a[0]+1, b[0]) if _f(rows[i].get("high")) is not None), default=None)
                if peak is not None:
                    confirmed = close > peak
                    add("DOUBLE_BOTTOM", "bullish", "CONFIRMED" if confirmed else "FORMING", 0.82 if confirmed else 0.60,
                        {"trough_1": a[1], "trough_2": b[1], "neckline": peak, "tolerance": tolerance})

        # Triangle/wedge geometry from the latest three swing highs/lows.
        recent_highs, recent_lows = highs[-3:], lows[-3:]
        hs, ls = _slope(recent_highs), _slope(recent_lows)
        if hs is not None and ls is not None and recent_highs and recent_lows:
            scale = max(abs(close), 1e-9)
            flat_eps = max(atr * 0.05, scale * 0.0004)
            # slopes are price-per-bar; compare against a bounded flat threshold
            if abs(hs) <= flat_eps and ls > flat_eps:
                resistance = statistics.mean([v for _, v in recent_highs])
                confirmed = close > resistance
                add("ASCENDING_TRIANGLE", "bullish", "CONFIRMED" if confirmed else "FORMING", 0.76 if confirmed else 0.58,
                    {"resistance": resistance, "low_slope": ls})
            elif abs(ls) <= flat_eps and hs < -flat_eps:
                support = statistics.mean([v for _, v in recent_lows])
                confirmed = close < support
                add("DESCENDING_TRIANGLE", "bearish", "CONFIRMED" if confirmed else "FORMING", 0.76 if confirmed else 0.58,
                    {"support": support, "high_slope": hs})
            elif hs < 0 < ls:
                upper = recent_highs[-1][1]; lower = recent_lows[-1][1]
                direction = "bullish" if close > upper else "bearish" if close < lower else "neutral"
                add("SYMMETRICAL_COMPRESSION", direction, "CONFIRMED" if direction != "neutral" else "FORMING", 0.74 if direction != "neutral" else 0.55,
                    {"high_slope": hs, "low_slope": ls, "upper": upper, "lower": lower})
            elif (hs < 0 and ls < 0 and hs < ls) or (hs > 0 and ls > 0 and hs < ls):
                add("CONVERGING_WEDGE", "neutral", "FORMING", 0.52, {"high_slope": hs, "low_slope": ls})

        # Flag-like consolidation: a large prior displacement followed by a
        # shallow, lower-range consolidation. This remains a candidate until a
        # completed close exits the consolidation in impulse direction.
        if len(rows) >= 18:
            impulse_start = _f(rows[-14].get("close")); impulse_end = _f(rows[-7].get("close"))
            recent = rows[-6:]
            if impulse_start is not None and impulse_end is not None:
                impulse = impulse_end - impulse_start
                recent_high = max(_f(r.get("high")) for r in recent if _f(r.get("high")) is not None)
                recent_low = min(_f(r.get("low")) for r in recent if _f(r.get("low")) is not None)
                consolidation_range = recent_high - recent_low
                if abs(impulse) >= 2.2 * atr and consolidation_range <= 1.8 * atr:
                    if impulse > 0:
                        confirmed = close > recent_high
                        add("BULL_FLAG", "bullish", "CONFIRMED" if confirmed else "FORMING", 0.73 if confirmed else 0.54,
                            {"impulse_atr": impulse/atr, "consolidation_range_atr": consolidation_range/atr})
                    else:
                        confirmed = close < recent_low
                        add("BEAR_FLAG", "bearish", "CONFIRMED" if confirmed else "FORMING", 0.73 if confirmed else 0.54,
                            {"impulse_atr": impulse/atr, "consolidation_range_atr": consolidation_range/atr})

        bullish = sum(p["strength"] for p in patterns if p["direction"] == "bullish" and p["state"] == "CONFIRMED")
        bearish = sum(p["strength"] for p in patterns if p["direction"] == "bearish" and p["state"] == "CONFIRMED")
        bias = "bullish" if bullish > bearish + 0.2 else "bearish" if bearish > bullish + 0.2 else "neutral"
        return {
            "ok": True, "state": "READY", "bias": bias, "patterns": patterns,
            "production_weight": 0,
            "validation_state": "SHADOW_AWAITING_WALK_FORWARD_VALIDATION",
            "unimplemented_families": ["HEAD_AND_SHOULDERS", "CUP_AND_HANDLE"],
            "semantics": "Mechanical completed-candle formations only; no automatic score and no visual/subjective pattern inference.",
            "version": self.version,
        }


DEFAULT_CHART_PATTERN_EVIDENCE_SERVICE = ChartPatternEvidenceService()
