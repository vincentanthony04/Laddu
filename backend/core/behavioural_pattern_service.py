"""Mechanical delivery/participation scoring and behavioural patterns."""
from __future__ import annotations

import math
import statistics
from typing import Any, Dict, List, Optional

from core.indicator_snapshot_authority import ema as canonical_ema, true_ranges, wilder_series

MODEL_VERSION = "behavioural-participation-1.0.0"

PATTERN_EVIDENCE = {
    "VOLUME_CLIMAX": {"positive_10d_pct": 58.0, "average_10d_return_pct": 2.4, "reliability": "strong"},
    "HIDDEN_ACCUMULATION": {"positive_10d_pct": 52.0, "average_10d_return_pct": 1.6, "reliability": "supportive"},
    "FALSE_BREAKOUT": {"positive_10d_pct": 46.0, "median_10d_return": "slightly_negative", "reliability": "bearish_warning"},
    "MULTIPLE_WAVES": {"positive_10d_pct": 50.0, "reliability": "marginal"},
    "SILENT_ACCUMULATION": {"positive_10d_pct": 49.0, "reliability": "weak"},
    "SHAKEOUT": {"positive_10d_pct": 49.0, "reliability": "weak"},
    "DELIVERY_DIVERGENCE": {"positive_10d_pct": 49.0, "reliability": "weak"},
}

PATTERN_BONUS = {
    "HIDDEN_ACCUMULATION": 8,
    "SILENT_ACCUMULATION": 8,
    "MULTIPLE_WAVES": 7,
    "DELIVERY_DIVERGENCE": 6,
    "SHAKEOUT": 10,
}


def _number(value: Any) -> Optional[float]:
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def _mean(values: List[float]) -> Optional[float]:
    return statistics.mean(values) if values else None


def _zscore(value: Optional[float], values: List[float]) -> Optional[float]:
    if value is None or len(values) < 10:
        return None
    deviation = statistics.pstdev(values)
    if not deviation:
        return 0.0
    return (value - statistics.mean(values)) / deviation


def _ema(values: List[float], period: int) -> Optional[float]:
    # Compatibility projection only; canonical arithmetic lives in
    # IndicatorSnapshotAuthority.
    return canonical_ema(list(values or []), period)


def _atr14(candles: List[Dict[str, Any]]) -> Optional[float]:
    clean = []
    for candle in candles or []:
        row = {key: _number(candle.get(key)) for key in ("high", "low", "close")}
        if None in row.values():
            continue
        clean.append(row)
    if len(clean) < 14:
        return None
    series = wilder_series(true_ranges(clean), 14)
    return series[-1] if series else None


def _component(name: str, raw: Any, points: float, maximum: float, available: bool = True) -> Dict[str, Any]:
    return {"name": name, "raw": raw, "points": round(points, 2) if available else None, "max_points": maximum, "available": available}


