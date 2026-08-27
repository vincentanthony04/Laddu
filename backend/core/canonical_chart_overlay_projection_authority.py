"""Canonical chart presentation projection.

The browser is a renderer, not an indicator engine.  This authority composes the
existing IndicatorSnapshotAuthority with display-only chart transforms and
returns time-aligned series.  It has no decision, scanner, risk, or capital
ownership.
"""
from __future__ import annotations

from datetime import datetime, timezone
import math
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo

from core.indicator_snapshot_authority import DEFAULT_INDICATOR_SNAPSHOT_AUTHORITY
from core.session_vwap_authority import DEFAULT_SESSION_VWAP_AUTHORITY

AUTHORITY_NAME = "CanonicalChartOverlayProjectionAuthority"
AUTHORITY_VERSION = "1.0.0"
IST = ZoneInfo("Asia/Kolkata")


def _number(value: Any) -> float | None:
    try:
        out = float(value)
        return out if math.isfinite(out) else None
    except (TypeError, ValueError):
        return None


def _epoch_seconds(value: Any) -> int | None:
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        raw = float(value)
        return int(raw / 1000.0 if raw > 10_000_000_000 else raw)
    text = str(value or "").strip()
    if not text:
        return None
    try:
        if text.replace(".", "", 1).isdigit():
            return _epoch_seconds(float(text))
        stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return int(stamp.timestamp())
    except Exception:
        return None


def _row_time(row: Mapping[str, Any]) -> int | None:
    for key in ("timestamp", "time", "datetime", "ts", "date"):
        value = _epoch_seconds(row.get(key))
        if value is not None:
            return value
    return None


def _points(times: list[int], values: list[Any], *, field: str = "value") -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for stamp, raw in zip(times, values):
        value = _number(raw)
        if value is not None:
            out.append({"time": stamp, field: value})
    return out


def _heikin(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    previous_open: float | None = None
    previous_close: float | None = None
    for row in rows:
        open_, high, low, close = (float(row[key]) for key in ("open", "high", "low", "close"))
        ha_close = (open_ + high + low + close) / 4.0
        ha_open = (open_ + close) / 2.0 if previous_open is None else (previous_open + float(previous_close)) / 2.0
        out.append({
            "time": int(row["time"]),
            "open": ha_open,
            "high": max(high, ha_open, ha_close),
            "low": min(low, ha_open, ha_close),
            "close": ha_close,
        })
        previous_open, previous_close = ha_open, ha_close
    return out


def _vwap(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = DEFAULT_SESSION_VWAP_AUTHORITY.calculate(rows)
    if result.get("state") != "READY":
        return []
    return [
        {"time": int(item["time"]), "value": float(item["value"])}
        for item in (result.get("series") or []) if item.get("value") is not None
    ]


def _cross_events(times: list[int], series: Mapping[str, list[Any]]) -> list[dict[str, Any]]:
    e9, e21, e50 = series.get("ema9", []), series.get("ema21", []), series.get("ema50", [])
    events: list[dict[str, Any]] = []
    pairs = ((e9, e21, "9↑21", "9↓21"), (e21, e50, "21↑50", "21↓50"))
    for index in range(1, len(times)):
        for left, right, up, down in pairs:
            if index >= len(left) or index >= len(right):
                continue
            previous_left, previous_right = _number(left[index - 1]), _number(right[index - 1])
            current_left, current_right = _number(left[index]), _number(right[index])
            if None in (previous_left, previous_right, current_left, current_right):
                continue
            if previous_left <= previous_right and current_left > current_right:
                events.append({"time": times[index], "direction": "bull", "text": up})
            elif previous_left >= previous_right and current_left < current_right:
                events.append({"time": times[index], "direction": "bear", "text": down})
    return events[-16:]


class CanonicalChartOverlayProjectionAuthority:
    authority = AUTHORITY_NAME
    authority_version = AUTHORITY_VERSION

    def project(self, rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
        clean: list[dict[str, Any]] = []
        for raw in rows or ():
            stamp = _row_time(raw)
            values = {field: _number(raw.get(field)) for field in ("open", "high", "low", "close", "volume")}
            if stamp is None or any(values[field] is None for field in ("open", "high", "low", "close")):
                continue
            clean.append({"time": stamp, **values})
        clean.sort(key=lambda row: row["time"])
        if not clean:
            return {"authority": self.authority, "authority_version": self.authority_version, "state": "UNAVAILABLE", "series": {}, "metrics": {}, "events": {}}

        indicator = DEFAULT_INDICATOR_SNAPSHOT_AUTHORITY.calculate(clean)
        raw_series = dict(indicator.get("series") or {})
        times = [int(row["time"]) for row in clean]
        supertrend = []
        st_values = list(raw_series.get("supertrend") or [])
        st_directions = list(raw_series.get("supertrend_direction") or [])
        for stamp, raw_value, raw_direction in zip(times, st_values, st_directions):
            value = _number(raw_value)
            direction = int(raw_direction) if raw_direction in (-1, 1) else None
            if value is not None and direction is not None:
                supertrend.append({"time": stamp, "value": value, "direction": direction})

        metrics = dict(indicator.get("metrics") or {})
        ema9 = _number(metrics.get("ema9"))
        ema21 = _number(metrics.get("ema21"))
        ema50 = _number(metrics.get("ema50"))
        ema_stack = (
            "BULLISH" if None not in (ema9, ema21, ema50) and ema9 > ema21 > ema50
            else "BEARISH" if None not in (ema9, ema21, ema50) and ema9 < ema21 < ema50
            else "MIXED"
        )
        series = {
            "ema9": _points(times, list(raw_series.get("ema9") or [])),
            "ema20": _points(times, list(raw_series.get("ema20") or [])),
            "ema21": _points(times, list(raw_series.get("ema21") or [])),
            "ema50": _points(times, list(raw_series.get("ema50") or [])),
            "rsi14": _points(times, list(raw_series.get("rsi14") or [])),
            "atr14": _points(times, list(raw_series.get("atr14") or [])),
            "macd": _points(times, list(raw_series.get("macd") or [])),
            "macd_signal": _points(times, list(raw_series.get("macd_signal") or [])),
            "macd_hist": _points(times, list(raw_series.get("macd_hist") or [])),
            "supertrend": supertrend,
            "vwap": _vwap(clean),
            "heikin": _heikin(clean),
        }
        return {
            "authority": self.authority,
            "authority_version": self.authority_version,
            "indicator_authority": indicator.get("authority"),
            "indicator_authority_version": indicator.get("authority_version"),
            "state": "READY",
            "source_first_candle": times[0],
            "source_last_candle": times[-1],
            "row_count": len(clean),
            "metrics": metrics,
            "states": {"ema_stack": ema_stack},
            "series": series,
            "events": {"ema_crosses": _cross_events(times, raw_series)},
            "policy": "Presentation projection only; indicator maths belongs to IndicatorSnapshotAuthority; no decision/risk/capital authority.",
        }


DEFAULT_CANONICAL_CHART_OVERLAY_PROJECTION_AUTHORITY = CanonicalChartOverlayProjectionAuthority()
