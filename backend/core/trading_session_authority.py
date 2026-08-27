"""Single exchange-session authority for NSE/BSE cash decision use.

The authority owns trading-day, market-phase, previous/next trading-session and
completed-period semantics.  Callers must not infer sessions from weekday/time
alone.  The default production calendar is immutable release evidence; a
future release can add newer exchange circulars by replacing the calendar
resource and bumping ``authority_version``.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
from datetime import date, datetime, time, timedelta
import json
from pathlib import Path
from typing import Any, Iterable, Mapping

from core.india_time import INDIA_TZ

AUTHORITY_NAME = "TradingSessionAuthority"
AUTHORITY_VERSION = "1.1.0-cas-phase1-instrument-aware"
_RESOURCE = Path(__file__).resolve().parents[1] / "resources" / "nse_equity_calendar_2026.json"


def _as_date(value: date | datetime | str) -> date:
    if isinstance(value, datetime):
        return value.astimezone(INDIA_TZ).date() if value.tzinfo else value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _parse_clock(value: str) -> time:
    hour, minute = (int(part) for part in str(value).split(":", 1))
    return time(hour, minute)


@dataclass(frozen=True)
class SessionWindow:
    trade_date: date
    open_time: time
    close_time: time
    kind: str = "REGULAR"
    reason: str | None = None

    def open_at(self) -> datetime:
        return datetime.combine(self.trade_date, self.open_time, tzinfo=INDIA_TZ)

    def close_at(self) -> datetime:
        return datetime.combine(self.trade_date, self.close_time, tzinfo=INDIA_TZ)


class TradingSessionAuthority:
    authority = AUTHORITY_NAME
    authority_version = AUTHORITY_VERSION

    def __init__(self, calendar: Mapping[str, Any] | None = None):
        payload = dict(calendar or json.loads(_RESOURCE.read_text(encoding="utf-8")))
        self.calendar = payload
        self.exchange = str(payload.get("exchange") or "NSE")
        self.segment = str(payload.get("segment") or "CAPITAL_MARKET_EQUITIES")
        self.coverage_start = _as_date(payload["coverage_start"])
        self.coverage_end = _as_date(payload["coverage_end"])
        self.holidays = frozenset(_as_date(item) for item in payload.get("holidays") or [])
        self.special_sessions = {
            _as_date(day): dict(spec or {}) for day, spec in (payload.get("special_sessions") or {}).items()
        }
        self.non_regular_notices = {
            _as_date(day): dict(spec or {}) for day, spec in (payload.get("non_regular_notices") or {}).items()
        }
        regular = dict(payload.get("regular_session") or {})
        self.regular_open = _parse_clock(regular.get("open") or "09:15")
        self.regular_close = _parse_clock(regular.get("close") or "15:30")
        cas = dict(payload.get("closing_auction_session") or {})
        self.cas_effective_from = _as_date(cas.get("effective_from") or "9999-12-31")
        self.cas_eligibility = str(cas.get("eligibility") or "")
        self.cas_continuous_close = _parse_clock(cas.get("continuous_close_for_cas") or "15:15")
        self.cas_reference_start = _parse_clock((cas.get("reference_transition") or {}).get("start") or "15:15")
        self.cas_reference_end = _parse_clock((cas.get("reference_transition") or {}).get("end") or "15:20")
        self.cas_order_start = _parse_clock((cas.get("order_entry") or {}).get("start") or "15:20")
        self.cas_order_end = _parse_clock((cas.get("order_entry") or {}).get("end") or "15:30")
        self.cas_match_start = _parse_clock((cas.get("matching") or {}).get("start") or "15:30")
        self.cas_match_end = _parse_clock((cas.get("matching") or {}).get("end") or "15:35")
        self.cas_post_close_start = _parse_clock((cas.get("post_close") or {}).get("start") or "15:50")
        self.cas_post_close_end = _parse_clock((cas.get("post_close") or {}).get("end") or "16:00")
        self.calendar_fingerprint = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
        ).hexdigest()

    def calendar_covered(self, value: date | datetime | str) -> bool:
        day = _as_date(value)
        return self.coverage_start <= day <= self.coverage_end

    def session_window(self, value: date | datetime | str) -> SessionWindow | None:
        day = _as_date(value)
        special = self.special_sessions.get(day)
        if special:
            return SessionWindow(
                day,
                _parse_clock(special.get("open") or "09:15"),
                _parse_clock(special.get("close") or "15:30"),
                kind="SPECIAL",
                reason=str(special.get("reason") or "special live trading session"),
            )
        if day.weekday() >= 5 or day in self.holidays or day in self.non_regular_notices:
            return None
        if not self.calendar_covered(day):
            return None
        return SessionWindow(day, self.regular_open, self.regular_close)

    def cas_active(self, value: date | datetime | str) -> bool:
        day = _as_date(value)
        return day >= self.cas_effective_from and self.is_trading_day_calendar_only(day)

    def is_trading_day_calendar_only(self, value: date | datetime | str) -> bool:
        return self.session_window(value) is not None

    def continuous_window(self, value: date | datetime | str, *, cas_eligible: bool | None = None) -> SessionWindow | None:
        """Instrument-aware continuous-trading window.

        After CAS Phase-1 activation, eligible stocks leave the normal
        continuous session at 15:15. Non-CAS securities remain continuous to
        15:30. Unknown eligibility is represented by ``None`` and callers that
        need post-15:15 actionability must fail closed rather than guess.
        """
        base = self.session_window(value)
        if base is None:
            return None
        if base.trade_date < self.cas_effective_from or base.kind == "SPECIAL":
            return base
        if cas_eligible is True:
            return SessionWindow(base.trade_date, base.open_time, self.cas_continuous_close, kind="REGULAR_CAS_ELIGIBLE", reason="CAS Phase-1 eligible; continuous trading ends before closing auction")
        if cas_eligible is False:
            return SessionWindow(base.trade_date, base.open_time, self.regular_close, kind="REGULAR_NON_CAS", reason="non-CAS security remains in continuous trading")
        return SessionWindow(base.trade_date, base.open_time, self.cas_continuous_close, kind="REGULAR_CAS_ELIGIBILITY_UNKNOWN", reason="instrument CAS eligibility required after 15:15")

    def is_trading_day(self, value: date | datetime | str) -> bool:
        return self.session_window(value) is not None

    def previous_trading_day(self, value: date | datetime | str) -> date:
        cursor = _as_date(value) - timedelta(days=1)
        for _ in range(370):
            if self.session_window(cursor):
                return cursor
            cursor -= timedelta(days=1)
        raise RuntimeError("previous trading session not found inside authority horizon")

    def next_trading_day(self, value: date | datetime | str) -> date:
        cursor = _as_date(value) + timedelta(days=1)
        for _ in range(370):
            if self.session_window(cursor):
                return cursor
            cursor += timedelta(days=1)
        raise RuntimeError("next trading session not found inside authority horizon")

    def phase(self, at: datetime | None = None, *, cas_eligible: bool | None = None) -> dict[str, Any]:
        current = at or datetime.now(INDIA_TZ)
        if current.tzinfo is None:
            current = current.replace(tzinfo=INDIA_TZ)
        else:
            current = current.astimezone(INDIA_TZ)
        base = self.session_window(current.date())
        covered = self.calendar_covered(current.date())
        if base is None:
            phase = "MARKET_CLOSED" if covered else "CALENDAR_UNVERIFIED"
            return {
                "authority": self.authority, "authority_version": self.authority_version,
                "calendar_covered": covered, "trade_date": current.date().isoformat(),
                "phase": phase, "market_open": False, "decision_usable": False,
                "session_kind": None, "session_open": None, "session_close": None,
                "cas_active": False, "cas_eligible": cas_eligible,
            }
        opened = base.open_at()
        if current < opened:
            phase, market_open, usable = "PRE_OPEN", False, False
        elif base.trade_date < self.cas_effective_from or base.kind == "SPECIAL":
            closed = base.close_at()
            phase = "OPEN" if current <= closed else "CLOSED_AT_SESSION"
            market_open = phase == "OPEN"
            usable = market_open
        else:
            tod = current.timetz().replace(tzinfo=None)
            if tod < self.cas_reference_start:
                phase, market_open, usable = "OPEN", True, True
            elif cas_eligible is None:
                phase, market_open, usable = "CAS_ELIGIBILITY_REQUIRED", False, False
            elif cas_eligible is False:
                if tod <= self.regular_close:
                    phase, market_open, usable = "OPEN_NON_CAS", True, True
                else:
                    phase, market_open, usable = "CLOSED_AT_SESSION", False, False
            elif tod < self.cas_reference_end:
                phase, market_open, usable = "CAS_REFERENCE_TRANSITION", False, False
            elif tod < self.cas_order_end:
                phase, market_open, usable = "CAS_ORDER_ENTRY", True, False
            elif tod < self.cas_match_end:
                phase, market_open, usable = "CAS_MATCHING", False, False
            elif self.cas_post_close_start <= tod <= self.cas_post_close_end:
                phase, market_open, usable = "POST_CLOSE", True, False
            else:
                phase, market_open, usable = "CLOSED_AT_SESSION", False, False
        continuous = self.continuous_window(current.date(), cas_eligible=cas_eligible)
        return {
            "authority": self.authority, "authority_version": self.authority_version,
            "calendar_covered": True, "trade_date": base.trade_date.isoformat(),
            "phase": phase, "market_open": market_open, "decision_usable": usable,
            "session_kind": continuous.kind if continuous else base.kind,
            "session_reason": continuous.reason if continuous else base.reason,
            "session_open": opened.isoformat(timespec="seconds"),
            "session_close": (continuous.close_at() if continuous else base.close_at()).isoformat(timespec="seconds"),
            "cas_active": base.trade_date >= self.cas_effective_from,
            "cas_eligible": cas_eligible,
            "cas_eligibility_rule": self.cas_eligibility or None,
            "cas_order_entry": f"{self.cas_order_start.strftime('%H:%M')}-{self.cas_order_end.strftime('%H:%M')}" if base.trade_date >= self.cas_effective_from else None,
            "cas_matching": f"{self.cas_match_start.strftime('%H:%M')}-{self.cas_match_end.strftime('%H:%M')}" if base.trade_date >= self.cas_effective_from else None,
        }

    def last_completed_trading_day(self, at: datetime | None = None) -> date:
        current = at or datetime.now(INDIA_TZ)
        if current.tzinfo is None:
            current = current.replace(tzinfo=INDIA_TZ)
        else:
            current = current.astimezone(INDIA_TZ)
        window = self.session_window(current.date())
        if window and current > window.close_at():
            return current.date()
        return self.previous_trading_day(current.date())

    def last_trading_day_of_week(self, value: date | datetime | str) -> date:
        day = _as_date(value)
        monday = day - timedelta(days=day.weekday())
        sunday = monday + timedelta(days=6)
        candidates = [monday + timedelta(days=i) for i in range(7)]
        trading = [candidate for candidate in candidates if candidate <= sunday and self.session_window(candidate)]
        if not trading:
            raise RuntimeError("week has no trading session inside calendar authority")
        return trading[-1]

    def last_trading_day_of_month(self, value: date | datetime | str) -> date:
        day = _as_date(value)
        if day.month == 12:
            cursor = date(day.year + 1, 1, 1) - timedelta(days=1)
        else:
            cursor = date(day.year, day.month + 1, 1) - timedelta(days=1)
        for _ in range(10):
            if self.session_window(cursor):
                return cursor
            cursor -= timedelta(days=1)
        raise RuntimeError("month has no trading session inside calendar authority")

    def period_complete(self, interval: str, candle_day: date | datetime | str, *, at: datetime | None = None) -> bool:
        interval = str(interval or "").strip().lower()
        day = _as_date(candle_day)
        completed_day = self.last_completed_trading_day(at)
        if interval in {"day", "1d", "daily"}:
            return day <= completed_day and self.is_trading_day(day)
        if interval in {"week", "1w", "weekly"}:
            return completed_day >= self.last_trading_day_of_week(day)
        if interval in {"month", "1m", "monthly"}:
            return completed_day >= self.last_trading_day_of_month(day)
        raise ValueError(f"period_complete supports only day/week/month, got {interval!r}")


DEFAULT_TRADING_SESSION_AUTHORITY = TradingSessionAuthority()
