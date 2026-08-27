from __future__ import annotations

import csv
import io
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from config import DATA_DIR, NSE_DELIVERY_HTTP_TIMEOUT, NSE_DELIVERY_LOOKBACK_DAYS
from models import now_iso
from core.official_report_publication_policy import DEFAULT_OFFICIAL_REPORT_PUBLICATION_POLICY
from core.trading_session_authority import DEFAULT_TRADING_SESSION_AUTHORITY


class NSEDeliveryClient:
    """Download NSE security-wise delivery/EOD bhavdata and save it locally.

    Important product rule: NSE delivery information is an EOD report. It is not
    tick-by-tick market data. Project Laddu uses Upstox quotes/candles live
    during the session; this client keeps the latest available NSE delivery
    report fresh once NSE publishes it.
    """

    PAGE_URL = "https://www.nseindia.com/all-reports"
    ARCHIVE_URLS = (
        "https://archives.nseindia.com/products/content/sec_bhavdata_full_{date}.csv",
        "https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_{date}.csv",
    )

    def __init__(self, event: Optional[Callable[..., None]] = None):
        self.event = event or (lambda *a, **k: None)
        self._opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor())
        self._warmed = False

    @staticmethod
    def _headers() -> Dict[str, str]:
        return {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) ProjectLaddu/38.1.2",
            "Accept": "text/csv,application/octet-stream,application/zip,application/json,text/plain,*/*",
            "Accept-Language": "en-IN,en;q=0.9",
            "Referer": "https://www.nseindia.com/all-reports",
            "Connection": "keep-alive",
        }

    def _warmup(self) -> None:
        if self._warmed:
            return
        try:
            req = urllib.request.Request(self.PAGE_URL, headers={**self._headers(), "Accept": "text/html,*/*"})
            with self._opener.open(req, timeout=NSE_DELIVERY_HTTP_TIMEOUT) as res:
                res.read(2048)
            self._warmed = True
        except Exception as exc:
            # Archive CDN often works even when the front page warm-up fails.
            self.event("WARN", "delivery_data", "NSE cookie warm-up failed; trying archives directly", {"error": str(exc)[:160]})

    @staticmethod
    def _candidate_dates(lookback_days: int, at: datetime | None = None) -> List[datetime]:
        """Return only exchange sessions, starting at the latest publishable report.

        Calendar weekends/holidays must never be probed as archive failures.
        The governed session authority owns date eligibility; provider 404 is
        reserved for a report that should exist but is genuinely unavailable.
        """
        lookback = max(1, int(lookback_days or NSE_DELIVERY_LOOKBACK_DAYS))
        publication = DEFAULT_OFFICIAL_REPORT_PUBLICATION_POLICY.latest_eligible_trade_date(at)
        trade_date = publication.get("trade_date")
        if not trade_date:
            return []
        sessions = DEFAULT_TRADING_SESSION_AUTHORITY
        cursor = datetime.fromisoformat(str(trade_date)[:10])
        out: List[datetime] = []
        for _ in range(lookback):
            out.append(cursor)
            try:
                prior = sessions.previous_trading_day(cursor.date())
            except Exception:
                # The immutable release calendar may not cover older history.
                # For that tail only, fall back to weekdays without inventing
                # current-session dates.
                probe = cursor - timedelta(days=1)
                while probe.weekday() >= 5:
                    probe -= timedelta(days=1)
                cursor = probe
            else:
                cursor = datetime.combine(prior, datetime.min.time())
        return out

    @staticmethod
    def _looks_like_delivery_csv(data: bytes) -> bool:
        if not data or len(data) < 100:
            return False
        head = data[:4096].decode("utf-8", errors="ignore").upper()
        return ("SYMBOL" in head and ("DELIV_QTY" in head or "DELIV" in head) and ("TTL_TRD_QNTY" in head or "TURNOVER" in head or "CLOSE_PRICE" in head))

    @staticmethod
    def _csv_data_rows(data: bytes) -> int:
        try:
            text = data.decode("utf-8", errors="replace")
            rows = list(csv.DictReader(io.StringIO(text)))
            return sum(1 for r in rows if (r.get("SYMBOL") or r.get(" SYMBOL") or "").strip())
        except Exception:
            return 0

    @staticmethod
    def _normalise_file_date(dt: datetime) -> str:
        return dt.strftime("%d%m%Y")

    @staticmethod
    def _target_file(dt: datetime) -> Path:
        return DATA_DIR / f"sec_bhavdata_full_{dt.strftime('%d%m%Y')}.csv"

    def _download_url(self, url: str) -> bytes:
        req = urllib.request.Request(url, headers=self._headers())
        with self._opener.open(req, timeout=NSE_DELIVERY_HTTP_TIMEOUT) as res:
            return res.read()

    def download_latest(self, *, force: bool = False, lookback_days: int | None = None) -> Dict[str, Any]:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        lookback = max(1, int(lookback_days or NSE_DELIVERY_LOOKBACK_DAYS))
        self._warmup()
        attempts = []

        for dt in self._candidate_dates(lookback):
            target = self._target_file(dt)
            report_date = self._normalise_file_date(dt)
            if target.exists() and target.stat().st_size > 100 and not force:
                cached_bytes = target.read_bytes()
                if self._looks_like_delivery_csv(cached_bytes) and self._csv_data_rows(cached_bytes) > 0:
                    return {
                        "ok": True,
                        "downloaded": False,
                        "cached": True,
                    "file": target.name,
                    "path": str(target),
                    "report_date": report_date,
                    "attempts": attempts,
                    "message": "Latest cached NSE delivery report already present",
                    "checked_at": now_iso(),
                    }
                attempts.append({"date": report_date, "url": str(target), "state": "cached_file_invalid_or_zero_rows"})

            for tmpl in self.ARCHIVE_URLS:
                url = tmpl.format(date=report_date)
                try:
                    data = self._download_url(url)
                    if not self._looks_like_delivery_csv(data):
                        attempts.append({"date": report_date, "url": url, "state": "not_delivery_csv", "bytes": len(data or b"")})
                        continue
                    row_count = self._csv_data_rows(data)
                    if row_count <= 0:
                        attempts.append({"date": report_date, "url": url, "state": "zero_rows", "bytes": len(data or b"")})
                        continue
                    target.write_bytes(data)
                    return {
                        "ok": True,
                        "downloaded": True,
                        "cached": False,
                        "file": target.name,
                        "path": str(target),
                        "report_date": report_date,
                        "source_url": url,
                        "bytes": len(data),
                        "attempts": attempts,
                        "message": "Downloaded latest available NSE delivery report",
                        "checked_at": now_iso(),
                    }
                except urllib.error.HTTPError as exc:
                    attempts.append({"date": report_date, "url": url, "state": f"http_{exc.code}"})
                    if exc.code not in (403, 404):
                        self.event("WARN", "delivery_data", "NSE delivery download HTTP error", {"url": url, "status": exc.code})
                except Exception as exc:
                    attempts.append({"date": report_date, "url": url, "state": "error", "error": str(exc)[:120]})
                    self.event("WARN", "delivery_data", "NSE delivery download failed", {"url": url, "error": str(exc)[:160]})

        return {
            "ok": False,
            "downloaded": False,
            "cached": False,
            "file": None,
            "path": None,
            "report_date": None,
            "attempts": attempts[-12:],
            "message": "No NSE delivery report found in lookback window; market holiday/publication delay/network block possible",
            "checked_at": now_iso(),
        }

    def backfill_missing(self, *, lookback_days: int | None = None, max_downloads: int = 8) -> Dict[str, Any]:
        """Incrementally fill the institutional model's history without a request storm.

        Existing valid files are skipped. At most ``max_downloads`` reports are
        fetched per maintenance pass; repeated passes converge to full coverage.
        """
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self._warmup()
        downloaded, cached, missing, attempts = [], [], [], []
        attempted_dates = 0
        for dt in self._candidate_dates(max(21, int(lookback_days or NSE_DELIVERY_LOOKBACK_DAYS))):
            if dt.weekday() >= 5:
                continue
            target, report_date = self._target_file(dt), self._normalise_file_date(dt)
            if target.exists() and target.stat().st_size > 100:
                try:
                    if self._looks_like_delivery_csv(target.read_bytes()):
                        cached.append(target.name); continue
                except Exception:
                    pass
            if len(downloaded) >= max(1, int(max_downloads)):
                missing.append(report_date); continue
            if attempted_dates >= max(30, int(max_downloads) * 3):
                missing.append(report_date); continue
            attempted_dates += 1
            found = False
            for tmpl in self.ARCHIVE_URLS:
                url = tmpl.format(date=report_date)
                try:
                    data = self._download_url(url)
                    if self._looks_like_delivery_csv(data) and self._csv_data_rows(data) > 0:
                        target.write_bytes(data); downloaded.append(target.name); found = True; break
                    attempts.append({"date": report_date, "state": "invalid"})
                except urllib.error.HTTPError as exc:
                    attempts.append({"date": report_date, "state": f"http_{exc.code}"})
                except Exception as exc:
                    attempts.append({"date": report_date, "state": "error", "error": str(exc)[:100]})
            if not found:
                missing.append(report_date)
        return {"ok": True, "state": "complete" if len(cached)+len(downloaded) >= 252 else "collecting_evidence",
                "downloaded_files": downloaded, "cached_files": len(cached), "missing_dates": missing,
                "coverage_files": len(cached)+len(downloaded), "required_files": 252,
                "attempts": attempts[-20:], "checked_at": now_iso()}


def parse_delivery_csv_text(text: str) -> List[Dict[str, Any]]:
    """Utility used by tests and future importers; main storage importer still
    accepts csv.DictReader rows directly."""
    rows = []
    for row in csv.DictReader(io.StringIO(text)):
        clean = {str(k or "").strip(): str(v or "").strip() for k, v in row.items()}
        if clean.get("SYMBOL") or clean.get("symbol"):
            rows.append(clean)
    return rows
