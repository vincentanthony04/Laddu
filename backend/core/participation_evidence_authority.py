"""Canonical participation-evidence semantics for Delivery and Intraday.

The authority intentionally preserves distinct participation measurements rather
than collapsing every volume ratio into one generic ``RVOL`` number:

* Intraday: latest completed bar volume versus the same clock-slot on prior
  sessions.  This is a point-in-time participation confirmation used by ORB/VWAP
  logic.  The historical formula is preserved; this authority adds explicit
  provenance/freshness and decision-usability semantics.
* Delivery: recent completed-session average volume versus its prior completed
  session baseline.  This is a multi-day participation/accumulation context.
* Radar: current cumulative volume versus completed full-day average is a
  discovery diagnostic only and never receives promotion authority here.

No provider I/O occurs in this module.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable, Mapping

from core.completeness_freshness_authority import (
    DEFAULT_COMPLETENESS_FRESHNESS_AUTHORITY,
    CompletenessFreshnessAuthority,
)
from core.india_time import INDIA_TZ
from core.market_clock import parse_timestamp
from core.historical_session_index_authority import HistoricalSessionIndexAuthority
from core.numeric_semantics import finite_number, nonnegative_number, positive_number

AUTHORITY_NAME = "ParticipationEvidenceAuthority"
AUTHORITY_VERSION = "1.2.0-strict-finite-volume-contract"

INTRADAY_LANE = "INTRADAY_SAME_CLOCK_COMPLETED_BAR_RVOL"
DELIVERY_LANE = "DELIVERY_RECENT_COMPLETED_SESSION_VOLUME"
RADAR_LANE = "RADAR_CUMULATIVE_VS_COMPLETED_FULL_DAY_DIAGNOSTIC"


def _number(value: Any) -> float | None:
    """Canonical finite-number decoder; bool/NaN/inf never become evidence."""
    return finite_number(value)


def _row_time(row: Mapping[str, Any]) -> datetime | None:
    return parse_timestamp(row.get("timestamp") or row.get("time") or row.get("date"))


class ParticipationEvidenceAuthority:
    authority = AUTHORITY_NAME
    authority_version = AUTHORITY_VERSION

    def __init__(
        self,
        completeness: CompletenessFreshnessAuthority | None = None,
        historical_sessions: HistoricalSessionIndexAuthority | None = None,
    ):
        base = completeness or DEFAULT_COMPLETENESS_FRESHNESS_AUTHORITY
        self.completeness = base.with_historical_sessions(historical_sessions) if historical_sessions is not None else base
        self.historical_sessions = historical_sessions or getattr(self.completeness, "historical_sessions", None)

    def intraday_same_clock_rvol(
        self,
        candles: Iterable[Mapping[str, Any]],
        *,
        at: datetime | None = None,
    ) -> dict[str, Any]:
        """Preserve Laddu's historical same-clock-bar RVOL with explicit proof.

        The newest completed intraday bar is compared with prior-session bars at
        the exact same clock slot.  This is deliberately *not* cumulative-day
        volume and must not be confused with the radar diagnostic.
        """
        rows = [dict(row) for row in candles or ()]
        stamped = [(row, _row_time(row)) for row in rows]
        stamped = [(row, stamp.astimezone(INDIA_TZ)) for row, stamp in stamped if stamp is not None]
        if not stamped:
            return self._result(INTRADAY_LANE, None, None, False, "MISSING", "no timestamped intraday bars", 0)
        stamped.sort(key=lambda item: item[1])
        latest_row, latest_at = stamped[-1]
        latest_volume = nonnegative_number(latest_row.get("volume"))
        if latest_volume is None:
            return self._result(INTRADAY_LANE, None, latest_at, False, "MISSING", "latest completed bar volume is missing/non-finite/negative", 0)

        slot = latest_at.timetz().replace(tzinfo=None)
        baseline_rows = [
            row for row, stamp in stamped[:-1]
            if stamp.date() < latest_at.date() and stamp.timetz().replace(tzinfo=None) == slot
        ]
        if not baseline_rows:
            return self._result(INTRADAY_LANE, None, latest_at, False, "BASELINE_MISSING", "same-clock prior-session volume baseline unavailable", 0)
        baseline = [positive_number(row.get("volume")) for row in baseline_rows]
        if any(value is None for value in baseline):
            return self._result(INTRADAY_LANE, None, latest_at, False, "INVALID_BASELINE", "same-clock prior-session volume baseline contains missing/non-finite/non-positive evidence", 0)

        baseline_volume = sum(baseline) / len(baseline)
        value = latest_volume / baseline_volume if baseline_volume > 0 else None
        current = at or datetime.now(INDIA_TZ)
        if current.tzinfo is None:
            current = current.replace(tzinfo=INDIA_TZ)
        else:
            current = current.astimezone(INDIA_TZ)
        freshness = self.completeness.session_evidence(latest_at, at=current)
        phase = self.completeness.sessions.phase(current)
        # Same-day Intraday promotion is allowed only inside a positively proved
        # session.  Current sessions use the official calendar; historical replay
        # requires an immutable observed-session window and never guesses 15:30.
        current_session_open = phase.get("market_open") is True
        historical_session_open = (
            not self.completeness.sessions.calendar_covered(current.date())
            and self.historical_sessions is not None
            and self.historical_sessions.same_intraday_session(latest_at, current) is True
        )
        usable = (
            value is not None
            and freshness.get("decision_usable") is True
            and latest_at.date() == current.date()
            and (current_session_open or historical_session_open)
        )
        state = "READY" if usable else "NON_ACTIONABLE"
        reason = "same-clock completed-bar participation is current" if usable else (
            "intraday participation is not from a currently proved exchange session"
        )
        return self._result(
            INTRADAY_LANE, value, latest_at, usable, state, reason, len(baseline),
            baseline_value=baseline_volume,
            freshness=freshness,
        )

    def delivery_recent_volume(
        self,
        candles: Iterable[Mapping[str, Any]],
        *,
        at: datetime | None = None,
        recent_sessions: int = 5,
        baseline_sessions: int = 25,
    ) -> dict[str, Any]:
        """Recent completed-session volume versus its preceding baseline."""
        rows = [dict(row) for row in candles or ()]
        if len(rows) < recent_sessions + baseline_sessions:
            return self._result(
                DELIVERY_LANE, None, _row_time(rows[-1]) if rows else None, False,
                "BASELINE_MISSING", "insufficient completed sessions for delivery volume baseline", 0,
            )
        recent_rows = rows[-recent_sessions:]
        base_rows = rows[-(recent_sessions + baseline_sessions):-recent_sessions]
        recent = [nonnegative_number(row.get("volume")) for row in recent_rows]
        base = [positive_number(row.get("volume")) for row in base_rows]
        if any(value is None for value in recent) or any(value is None for value in base):
            return self._result(
                DELIVERY_LANE, None, _row_time(rows[-1]), False,
                "MISSING", "completed-session volume contains missing/non-finite/invalid values", 0,
            )
        recent_avg = sum(recent) / len(recent)
        base_avg = sum(base) / len(base)
        source_time = _row_time(rows[-1])
        if base_avg <= 0:
            return self._result(DELIVERY_LANE, None, source_time, False, "BASELINE_MISSING", "delivery volume baseline is zero", len(base))
        value = recent_avg / base_avg
        freshness = self.completeness.session_evidence(source_time, at=at) if source_time else {
            "state": "MISSING", "decision_usable": False, "reason": "source timestamp unavailable"
        }
        usable = value is not None and freshness.get("decision_usable") is True
        return self._result(
            DELIVERY_LANE, value, source_time, usable,
            "READY" if usable else "NON_ACTIONABLE",
            "recent completed-session participation is current" if usable else str(freshness.get("reason") or "delivery participation freshness unavailable"),
            len(base), baseline_value=base_avg, freshness=freshness,
        )

    @staticmethod
    def radar_diagnostic(value: Any, *, source_time: Any = None) -> dict[str, Any]:
        parsed = parse_timestamp(source_time)
        numeric = _number(value)
        return {
            "authority": AUTHORITY_NAME,
            "authority_version": AUTHORITY_VERSION,
            "lane": RADAR_LANE,
            "value": numeric,
            "decision_usable": False,
            "state": "DIAGNOSTIC_ONLY" if numeric is not None else "MISSING",
            "reason": "radar cumulative/full-day ratio is discovery-only; it is not time-of-day decision RVOL",
            "source_time": parsed.isoformat() if parsed else None,
        }

    @staticmethod
    def _result(
        lane: str,
        value: float | None,
        source_time: datetime | None,
        usable: bool,
        state: str,
        reason: str,
        baseline_count: int,
        *,
        baseline_value: float | None = None,
        freshness: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            "authority": AUTHORITY_NAME,
            "authority_version": AUTHORITY_VERSION,
            "lane": lane,
            "value": None if finite_number(value) is None else round(finite_number(value), 4),
            "decision_usable": usable is True,
            "state": state,
            "reason": reason,
            "source_time": source_time.isoformat() if source_time else None,
            "baseline_count": int(baseline_count) if isinstance(baseline_count, int) and not isinstance(baseline_count, bool) and baseline_count >= 0 else 0,
            "baseline_value": None if finite_number(baseline_value) is None else round(finite_number(baseline_value), 4),
            "freshness": dict(freshness or {}),
        }


DEFAULT_PARTICIPATION_EVIDENCE_AUTHORITY = ParticipationEvidenceAuthority()
