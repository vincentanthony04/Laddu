"""Asia/Kolkata authoritative Intraday session/admission policy."""
from __future__ import annotations
from datetime import datetime, time
from typing import Any, Dict
from core.india_time import INDIA_TZ
from core.trading_session_authority import DEFAULT_TRADING_SESSION_AUTHORITY

class IntradaySessionPolicy:
    """Binding same-day clock contract.

    09:00-09:15 pre-open intelligence only; 09:15-09:20 ORB5 observe-only;
    entries start at 09:20; 14:15-14:30 is A+ only; no new risk after 14:30;
    every Intraday position is flat by 15:00. Delivery is unaffected.
    """
    PREOPEN_START=time(9,0); SESSION_OPEN=time(9,15); ENTRY_START=time(9,20)
    A_PLUS_ONLY_START=time(14,15); ENTRY_CUTOFF=time(14,30); MANAGE_ONLY=ENTRY_CUTOFF
    FORCE_EXIT=time(15,0); MANDATORY_FLAT=time(15,0); MANDATORY_EXIT=FORCE_EXIT; SESSION_END=time(15,30)

    @classmethod
    def mandatory_flat_label(cls) -> str:
        return cls.MANDATORY_FLAT.strftime("%H:%M")

    @classmethod
    def mandatory_flat_at(cls, opened_at: datetime) -> datetime:
        local = cls.local(opened_at)
        return local.replace(
            hour=cls.MANDATORY_FLAT.hour, minute=cls.MANDATORY_FLAT.minute,
            second=0, microsecond=0,
        )

    @staticmethod
    def local(at: datetime|None=None)->datetime:
        current=at or datetime.now(INDIA_TZ)
        return current.replace(tzinfo=INDIA_TZ) if current.tzinfo is None else current.astimezone(INDIA_TZ)

    def at(self, at: datetime|None=None)->Dict[str,Any]:
        local=self.local(at); tod=local.timetz().replace(tzinfo=None)
        session_meta=DEFAULT_TRADING_SESSION_AUTHORITY.phase(local); trading_day=DEFAULT_TRADING_SESSION_AUTHORITY.is_trading_day(local.date())
        regular=bool(session_meta.get("market_open"))
        if not trading_day:
            phase="MARKET_CLOSED" if session_meta.get("calendar_covered") else "CALENDAR_UNVERIFIED"
        elif tod >= self.MANDATORY_FLAT:
            phase="MANDATORY_FLAT"
        elif tod >= self.ENTRY_CUTOFF:
            phase="NO_NEW_INTRADAY"
        elif tod >= self.A_PLUS_ONLY_START:
            phase="A_PLUS_ONLY"
        elif tod >= self.ENTRY_START:
            phase="ENTRY_ALLOWED"
        elif tod >= self.SESSION_OPEN:
            phase="ORB5_OBSERVE_ONLY"
        elif tod >= self.PREOPEN_START:
            phase="PREOPEN_INTELLIGENCE"
        else:
            phase="MARKET_CLOSED" if session_meta.get("calendar_covered") else "CALENDAR_UNVERIFIED"
        new_entry=bool(trading_day and self.ENTRY_START <= tod < self.ENTRY_CUTOFF)
        return {
            "as_of":local.isoformat(timespec="seconds"),"timezone":"Asia/Kolkata","phase":phase,"market_session":regular,
            "preopen_intelligence":bool(trading_day and self.PREOPEN_START<=tod<self.SESSION_OPEN),
            "observe_only":bool(trading_day and self.SESSION_OPEN<=tod<self.ENTRY_START),
            "new_entry_allowed":new_entry,"a_plus_only":bool(trading_day and self.A_PLUS_ONLY_START<=tod<self.ENTRY_CUTOFF),
            "manage_only":bool(trading_day and tod>=self.ENTRY_CUTOFF),"mandatory_exit":bool(trading_day and tod>=self.FORCE_EXIT),
            "mandatory_flat":bool(trading_day and tod>=self.MANDATORY_FLAT),"risk_increasing_actions_allowed":new_entry,
            "session_authority":session_meta.get("authority"),"session_authority_version":session_meta.get("authority_version"),"calendar_covered":session_meta.get("calendar_covered"),
            "preopen_start":"09:00","session_open":"09:15","entry_start":"09:20","a_plus_only_from":"14:15","entry_cutoff":"14:30","manage_only_from":"14:30",
            "forced_exit_from":self.FORCE_EXIT.strftime("%H:%M"),
            "mandatory_exit_at":self.MANDATORY_EXIT.strftime("%H:%M"),
            "mandatory_flat_at":self.mandatory_flat_label(),
        }
