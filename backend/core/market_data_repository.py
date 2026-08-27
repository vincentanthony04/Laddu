"""Bounded persistence context for OHLCV candles, live quotes, and price
snapshots. Extracted verbatim from storage.py's Store class (v51 storage
split, cluster 2) -- same extraction pattern as core/manual_watch_repository.py
and the LadduRuntime clusters 3-9 done in main.py. No behavior change.

v60.13: this repo's two writers (save_candles, save_quote) were never given
Store.write_lock -- signal_ledger_repository and performance_journal_repository
were fixed in v60.5/v60.7 (see those files' history), but this one was missed
because its writes don't have a check-then-act race, only PurposeError-free
single-statement/executemany+commit sequences. WAL + busy_timeout=5000 was
assumed to be enough to serialize those against other writers, but live
telemetry (api_errors on quote_delta and sticky_selected_merge, both
"database is locked") shows contention exceeding the 5s busy_timeout under
real concurrent load: intraday scanning (up to 30 save_decision calls/cycle,
already write_lock-guarded) racing quote_delta's per-poll save_quote calls,
which were NOT going through the same lock and so could hold the SQLite
writer slot at the same moment another write_lock holder was already
mid-transaction, pushing wait times past 5000ms. Routing every writer in the
app through the single shared write_lock removes the OS/SQLite-level race
entirely (only one thread ever attempts a write transaction at a time), which
is a stronger fix than raising the timeout further."""
from __future__ import annotations

import json
import threading
from typing import Any, Dict, List

from core.db_utils import canonical_interval, canonical_timestamp, utc_now_iso, timed_write
from core.historical_data_service import ensure_historical_data_schema, refresh_historical_manifest


