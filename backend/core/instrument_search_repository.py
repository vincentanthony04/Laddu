"""Instrument/symbol search & cache: persistent instrument-key cache,
in-memory symbol typeahead index, cash-equity/index instrument search,
and universe helpers (intelligence_universe/liquid_wide_universe).
Extracted verbatim from storage.py's Store class (v51 storage split,
cluster 1). No behavior change.

Unlike the other cluster repositories (ManualWatchRepository,
MarketDataRepository, PriorityRepository, ...), this one is NOT
constructed fresh per call. The in-memory symbol index
(_symbol_index / _symbol_index_built_at / _symbol_index_lock) is a
cache that must survive across calls -- that's the whole point of it
(amortize the cost of building it over thousands of keystrokes) -- so
Store holds a single instance of this repository, created once in
Store.__init__, exactly as the old code held the index as instance
state on Store itself.

conn_getter is a zero-arg callable returning the current thread's
sqlite3.Connection (Store.conn is a per-thread property), so this
repository stays correct under the same one-connection-per-thread
model as the rest of storage.py.
"""
from __future__ import annotations

import bisect
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from models import now_iso
from core.production_mode_policy import require_production_mode


class InstrumentSearchRepository:
    def __init__(self, conn_getter: Callable[[], Any], is_clean_stock_symbol: Callable[[Dict[str, Any]], bool],
                 intelligence_scan_symbols: List[str], write_lock: Optional[threading.Lock] = None):
        self._conn_getter = conn_getter
        self._is_clean_stock_symbol = is_clean_stock_symbol
        self._intelligence_scan_symbols = intelligence_scan_symbols
        # v60.14 P0 fix: set_cached_instrument (fires per-symbol on every
        # cold quote resolution) and upsert_instruments (bulk instrument
        # bootstrap) wrote without Store.write_lock. Same fallback-lock
        # pattern as the other repos fixed for "database is locked".
        self.write_lock = write_lock or threading.Lock()
        # v36.7: slim in-memory symbol index for the keystroke-driven typeahead
        # (/api/suggest). Every keystroke previously ran a SQLite query against
        # the instrument catalogue; even indexed, that's still disk/WAL
        # round-trips per key. This index holds only (symbol, name, exchange,
        # instrument_key) sorted by symbol so lookups are a binary search over
        # a Python list in RAM. Full instrument detail is still loaded from
        # SQLite only once, at selection time (find_instruments/_first_instrument).
        self._symbol_index: List[tuple] = []  # sorted list of (symbol, name, exchange, instrument_key)
        self._symbol_index_built_at = 0.0
        self._symbol_index_lock = threading.Lock()

    @property
    def conn(self):
        return self._conn_getter()

    # v36.6.1: persistent symbol -> instrument resolution cache (kv-backed).
    # Previously this only lived in an in-process dict, so every backend
    # restart wiped it and the next quote poll for every visible symbol had
    # to re-run a live instrument search inline on the request thread --
    # that's what produced the "Quotes timeout Nx" pileup right after restart.
    def get_cached_instrument(self, symbol: str) -> Optional[Dict[str, Any]]:
        import json
        try:
            row = self.conn.execute("SELECT v FROM kv WHERE k=?", (f"instkey:{symbol.upper()}",)).fetchone()
            return json.loads(row["v"]) if row else None
        except Exception:
            return None

    def find_instrument_by_key(self, instrument_key: str) -> Optional[Dict[str, Any]]:
        """Resolve an already-authoritative provider token without fuzzy search."""
        key = str(instrument_key or "").strip()
        if not key:
            return None
        try:
            row = self.conn.execute(
                "SELECT * FROM instruments WHERE UPPER(instrument_key)=UPPER(?) LIMIT 1",
                (key,),
            ).fetchone()
            return dict(row) if row else None
        except Exception:
            return None

    def set_cached_instrument(self, symbol: str, inst: Dict[str, Any]) -> None:
        import json
        try:
            with self.write_lock:
                self.conn.execute(
                    "INSERT INTO kv(k, v, updated_at) VALUES(?, ?, CURRENT_TIMESTAMP) ON CONFLICT(k) DO UPDATE SET v=excluded.v, updated_at=CURRENT_TIMESTAMP",
                    (f"instkey:{symbol.upper()}", json.dumps(inst)),
                )
                self.conn.commit()
        except Exception:
            pass

    def upsert_instruments(self, rows: List[Dict[str, Any]]) -> None:
        if not rows:
            return
        with self.write_lock:
            self.conn.executemany(
                """INSERT INTO instruments(instrument_key,exchange,segment,trading_symbol,name,instrument_type,isin,expiry,strike,option_type,lot_size)
                   VALUES(:instrument_key,:exchange,:segment,:trading_symbol,:name,:instrument_type,:isin,:expiry,:strike,:option_type,:lot_size)
                   ON CONFLICT(instrument_key) DO UPDATE SET exchange=excluded.exchange, segment=excluded.segment, trading_symbol=excluded.trading_symbol,
                   name=excluded.name, instrument_type=excluded.instrument_type, isin=excluded.isin, expiry=excluded.expiry, strike=excluded.strike,
                   option_type=excluded.option_type, lot_size=excluded.lot_size""", rows)
            self.conn.commit()
        # A typeahead index built while the instrument table was empty used to
        # remain empty for up to 30 minutes after the master download completed.
        # Invalidate it atomically after every bulk upsert so the next keystroke
        # rebuilds from the new authoritative rows.
        with self._symbol_index_lock:
            self._symbol_index = []
            self._symbol_index_built_at = 0.0

    def replace_active_instruments(self, rows: List[Dict[str, Any]]) -> None:
        """Atomically replace the provider-wide master with the binding active catalogue.

        The previous catalogue remains visible until the transaction commits.
        Cached symbol identities are cleared in the same transaction so no
        derivative or suppressed BSE duplicate survives the universe change.
        """
        if not rows:
            raise ValueError("active instrument catalogue cannot be empty")
        sql = """INSERT INTO instruments(instrument_key,exchange,segment,trading_symbol,name,instrument_type,isin,expiry,strike,option_type,lot_size)
                 VALUES(:instrument_key,:exchange,:segment,:trading_symbol,:name,:instrument_type,:isin,:expiry,:strike,:option_type,:lot_size)"""
        with self.write_lock:
            conn = self.conn
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("DELETE FROM instruments")
                conn.executemany(sql, rows)
                conn.execute("DELETE FROM kv WHERE k LIKE 'instkey:%'")
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        with self._symbol_index_lock:
            self._symbol_index = []
            self._symbol_index_built_at = 0.0
        # Build the compact RAM index on the refresh worker, not on the first
        # user keystroke.  This keeps /api/suggest independent of SQLite even
        # immediately after an atomic catalogue swap.
        self._ensure_symbol_index()

    def instrument_universe_stats(self) -> Dict[str, Any]:
        row = self.conn.execute("""SELECT
            SUM(CASE WHEN UPPER(segment)='NSE_EQ' THEN 1 ELSE 0 END) AS nse_equities,
            SUM(CASE WHEN UPPER(segment)='BSE_EQ' THEN 1 ELSE 0 END) AS bse_only_equities,
            SUM(CASE WHEN UPPER(segment) IN ('NSE_INDEX','BSE_INDEX') THEN 1 ELSE 0 END) AS indices,
            SUM(CASE WHEN UPPER(COALESCE(option_type,'')) IN ('CE','PE')
                       OR UPPER(COALESCE(instrument_type,'')) IN ('CE','PE','FUT','FUTIDX','FUTSTK','OPTIDX','OPTSTK')
                       OR UPPER(COALESCE(segment,'')) LIKE '%FO%'
                     THEN 1 ELSE 0 END) AS derivatives,
            SUM(CASE
                  WHEN UPPER(segment)='NSE_EQ' AND UPPER(COALESCE(instrument_type,'')) NOT IN ('EQ','BE','SM','ST','BZ') THEN 1
                  WHEN UPPER(segment)='BSE_EQ' AND UPPER(COALESCE(instrument_type,'')) NOT IN ('A','B','X','XT','T','M','MT','TS','MS','Z','ZP') THEN 1
                  WHEN UPPER(segment) NOT IN ('NSE_EQ','BSE_EQ','NSE_INDEX','BSE_INDEX') THEN 1
                  ELSE 0 END) AS out_of_policy_rows,
            COUNT(*) AS active_total
          FROM instruments""").fetchone()
        data = dict(row) if row else {}
        out: Dict[str, Any] = {key: int(data.get(key) or 0) for key in
                ("nse_equities", "bse_only_equities", "indices", "derivatives", "out_of_policy_rows", "active_total")}
        samples = self.conn.execute("""SELECT trading_symbol, name, instrument_key
            FROM instruments WHERE UPPER(segment)='BSE_EQ'
            ORDER BY trading_symbol LIMIT 5""").fetchall()
        out["bse_only_sample"] = [dict(r) for r in samples]
        return out

    def _rank_and_dedupe_stock_rows(self, rows: List[Dict[str, Any]], q: str, limit: int) -> List[Dict[str, Any]]:
        q = (q or "").strip().upper()
        clean = [r for r in rows if self._is_clean_stock_symbol(r)]
        exact_nse = [r for r in clean if (r.get("trading_symbol") or "").upper() == q and str(r.get("exchange") or "").upper().startswith("NSE")]
        if exact_nse:
            return exact_nse[:1]
        exact_bse = [r for r in clean if (r.get("trading_symbol") or "").upper() == q and str(r.get("exchange") or "").upper().startswith("BSE")]
        if exact_bse:
            return exact_bse[:1]
        # For non-exact suggestions, prefer NSE and suppress the BSE duplicate when an NSE row exists.
        by_symbol: Dict[str, Dict[str, Any]] = {}
        for r in clean:
            sym = (r.get("trading_symbol") or "").upper()
            if not sym:
                continue
            cur = by_symbol.get(sym)
            if cur is None or str(r.get("exchange") or "").upper().startswith("NSE"):
                by_symbol[sym] = r
        return list(by_symbol.values())[:limit]

    def _ensure_symbol_index(self, max_age: float = 1800.0) -> None:
        """Build/refresh the slim in-memory symbol index. Cheap to rebuild
        (a few thousand short tuples) and only happens once per max_age window, so the
        cost is amortized across thousands of keystrokes."""
        if self._symbol_index and (time.time() - self._symbol_index_built_at) < max_age:
            return
        with self._symbol_index_lock:
            if self._symbol_index and (time.time() - self._symbol_index_built_at) < max_age:
                return
            type_filter = "(UPPER(segment) IN ('NSE_EQ','BSE_EQ','NSE','BSE') OR UPPER(segment) LIKE 'NSE%EQ%' OR UPPER(segment) LIKE 'BSE%EQ%') AND UPPER(COALESCE(instrument_type,'')) NOT IN ('CE','PE','FUT','INDEX','BOND','NCD') AND COALESCE(option_type,'')=''"
            rows = self.conn.execute(
                f"SELECT trading_symbol, name, exchange, instrument_key FROM instruments WHERE {type_filter}"
            ).fetchall()
            idx = []
            for r in rows:
                d = dict(r)
                if self._is_clean_stock_symbol(d):
                    idx.append((str(d.get("trading_symbol") or "").upper(), str(d.get("name") or ""), str(d.get("exchange") or ""), str(d.get("instrument_key") or "")))
            idx.sort(key=lambda t: t[0])
            self._symbol_index = idx
            self._symbol_index_built_at = time.time()

    def warm_symbol_index(self) -> int:
        self._ensure_symbol_index()
        return len(self._symbol_index)

    def quick_symbol_search(self, q: str, limit: int = 8) -> List[Dict[str, Any]]:
        """Fast typeahead lookup for /api/suggest. Stock mode is NSE-first,
        BSE fallback-only: if a symbol exists on NSE and BSE, only the NSE row is
        shown. This keeps the search contract aligned with /api/search."""
        q = (q or "").strip().upper()
        if not q:
            return []
        self._ensure_symbol_index()
        idx = self._symbol_index
        if not idx:
            return []
        keys = [t[0] for t in idx]
        lo = bisect.bisect_left(keys, q)
        raw = []
        i = lo
        while i < len(idx) and idx[i][0].startswith(q) and len(raw) < max(limit * 6, 40):
            sym, name, exch, ikey = idx[i]
            raw.append({"trading_symbol": sym, "name": name, "exchange": exch, "instrument_key": ikey})
            i += 1
        if len(raw) < max(limit * 2, 16) and len(q) >= 3:
            # substring fallback over the in-memory list only (still no disk I/O)
            for sym, name, exch, ikey in idx:
                if len(raw) >= max(limit * 6, 40):
                    break
                if q in sym or q in name.upper():
                    if not any(o["instrument_key"] == ikey for o in raw):
                        raw.append({"trading_symbol": sym, "name": name, "exchange": exch, "instrument_key": ikey})
        # exact NSE first; exact BSE only when exact NSE is absent; prefix rows dedupe by symbol, NSE wins.
        exact_nse = [r for r in raw if r["trading_symbol"] == q and str(r.get("exchange") or "").upper().startswith("NSE")]
        if exact_nse:
            return exact_nse[:1]
        exact_bse = [r for r in raw if r["trading_symbol"] == q and str(r.get("exchange") or "").upper().startswith("BSE")]
        if exact_bse:
            return exact_bse[:1]
        by_symbol: Dict[str, Dict[str, Any]] = {}
        order: List[str] = []
        for r in raw:
            sym = str(r.get("trading_symbol") or "").upper()
            if not sym:
                continue
            cur = by_symbol.get(sym)
            if cur is None:
                by_symbol[sym] = r; order.append(sym)
            elif str(r.get("exchange") or "").upper().startswith("NSE") and not str(cur.get("exchange") or "").upper().startswith("NSE"):
                by_symbol[sym] = r
        return [by_symbol[sym] for sym in order][:limit]

    def find_instruments(self, q: str, limit: int = 20) -> List[Dict[str, Any]]:
        q = (q or "").strip().upper()
        if not q:
            return []
        # v36.6: prefix-only match first (sargable, hits ix_instruments_symbol_upper /
        # ix_instruments_name_upper). A leading-wildcard LIKE '%q%' can never use those
        # indexes and forces a full scan of the instrument catalogue on every
        # keystroke -- that scan storm was the source of WinError 10053 / timeouts that
        # blacklisted symbols and left charts, support/resistance, and candidate lists empty.
        base_type_filter = "UPPER(COALESCE(instrument_type,'')) NOT IN ('CE','PE','FUT','INDEX','BOND','NCD') AND COALESCE(option_type,'')=''"
        nse_filter = f"(UPPER(segment) IN ('NSE_EQ','NSE') OR UPPER(segment) LIKE 'NSE%EQ%') AND {base_type_filter}"
        bse_filter = f"(UPPER(segment) IN ('BSE_EQ','BSE') OR UPPER(segment) LIKE 'BSE%EQ%') AND {base_type_filter}"
        order = "CASE WHEN UPPER(trading_symbol)=? THEN 0 WHEN UPPER(trading_symbol) LIKE ? THEN 1 ELSE 2 END, trading_symbol"

        def _query(exch_filter: str, substr: bool) -> list:
            match = "(UPPER(trading_symbol)=? OR UPPER(trading_symbol) LIKE ? OR UPPER(name) LIKE ?)"
            params = [q, q + "%", ("%" + q + "%") if substr else (q + "%")]
            return self.conn.execute(
                f"SELECT * FROM instruments WHERE {match} AND {exch_filter} ORDER BY {order} LIMIT ?",
                tuple(params + [q, q + "%", max(limit * 12, 120)]),
            ).fetchall()

        # NSE-only first (sargable prefix match). BSE is only consulted for symbols
        # NSE didn't have -- never shown alongside an NSE match for the same query.
        rows = _query(nse_filter, substr=False)
        if len(rows) < limit and len(q) >= 3:
            rows = list(rows) + list(_query(nse_filter, substr=True))
        nse_symbols = {str(dict(r).get("trading_symbol") or "").upper() for r in rows}

        if len(rows) < limit:
            bse_rows = _query(bse_filter, substr=False)
            if len(bse_rows) < limit and len(q) >= 3:
                bse_rows = list(bse_rows) + list(_query(bse_filter, substr=True))
            # drop any BSE row whose symbol already has an NSE match
            bse_rows = [r for r in bse_rows if str(dict(r).get("trading_symbol") or "").upper() not in nse_symbols]
            rows = list(rows) + bse_rows

        return self._rank_and_dedupe_stock_rows([dict(r) for r in rows], q, limit)

    def find_any_instruments(self, q: str, limit: int = 20) -> List[Dict[str, Any]]:
        # v37.4: second query was an unconditional leading-wildcard LIKE '%q%'
        # -- a full scan of the instrument catalogue -- run on every call that fell
        # through here, including every time a symbol matched nothing at all
        # (nothing to cache -> re-scanned again next poll, forever). Mirrors
        # find_instruments' prefix-first strategy: try the sargable prefix
        # match first, only pay for the substring scan if that comes up dry.
        q = (q or "").strip().upper()
        if not q:
            return []
        order_sql = """ORDER BY CASE WHEN UPPER(trading_symbol)=? THEN 0 WHEN UPPER(trading_symbol) LIKE ? THEN 1 ELSE 2 END,
                     CASE WHEN UPPER(exchange)='NSE' OR UPPER(exchange) LIKE 'NSE%' THEN 0 WHEN UPPER(exchange)='BSE' OR UPPER(exchange) LIKE 'BSE%' THEN 1 ELSE 2 END, trading_symbol"""
        rows = self.conn.execute(f"""SELECT * FROM instruments
            WHERE UPPER(trading_symbol)=? OR UPPER(trading_symbol) LIKE ? OR UPPER(name) LIKE ?
            {order_sql} LIMIT ?""", (q, q + "%", q + "%", q, q + "%", limit)).fetchall()
        if len(rows) < limit and len(q) >= 3:
            substr_rows = self.conn.execute(f"""SELECT * FROM instruments
                WHERE UPPER(trading_symbol) LIKE ? OR UPPER(name) LIKE ?
                {order_sql} LIMIT ?""", ("%" + q + "%", "%" + q + "%", q, q + "%", limit)).fetchall()
            seen = {dict(r).get("instrument_key") for r in rows}
            rows = list(rows) + [r for r in substr_rows if dict(r).get("instrument_key") not in seen]
        return [dict(r) for r in rows]

    def find_index_instruments(self, q: str, limit: int = 5) -> List[Dict[str, Any]]:
        # v37.4: was an unconditional leading-wildcard LIKE '%q%' every call --
        # same full-table-scan bug as find_any_instruments above, hit
        # constantly since every "NIFTY ..." sector-index quote-delta symbol
        # routed through here. Prefix match first; only fall back to the
        # substring scan if the sargable query comes up empty.
        q = (q or "").strip().upper()
        if not q:
            return []
        index_filter = "(UPPER(instrument_type)='INDEX' OR UPPER(segment) LIKE '%INDEX%' OR UPPER(name) LIKE '%INDEX%')"
        order_sql = "ORDER BY CASE WHEN UPPER(trading_symbol)=? THEN 0 WHEN UPPER(trading_symbol) LIKE ? THEN 1 ELSE 2 END, trading_symbol"
        rows = self.conn.execute(f"""SELECT * FROM instruments
            WHERE (UPPER(trading_symbol)=? OR UPPER(trading_symbol) LIKE ? OR UPPER(name) LIKE ?)
              AND {index_filter}
            {order_sql} LIMIT ?""", (q, q + "%", q + "%", q, q + "%", limit)).fetchall()
        if len(rows) < limit and len(q) >= 3:
            substr_rows = self.conn.execute(f"""SELECT * FROM instruments
                WHERE (UPPER(trading_symbol) LIKE ? OR UPPER(name) LIKE ?)
                  AND {index_filter}
                {order_sql} LIMIT ?""", ("%" + q + "%", "%" + q + "%", q, q + "%", limit)).fetchall()
            seen = {dict(r).get("instrument_key") for r in rows}
            rows = list(rows) + [r for r in substr_rows if dict(r).get("instrument_key") not in seen]
        return [dict(r) for r in rows]

    def instrument_count(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) AS c FROM instruments").fetchone()
        return int(row["c"] or 0) if row else 0

    def all_eligible_equity_keys(self, limit: int = 5000) -> List[Dict[str, Any]]:
        rows = self.conn.execute("""SELECT * FROM instruments
            WHERE (UPPER(segment) IN ('NSE_EQ','BSE_EQ','NSE','BSE') OR UPPER(segment) LIKE 'NSE%EQ%' OR UPPER(segment) LIKE 'BSE%EQ%')
              AND UPPER(COALESCE(instrument_type,'')) NOT IN ('CE','PE','FUT','INDEX')
              AND COALESCE(option_type,'')=''
            ORDER BY CASE WHEN UPPER(exchange)='NSE' OR UPPER(exchange) LIKE 'NSE%' THEN 0 ELSE 1 END, trading_symbol LIMIT ?""", (max(limit*4, limit),)).fetchall()
        clean = [dict(r) for r in rows if self._is_clean_stock_symbol(dict(r))]
        return clean[:limit]

    def all_authoritative_reference_rows(self, limit: int = 10000) -> List[Dict[str, Any]]:
        """Return ordinary cash equities plus read-only index context rows.

        Desk snapshots still receive equities only.  Indices are included here
        solely so ``build_canonical_universe`` can populate its separate
        market-context collection from the same PostgreSQL-backed catalogue.
        """
        cap = max(1, int(limit))
        rows = self.conn.execute("""SELECT * FROM instruments
            WHERE UPPER(segment) IN ('NSE_EQ','BSE_EQ','NSE_INDEX','BSE_INDEX')
              AND COALESCE(option_type,'')=''
              AND UPPER(COALESCE(instrument_type,'')) NOT IN ('CE','PE','FUT','FUTIDX','FUTSTK','OPTIDX','OPTSTK')
            ORDER BY CASE
                       WHEN UPPER(segment)='NSE_INDEX' THEN 0
                       WHEN UPPER(segment)='BSE_INDEX' THEN 1
                       WHEN UPPER(segment)='NSE_EQ' THEN 2
                       ELSE 3
                     END, trading_symbol
            LIMIT ?""", (cap,)).fetchall()
        out: List[Dict[str, Any]] = []
        for raw in rows:
            row = dict(raw)
            segment = str(row.get('segment') or '').upper()
            if segment in ('NSE_INDEX','BSE_INDEX'):
                out.append(row)
            elif self._is_clean_stock_symbol(row):
                out.append(row)
        return out

    def tradable_nse_equity_universe(self, limit: int = 5000) -> List[Dict[str, Any]]:
        rows = self.conn.execute("""SELECT * FROM instruments
            WHERE UPPER(segment)='NSE_EQ'
              AND UPPER(COALESCE(instrument_type,'')) IN ('EQ','BE','SM','ST','BZ')
              AND COALESCE(option_type,'')=''
            ORDER BY CASE WHEN UPPER(instrument_type)='EQ' THEN 0 ELSE 1 END, trading_symbol
            LIMIT ?""", (limit,)).fetchall()
        clean = [dict(row) for row in rows if self._is_clean_stock_symbol(dict(row))]
        seen = set()
        out = []
        for row in clean:
            symbol = str(row.get("trading_symbol") or "").upper().strip()
            if symbol and symbol not in seen:
                seen.add(symbol)
                out.append(row)
        return out

    def symbols_to_equity_rows(self, symbols: List[str], limit: int = 500) -> List[Dict[str, Any]]:
        """Resolve a provided symbol order to local instrument rows. Exact NSE first, no API/network."""
        out: List[Dict[str, Any]] = []
        seen = set()
        for sym in symbols:
            q = str(sym or '').upper().strip()
            if not q or q in seen:
                continue
            rows = self.conn.execute("""SELECT * FROM instruments
                WHERE UPPER(trading_symbol)=?
                  AND (UPPER(segment) IN ('NSE_EQ','BSE_EQ','NSE','BSE') OR UPPER(segment) LIKE 'NSE%EQ%' OR UPPER(segment) LIKE 'BSE%EQ%')
                  AND UPPER(COALESCE(instrument_type,'')) NOT IN ('CE','PE','FUT','INDEX','BOND','NCD')
                  AND COALESCE(option_type,'')=''
                ORDER BY CASE WHEN UPPER(exchange)='NSE' OR UPPER(exchange) LIKE 'NSE%' THEN 0 ELSE 1 END
                LIMIT 1""", (q,)).fetchall()
            if rows:
                r = dict(rows[0])
                if self._is_clean_stock_symbol(r):
                    out.append(r); seen.add(q)
            if len(out) >= limit:
                break
        return out

    def intelligence_universe(self, limit: int = 500, include_rest: bool = False, offset: int = 0) -> List[Dict[str, Any]]:
        """Priority scanner universe: manual queue is handled elsewhere; this starts with liquid/index names, not A-list alphabetical rows."""
        primary = self.symbols_to_equity_rows(self._intelligence_scan_symbols, limit=limit)
        if not include_rest or len(primary) >= limit:
            return primary[:limit]
        primary_keys = {r.get('instrument_key') for r in primary}
        rest = [r for r in self.all_eligible_equity_keys(limit=max(limit*3, 1000)) if r.get('instrument_key') not in primary_keys]
        return (primary + rest[offset:offset+max(0, limit-len(primary))])[:limit]

    def liquid_wide_universe(self, limit: int = 5000, lookback_days: int = 60, min_avg_traded_qty: float = 50000.0) -> List[Dict[str, Any]]:
        """Delivery research universe, ranked by real trailing
        liquidity from delivery_data rather than a hand-curated ticker list.
        See VALIDATION_FINDINGS_2026-07-18.md sections 1/5/14: the same
        delivery-based signal that clears IR>=0.7 on ~1,700 liquid symbols
        collapses on the narrow ~170-symbol curated list. This does NOT feed
        Intraday -- those stay on INTELLIGENCE_SCAN_SYMBOLS to protect
        refresh cadence (a 10x universe multiplies deep-scan sweep time
        roughly 10x, which Intraday cannot tolerate; see section 5).
        Falls back to INTELLIGENCE_SCAN_SYMBOLS-derived rows if delivery_data
        is too sparse to rank (e.g. fresh install, no history yet).
        """
        rows = self.conn.execute(
            """SELECT symbol, AVG(traded_qty) AS avg_qty, COUNT(*) AS n
               FROM delivery_data
               WHERE traded_qty IS NOT NULL
                 AND trade_date >= date('now', ?)
               GROUP BY symbol
               HAVING n >= 10 AND avg_qty >= ?
               ORDER BY avg_qty DESC
               LIMIT ?""",
            (f"-{int(lookback_days)} days", float(min_avg_traded_qty), int(limit)),
        ).fetchall()
        symbols = [str(r["symbol"]).upper() for r in rows if r["symbol"]]
        if len(symbols) < 200:
            # Not enough NSE delivery history yet (fresh install / backfill in
            # progress): use the curated NSE priority tier, but still append all
            # BSE-only cash equities so they are not silently outside Delivery.
            primary = self.symbols_to_equity_rows(self._intelligence_scan_symbols, limit=limit)
        else:
            primary = self.symbols_to_equity_rows(symbols, limit=limit)

        bse_rows = self.conn.execute("""SELECT * FROM instruments
            WHERE UPPER(segment)='BSE_EQ'
              AND UPPER(COALESCE(instrument_type,'')) IN ('A','B','X','XT','T','M','MT','TS','MS','Z','ZP')
              AND COALESCE(option_type,'')=''
            ORDER BY trading_symbol""").fetchall()
        out = list(primary)
        seen = {str(r.get('instrument_key') or '') for r in out}
        for raw in bse_rows:
            row = dict(raw)
            key = str(row.get('instrument_key') or '')
            if key and key not in seen and self._is_clean_stock_symbol(row):
                out.append(row); seen.add(key)
            if len(out) >= limit:
                break
        return out[:limit]

    def liquidity_ranked_symbols(self, limit: int = 1500, lookback_days: int = 60, min_avg_turnover: float = 50_000_000.0) -> List[str]:
        """Return symbols with measured trailing cash-market liquidity.

        Eligibility is based on average traded value (quantity × close), not a
        provider catalogue row or share quantity alone.  The result is bounded
        as a scheduler safety ceiling, while the actual population remains
        data-driven.  BSE-only names enter only when equivalent qualifying
        evidence is available or the operator explicitly prioritises them.
        """
        rows = self.conn.execute(
            """SELECT UPPER(symbol) AS symbol,
                      AVG(traded_qty * close) AS avg_turnover,
                      COUNT(*) AS n
                 FROM delivery_data
                WHERE traded_qty IS NOT NULL
                  AND close IS NOT NULL
                  AND traded_qty > 0
                  AND close > 0
                  AND trade_date >= date('now', ?)
                GROUP BY UPPER(symbol)
               HAVING COUNT(*) >= 10 AND AVG(traded_qty * close) >= ?
                ORDER BY avg_turnover DESC
                LIMIT ?""",
            (f"-{int(lookback_days)} days", float(min_avg_turnover), int(limit)),
        ).fetchall()
        return [str(row["symbol"] or "").upper().strip() for row in rows if row["symbol"]]

    def priority_symbols_set(self) -> set:
        rows = self.conn.execute("SELECT UPPER(symbol) AS symbol FROM priority_symbols UNION SELECT UPPER(symbol) AS symbol FROM manual_watch").fetchall()
        return {str(r['symbol']).upper() for r in rows if r['symbol']}

    def cleanup_scanner_artifacts(
        self, core_symbols: List[str], *, include_legacy_decisions: bool = True
    ) -> Dict[str, int]:
        """Remove compatibility artefacts without crossing a production authority boundary."""
        core = {s.upper() for s in core_symbols}
        priority = self.priority_symbols_set()
        keep = core | priority
        removed_decisions = removed_signals = 0
        if include_legacy_decisions:
            rows = self.conn.execute("SELECT id, symbol, mode, score, status, decision FROM decisions ORDER BY id DESC LIMIT 1000").fetchall()
            for r in rows:
                sym = str(r['symbol'] or '').upper()
                try:
                    mode = require_production_mode(r['mode'])
                except ValueError:
                    continue
                score = int(r['score'] or 0)
                if sym and sym not in keep and mode == 'delivery' and score < 84:
                    self.conn.execute("DELETE FROM decisions WHERE id=?", (r['id'],)); removed_decisions += 1
            rows = self.conn.execute("SELECT signal_id, symbol, mode, score, source FROM signal_ledger WHERE trade_date=?", (now_iso()[:10],)).fetchall()
            for r in rows:
                sym = str(r['symbol'] or '').upper()
                try:
                    mode = require_production_mode(r['mode'])
                except ValueError:
                    continue
                score = int(r['score'] or 0)
                if sym and sym not in keep and mode == 'delivery' and score < 84:
                    self.conn.execute("DELETE FROM signal_ledger WHERE signal_id=?", (r['signal_id'],)); removed_signals += 1
            self.conn.commit()
        return {
            "decisions": removed_decisions,
            "signals": removed_signals,
            "legacy_decision_cleanup": bool(include_legacy_decisions),
        }
