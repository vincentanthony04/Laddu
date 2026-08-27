"""Immutable historical exchange-session evidence for research and replay.

This authority never guesses that a missing calendar date is a holiday.  It
records only positively observed trading sessions from immutable exchange or
canonical market-data evidence and carries a fingerprint over that evidence.

Daily/weekly/monthly historical completeness can be proved from observed
session progression.  Intraday replay additionally requires an observed
session window; a date-only bhavcopy index is deliberately insufficient to
prove an intraday close boundary.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
import hashlib
import json
from typing import Any, Iterable, Mapping, Sequence

from core.india_time import INDIA_TZ
from core.market_clock import parse_timestamp

AUTHORITY_NAME = "HistoricalSessionIndexAuthority"
AUTHORITY_VERSION = "1.0.0"


def _as_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.astimezone(INDIA_TZ).date() if value.tzinfo else value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _clock(value: Any) -> time | None:
    if isinstance(value, time):
        return value.replace(tzinfo=None)
    if isinstance(value, datetime):
        local = value.astimezone(INDIA_TZ) if value.tzinfo else value
        return local.time().replace(tzinfo=None)
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return time.fromisoformat(text[:8])
    except ValueError:
        return None


@dataclass(frozen=True)
class HistoricalSessionEvidence:
    trade_date: date
    source: str
    content_hash: str | None = None
    open_time: time | None = None
    close_time: time | None = None

    @property
    def window_proven(self) -> bool:
        return self.open_time is not None and self.close_time is not None and self.open_time <= self.close_time

    def as_dict(self) -> dict[str, Any]:
        return {
            "trade_date": self.trade_date.isoformat(),
            "source": self.source,
            "content_hash": self.content_hash,
            "open_time": self.open_time.isoformat(timespec="seconds") if self.open_time else None,
            "close_time": self.close_time.isoformat(timespec="seconds") if self.close_time else None,
            "window_proven": self.window_proven,
        }


class HistoricalSessionIndexAuthority:
    authority = AUTHORITY_NAME
    authority_version = AUTHORITY_VERSION

    def __init__(self, records: Iterable[HistoricalSessionEvidence], *, source: str, source_fingerprint: str | None = None):
        by_date: dict[date, HistoricalSessionEvidence] = {}
        for record in records:
            existing = by_date.get(record.trade_date)
            # Prefer evidence that proves an intraday window over date-only
            # evidence, otherwise keep the first immutable observation.
            if existing is None or (record.window_proven and not existing.window_proven):
                by_date[record.trade_date] = record
        self._records = dict(sorted(by_date.items()))
        self.source = str(source or "UNSPECIFIED_OBSERVED_SESSION_EVIDENCE")
        canonical = [self._records[day].as_dict() for day in sorted(self._records)]
        calculated = hashlib.sha256(
            json.dumps(canonical, sort_keys=True, separators=(",", ":"), default=str).encode()
        ).hexdigest()
        self.session_index_fingerprint = str(source_fingerprint or calculated)

    @classmethod
    def from_records(
        cls,
        rows: Iterable[Mapping[str, Any]],
        *,
        source: str = "NSE_OFFICIAL_OBSERVED_SESSIONS",
        source_fingerprint: str | None = None,
    ) -> "HistoricalSessionIndexAuthority":
        records: list[HistoricalSessionEvidence] = []
        for row in rows or ():
            day = _as_date(row.get("trade_date") or row.get("date") or row.get("session_date"))
            if day is None:
                continue
            records.append(HistoricalSessionEvidence(
                trade_date=day,
                source=str(row.get("source") or row.get("source_key") or source),
                content_hash=str(row.get("content_hash") or "").strip() or None,
                open_time=_clock(row.get("open_time") or row.get("session_open")),
                close_time=_clock(row.get("close_time") or row.get("session_close")),
            ))
        return cls(records, source=source, source_fingerprint=source_fingerprint)

    @classmethod
    def from_session_dates(
        cls,
        dates: Iterable[Any],
        *,
        source: str,
        source_fingerprint: str | None = None,
    ) -> "HistoricalSessionIndexAuthority":
        rows = [
            {"trade_date": day, "source": source}
            for day in dates or ()
            if _as_date(day) is not None
        ]
        return cls.from_records(rows, source=source, source_fingerprint=source_fingerprint)

    @classmethod
    def from_canonical_intraday_bars(
        cls,
        bars: Iterable[Mapping[str, Any]],
        *,
        source: str,
        source_fingerprint: str | None = None,
    ) -> "HistoricalSessionIndexAuthority":
        grouped: dict[date, list[datetime]] = {}
        for bar in bars or ():
            stamp = parse_timestamp(bar.get("timestamp") or bar.get("time") or bar.get("date"))
            if stamp is None:
                continue
            local = stamp.astimezone(INDIA_TZ)
            grouped.setdefault(local.date(), []).append(local)
        records = [
            HistoricalSessionEvidence(
                trade_date=day,
                source=source,
                open_time=min(stamps).time().replace(tzinfo=None),
                close_time=max(stamps).time().replace(tzinfo=None),
            )
            for day, stamps in sorted(grouped.items()) if stamps
        ]
        return cls(records, source=source, source_fingerprint=source_fingerprint)

    @property
    def session_count(self) -> int:
        return len(self._records)

    @property
    def session_dates(self) -> tuple[date, ...]:
        return tuple(self._records)

    @property
    def coverage_start(self) -> date | None:
        return min(self._records) if self._records else None

    @property
    def coverage_end(self) -> date | None:
        return max(self._records) if self._records else None

    @property
    def window_coverage_count(self) -> int:
        return sum(record.window_proven for record in self._records.values())

    def observed_session(self, value: Any) -> bool:
        day = _as_date(value)
        return bool(day is not None and day in self._records)

    def latest_observed_on_or_before(self, value: Any) -> date | None:
        day = _as_date(value)
        if day is None:
            return None
        candidates = [candidate for candidate in self._records if candidate <= day]
        return max(candidates) if candidates else None

    def evidence(self, value: Any) -> dict[str, Any]:
        day = _as_date(value)
        record = self._records.get(day) if day is not None else None
        if record is None:
            return {
                "authority": self.authority,
                "authority_version": self.authority_version,
                "state": "UNKNOWN",
                "observed_session": False,
                "decision_usable": False,
                "trade_date": day.isoformat() if day else None,
                "reason": "date is not positively observed in the historical session index; absence is not classified as a holiday",
                "session_index_fingerprint": self.session_index_fingerprint,
                "source": self.source,
            }
        return {
            "authority": self.authority,
            "authority_version": self.authority_version,
            "state": "OBSERVED_SESSION",
            "observed_session": True,
            "decision_usable": True,
            "trade_date": record.trade_date.isoformat(),
            "reason": "trading session positively observed in immutable historical evidence",
            "session_index_fingerprint": self.session_index_fingerprint,
            "source": self.source,
            "content_hash": record.content_hash,
            "open_time": record.open_time.isoformat(timespec="seconds") if record.open_time else None,
            "close_time": record.close_time.isoformat(timespec="seconds") if record.close_time else None,
            "window_proven": record.window_proven,
        }

    def same_intraday_session(self, observed_at: datetime, bar_at: datetime) -> bool:
        observed_local = observed_at.astimezone(INDIA_TZ)
        bar_local = bar_at.astimezone(INDIA_TZ)
        if observed_local.date() != bar_local.date():
            return False
        record = self._records.get(observed_local.date())
        if record is None or not record.window_proven:
            return False
        observed_clock = observed_local.time().replace(tzinfo=None)
        bar_clock = bar_local.time().replace(tzinfo=None)
        return bool(record.open_time <= observed_clock <= record.close_time and record.open_time <= bar_clock <= record.close_time)

    def completed_period(self, interval: str, value: Any, *, as_of: Any | None = None) -> dict[str, Any]:
        day = _as_date(value)
        ref = _as_date(as_of) if as_of is not None else self.coverage_end
        if day is None or ref is None:
            return self._period_result("MISSING", False, day, ref, "period date or as-of date unavailable")
        if day not in self._records:
            return self._period_result("UNKNOWN", False, day, ref, "period date is not a positively observed trading session")
        key = str(interval or "").strip().lower()
        if key in {"day", "1d", "daily"}:
            return self._period_result("COMPLETE", True, day, ref, "daily session is positively observed")
        later = [candidate for candidate in self._records if day < candidate <= ref]
        if key in {"week", "1w", "weekly"}:
            iso = day.isocalendar()[:2]
            progressed = any(candidate.isocalendar()[:2] != iso for candidate in later)
            return self._period_result(
                "COMPLETE" if progressed else "UNPROVEN_CURRENT_PERIOD", progressed, day, ref,
                "observed session index has progressed into a later ISO week" if progressed else "no later observed week proves this historical week complete",
            )
        if key in {"month", "1m", "monthly"}:
            period = (day.year, day.month)
            progressed = any((candidate.year, candidate.month) != period for candidate in later)
            return self._period_result(
                "COMPLETE" if progressed else "UNPROVEN_CURRENT_PERIOD", progressed, day, ref,
                "observed session index has progressed into a later month" if progressed else "no later observed month proves this historical month complete",
            )
        raise ValueError(f"completed_period supports only day/week/month, got {interval!r}")

    def continuity(self, observed_dates: Iterable[Any], *, start: Any | None = None, end: Any | None = None) -> dict[str, Any]:
        actual = {_as_date(item) for item in observed_dates or ()}
        actual.discard(None)
        if not actual:
            return {
                "authority": self.authority,
                "authority_version": self.authority_version,
                "state": "MISSING",
                "decision_usable": False,
                "required_session_count": 0,
                "observed_count": 0,
                "missing_sessions": [],
                "session_index_fingerprint": self.session_index_fingerprint,
            }
        begin = _as_date(start) or min(actual)
        finish = _as_date(end) or max(actual)
        required = [day for day in self._records if begin <= day <= finish]
        missing = [day.isoformat() for day in required if day not in actual]
        return {
            "authority": self.authority,
            "authority_version": self.authority_version,
            "state": "COMPLETE" if required and not missing else ("GAP" if missing else "UNPROVEN"),
            "decision_usable": bool(required and not missing),
            "required_session_count": len(required),
            "observed_count": sum(day in actual for day in required),
            "missing_sessions": missing,
            "coverage_start": begin.isoformat() if begin else None,
            "coverage_end": finish.isoformat() if finish else None,
            "session_index_fingerprint": self.session_index_fingerprint,
            "source": self.source,
        }

    def summary(self) -> dict[str, Any]:
        return {
            "authority": self.authority,
            "authority_version": self.authority_version,
            "source": self.source,
            "session_index_fingerprint": self.session_index_fingerprint,
            "session_count": self.session_count,
            "window_coverage_count": self.window_coverage_count,
            "coverage_start": self.coverage_start.isoformat() if self.coverage_start else None,
            "coverage_end": self.coverage_end.isoformat() if self.coverage_end else None,
        }

    def _period_result(self, state: str, usable: bool, day: date | None, ref: date | None, reason: str) -> dict[str, Any]:
        return {
            "authority": self.authority,
            "authority_version": self.authority_version,
            "state": state,
            "decision_usable": bool(usable),
            "period_date": day.isoformat() if day else None,
            "as_of": ref.isoformat() if ref else None,
            "reason": reason,
            "session_index_fingerprint": self.session_index_fingerprint,
            "source": self.source,
        }