class MarketDataRepository:
    def __init__(self, connection, write_lock=None):
        self.conn = connection
        # v60.13: falls back to a private lock if not supplied so this repo
        # is never unsynchronized, but Store always passes its shared one --
        # same fallback pattern as SignalLedgerRepository.
        self.write_lock = write_lock or threading.Lock()

    def save_candles(self, instrument_key: str, interval: str, candles: List[Dict[str, Any]], source: str = "upstox_historical") -> int:
        """Persist OHLCV candles with provenance, canonical interval and canonical timestamps."""
        if not instrument_key or not candles:
            return 0
        norm_interval = canonical_interval(interval)
        received_at = utc_now_iso()
        rows = []
        for c in candles:
            provider_ts = c.get("timestamp") or c.get("time") or c.get("date")
            ts = canonical_timestamp(provider_ts, norm_interval)
            if ts is None:
                continue
            rows.append((instrument_key, norm_interval, ts, c.get("open"), c.get("high"), c.get("low"), c.get("close"), c.get("volume"), c.get("oi"), source, str(provider_ts or ""), received_at, None))
        if not rows:
            return 0
        def _write():
            with self.write_lock:
                self.conn.executemany(
                    """INSERT INTO candles(instrument_key,interval,ts,open,high,low,close,volume,oi,source,provider_ts,received_at,raw_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(instrument_key,interval,ts) DO UPDATE SET open=excluded.open, high=excluded.high, low=excluded.low,
                    close=excluded.close, volume=excluded.volume, oi=excluded.oi, source=excluded.source,
                    provider_ts=excluded.provider_ts, received_at=excluded.received_at, raw_json=NULL""", rows)
                try:
                    refresh_historical_manifest(self.conn, instrument_key, norm_interval)
                except Exception:
                    # Direct repository tests may not run Store migrations.
                    # Create the additive manifest schema and retry without
                    # affecting the candle write contract.
                    ensure_historical_data_schema(self.conn)
                    refresh_historical_manifest(self.conn, instrument_key, norm_interval)
                self.conn.commit()
        timed_write(f"save_candles {instrument_key}/{norm_interval} ({len(rows)} rows)", _write)
        return len(rows)

    def get_candles(self, instrument_key: str, interval: str, limit: int = 2000) -> List[Dict[str, Any]]:
        """Read back accumulated history, oldest-first, capped at `limit` most recent."""
        norm_interval = canonical_interval(interval)
        rows = self.conn.execute(
            "SELECT ts,open,high,low,close,volume,oi,source,provider_ts,received_at FROM candles WHERE instrument_key=? AND interval=? ORDER BY ts DESC LIMIT ?",
            (instrument_key, norm_interval, int(limit))
        ).fetchall()
        out = [{"timestamp": r["ts"], "open": r["open"], "high": r["high"], "low": r["low"], "close": r["close"], "volume": r["volume"], "oi": r["oi"], "source": r["source"], "provider_timestamp": r["provider_ts"], "received_at": r["received_at"]} for r in rows]
        out.reverse()
        return out

    def get_candles_before(self, instrument_key: str, interval: str, before: Any, limit: int = 2000) -> List[Dict[str, Any]]:
        """Read the local page strictly older than ``before`` (oldest-first).

        This is a chart-navigation read primitive only. It performs no provider
        I/O and preserves the same canonical timestamp authority as get_candles.
        """
        norm_interval = canonical_interval(interval)
        before_ts = canonical_timestamp(before, norm_interval)
        if not before_ts:
            return []
        rows = self.conn.execute(
            "SELECT ts,open,high,low,close,volume,oi,source,provider_ts,received_at FROM candles WHERE instrument_key=? AND interval=? AND ts<? ORDER BY ts DESC LIMIT ?",
            (instrument_key, norm_interval, before_ts, max(1, int(limit))),
        ).fetchall()
        out = [{"timestamp": r["ts"], "open": r["open"], "high": r["high"], "low": r["low"], "close": r["close"], "volume": r["volume"], "oi": r["oi"], "source": r["source"], "provider_timestamp": r["provider_ts"], "received_at": r["received_at"]} for r in rows]
        out.reverse()
        return out

    def candle_coverage(self, instrument_key: str, interval: str) -> Dict[str, Any]:
        norm_interval = canonical_interval(interval)
        row = self.conn.execute("SELECT COUNT(*) AS n, MIN(ts) AS first, MAX(ts) AS last, MAX(received_at) AS last_received_at, MAX(source) AS source FROM candles WHERE instrument_key=? AND interval=?", (instrument_key, norm_interval)).fetchone()
        return {"count": row["n"] or 0, "first": row["first"], "last": row["last"], "last_received_at": row["last_received_at"], "source": row["source"]}

    def recent_daily_candles_many(self, instrument_keys: List[str], limit_per_key: int = 25) -> Dict[str, List[Dict[str, Any]]]:
        """Read a bounded daily reference panel in one SQLite query.

        Broad quote coverage uses the lightweight LTP endpoint, which may not
        include previous close or volume.  This method supplies completed local
        daily references without issuing one query per symbol.
        """
        keys = [str(k or "").strip() for k in instrument_keys or [] if str(k or "").strip()]
        keys = list(dict.fromkeys(keys))
        if not keys:
            return {}
        marks = ",".join("?" for _ in keys)
        cap = max(2, min(60, int(limit_per_key or 25)))
        rows = self.conn.execute(f"""
            SELECT instrument_key,ts,close,volume FROM (
                SELECT instrument_key,ts,close,volume,
                       ROW_NUMBER() OVER (PARTITION BY instrument_key ORDER BY ts DESC) AS rn
                FROM candles
                WHERE interval='1d' AND instrument_key IN ({marks})
            ) WHERE rn<=?
            ORDER BY instrument_key,ts DESC
        """, tuple(keys) + (cap,)).fetchall()
        out: Dict[str, List[Dict[str, Any]]] = {key: [] for key in keys}
        for row in rows:
            out.setdefault(str(row["instrument_key"]), []).append({
                "timestamp": row["ts"], "close": row["close"], "volume": row["volume"]
            })
        return out

    def save_quotes(self, quotes: List[Dict[str, Any]]) -> int:
        """Persist a quote batch in one transaction.

        This replaces one commit per symbol, the dominant source of SQLite
        writer contention during quote-delta and scanner refreshes.
        """
        clean = [q for q in (quotes or []) if q and q.get("instrument_key")]
        if not clean:
            return 0
        quote_rows = []
        snapshot_rows = []
        received_at = utc_now_iso()
        for q in clean:
            raw = {}
            try:
                raw = json.loads(q.get("raw_json") or "{}") if isinstance(q.get("raw_json"), str) else (q.get("raw_json") or {})
            except Exception:
                raw = {}
            quote_rows.append({**q, "raw_json": q.get("raw_json") or json.dumps(raw)})
            provider_ts = q.get("provider_timestamp") or q.get("source_time") or raw.get("timestamp") or raw.get("last_trade_time") or q.get("timestamp")
            snapshot_rows.append((str(q.get("instrument_key")), canonical_timestamp(provider_ts, "1m") or received_at,
                q.get("symbol"), q.get("exchange"), q.get("ltp"), q.get("change_pct"), str(provider_ts or ""),
                received_at, q.get("source") or "upstox_ltp", None))
        def _write():
            with self.write_lock:
                self.conn.executemany("""INSERT INTO quotes(instrument_key,symbol,exchange,ltp,open,high,low,close,volume,oi,iv,change_pct,timestamp,raw_json)
                VALUES(:instrument_key,:symbol,:exchange,:ltp,:open,:high,:low,:close,:volume,:oi,:iv,:change_pct,:timestamp,:raw_json)
                ON CONFLICT(instrument_key) DO UPDATE SET ltp=excluded.ltp, open=excluded.open, high=excluded.high, low=excluded.low, close=excluded.close,
                volume=excluded.volume, oi=excluded.oi, iv=excluded.iv, change_pct=excluded.change_pct, timestamp=excluded.timestamp, raw_json=excluded.raw_json""", quote_rows)
                self.conn.executemany("""INSERT INTO price_snapshots(instrument_key,captured_at,symbol,exchange,ltp,change_pct,provider_ts,received_at,source,raw_json)
                VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(instrument_key,captured_at) DO UPDATE SET
                symbol=excluded.symbol, exchange=excluded.exchange, ltp=excluded.ltp,
                change_pct=excluded.change_pct, provider_ts=excluded.provider_ts,
                received_at=excluded.received_at, raw_json=excluded.raw_json""", snapshot_rows)
                self.conn.commit()
        timed_write(f"save_quotes ({len(clean)} rows)", _write)
        return len(clean)

    def save_quote(self, q: Dict[str, Any]) -> None:
        self.save_quotes([q])

    def price_snapshots(self, symbol: str = "", instrument_key: str = "", limit: int = 1000,
                        start: str = "", end: str = "") -> List[Dict[str, Any]]:
        where, params = [], []
        if instrument_key:
            where.append("instrument_key=?"); params.append(instrument_key)
        if symbol:
            where.append("UPPER(symbol)=?"); params.append(str(symbol).upper())
        if start:
            where.append("captured_at>=?"); params.append(start)
        if end:
            where.append("captured_at<=?"); params.append(end)
        if not where:
            where.append("1=1")
        params.append(int(limit))
        rows = self.conn.execute(f"""SELECT instrument_key,captured_at,symbol,exchange,ltp,change_pct,provider_ts,received_at,source
            FROM price_snapshots WHERE {' AND '.join(where)} ORDER BY captured_at DESC LIMIT ?""", tuple(params)).fetchall()
        return [dict(r) for r in rows]

    def latest_quotes_by_symbol(self, symbols: List[str]) -> Dict[str, Dict[str, Any]]:
        clean = []
        seen = set()
        for s in symbols or []:
            sym = str(s or "").upper().strip()
            if sym and sym not in seen:
                seen.add(sym)
                clean.append(sym)
        if not clean:
            return {}
        marks = ",".join("?" for _ in clean)
        def _read():
            return self.conn.execute(f"""
                SELECT q.* FROM quotes q
                JOIN (
                    SELECT UPPER(symbol) AS symbol, MAX(timestamp) AS timestamp
                    FROM quotes
                    WHERE UPPER(symbol) IN ({marks})
                    GROUP BY UPPER(symbol)
                ) latest ON UPPER(q.symbol)=latest.symbol AND q.timestamp=latest.timestamp
            """, tuple(clean)).fetchall()
        rows = timed_write(f"latest_quotes_by_symbol ({len(clean)} symbols)", _read)
        return {str(r["symbol"] or "").upper(): dict(r) for r in rows if r["symbol"]}

    def recent_nse_equity_quotes(self, limit: int = 250) -> List[Dict[str, Any]]:
        rows = self.conn.execute("""SELECT q.*
            FROM quotes q
            JOIN instruments i ON i.instrument_key=q.instrument_key
            WHERE UPPER(i.segment)='NSE_EQ'
              AND UPPER(COALESCE(i.instrument_type,'')) IN ('EQ','BE')
              AND q.ltp IS NOT NULL
            ORDER BY q.timestamp DESC, COALESCE(q.volume,0) DESC
            LIMIT ?""", (limit,)).fetchall()
        seen = set()
        out = []
        for row in rows:
            item = dict(row)
            symbol = str(item.get("symbol") or "").upper().strip()
            if symbol and symbol not in seen:
                seen.add(symbol)
                out.append(item)
        return out

    def storage_stats(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        try:
            page_count = int(self.conn.execute("PRAGMA page_count").fetchone()[0])
            page_size = int(self.conn.execute("PRAGMA page_size").fetchone()[0])
            out["database_bytes"] = page_count * page_size
        except Exception:
            out["database_bytes"] = None
        for table in ("candles", "price_snapshots", "decisions", "signal_ledger"):
            try:
                out[f"{table}_rows"] = int(self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            except Exception:
                out[f"{table}_rows"] = None
        return out

    def prune_runtime_data(self, *, now_iso: str, chunk_size: int = 5000, max_chunks_per_table: int = 8, include_decisions: bool = True) -> Dict[str, Any]:
        """Bounded post-close retention maintenance.

        Deletes only transient market/cache evidence. signal_ledger and the
        performance journal are intentionally untouched. No VACUUM is issued,
        because a full-file rewrite would block the live service for minutes on
        multi-gigabyte installations.
        """
        from datetime import datetime, timedelta, timezone
        try:
            clock = datetime.fromisoformat(str(now_iso).replace("Z", "+00:00"))
        except Exception:
            clock = datetime.now(timezone.utc)
        if clock.tzinfo is None:
            clock = clock.replace(tzinfo=timezone.utc)
        retention_days = {"1m": 15, "3m": 30, "5m": 45, "10m": 60, "15m": 180, "30m": 180, "60m": 730}
        deleted: Dict[str, int] = {}

        def delete_chunks(label: str, sql: str, params_prefix: tuple):
            total = 0
            for _ in range(max(1, int(max_chunks_per_table))):
                with self.write_lock:
                    cur = self.conn.execute(sql, params_prefix + (int(chunk_size),))
                    count = max(0, int(cur.rowcount or 0))
                    self.conn.commit()
                total += count
                if count < int(chunk_size):
                    break
            deleted[label] = total

        for interval, days in retention_days.items():
            cutoff = (clock - timedelta(days=days)).astimezone(timezone.utc).isoformat()
            delete_chunks(
                f"candles_{interval}",
                """DELETE FROM candles WHERE rowid IN (
                     SELECT rowid FROM candles
                     WHERE interval=? AND ts<?
                       AND COALESCE(source,'') NOT IN (
                         'authorized_backtest_import','point_in_time_backtest','backtest_archive'
                       )
                     LIMIT ?
                   )""",
                (interval, cutoff),
            )
        cutoff_snapshots = (clock - timedelta(days=45)).astimezone(timezone.utc).isoformat()
        delete_chunks("price_snapshots", "DELETE FROM price_snapshots WHERE rowid IN (SELECT rowid FROM price_snapshots WHERE captured_at<? LIMIT ?)", (cutoff_snapshots,))
        if include_decisions:
            cutoff_decisions = (clock - timedelta(days=180)).astimezone(timezone.utc).isoformat()
            delete_chunks("decisions", "DELETE FROM decisions WHERE id IN (SELECT id FROM decisions WHERE created_at<? LIMIT ?)", (cutoff_decisions,))
        else:
            deleted["decisions"] = 0
        # Remove legacy duplicated JSON gradually even when the normalized row
        # remains inside retention. This is safe because all read paths select
        # normalized columns.
        for table in ("candles", "price_snapshots"):
            total = 0
            for _ in range(2):
                with self.write_lock:
                    cur = self.conn.execute(f"UPDATE {table} SET raw_json=NULL WHERE rowid IN (SELECT rowid FROM {table} WHERE raw_json IS NOT NULL LIMIT ?)", (int(chunk_size),))
                    count = max(0, int(cur.rowcount or 0)); self.conn.commit()
                total += count
                if count < int(chunk_size): break
            deleted[f"{table}_raw_json_cleared"] = total
        try:
            with self.write_lock:
                self.conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
        except Exception:
            pass
        return {"ok": True, "deleted": deleted, "stats": self.storage_stats(), "vacuum": "not_run_live_safety"}