def analyze(symbol: str, delivery_rows: List[Dict[str, Any]], candles: List[Dict[str, Any]]) -> Dict[str, Any]:
    delivery = sorted((dict(row) for row in delivery_rows or []), key=lambda row: str(row.get("trade_date") or ""))
    daily = sorted((dict(row) for row in candles or []), key=lambda row: str(row.get("timestamp") or row.get("ts") or ""))
    coverage = {"delivery_rows": len(delivery), "candle_rows": len(daily), "required_delivery_rows": 50, "required_candle_rows": 100}
    if len(delivery) < coverage["required_delivery_rows"] or len(daily) < coverage["required_candle_rows"]:
        missing = []
        if len(delivery) < coverage["required_delivery_rows"]:
            missing.append(f"delivery {len(delivery)}/{coverage['required_delivery_rows']}")
        if len(daily) < coverage["required_candle_rows"]:
            missing.append(f"candles {len(daily)}/{coverage['required_candle_rows']}")
        return {"ok": False, "state": "collecting_evidence", "signal": "NEUTRAL", "patterns": [], "score": None,
                "coverage": coverage, "missing_evidence": missing,
                "reason": "Required behavioural evidence is still being collected: " + ", ".join(missing),
                "model_version": MODEL_VERSION}

    latest_delivery = delivery[-1]
    latest = daily[-1]
    closes = [_number(row.get("close")) for row in daily]
    highs = [_number(row.get("high")) for row in daily]
    lows = [_number(row.get("low")) for row in daily]
    volumes = [_number(row.get("volume")) for row in daily]
    closes = [value for value in closes if value is not None]
    volumes = [value for value in volumes if value is not None]
    close = _number(latest.get("close"))
    high = _number(latest.get("high"))
    low = _number(latest.get("low"))
    previous_low = _number(daily[-2].get("low"))
    previous_close = _number(daily[-2].get("close"))
    delivery_pct = _number(latest_delivery.get("delivery_pct"))
    delivery_values = [value for value in (_number(row.get("delivery_pct")) for row in delivery) if value is not None]
    delivered_values = [value for value in (_number(row.get("deliverable_qty")) for row in delivery) if value is not None]
    traded_values = [value for value in (_number(row.get("traded_qty")) for row in delivery) if value is not None]

    delivery_20 = _mean(delivery_values[-21:-1])
    delivery_7 = _mean(delivery_values[-7:])
    delivery_50 = _mean(delivery_values[-50:])
    delivery_z = _zscore(delivery_pct, delivery_values[-21:-1])
    delivered_z = _zscore(delivered_values[-1] if delivered_values else None, delivered_values[-21:-1])
    volume_20 = _mean(volumes[-21:-1])
    volume_7 = _mean(volumes[-7:])
    volume_50 = _mean(volumes[-50:])
    rvol = volumes[-1] / volume_20 if volumes and volume_20 else None
    ema20 = _ema(closes, 20)
    session_vwap = _number(latest.get("vwap") or latest.get("avg_price"))
    return_1d = ((close / previous_close) - 1.0) * 100.0 if close and previous_close else None
    return_5d = ((close / closes[-6]) - 1.0) * 100.0 if close and len(closes) >= 6 and closes[-6] else None
    buyer_control = (close - low) / (high - low) if None not in (close, high, low) and high != low else None
    high_52w = max(value for value in highs[-252:] if value is not None)
    low_52w = min(value for value in lows[-252:] if value is not None)
    position_52w = (close - low_52w) / (high_52w - low_52w) * 100.0 if close is not None and high_52w != low_52w else None
    near_high = bool(close is not None and high_52w and close >= high_52w * 0.97)
    higher_low = bool(low is not None and previous_low is not None and low > previous_low)
    delivery_7v20 = delivery_7 / delivery_20 if delivery_7 is not None and delivery_20 else None
    delivery_20v50 = delivery_20 / delivery_50 if delivery_20 is not None and delivery_50 else None
    volume_7v50 = volume_7 / volume_50 if volume_7 is not None and volume_50 else None
    range_20 = (max(highs[-20:]) - min(lows[-20:])) if all(value is not None for value in highs[-20:] + lows[-20:]) else None
    range_50 = (max(highs[-50:]) - min(lows[-50:])) if all(value is not None for value in highs[-50:] + lows[-50:]) else None
    range_contraction = range_20 / range_50 if range_20 is not None and range_50 else None
    traded_values_20 = []
    for candle in daily[-20:]:
        candle_close = _number(candle.get("close"))
        candle_volume = _number(candle.get("volume"))
        if candle_close is not None and candle_volume is not None:
            traded_values_20.append(candle_close * candle_volume)
    average_traded_value_20 = _mean(traded_values_20)
    average_traded_value_crore = average_traded_value_20 / 10_000_000 if average_traded_value_20 is not None else None
    liquidity_state = (
        "high" if average_traded_value_crore is not None and average_traded_value_crore >= 10
        else "adequate" if average_traded_value_crore is not None and average_traded_value_crore >= 2
        else "low" if average_traded_value_crore is not None
        else "unavailable"
    )
    buying_interest = (
        "strong" if buyer_control is not None and buyer_control >= 0.65 and delivery_7v20 is not None and delivery_7v20 > 1
        else "positive" if buyer_control is not None and buyer_control >= 0.50
        else "weak" if buyer_control is not None
        else "unavailable"
    )

    components = []
    delivery_points = 0 if delivery_z is None or delivery_z <= 0 else 4 if delivery_z < 1 else 7 if delivery_z < 2 else 10
    components.append(_component("delivery_zscore", delivery_z, delivery_points, 10, delivery_z is not None))
    rvol_points = 0 if rvol is None or rvol < 0.8 else 5 if rvol < 1.2 else 10 if rvol < 2 else 15
    components.append(_component("rvol", rvol, rvol_points, 15, rvol is not None))
    vwap_points = 0 if session_vwap is None or close is None or close <= session_vwap else 6 if close <= session_vwap * 1.01 else 10
    components.append(_component("close_above_vwap", None if session_vwap is None else (close - session_vwap) / session_vwap * 100, vwap_points, 10, session_vwap is not None))
    ema_points = 0 if ema20 is None or close is None or close <= ema20 else 6 if close <= ema20 * 1.02 else 10
    components.append(_component("close_above_ema20", None if ema20 is None else (close - ema20) / ema20 * 100, ema_points, 10, ema20 is not None))
    components.append(_component("higher_low", higher_low, 15 if higher_low else 0, 15))
    components.append(_component("breakout", {"near_52w_high": near_high, "rvol": rvol}, 10 if near_high and rvol is not None and rvol >= 1.2 else 5 if near_high else 0, 10))
    delivery_momentum_points = 0 if delivery_7v20 is None or delivery_7v20 <= 1 else 7 if delivery_7v20 < 1.1 else 15
    components.append(_component("delivery_momentum_7v20", delivery_7v20, delivery_momentum_points, 15, delivery_7v20 is not None))
    expansion_points = 0 if volume_7v50 is None or volume_7v50 <= 1 else 7 if volume_7v50 < 1.2 else 15
    components.append(_component("volume_expansion_7v50", volume_7v50, expansion_points, 15, volume_7v50 is not None))

    patterns: List[str] = []
    volume_climax = bool(rvol is not None and rvol >= 5 and delivery_pct is not None and delivery_pct >= 70 and return_1d is not None and abs(return_1d) < 2)
    hidden = bool(delivered_z is not None and delivered_z >= 2 and range_contraction is not None and range_contraction < 0.8 and delivery_pct is not None and delivery_20 is not None and delivery_pct > delivery_20)
    false_breakout = bool(close is not None and len(highs) >= 21 and close >= max(value for value in highs[-21:-1] if value is not None) and rvol is not None and rvol >= 1.5 and delivery_pct is not None and delivery_20 is not None and delivery_pct < delivery_20)
    elevated_sessions = sum(1 for value in delivery_values[-7:] if delivery_20 is not None and value > delivery_20)
    multiple_waves = bool(elevated_sessions >= 3 and delivery_7 is not None and delivery_50 is not None and delivery_7 > delivery_50)
    sideways = bool(len(closes) >= 21 and abs((closes[-1] / closes[-21] - 1) * 100) <= 3)
    stable_volume = bool(volume_7v50 is not None and 0.8 <= volume_7v50 <= 1.2)
    silent = bool(sideways and delivery_7v20 is not None and delivery_7v20 > 1 and stable_volume)
    yesterday_return = ((closes[-2] / closes[-3]) - 1) * 100 if len(closes) >= 3 and closes[-3] else None
    yesterday_delivery = delivery_values[-2] if len(delivery_values) >= 2 else None
    shakeout = bool(yesterday_return is not None and yesterday_return <= -2 and yesterday_delivery is not None and delivery_20 is not None and yesterday_delivery > delivery_20 and return_1d is not None and return_1d > 0)
    divergence = bool(close is not None and len(closes) >= 21 and close <= min(closes[-21:-1]) and delivery_7v20 is not None and delivery_7v20 > 1)
    for active, name in ((volume_climax, "VOLUME_CLIMAX"), (hidden, "HIDDEN_ACCUMULATION"), (false_breakout, "FALSE_BREAKOUT"),
                         (multiple_waves, "MULTIPLE_WAVES"), (silent, "SILENT_ACCUMULATION"),
                         (shakeout, "SHAKEOUT"), (divergence, "DELIVERY_DIVERGENCE")):
        if active:
            patterns.append(name)

    accumulation_score = sum(component["points"] or 0 for component in components)
    pattern_bonus = min(15, sum(PATTERN_BONUS.get(pattern, 0) for pattern in patterns))
    false_breakout_penalty = 15 if false_breakout else 0
    raw_score = accumulation_score + pattern_bonus - false_breakout_penalty
    score = max(0, min(100, raw_score))
    distribution = bool(delivery_7v20 is not None and delivery_7v20 < 0.95 and buyer_control is not None and buyer_control < 0.45 and return_5d is not None and return_5d < 0)
    if false_breakout:
        signal = "FALSE_BREAKOUT"
    elif volume_climax:
        signal = "CLIMAX"
    elif distribution:
        signal = "DISTRIBUTION"
    elif score >= 65:
        signal = "ACCUMULATION"
    elif score >= 40:
        signal = "WATCH"
    else:
        signal = "NEUTRAL"

    atr14 = _atr14(daily)
    entry = close * 1.002 if close is not None else None
    stop = entry - 2.0 * atr14 if entry is not None and atr14 is not None else None
    target = entry + 2.5 * atr14 if entry is not None and atr14 is not None else None
    risk_pct = abs(entry - stop) / entry * 100 if entry and stop is not None else None
    return {
        "ok": True, "state": "ready", "model_version": MODEL_VERSION, "symbol": str(symbol or "").upper(),
        "signal": signal, "patterns": patterns, "score": round(score, 2), "raw_score": round(raw_score, 2),
        "accumulation_score": round(accumulation_score, 2), "pattern_bonus": pattern_bonus,
        "false_breakout_penalty": false_breakout_penalty, "components": components,
        "multi_window": {"delivery_7v20": delivery_7v20, "delivery_20v50": delivery_20v50,
                         "volume_7v50": volume_7v50, "range_contraction_20v50": range_contraction},
        "market_participation": {
            "rvol": rvol, "volume_7v50": volume_7v50, "buyer_control": buyer_control,
            "buying_interest": buying_interest, "average_traded_value_20_crore": average_traded_value_crore,
            "liquidity_state": liquidity_state,
        },
        "delivery": {"delivery_pct": delivery_pct, "delivery_20d": delivery_20, "delivery_z": delivery_z,
                     "deliverable_qty_z20": delivered_z, "rvol": rvol},
        "price": {"close": close, "vwap": session_vwap, "ema20": ema20, "return_5d_pct": return_5d,
                  "position_52w_pct": position_52w, "buyer_control": buyer_control},
        "trade_levels": {"entry": entry, "stop": stop, "target": target, "risk_pct": risk_pct, "atr14": atr14,
                         "policy": "entry=close*1.002; stop=entry-2*ATR14; target=entry+2.5*ATR14 trail trigger"},
        "pattern_evidence": {pattern: PATTERN_EVIDENCE.get(pattern) for pattern in patterns},
        "coverage": coverage,
    }
