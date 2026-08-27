"""Versioned publication-readiness policy for official NSE end-of-day reports.

TradingSessionAuthority owns whether a session exists.  This policy owns only
when the collector may expect that session's end-of-day files to be published.
Separating these concerns prevents a publication clock (18:00 IST) from being
mistaken for exchange-session existence.
"""
from __future__ import annotations

from datetime import datetime, time
from typing import Any

from core.india_time import INDIA_TZ
from core.trading_session_authority import TradingSessionAuthority, DEFAULT_TRADING_SESSION_AUTHORITY

AUTHORITY_NAME = "OfficialReportPublicationPolicy"
AUTHORITY_VERSION = "1.0.0"


class OfficialReportPublicationPolicy:
    authority = AUTHORITY_NAME
    authority_version = AUTHORITY_VERSION

    def __init__(
        self,
        *,
        sessions: TradingSessionAuthority | None = None,
        publication_time: time = time(18, 0),
    ):
        self.sessions = sessions or DEFAULT_TRADING_SESSION_AUTHORITY
        self.publication_time = publication_time

    def latest_eligible_trade_date(self, at: datetime | None = None) -> dict[str, Any]:
        current = at or datetime.now(INDIA_TZ)
        current = current.replace(tzinfo=INDIA_TZ) if current.tzinfo is None else current.astimezone(INDIA_TZ)
        day = current.date()
        if not self.sessions.calendar_covered(day):
            return {
                "authority": self.authority,
                "authority_version": self.authority_version,
                "state": "CALENDAR_UNVERIFIED",
                "trade_date": None,
                "observed_at": current.isoformat(timespec="seconds"),
                "publication_time": self.publication_time.isoformat(timespec="minutes"),
            }
        window = self.sessions.session_window(day)
        if window is not None and current.time().replace(tzinfo=None) >= self.publication_time:
            trade_day = day
        else:
            trade_day = self.sessions.previous_trading_day(day)
        return {
            "authority": self.authority,
            "authority_version": self.authority_version,
            "state": "READY",
            "trade_date": trade_day.isoformat(),
            "observed_at": current.isoformat(timespec="seconds"),
            "publication_time": self.publication_time.isoformat(timespec="minutes"),
        }


DEFAULT_OFFICIAL_REPORT_PUBLICATION_POLICY = OfficialReportPublicationPolicy()
