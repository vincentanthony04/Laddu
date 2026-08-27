"""Canonical decision-use completeness/freshness classification.

This module deliberately does not calculate trading indicators.  It answers one
question for every consumer: is the supplied evidence complete/fresh enough to
be used for the requested decision horizon?

Current-session truth comes from TradingSessionAuthority.  Historical research
can opt into an immutable HistoricalSessionIndexAuthority; missing historical
dates remain UNKNOWN and are never guessed to be holidays.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping

from core.india_time import INDIA_TZ
from core.market_clock import parse_timestamp
from core.trading_session_authority import DEFAULT_TRADING_SESSION_AUTHORITY, TradingSessionAuthority
from core.historical_session_index_authority import HistoricalSessionIndexAuthority

AUTHORITY_NAME = "CompletenessFreshnessAuthority"
AUTHORITY_VERSION = "1.1.0"


class CompletenessFreshnessAuthority:
    authority = AUTHORITY_NAME
    authority_version = AUTHORITY_VERSION

    def __init__(
        self,
        sessions: TradingSessionAuthority | None = None,
        historical_sessions: HistoricalSessionIndexAuthority | None = None,
    ):
        self.sessions = sessions or DEFAULT_TRADING_SESSION_AUTHORITY
        self.historical_sessions = historical_sessions

    @staticmethod
    def _local(value: datetime) -> datetime:
        return value.replace(tzinfo=INDIA_TZ) if value.tzinfo is None else value.astimezone(INDIA_TZ)

    def with_historical_sessions(self, historical_sessions: HistoricalSessionIndexAuthority | None):
        return CompletenessFreshnessAuthority(self.sessions, historical_sessions)

    def session_evidence(self, source_time: Any, *, at: datetime | None = None) -> dict[str, Any]:
        observed = parse_timestamp(source_time)
        current = self._local(at or datetime.now(INDIA_TZ))
        if observed is None:
            return self._result("MISSING", False, "source timestamp unavailable", None)
        observed = self._local(observed)
        if observed > current:
            return self._result("INVALID", False, "source timestamp is in the future", observed)

        if self.sessions.calendar_covered(current.date()):
            last_complete = self.sessions.last_completed_trading_day(current)
            phase = self.sessions.phase(current)
            if phase["market_open"] and observed.date() == current.date():
                return self._result("LIVE", True, "current trading session evidence", observed)
            if observed.date() == last_complete:
                return self._result("CURRENT_AT_CLOSE", True, "latest completed trading session", observed)
            return self._result("STALE", False, f"latest completed trading session is {last_complete.isoformat()}", observed)

        historical = self.historical_sessions
        if historical is None:
            return self._result("CALENDAR_UNVERIFIED", False, "exchange calendar/session index does not cover decision date", observed)
        evidence = historical.evidence(observed.date())
        if not evidence.get("observed_session"):
            return self._result(
                "HISTORICAL_SESSION_UNVERIFIED", False,
                "source date is not positively observed in the historical session index", observed,
                historical=evidence,
            )
        latest = historical.latest_observed_on_or_before(current.date())
        if latest is None:
            return self._result(
                "HISTORICAL_SESSION_UNVERIFIED", False,
                "historical session index has no observed session on or before decision date", observed,
                historical=evidence,
            )
        if observed.date() == latest:
            return self._result(
                "HISTORICAL_OBSERVED_SESSION", True,
                "latest positively observed historical trading session", observed,
                historical=evidence,
            )
        return self._result(
            "STALE", False, f"latest positively observed historical session is {latest.isoformat()}", observed,
            historical=evidence,
        )

    def completed_period(self, interval: str, candle: Mapping[str, Any], *, at: datetime | None = None) -> dict[str, Any]:
        stamp = parse_timestamp(candle.get("timestamp") or candle.get("time") or candle.get("date"))
        if stamp is None:
            return self._result("MISSING", False, "candle timestamp unavailable", None)
        current = self._local(at or datetime.now(INDIA_TZ))
        try:
            if self.sessions.calendar_covered(current.date()):
                complete = self.sessions.period_complete(interval, stamp.date(), at=current)
                state = "COMPLETE" if complete else "FORMING"
                reason = "completed exchange period" if complete else "period still forming under exchange calendar"
                return self._result(state, complete, reason, stamp)
            if self.historical_sessions is None:
                return self._result(
                    "CALENDAR_UNVERIFIED", False,
                    "historical period cannot be classified without an observed-session index", stamp,
                )
            result = self.historical_sessions.completed_period(interval, stamp.date(), as_of=current.date())
            return self._result(
                str(result.get("state") or "UNVERIFIED"),
                bool(result.get("decision_usable")),
                str(result.get("reason") or "historical period evidence unavailable"),
                stamp,
                historical=result,
            )
        except ValueError:
            return self._result("UNSUPPORTED", False, f"interval {interval!r} is not a completed-period authority interval", stamp)

    def historical_continuity(
        self,
        observed_dates: Any,
        *,
        start: Any | None = None,
        end: Any | None = None,
    ) -> dict[str, Any]:
        if self.historical_sessions is None:
            return {
                "authority": self.authority,
                "authority_version": self.authority_version,
                "state": "SESSION_INDEX_UNAVAILABLE",
                "decision_usable": False,
                "reason": "historical session index is required for continuity proof",
            }
        result = self.historical_sessions.continuity(observed_dates, start=start, end=end)
        return {
            "authority": self.authority,
            "authority_version": self.authority_version,
            "state": result["state"],
            "decision_usable": result["decision_usable"],
            "reason": "all positively observed exchange sessions are present" if result["decision_usable"] else "one or more positively observed exchange sessions are missing or coverage is unproven",
            "historical_session_evidence": result,
        }

    def _result(
        self,
        state: str,
        usable: bool,
        reason: str,
        observed: datetime | None,
        *,
        historical: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = {
            "authority": self.authority,
            "authority_version": self.authority_version,
            "state": state,
            "decision_usable": bool(usable),
            "reason": reason,
            "source_time": observed.isoformat() if observed else None,
        }
        if historical:
            payload["historical_session_evidence"] = dict(historical)
            payload["session_index_fingerprint"] = historical.get("session_index_fingerprint")
        return payload


DEFAULT_COMPLETENESS_FRESHNESS_AUTHORITY = CompletenessFreshnessAuthority()
