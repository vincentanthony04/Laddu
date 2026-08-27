"""Shared India-market time and freshness primitives without runtime imports."""
from __future__ import annotations

from datetime import datetime, time as dtime, timedelta, timezone
import re
from typing import Any, Mapping

from core.trading_session_authority import DEFAULT_TRADING_SESSION_AUTHORITY


IST = timezone(timedelta(hours=5, minutes=30))


def india_now() -> datetime:
    return datetime.now(IST)


def is_india_market_open(now: datetime | None = None) -> bool:
    """Compatibility facade over TradingSessionAuthority; owns no calendar math."""
    current = now or india_now()
    return bool(DEFAULT_TRADING_SESSION_AUTHORITY.phase(current).get("market_open"))


def parse_timestamp(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        if isinstance(value, (int, float)):
            seconds = float(value) / 1000.0 if float(value) > 1_000_000_000_000 else float(value)
            return datetime.fromtimestamp(seconds, tz=timezone.utc).astimezone(IST)
        raw = str(value).strip()
        if raw.startswith("historical:"):
            raw = raw.split("historical:", 1)[1]
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00").replace(" ", "T"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=IST)
        return parsed.astimezone(IST)
    except Exception:
        match = re.search(r"\d{4}-\d{2}-\d{2}", str(value))
        if not match:
            return None
        try:
            return datetime.fromisoformat(match.group(0)).replace(tzinfo=IST)
        except ValueError:
            return None


def candle_staleness(interval: str, last_candle: Mapping[str, Any] | None) -> dict[str, Any]:
    # Lazy compatibility import avoids making the low-level market clock depend
    # on the higher-level candle freshness facade at module-import time.
    from core.candle_freshness_service import CandleFreshnessService
    return CandleFreshnessService.classify(interval, dict(last_candle or {}), now=india_now())


def symbol_key(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().upper())
