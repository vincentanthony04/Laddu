"""Shared, dependency-free helpers used by storage.py and the core/*_repository
modules. Split out of storage.py during the storage.py de-God-object pass
(same extraction pattern used for LadduRuntime in main.py, clusters 3-9) so
that repository modules can use these without importing storage.py back."""
from __future__ import annotations

import re
import time
import sqlite3
from datetime import datetime, timezone
from typing import Any, Optional

from config import LOG_DIR
from models import now_iso

# v36.9.13: minimal perf/lock-contention logging, independent of main.py's
# logger (storage.py is imported by main.py, so it can't import back). Writes
# to the same logs/backend.log file so `logs_last10.ps1` picks it up without
# any new tooling. Only fires above a threshold, so normal fast writes are
# silent -- this is diagnostic instrumentation, not routine logging.
_SLOW_WRITE_MS = 250


def perf_log(msg: str) -> None:
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        line = f"{now_iso()} WARN [storage] {msg}\n"
        with open(LOG_DIR / "backend.log", "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass


# v60.6: busy_timeout=5000 makes SQLite itself retry internally while the
# writer lock is held by another thread, but that's a single 5s window --
# once it's exceeded (e.g. deep_history_backfill_loop's multi-hundred-row
# executemany holding the write lock while up to 12 _hist_executor + 4
# _quote_executor threads also try to write) the OperationalError propagates
# straight to whatever try/except wraps the call site and is swallowed as a
# WARN, matching the "database is locked" log pattern with no crash. This
# adds a small bounded retry on top of SQLite's own busy_timeout wait, so a
# write that loses the race once gets a few more chances before giving up.
_LOCK_RETRY_ATTEMPTS = 3
_LOCK_RETRY_BASE_SLEEP = 0.15


def timed_write(label: str, fn):
    """Run fn(), retrying a bounded number of times if it fails on SQLite
    writer-lock contention specifically ("database is locked" /
    "database is busy"), and if it (including any retry/busy_timeout wait)
    takes longer than _SLOW_WRITE_MS, log it -- this is what will tell us
    whether quote-delta/historical/market-intelligence stalls are actually
    SQLite writer-lock contention, and which write is the culprit."""
    started = time.time()
    attempt = 0
    while True:
        try:
            return fn()
        except sqlite3.OperationalError as exc:
            locked = "database is locked" in str(exc) or "database is busy" in str(exc)
            attempt += 1
            if not locked or attempt > _LOCK_RETRY_ATTEMPTS:
                elapsed_ms = int((time.time() - started) * 1000)
                perf_log(f"{label} FAILED after {elapsed_ms}ms (attempt {attempt}): {exc}")
                raise
            perf_log(f"{label} retrying after lock contention (attempt {attempt}/{_LOCK_RETRY_ATTEMPTS}): {exc}")
            time.sleep(_LOCK_RETRY_BASE_SLEEP * attempt)
        finally:
            elapsed_ms = int((time.time() - started) * 1000)
            if elapsed_ms >= _SLOW_WRITE_MS:
                perf_log(f"{label} took {elapsed_ms}ms (threshold {_SLOW_WRITE_MS}ms)")


def canonical_interval(interval: str) -> str:
    """Return the canonical storage interval via the single timeframe authority."""
    from core.timeframe import storage_interval
    return storage_interval(interval)


def canonical_timestamp(value: Any, interval: str = "1d") -> Optional[str]:
    if value in (None, ""):
        return None
    canonical = canonical_interval(interval)
    try:
        if isinstance(value, (int, float)):
            seconds = float(value) / 1000.0 if float(value) > 1000000000000 else float(value)
            dt = datetime.fromtimestamp(seconds, tz=timezone.utc)
        else:
            raw = str(value).strip()
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
                return raw + "T00:00:00Z"
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00").replace(" ", "T"))
            trading_date = dt.date()
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            dt = dt.astimezone(timezone.utc)
        if canonical in ("1d", "1w", "1mo"):
            return (trading_date if 'trading_date' in locals() else dt.date()).isoformat() + "T00:00:00Z"
        return dt.isoformat(timespec="seconds").replace("+00:00", "Z")
    except Exception:
        return str(value).strip() or None


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def to_float(v: Any) -> Optional[float]:
    """Moved from Store._f verbatim. Tolerant numeric coercion used across
    save_* methods; treats blanks/placeholders as missing rather than 0."""
    try:
        if v in (None, "", "pending", "—"):
            return None
        return float(v)
    except Exception:
        return None
