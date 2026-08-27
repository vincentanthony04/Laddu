"""
EarningsCalendarService -- v37.5, Phase 6.

Owns the corporate-action / board-meeting calendar and exposes a single
cheap event-risk lookup: "is this symbol about to report earnings or hold
a board meeting in the next N days?" Purely a read-only input to the
analytics layer (Layer 3's Event-risk flag in the architecture doc) -- it
never blocks or overrides a promotion, it only flags one so a technically
clean setup with earnings in 2 days is surfaced as flagged, not silently
promoted the same as any other setup.

Same isolation discipline as ReferenceDataService (Phase 2/3): own table
(earnings_calendar), own daily cadence, own failure domain. Reuses the
same NSE cookie-warmup pattern since it hits the same corporate-filings
domain.

NOTE ON NETWORK ACCESS: NSE's board-meetings/corporate-announcements
endpoints (nseindia.com/api/corporate-board-meetings,
/api/corporate-announcements) need the same browser User-Agent + cookie
warm-up as reference_data_service.py. I could not exercise a live call
from this sandbox (no egress to nseindia.com here) -- parsing below is
written against NSE's documented JSON shape (symbol, purpose, bm_date /
an_dt) but must be verified against a live response on first real run.
If NSE has changed field names, check that first before assuming the
whole ingestion path is broken.
"""
from __future__ import annotations

import time
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None

NSE_HOME = "https://www.nseindia.com"
NSE_BOARD_MEETINGS_URL = "https://www.nseindia.com/api/corporate-board-meetings"
NSE_ANNOUNCEMENTS_URL = "https://www.nseindia.com/api/corporate-announcements"

_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept": "application/json,*/*",
    "Referer": "https://www.nseindia.com/companies-listing/corporate-filings-board-meetings",
}



class _UrllibResponse:
    def __init__(self, data: bytes, url: str):
        self.content = data
        self.text = data.decode("utf-8", errors="replace")
        self.url = url
    def raise_for_status(self):
        return None
    def json(self):
        import json
        return json.loads(self.text or "{}")

class _UrllibSession:
    def __init__(self):
        self.headers = dict(_HEADERS)
        self._opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor())
    def get(self, url: str, params=None, timeout=15):
        if params:
            qs = urllib.parse.urlencode(params)
            sep = '&' if '?' in url else '?'
            url = url + sep + qs
        req = urllib.request.Request(url, headers=self.headers)
        with self._opener.open(req, timeout=timeout) as res:
            return _UrllibResponse(res.read(), url)

class EarningsCalendarService:
    def __init__(self, store, event: Callable[..., None], record_error: Callable[..., None]):
        self.store = store
        self.event = event
        self.record_error = record_error
        self._session = None
        self._session_ts = 0.0

    def _get_session(self):
        if self._session is not None and (time.time() - self._session_ts) < 600:
            return self._session
        if requests is not None:
            s = requests.Session()
            s.headers.update(_HEADERS)
        else:
            s = _UrllibSession()
        try:
            s.get(NSE_HOME, timeout=8)
        except Exception as exc:
            self.event("WARN", "earnings_calendar", "NSE homepage warm-up failed (cookies may be missing)", {"error": str(exc)[:160]})
        self._session = s
        self._session_ts = time.time()
        return s

    def _fetch_json(self, url: str, params: Optional[Dict[str, str]] = None) -> Any:
        sess = self._get_session()
        resp = sess.get(url, params=params or {}, timeout=15)
        resp.raise_for_status()
        return resp.json()

    def fetch_board_meetings(self, index: str = "equities") -> Dict[str, Any]:
        """Board meetings often include results/earnings agenda items --
        NSE's own feed doesn't cleanly separate 'earnings' from other
        board business, so we tag everything here as board_meeting and
        let the purpose text carry the detail rather than guessing."""
        td = datetime.now().strftime("%Y-%m-%d")
        try:
            raw = self._fetch_json(NSE_BOARD_MEETINGS_URL, {"index": index})
            rows = raw if isinstance(raw, list) else raw.get("data", [])
            norm = []
            for r in rows:
                sym = r.get("symbol") or r.get("SYMBOL")
                ev_date = r.get("bm_date") or r.get("BM_DATE") or r.get("date")
                if not sym or not ev_date:
                    continue
                norm.append({
                    "symbol": sym, "event_date": ev_date, "event_type": "board_meeting",
                    "purpose": r.get("bm_purpose") or r.get("BM_PURPOSE") or "",
                })
            n = self.store.save_earnings_calendar(norm)
            self.store.record_reference_run("earnings_board_meetings", td, "OK" if n else "PARTIAL", n)
            self.event("INFO", "earnings_calendar", "Board meetings ingested", {"count": n})
            return {"ok": True, "rows": n}
        except Exception as exc:
            self.store.record_reference_run("earnings_board_meetings", td, "FAILED", 0, str(exc))
            self.record_error("earnings_calendar", str(exc), NSE_BOARD_MEETINGS_URL)
            return {"ok": False, "error": str(exc)[:200]}

    def run_daily_job(self) -> Dict[str, Any]:
        """Single entrypoint the scheduler calls once/day. Kept to one
        sub-fetch today (board meetings) -- corporate-announcements can
        be added the same way later without touching this signature."""
        result = {"board_meetings": self.fetch_board_meetings()}
        self.event("INFO", "earnings_calendar", "Daily earnings calendar job complete", {})
        return result

    # ------------------------------------------------------------ event-risk read API
    def event_risk_for(self, symbol: str, within_days: int = 3) -> Optional[Dict[str, Any]]:
        """Read-only check: does this symbol have an earnings/board-meeting
        event within `within_days`? Returns None if clear, else the nearest
        event row -- the analytics layer decides what to do with a flag,
        this module only reports the fact."""
        rows = self.store.get_upcoming_earnings(symbol, within_days)
        return rows[0] if rows else None

    def event_risk_map(self, within_days: int = 3) -> Dict[str, str]:
        """Bulk version for scan cycles checking many candidates at once --
        one query instead of N, so this is safe to call per scan pass
        without adding meaningful DB load."""
        return self.store.event_risk_symbols(within_days)
