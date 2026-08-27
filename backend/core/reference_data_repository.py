"""Reference data: delivery %, bulk/block deals, market
breadth, reference-job status, option chain snapshots, earnings calendar.
Extracted verbatim from storage.py's Store class (v51 storage split,
cluster 5). Separate write paths from the live tick pipeline on purpose --
a malformed NSE CSV must never be able to raise into the scanner loop or
touch signal_ledger/candles. No behavior change."""
from __future__ import annotations

import json
import hashlib
import re
from typing import Any, Callable, Dict, List, Optional

from core.db_utils import timed_write, to_float
from models import now_iso

import threading


def ensure_fundamentals_history_schema(connection) -> None:
    connection.executescript(
        """CREATE TABLE IF NOT EXISTS fundamentals_history (
          isin TEXT NOT NULL, payload_hash TEXT NOT NULL, as_of TEXT, ok INTEGER NOT NULL,
          payload_json TEXT NOT NULL, source TEXT, first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
          PRIMARY KEY(isin,payload_hash));
        CREATE INDEX IF NOT EXISTS ix_fundamentals_history_isin_asof
          ON fundamentals_history(isin,as_of,last_seen_at);"""
    )


class ReferenceDataRepository:
    """v60.14 P0 fix: this repo's several writers (delivery rows, bulk/block
    deals, market breadth and reference-run status
    snapshots, earnings calendar) executed+committed directly on the shared
    connection without Store.write_lock. Same fallback-lock pattern applied
    here as the other repos fixed for "database is locked" contention."""
    _TRADE_DATE_DMY = re.compile(r"^(\d{1,2})-([A-Za-z]{3})-(\d{4})$")
    _MONTH_ABBR = {m: i for i, m in enumerate(
        ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"], start=1)}

    def __init__(self, connection, write_lock=None):
        self.conn = connection
        self.write_lock = write_lock or threading.Lock()

    @classmethod
    def _normalize_trade_date(cls, raw: str) -> str:
        """Normalize a trade-date string to ISO YYYY-MM-DD.

        Found in production: delivery_data.trade_date contains a live mix of
        'DD-Mon-YYYY' (raw NSE bhavcopy DATE1 column, written as-is by
        save_delivery_rows) and 'YYYY-MM-DD' (written by save_delivery_data).
        SQLite MIN/MAX/ORDER BY on this column sort lexicographically, so the
        two formats interleave incorrectly -- see
        VALIDATION_FINDINGS_2026-07-18.md section 11. This normalizes at
        write time so every row lands in ISO form going forward; it does not
        rewrite existing rows (see migrate_delivery_data_dates below for that).
        """
        s = str(raw or "").strip()
        m = cls._TRADE_DATE_DMY.match(s)
        if m:
            day, mon_abbr, year = m.group(1), m.group(2), m.group(3)
            mon = cls._MONTH_ABBR.get(mon_abbr[:3].title())
            if mon:
                return f"{year}-{mon:02d}-{int(day):02d}"
        return s

    def migrate_delivery_data_dates(self) -> Dict[str, int]:
        """One-time cleanup: rewrite existing DD-Mon-YYYY rows in
        delivery_data to ISO YYYY-MM-DD so historical rows sort correctly
        alongside new ones. Safe to run repeatedly (no-op once clean)."""
        rows = self.conn.execute("SELECT DISTINCT trade_date FROM delivery_data").fetchall()
        rewritten = 0
        with self.write_lock:
            for r in rows:
                old = str(r["trade_date"] or "")
                new = self._normalize_trade_date(old)
                if new and new != old:
                    self.conn.execute("UPDATE delivery_data SET trade_date=? WHERE trade_date=?", (new, old))
                    rewritten += 1
            self.conn.commit()
        return {"distinct_dates_checked": len(rows), "rewritten": rewritten}

    def save_delivery_rows(self, rows: List[Dict[str, Any]], source: str = "nse_delivery") -> int:
        payload = []
        for row in rows or []:
            sym = str(row.get("symbol") or row.get("SYMBOL") or row.get(" Symbol") or "").upper().strip()
            if not sym:
                continue
            trade_date_raw = str(row.get("trade_date") or row.get("DATE1") or row.get(" Date") or row.get("TIMESTAMP") or now_iso()[:10]).strip()
            trade_date = self._normalize_trade_date(trade_date_raw)
            payload.append((sym, str(row.get("exchange") or "NSE").upper(), trade_date,
                to_float(row.get("traded_qty") or row.get("TTL_TRD_QNTY") or row.get(" TTL_TRD_QNTY")),
                to_float(row.get("deliverable_qty") or row.get("DELIV_QTY") or row.get(" DELIV_QTY")),
                to_float(row.get("delivery_pct") or row.get("DELIV_PER") or row.get(" DELIV_PER")),
                to_float(row.get("close") or row.get("CLOSE_PRICE") or row.get(" CLOSE_PRICE")), source, json.dumps(row)))
        if not payload:
            return 0
        def _write():
            with self.write_lock:
                self.conn.executemany("""INSERT INTO delivery_data(symbol,exchange,trade_date,traded_qty,deliverable_qty,delivery_pct,close,source,raw_json)
                VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(trade_date,symbol) DO UPDATE SET exchange=excluded.exchange,
                traded_qty=excluded.traded_qty, deliverable_qty=excluded.deliverable_qty, delivery_pct=excluded.delivery_pct, close=excluded.close,
                source=excluded.source, raw_json=excluded.raw_json, updated_at=CURRENT_TIMESTAMP""", payload)
                self.conn.commit()
        timed_write(f"save_delivery_rows ({len(payload)} rows)", _write)
        return len(payload)

    def latest_delivery(self, symbol: str, limit: int = 20) -> List[Dict[str, Any]]:
        rows = self.conn.execute("""SELECT symbol,exchange,trade_date,traded_qty,deliverable_qty,delivery_pct,close,source
            FROM delivery_data WHERE UPPER(symbol)=? ORDER BY trade_date DESC LIMIT ?""", (str(symbol or "").upper().strip(), int(limit))).fetchall()
        return [dict(r) for r in rows]

    def save_delivery_data(self, trade_date: str, rows: List[Dict[str, Any]]) -> int:
        trade_date = self._normalize_trade_date(trade_date)
        n = 0
        with self.write_lock:
            for r in rows:
                try:
                    sym = str(r.get("symbol") or "").upper().strip()
                    if not sym:
                        continue
                    traded = to_float(r.get("traded_qty"))
                    deliv = to_float(r.get("deliverable_qty"))
                    pct = to_float(r.get("delivery_pct"))
                    if pct is None and traded and deliv is not None and traded > 0:
                        pct = round((deliv / traded) * 100, 2)
                    self.conn.execute(
                        "INSERT INTO delivery_data(trade_date,symbol,traded_qty,deliverable_qty,delivery_pct) VALUES(?,?,?,?,?) "
                        "ON CONFLICT(trade_date,symbol) DO UPDATE SET traded_qty=excluded.traded_qty, deliverable_qty=excluded.deliverable_qty, delivery_pct=excluded.delivery_pct",
                        (trade_date, sym, traded, deliv, pct))
                    n += 1
                except Exception:
                    continue
            self.conn.commit()
        return n

    def get_delivery_data(self, symbol: str, days: int = 10) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM delivery_data WHERE symbol=? ORDER BY trade_date DESC LIMIT ?",
            (str(symbol or "").upper().strip(), days)).fetchall()
        return [dict(r) for r in rows]

    def save_bulk_block_deals(self, trade_date: str, deal_type: str, rows: List[Dict[str, Any]]) -> int:
        n = 0
        with self.write_lock:
            for r in rows:
                try:
                    sym = str(r.get("symbol") or "").upper().strip()
                    if not sym:
                        continue
                    self.conn.execute(
                        "INSERT INTO bulk_block_deals(trade_date,symbol,deal_type,client_name,buy_sell,qty,price) VALUES(?,?,?,?,?,?,?)",
                        (trade_date, sym, deal_type, r.get("client_name"), r.get("buy_sell"),
                         to_float(r.get("qty")), to_float(r.get("price"))))
                    n += 1
                except Exception:
                    continue
            self.conn.commit()
        return n

    def get_bulk_block_deals(self, symbol: str = "", days: int = 5) -> List[Dict[str, Any]]:
        if symbol:
            rows = self.conn.execute(
                "SELECT * FROM bulk_block_deals WHERE symbol=? AND trade_date >= date('now', ?) ORDER BY trade_date DESC",
                (str(symbol).upper().strip(), f"-{days} days")).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM bulk_block_deals WHERE trade_date >= date('now', ?) ORDER BY trade_date DESC",
                (f"-{days} days",)).fetchall()
        return [dict(r) for r in rows]

    def save_market_breadth(self, universe: str, advances: int, declines: int, unchanged: int) -> None:
        with self.write_lock:
            self.conn.execute(
                "INSERT INTO market_breadth_daily(ts,universe,advances,declines,unchanged) VALUES(?,?,?,?,?) "
                "ON CONFLICT(ts,universe) DO UPDATE SET advances=excluded.advances, declines=excluded.declines, unchanged=excluded.unchanged",
                (now_iso(), universe, advances, declines, unchanged))
            self.conn.commit()

    def get_latest_market_breadth(self, universe: str = "NIFTY250_CORE") -> Optional[Dict[str, Any]]:
        row = self.conn.execute(
            "SELECT * FROM market_breadth_daily WHERE universe=? ORDER BY ts DESC LIMIT 1", (universe,)).fetchone()
        return dict(row) if row else None

    def record_reference_run(self, job_name: str, run_date: str, status: str, rows_written: int, error: str = "") -> None:
        rows_written = max(0, int(rows_written or 0))
        status = str(status or "UNKNOWN").upper()
        # A request that raised no exception but produced no verified rows is
        # not successful evidence.  Consumers must distinguish a real empty
        # trading-day result from absent/unparsed data.
        if status in {"OK", "READY", "COMPLETE"} and rows_written == 0:
            status = "EMPTY_UNVERIFIED"
            error = error or "provider completed without verified rows"
        with self.write_lock:
            self.conn.execute(
                "INSERT INTO reference_data_runs(job_name,run_date,status,rows_written,error) VALUES(?,?,?,?,?) "
                "ON CONFLICT(job_name,run_date) DO UPDATE SET status=excluded.status, rows_written=excluded.rows_written, error=excluded.error, finished_at=CURRENT_TIMESTAMP",
                (job_name, run_date, status, rows_written, error[:300] if error else ""))
            self.conn.commit()

    def reference_run_status(self) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM reference_data_runs ORDER BY finished_at DESC LIMIT 20").fetchall()
        return [dict(r) for r in rows]

    def save_fundamentals_cache(self, isin: str, ok: bool, payload: Dict[str, Any]) -> None:
        """Persist a live Upstox fundamentals_snapshot() result. Replaces the
        old in-memory-only _fund_api_cache dict so results survive restarts
        (see storage.py fundamentals_cache table comment for why)."""
        isin = str(isin or "").strip().upper()
        if not isin:
            return
        observed_at = now_iso()
        payload_json = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        with self.write_lock:
            self.conn.execute(
                "INSERT INTO fundamentals_cache(isin,ok,payload_json,fetched_at) VALUES(?,?,?,?) "
                "ON CONFLICT(isin) DO UPDATE SET ok=excluded.ok, payload_json=excluded.payload_json, fetched_at=excluded.fetched_at",
                (isin, 1 if ok else 0, payload_json, observed_at))
            # Successful fundamentals are durable, point-in-time evidence.
            # Keep every distinct provider version instead of overwriting the
            # only historical copy when a newer filing arrives.
            if ok:
                payload_hash = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
                as_of = str(
                    payload.get("effective_date")
                    or payload.get("as_of")
                    or payload.get("reporting_date")
                    or payload.get("period_end")
                    or ""
                )
                self.conn.execute(
                    """INSERT INTO fundamentals_history(
                         isin,payload_hash,as_of,ok,payload_json,source,first_seen_at,last_seen_at
                       ) VALUES(?,?,?,?,?,?,?,?)
                       ON CONFLICT(isin,payload_hash) DO UPDATE SET
                         last_seen_at=excluded.last_seen_at""",
                    (isin, payload_hash, as_of, 1, payload_json, str(payload.get("source") or ""), observed_at, observed_at),
                )
            self.conn.commit()

    def get_fundamentals_cache(self, isin: str) -> Optional[Dict[str, Any]]:
        isin = str(isin or "").strip().upper()
        if not isin:
            return None
        row = self.conn.execute(
            "SELECT ok, payload_json, fetched_at FROM fundamentals_cache WHERE isin=?", (isin,)).fetchone()
        if not row:
            return None
        try:
            payload = json.loads(row["payload_json"])
        except Exception:
            return None
        return {"ok": bool(row["ok"]), "payload": payload, "fetched_at": row["fetched_at"]}

    def get_all_fundamentals_cache(self) -> Dict[str, Dict[str, Any]]:
        """Bulk read for startup hydration -- one query instead of one per
        symbol. Hot loops (the position/delivery scanner batch) must never
        hit the DB per-symbol; they read the in-memory dict this populates."""
        out: Dict[str, Dict[str, Any]] = {}
        try:
            rows = self.conn.execute("SELECT isin, ok, payload_json, fetched_at FROM fundamentals_cache").fetchall()
        except Exception:
            return out
        for row in rows:
            try:
                out[row["isin"]] = {"ok": bool(row["ok"]), "payload": json.loads(row["payload_json"]), "fetched_at": row["fetched_at"]}
            except Exception:
                continue
        return out

    def get_fundamentals_history(self, isin: str, limit: int = 100) -> List[Dict[str, Any]]:
        isin = str(isin or "").strip().upper()
        if not isin:
            return []
        rows = self.conn.execute(
            """SELECT isin,payload_hash,as_of,ok,payload_json,source,first_seen_at,last_seen_at
               FROM fundamentals_history WHERE isin=?
               ORDER BY COALESCE(as_of,last_seen_at) DESC,last_seen_at DESC LIMIT ?""",
            (isin, max(1, min(1000, int(limit or 100)))),
        ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            try:
                item["payload"] = json.loads(item.pop("payload_json"))
            except Exception:
                continue
            item["ok"] = bool(item.get("ok"))
            out.append(item)
        return out



    def save_earnings_calendar(self, rows: List[Dict[str, Any]]) -> int:
        n = 0
        with self.write_lock:
            for r in rows:
                try:
                    sym = str(r.get("symbol") or "").upper().strip()
                    ev_date = str(r.get("event_date") or "").strip()
                    if not sym or not ev_date:
                        continue
                    self.conn.execute(
                        "INSERT INTO earnings_calendar(symbol,event_date,event_type,purpose) VALUES(?,?,?,?) "
                        "ON CONFLICT(symbol,event_date,event_type) DO UPDATE SET purpose=excluded.purpose",
                        (sym, ev_date, r.get("event_type") or "board_meeting", r.get("purpose") or ""))
                    n += 1
                except Exception:
                    continue
            self.conn.commit()
        return n

    def get_upcoming_earnings(self, symbol: str, within_days: int = 3) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM earnings_calendar WHERE symbol=? AND event_date BETWEEN date('now') AND date('now', ?) ORDER BY event_date",
            (str(symbol or "").upper().strip(), f"+{within_days} days")).fetchall()
        return [dict(r) for r in rows]

    def event_risk_symbols(self, within_days: int = 3) -> Dict[str, str]:
        """Returns {symbol: nearest_event_date} for every symbol with an
        earnings/corp-action event within the window -- used as a single
        cheap lookup the analytics layer can check per candidate instead
        of querying per-symbol in a hot loop."""
        rows = self.conn.execute(
            "SELECT symbol, MIN(event_date) as nearest FROM earnings_calendar "
            "WHERE event_date BETWEEN date('now') AND date('now', ?) GROUP BY symbol",
            (f"+{within_days} days",)).fetchall()
        return {r["symbol"]: r["nearest"] for r in rows}
