"""Mechanically reproducible completed-candle pattern evidence.

Patterns are evidence, not automatic score.  New patterns remain shadow-weighted
until governed point-in-time walk-forward validation demonstrates incremental edge.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional

VERSION = "candle-pattern-evidence-1.0.0"


def _f(value: Any) -> Optional[float]:
    try:
        n = float(value)
        return n if math.isfinite(n) else None
    except (TypeError, ValueError):
        return None


def _shape(row: Dict[str, Any]) -> Optional[Dict[str, float]]:
    o, h, l, c = (_f(row.get(key)) for key in ("open", "high", "low", "close"))
    if None in (o, h, l, c) or h < l:
        return None
    rng = max(h - l, 1e-12)
    body = abs(c - o)
    upper = h - max(o, c)
    lower = min(o, c) - l
    return {"open": o, "high": h, "low": l, "close": c, "range": rng, "body": body, "upper": upper, "lower": lower}


class PatternEvidenceService:
    version = VERSION

    def analyze(self, candles: List[Dict[str, Any]]) -> Dict[str, Any]:
        rows = [dict(row) for row in candles or []]
        if len(rows) < 2:
            return {"ok": False, "state": "INSUFFICIENT_EVIDENCE", "patterns": [], "production_weight": 0, "version": self.version}
        current, previous = _shape(rows[-1]), _shape(rows[-2])
        if current is None or previous is None:
            return {"ok": False, "state": "INVALID_OHLC", "patterns": [], "production_weight": 0, "version": self.version}

        patterns: List[Dict[str, Any]] = []

        def add(name: str, direction: str, strength: float, rationale: str) -> None:
            patterns.append({"name": name, "direction": direction, "strength": round(max(0.0, min(1.0, strength)), 3), "rationale": rationale})

        # Inside/outside structures are direction-neutral until context confirms them.
        if current["high"] <= previous["high"] and current["low"] >= previous["low"]:
            add("INSIDE_BAR", "neutral", 0.60, "current range is contained by previous completed candle")
        if current["high"] > previous["high"] and current["low"] < previous["low"]:
            direction = "bullish" if current["close"] > current["open"] else "bearish" if current["close"] < current["open"] else "neutral"
            add("OUTSIDE_BAR", direction, 0.68, "current completed candle exceeds both sides of previous range")

        prev_bull = previous["close"] > previous["open"]
        prev_bear = previous["close"] < previous["open"]
        cur_bull = current["close"] > current["open"]
        cur_bear = current["close"] < current["open"]
        if prev_bear and cur_bull and current["open"] <= previous["close"] and current["close"] >= previous["open"]:
            add("BULLISH_ENGULFING", "bullish", 0.78, "bullish real body engulfs prior bearish real body")
        if prev_bull and cur_bear and current["open"] >= previous["close"] and current["close"] <= previous["open"]:
            add("BEARISH_ENGULFING", "bearish", 0.78, "bearish real body engulfs prior bullish real body")

        body_ratio = current["body"] / current["range"]
        if body_ratio <= 0.10:
            add("DOJI_INDECISION", "neutral", 0.55, "real body is <=10% of completed candle range")
        if current["body"] > 0:
            if current["lower"] >= 2.0 * current["body"] and current["upper"] <= 0.6 * current["body"]:
                add("LOWER_REJECTION_HAMMER", "bullish", 0.72, "lower wick is >=2x body with limited upper wick")
            if current["upper"] >= 2.0 * current["body"] and current["lower"] <= 0.6 * current["body"]:
                add("UPPER_REJECTION_SHOOTING_STAR", "bearish", 0.72, "upper wick is >=2x body with limited lower wick")

        # Three-candle reversal structures use only completed candles and explicit geometry.
        if len(rows) >= 3:
            first = _shape(rows[-3])
            middle = _shape(rows[-2])
            last = current
            if first and middle:
                first_mid = (first["open"] + first["close"]) / 2.0
                small_middle = middle["body"] / middle["range"] <= 0.35
                if first["close"] < first["open"] and small_middle and last["close"] > last["open"] and last["close"] >= first_mid:
                    add("MORNING_REVERSAL", "bullish", 0.70, "bearish impulse, indecision, then bullish close through first-body midpoint")
                if first["close"] > first["open"] and small_middle and last["close"] < last["open"] and last["close"] <= first_mid:
                    add("EVENING_REVERSAL", "bearish", 0.70, "bullish impulse, indecision, then bearish close through first-body midpoint")

        bullish = sum(p["strength"] for p in patterns if p["direction"] == "bullish")
        bearish = sum(p["strength"] for p in patterns if p["direction"] == "bearish")
        bias = "bullish" if bullish > bearish + 0.2 else "bearish" if bearish > bullish + 0.2 else "neutral"
        return {
            "ok": True,
            "state": "READY",
            "bias": bias,
            "patterns": patterns,
            "production_weight": 0,
            "validation_state": "SHADOW_AWAITING_WALK_FORWARD_VALIDATION",
            "semantics": "Mechanical candle-pattern evidence only; no automatic production score or visual guesswork.",
            "version": self.version,
        }


DEFAULT_PATTERN_EVIDENCE_SERVICE = PatternEvidenceService()
