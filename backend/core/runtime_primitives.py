from __future__ import annotations

import re
from datetime import datetime, time as dtime, timedelta, timezone
from typing import Any, Dict

from core.candle_freshness_service import CandleFreshnessService
from core.trading_session_authority import DEFAULT_TRADING_SESSION_AUTHORITY
from session_candles import candle_is_closed, latest_closed_age_seconds

IST = timezone(timedelta(hours=5, minutes=30))

def india_now() -> datetime:
    """Return India Standard Time explicitly; do not depend on Windows/server local timezone."""
    return datetime.now(IST)

def is_india_market_open() -> bool:
    """Compatibility facade over TradingSessionAuthority; owns no calendar math."""
    return bool(DEFAULT_TRADING_SESSION_AUTHORITY.phase(india_now()).get("market_open"))

def minutes_to_close() -> int | None:
    now = india_now()
    phase = DEFAULT_TRADING_SESSION_AUTHORITY.phase(now)
    close_raw = phase.get("session_close")
    if not close_raw:
        return None
    close_dt = datetime.fromisoformat(str(close_raw))
    if phase.get("phase") == "PRE_OPEN":
        return max(0, int((close_dt - now).total_seconds() // 60))
    if not phase.get("market_open"):
        return None
    return max(0, int((close_dt - now).total_seconds() // 60))

def _parse_candle_ist_date(ts: Any) -> str | None:
    if ts in (None, ""):
        return None
    try:
        if isinstance(ts, (int, float)):
            seconds = float(ts) / 1000.0 if float(ts) > 1000000000000 else float(ts)
            return datetime.fromtimestamp(seconds, tz=timezone.utc).astimezone(IST).date().isoformat()
        raw = str(ts).strip()
        if not raw:
            return None
        # Upstox often returns ISO strings with +05:30. Python handles those after normalizing Z.
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00").replace(" ", "T"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=IST)
        return parsed.astimezone(IST).date().isoformat()
    except Exception:
        m = re.search(r"\d{4}-\d{2}-\d{2}", str(ts))
        return m.group(0) if m else None

def candle_staleness(interval: str, last_candle: Dict[str, Any] | None) -> Dict[str, Any]:
    return CandleFreshnessService.classify(interval, last_candle, now=india_now())

def _parse_ts_datetime(ts: Any) -> datetime | None:
    if ts in (None, ""):
        return None
    try:
        if isinstance(ts, (int, float)):
            seconds = float(ts) / 1000.0 if float(ts) > 1000000000000 else float(ts)
            return datetime.fromtimestamp(seconds, tz=timezone.utc).astimezone(IST)
        raw = str(ts).strip()
        if raw.startswith("historical:"):
            raw = raw.split("historical:", 1)[1]
        if not raw:
            return None
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00").replace(" ", "T"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=IST)
        return dt.astimezone(IST)
    except Exception:
        m = re.search(r"\d{4}-\d{2}-\d{2}", str(ts))
        if m:
            try:
                return datetime.fromisoformat(m.group(0)).replace(tzinfo=IST)
            except Exception:
                return None
    return None

def quote_freshness_guard(mode: str, quote: Dict[str, Any] | None, candles: list[Dict[str, Any]] | None, interval: str) -> Dict[str, Any]:
    """v36.9 trust gate: classify quote/candle data before an engine may promote.

    Intraday is deliberately strict. Delivery may use validated historical
    evidence, but the frontend still sees delayed/historical state explicitly.
    """
    mode = str(mode or "").lower()
    quote = quote or {}
    candles = candles or []
    ts = quote.get("timestamp") or quote.get("source_time")
    raw_ts = str(ts or "")
    now = india_now()
    q_dt = _parse_ts_datetime(ts)
    q_age = int((now - q_dt).total_seconds()) if q_dt else None
    same_day = mode == "intraday"
    if not quote or quote.get("ltp") is None:
        q_state = "pending"
    elif raw_ts.startswith("historical:") or (quote.get("raw") or {}).get("source") == "historical_candle_fallback":
        q_state = "historical"
    elif q_age is None:
        q_state = "delayed" if same_day else "cached_daily_unknown_age"
    else:
        max_age = 20 if mode == "intraday" else 86400 * 4
        q_state = "live" if same_day and q_age <= max_age else "stale" if same_day else "live" if is_india_market_open() and q_age <= 60 else "cached_daily_current" if q_age <= max_age else "stale_daily"

    last_candle = candles[-1] if candles else None
    cmeta = candle_staleness(interval, last_candle)
    c_dt = _parse_ts_datetime((last_candle or {}).get("timestamp") or (last_candle or {}).get("time") or (last_candle or {}).get("date"))
    c_age = int((now - c_dt).total_seconds()) if c_dt else None
    c_state = "pending" if not candles else ("forming" if same_day and not candle_is_closed(last_candle, interval, now) else "stale" if cmeta.get("stale_candles") else "fresh")
    if same_day and is_india_market_open() and c_age is not None:
        closed_age = latest_closed_age_seconds(candles, interval, now)
        c_state = "fresh" if closed_age is not None and closed_age <= 420 else "delayed_warning" if closed_age is not None and closed_age <= 720 else "stale"
    blocked = bool(same_day and (q_state in ("pending", "historical", "delayed", "stale", "invalid") or c_state in ("pending", "forming", "delayed", "stale", "invalid")))
    return {
        "state": q_state,
        "quote_state": q_state,
        "quote_age_seconds": q_age,
        "quote_timestamp": ts,
        "candle_state": c_state,
        "candle_age_seconds": c_age,
        "candle_timestamp": (last_candle or {}).get("timestamp") or (last_candle or {}).get("time") or (last_candle or {}).get("date"),
        "same_day_blocked": blocked,
        "stale_guard": "blocked" if blocked else "pass",
        **cmeta,
    }

def symbolKey_py(v: Any) -> str:
    return re.sub(r"\s+", " ", str(v or "").strip().upper())

def mode_uses_history_without_live(mode: str) -> bool:
    return str(mode or "").lower().strip() == "delivery"

