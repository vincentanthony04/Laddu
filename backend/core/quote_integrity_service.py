"""Pure quote identity/freshness rules used by UI quote delivery and tests."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Mapping

from core.candle_freshness_service import CandleFreshnessService
from core.india_time import INDIA_TZ as IST


def parse_quote_time(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        if isinstance(value, (int, float)) or str(value).strip().isdigit():
            number = float(value)
            seconds = number / 1000.0 if number > 100_000_000_000 else number
            return datetime.fromtimestamp(seconds, tz=timezone.utc).astimezone(IST)
        raw = str(value).strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=IST)
        return dt.astimezone(IST)
    except Exception:
        return None


def classify_quote(
    quote: Dict[str, Any] | None,
    *,
    now: datetime,
    market_open: bool,
    max_live_age_sec: float = 45.0,
) -> Dict[str, Any]:
    q = dict(quote or {})
    # ``received_at`` is deliberately excluded.  A local HTTP receipt clock
    # proves transport completion, not when the exchange last formed/traded
    # the price.  Calling receipt time a provider timestamp can keep an old LTP
    # green and "live" forever.
    source_time = q.get("provider_timestamp") or q.get("source_time") or q.get("timestamp")
    dt = parse_quote_time(source_time)
    now_ist = now.astimezone(IST) if now.tzinfo else now.replace(tzinfo=IST)
    signed_age = (now_ist - dt).total_seconds() if dt else None
    age = max(0.0, signed_age) if signed_age is not None else None
    identity_ok = bool(q.get("identity_verified"))
    has_price = q.get("ltp") is not None
    if not has_price:
        state, reason = "missing", "price_missing"
    elif not identity_ok:
        state, reason = "invalid", "instrument_identity_unverified"
    elif dt is None:
        state, reason = "unverified", "provider_timestamp_missing"
    elif signed_age is not None and signed_age < -300:
        state, reason = "unverified", "provider_timestamp_in_future"
    elif market_open and age > max_live_age_sec:
        state, reason = "stale", "provider_quote_too_old"
    elif not market_open and dt.date().isoformat() != CandleFreshnessService.expected_intraday_date(now_ist):
        state, reason = "stale", "provider_quote_not_latest_completed_session"
    else:
        state, reason = "live" if market_open else "closed_market", "verified_exchange_snapshot"
    return {
        "state": state,
        "reason": reason,
        "source_time": source_time,
        "age_seconds": round(age, 3) if age is not None else None,
        "identity_verified": identity_ok,
        "usable_for_promotion": state == "live",
        "display_as_live": state == "live",
        "provider_timestamp_verified": dt is not None,
    }


def newer_quote(existing: Dict[str, Any] | None, incoming: Dict[str, Any] | None) -> bool:
    """Return true only when incoming must be allowed to replace existing."""
    old, new = dict(existing or {}), dict(incoming or {})
    if not old:
        return True
    old_live = str(old.get("freshness_state") or "").lower() == "live" and not old.get("stale")
    new_live = str(new.get("freshness_state") or "").lower() == "live" and not new.get("stale")
    if old_live and not new_live:
        return False
    old_dt = parse_quote_time(old.get("source_time") or old.get("timestamp") or old.get("received_time"))
    new_dt = parse_quote_time(new.get("source_time") or new.get("timestamp") or new.get("received_time"))
    if old_dt and new_dt:
        return new_dt >= old_dt
    if old_dt and not new_dt:
        return False
    return True


def revalidate_cached_quote(
    quote: Dict[str, Any] | None,
    *,
    now: datetime,
    market_open: bool,
    max_live_age_sec: float = 45.0,
) -> Dict[str, Any]:
    """Reclassify a cached quote without changing its provider timestamp.

    Cache receipt time is never promoted to exchange time.  The returned row is
    safe for display/promotion decisions at the caller's current market clock.
    """
    value = dict(quote or {})
    integrity = classify_quote(
        value, now=now, market_open=market_open, max_live_age_sec=max_live_age_sec
    )
    state = str(integrity.get("state") or "unverified")
    source_time = integrity.get("source_time")
    value.update({
        "source_time": source_time,
        "timestamp": source_time,
        "freshness_state": state,
        "freshness_reason": integrity.get("reason"),
        "freshness": state.replace("_", " ") + (f" @ {source_time}" if source_time else ""),
        "age_seconds": integrity.get("age_seconds"),
        "stale": state not in ("live", "closed_market"),
        "usable_for_promotion": bool(integrity.get("usable_for_promotion")),
        "display_as_live": bool(integrity.get("display_as_live")),
        "provider_timestamp_verified": bool(integrity.get("provider_timestamp_verified")),
    })
    return value


def visible_market_leader_symbols(
    coverage_cache: Mapping[str, Dict[str, Any]] | None,
    *,
    gainers_limit: int = 4,
    losers_limit: int = 4,
    active_limit: int = 8,
) -> list[str]:
    """Return the visible Market Trends leaders needing exact quote hydration."""
    rows: Iterable[Dict[str, Any]] = (coverage_cache or {}).values()
    coverage = [dict(row) for row in rows if isinstance(row, dict) and row.get("ltp") is not None]
    gainers = sorted(
        (row for row in coverage if float(row.get("change_pct") or 0.0) > 0),
        key=lambda row: float(row.get("change_pct") or 0.0),
        reverse=True,
    )[:gainers_limit]
    losers = sorted(
        (row for row in coverage if float(row.get("change_pct") or 0.0) < 0),
        key=lambda row: float(row.get("change_pct") or 0.0),
    )[:losers_limit]
    active = sorted(
        coverage,
        key=lambda row: (
            float(row.get("activity_score") or 0.0),
            abs(float(row.get("change_pct") or 0.0)),
        ),
        reverse=True,
    )[:active_limit]
    out: list[str] = []
    for row in gainers + losers + active:
        symbol = str(row.get("symbol") or row.get("trading_symbol") or "").upper().strip()
        if symbol and symbol not in out:
            out.append(symbol)
    return out
