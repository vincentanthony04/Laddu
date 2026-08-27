from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional

from core.trading_session_authority import DEFAULT_TRADING_SESSION_AUTHORITY

IST = timezone(timedelta(hours=5, minutes=30))


def candle_datetime(candle_or_timestamp: Any) -> Optional[datetime]:
    value = candle_or_timestamp
    if isinstance(value, dict):
        value = value.get("timestamp") or value.get("time") or value.get("date")
    if value in (None, ""):
        return None
    try:
        if isinstance(value, (int, float)) or str(value).strip().isdigit():
            raw = float(value)
            if raw > 10**12:
                raw /= 1000.0
            return datetime.fromtimestamp(raw, tz=timezone.utc).astimezone(IST)
        raw = str(value).strip().replace("Z", "+00:00")
        parsed = datetime.fromisoformat(raw)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=IST)
        return parsed.astimezone(IST)
    except Exception:
        return None


def interval_minutes(interval: Any, default: int = 5) -> int:
    from core.timeframe import interval_minutes as canonical_minutes
    return canonical_minutes(interval, default)


def candle_is_closed(candle: Dict[str, Any], interval: Any = "5minute", now: Optional[datetime] = None) -> bool:
    started = candle_datetime(candle)
    if started is None:
        return False
    clock = (now or datetime.now(IST)).astimezone(IST)
    return started + timedelta(minutes=interval_minutes(interval)) <= clock


def closed_candles(candles: Iterable[Dict[str, Any]], interval: Any = "5minute", now: Optional[datetime] = None) -> List[Dict[str, Any]]:
    return [c for c in candles if candle_is_closed(c, interval, now)]


def current_session_candles(
    candles: Iterable[Dict[str, Any]], interval: Any = "5minute", now: Optional[datetime] = None,
    *, cas_eligible: bool | None = None,
) -> List[Dict[str, Any]]:
    clock = (now or datetime.now(IST)).astimezone(IST)
    rows = closed_candles(candles, interval, clock)
    if not rows:
        return []
    window = DEFAULT_TRADING_SESSION_AUTHORITY.continuous_window(clock.date(), cas_eligible=cas_eligible)
    if window is None:
        return []
    # Unknown CAS eligibility intentionally caps continuous evidence at 15:15;
    # callers requiring later actionability must supply the instrument flag.
    opened, closed = window.open_at(), window.close_at()
    result: List[Dict[str, Any]] = []
    for candle in rows:
        started = candle_datetime(candle)
        if started is not None and opened <= started <= closed:
            result.append(candle)
    return result


def latest_closed_age_seconds(candles: Iterable[Dict[str, Any]], interval: Any = "5minute", now: Optional[datetime] = None) -> Optional[int]:
    clock = (now or datetime.now(IST)).astimezone(IST)
    rows = closed_candles(candles, interval, clock)
    if not rows:
        return None
    started = candle_datetime(rows[-1])
    return max(0, int((clock - (started + timedelta(minutes=interval_minutes(interval)))).total_seconds())) if started else None
