"""Completed-period master-candle identity and breakout/retest state.

This service is deliberately pure.  It never fetches data, never mutates a
trade, and never treats a forming higher-timeframe candle as confirmation.
The immutable identity is derived from instrument, timeframe, timestamp and
OHLC so every downstream decision, chart marker and research outcome can refer
to the same structural object.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
from typing import Any, Dict, Iterable, List, Optional


MASTER_CANDLE_VERSION = "master-candle-identity-1.0.0"
SUPPORTED_MASTER_FRAMES = ("1W", "1M")


def _number(value: Any) -> Optional[float]:
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _timestamp(row: Dict[str, Any]) -> Optional[str]:
    value = row.get("timestamp") or row.get("time") or row.get("date") or row.get("period")
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except Exception:
            return str(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _normalise(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for raw in rows or ():
        row = dict(raw or {})
        values = {key: _number(row.get(key)) for key in ("open", "high", "low", "close")}
        ts = _timestamp(row)
        if ts is None or any(values[key] is None for key in values):
            continue
        # Explicitly reject forming/provisional rows.  Aggregators should set
        # is_closed; absence is accepted only for already completed input sets.
        if row.get("forming") is True or row.get("is_closed") is False:
            continue
        if row.get("session_partial") is True or row.get("pattern_eligible") is False:
            continue
        out.append({
            **row,
            **values,
            "timestamp": ts,
            "is_closed": True,
            "forming": False,
        })
    out.sort(key=lambda item: str(item.get("timestamp") or ""))
    return out


def _identity(instrument_key: str, timeframe: str, row: Dict[str, Any]) -> str:
    material = {
        "instrument_key": str(instrument_key or ""),
        "timeframe": timeframe,
        "timestamp": row.get("timestamp"),
        "open": row.get("open"),
        "high": row.get("high"),
        "low": row.get("low"),
        "close": row.get("close"),
        "version": MASTER_CANDLE_VERSION,
    }
    return hashlib.sha256(json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _inside(row: Dict[str, Any], master: Dict[str, Any]) -> bool:
    return float(row["high"]) <= float(master["high"]) and float(row["low"]) >= float(master["low"])


def _breakout_direction(row: Dict[str, Any], master: Dict[str, Any]) -> Optional[str]:
    close = float(row["close"])
    if close > float(master["high"]):
        return "UP"
    if close < float(master["low"]):
        return "DOWN"
    return None


def evaluate_master_candle(
    rows: Iterable[Dict[str, Any]],
    *,
    instrument_key: str,
    timeframe: str = "1W",
    retest_tolerance_bps: float = 20.0,
    minimum_inside_candles: int = 2,
) -> Dict[str, Any]:
    """Return the latest completed master-candle structure.

    A candidate becomes a master only after at least one later completed candle
    is fully inside its range.  Its identity never changes.  A breakout is
    confirmed only by a completed close outside the range.  A later completed
    candle confirms a retest when it revisits the broken boundary within the
    configured tolerance and closes back on the breakout side.
    """
    tf = str(timeframe or "1W").upper()
    if tf not in SUPPORTED_MASTER_FRAMES:
        raise ValueError(f"unsupported master-candle timeframe {timeframe}")
    candles = _normalise(rows)
    if len(candles) < 2:
        return {
            "ok": False,
            "state": "INSUFFICIENT_COMPLETED_CANDLES",
            "timeframe": tf,
            "completed_bars": len(candles),
            "master_candle": None,
            "version": MASTER_CANDLE_VERSION,
        }

    latest: Optional[Dict[str, Any]] = None
    for index, candidate in enumerate(candles[:-1]):
        followers = candles[index + 1 :]
        inside_rows: List[Dict[str, Any]] = []
        breakout: Optional[Dict[str, Any]] = None
        retest: Optional[Dict[str, Any]] = None
        for follower in followers:
            if breakout is None and _inside(follower, candidate):
                inside_rows.append(follower)
                continue
            if breakout is None:
                direction = _breakout_direction(follower, candidate)
                if direction is not None:
                    breakout = {"direction": direction, "candle": follower}
                    continue
                # A candle can pierce both boundaries yet close back inside.
                # That invalidates this containment sequence without inventing
                # a confirmed breakout.
                if float(follower["high"]) > float(candidate["high"]) or float(follower["low"]) < float(candidate["low"]):
                    break
                continue
            direction = str(breakout["direction"])
            boundary = float(candidate["high"] if direction == "UP" else candidate["low"])
            tolerance = abs(boundary) * max(0.0, float(retest_tolerance_bps)) / 10_000.0
            if direction == "UP":
                touched = float(follower["low"]) <= boundary + tolerance
                held = float(follower["close"]) >= boundary
            else:
                touched = float(follower["high"]) >= boundary - tolerance
                held = float(follower["close"]) <= boundary
            if touched and held:
                retest = {"direction": direction, "candle": follower, "boundary": boundary, "tolerance": tolerance}
                break

        if len(inside_rows) < max(1, int(minimum_inside_candles)):
            continue
        identity = _identity(instrument_key, tf, candidate)
        if retest is not None:
            state = f"RETEST_CONFIRMED_{retest['direction']}"
        elif breakout is not None:
            state = f"BREAKOUT_CONFIRMED_{breakout['direction']}"
        else:
            state = "INSIDE_SEQUENCE_ACTIVE"
        latest = {
            "ok": True,
            "state": state,
            "timeframe": tf,
            "identity": identity,
            "master_candle": {
                "identity": identity,
                "timestamp": candidate["timestamp"],
                "open": candidate["open"],
                "high": candidate["high"],
                "low": candidate["low"],
                "close": candidate["close"],
                "range": round(float(candidate["high"]) - float(candidate["low"]), 8),
                "source": candidate.get("source"),
                "completed": True,
            },
            "inside_candles": [
                {
                    "timestamp": row["timestamp"],
                    "high": row["high"],
                    "low": row["low"],
                    "close": row["close"],
                }
                for row in inside_rows
            ],
            "inside_count": len(inside_rows),
            "breakout": None if breakout is None else {
                "direction": breakout["direction"],
                "timestamp": breakout["candle"]["timestamp"],
                "close": breakout["candle"]["close"],
                "boundary": candidate["high"] if breakout["direction"] == "UP" else candidate["low"],
                "completed": True,
            },
            "retest": None if retest is None else {
                "direction": retest["direction"],
                "timestamp": retest["candle"]["timestamp"],
                "close": retest["candle"]["close"],
                "boundary": retest["boundary"],
                "tolerance": round(retest["tolerance"], 8),
                "completed": True,
            },
            "confirmation_policy": "completed higher-timeframe close only; forming periods and session-partial bars excluded",
            "minimum_inside_candles": max(1, int(minimum_inside_candles)),
            "version": MASTER_CANDLE_VERSION,
        }
    if latest is None:
        return {
            "ok": True,
            "state": "NO_MASTER_CANDLE",
            "timeframe": tf,
            "completed_bars": len(candles),
            "master_candle": None,
            "confirmation_policy": "completed higher-timeframe close only",
            "minimum_inside_candles": max(1, int(minimum_inside_candles)),
            "version": MASTER_CANDLE_VERSION,
        }
    latest["completed_bars"] = len(candles)
    return latest


def evaluate_higher_timeframe_structures(
    *,
    instrument_key: str,
    weekly: Iterable[Dict[str, Any]],
    monthly: Iterable[Dict[str, Any]],
) -> Dict[str, Any]:
    return {
        "weekly_master_candle": evaluate_master_candle(
            weekly, instrument_key=instrument_key, timeframe="1W"
        ),
        "monthly_master_candle": evaluate_master_candle(
            monthly, instrument_key=instrument_key, timeframe="1M"
        ),
        "completed_periods_only": True,
        "version": MASTER_CANDLE_VERSION,
    }
