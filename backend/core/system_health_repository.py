"""System health snapshot + scanner event log. Extracted verbatim from
storage.py's Store class (v51 storage split, cluster 6).

v60.14 P0 fix: event() is the shared logger called from nearly every module
in the app (scanner cycles, quote polling, error handlers) -- it is by far
the hottest writer in the process, and it was never routed through
Store.write_lock even after v60.13 established that pattern for
save_candles/save_quote. Under real load this writer was almost certainly
the single biggest contributor to the "database is locked" contention on
quote_delta / sticky_selected_merge, since a log call can fire in the middle
of any other write's busy_timeout wait. Now takes write_lock the same way
MarketDataRepository/SignalLedgerRepository do."""
from __future__ import annotations

import json
import threading
from typing import Any, Dict, List, Optional


class SystemHealthRepository:
    def __init__(self, connection, write_lock=None):
        self.conn = connection
        self.write_lock = write_lock or threading.Lock()

    def system_health_snapshot(self) -> Dict[str, Any]:
        """v37.5: Phase 0 observability. Answers 'is the pipeline actually
        producing data' with facts pulled from storage itself, not from
        in-memory state that resets on restart or can drift from what's
        really persisted. This is the thing that should be checked instead
        of re-reading log files every time something feels stale."""
        out: Dict[str, Any] = {}
        try:
            row = self.conn.execute("SELECT MAX(last_update) AS last FROM signal_ledger").fetchone()
            out["last_ledger_write"] = row["last"] if row else None
            out["open_ledger_rows"] = self.conn.execute("SELECT COUNT(*) AS c FROM signal_ledger WHERE status='OPEN'").fetchone()["c"]
            out["ledger_rows_total"] = self.conn.execute("SELECT COUNT(*) AS c FROM signal_ledger").fetchone()["c"]
        except Exception as exc:
            out["ledger_error"] = str(exc)[:160]
        try:
            row = self.conn.execute("SELECT MAX(COALESCE(closed_at, created_at)) AS last FROM trade_journal").fetchone()
            out["last_journal_write"] = row["last"] if row else None
        except Exception as exc:
            out["journal_error"] = str(exc)[:160]
        try:
            row = self.conn.execute("SELECT MAX(ts) AS last, COUNT(*) AS c FROM candles").fetchone()
            out["last_candle_stored"] = row["last"] if row else None
            out["candles_total"] = row["c"] if row else 0
        except Exception as exc:
            out["candles_error"] = str(exc)[:160]
        try:
            row = self.conn.execute("SELECT MAX(timestamp) AS last FROM quotes").fetchone()
            out["last_quote_stored"] = row["last"] if row else None
        except Exception as exc:
            out["quotes_error"] = str(exc)[:160]
        try:
            row = self.conn.execute("SELECT MAX(captured_at) AS last, COUNT(*) AS c FROM price_snapshots").fetchone()
            out["last_price_snapshot"] = row["last"] if row else None
            out["price_snapshots_total"] = row["c"] if row else 0
        except Exception as exc:
            out["price_snapshots_error"] = str(exc)[:160]
        try:
            row = self.conn.execute("SELECT MAX(trade_date) AS last, COUNT(*) AS c FROM delivery_data").fetchone()
            out["last_delivery_date"] = row["last"] if row else None
            out["delivery_rows_total"] = row["c"] if row else 0
        except Exception as exc:
            out["delivery_error"] = str(exc)[:160]
        return out

    def event(self, level: str, module: str, message: str, detail: Optional[Dict[str, Any]] = None) -> None:
        with self.write_lock:
            self.conn.execute("INSERT INTO scanner_events(level,module,message,detail_json) VALUES(?,?,?,?)", (level, module, message, json.dumps(detail or {})))
            self.conn.commit()

    def events(self, limit: int = 80) -> List[Dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM scanner_events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [{"level": r["level"], "module": r["module"], "message": r["message"], "detail": json.loads(r["detail_json"] or "{}"), "timestamp": r["created_at"]} for r in rows]
