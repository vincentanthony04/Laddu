"""Priority queue for the scanner: DB-backed priority_symbols/manual_watch/
opportunity_memory merge, the sticky-selected TTL ledger (kv-backed), and the
rotating auto-live-priority seed window. Extracted verbatim from storage.py's
Store class (v51 storage split, cluster 4). No behavior change to the working
paths -- one pre-existing bug (NameError on every call to
sticky_selected_dismiss, an undefined `default` name in its return
statement) was found during extraction and initially preserved as-is rather
than silently fixed during an architecture-only pass; fixed in v60.7 after
confirming no caller anywhere in the backend currently invokes it (dead code
today, but a landmine for whenever a "dismiss" action is wired up).

Dependency shape differs from MarketDataRepository: sticky_selected_* needs a
lock + kv get/set (not just the raw connection), and auto_live_priority needs
the scan universe list. Rather than pass the whole Store back in (which would
recreate the God-object coupling this split is meant to remove), the caller
injects exactly what's needed."""
from __future__ import annotations

import time
from typing import Any, Callable, Dict, List, Optional


class PriorityRepository:
    _STICKY_KEY = "sticky_selected:v1"

    def __init__(self, connection, write_lock, get_kv: Callable, set_kv: Callable):
        self.conn = connection
        self.write_lock = write_lock
        self.get_kv = get_kv
        self.set_kv = set_kv

    def add_priority(self, symbol: str, exchange: str, mode: str, source: str) -> None:
        self.conn.execute("INSERT OR REPLACE INTO priority_symbols(symbol,exchange,mode,source,created_at) VALUES(?,?,?,?,CURRENT_TIMESTAMP)", (symbol.upper(), exchange.upper(), mode.lower(), source))
        self.conn.commit()

    def auto_live_priority(self, universe: List[str], limit: int = 6, cursor: int = 0) -> List[Dict[str, Any]]:
        """Seed a rotating Intraday validation window across the liquid universe.

        These rows are scanner priorities only, never recommendations. Every
        candidate must still clear live quote, closed-candle, ORB/VWAP/volume,
        evidence, risk-authority and final-decision gates.
        """
        n = len(universe)
        window = max(1, min(limit, n)) if n else 0
        start = (cursor % n) if n else 0
        names = [universe[(start + i) % n] for i in range(window)] if n else []
        return [
            {"symbol": sym, "exchange": "NSE", "mode": "intraday", "source": "auto_liquid_seed", "created_at": None, "priority_score": 86}
            for sym in names
        ]

    def priority_list(self, universe: List[str], limit: int = 100) -> List[Dict[str, Any]]:
        """Priority queue used by fast/deep scanners.

        Manual/searched rows stay first, then pinned/manual watch, bounded live-mode seeds,
        then opportunity memory. This prevents Delivery/deep-scan history from being the only
        visible source while Intraday has no live candidates to validate.
        """
        db_limit = max(limit * 4, 120)
        rows = self.conn.execute("""
        SELECT symbol, exchange, mode, source, created_at, priority_score FROM (
          SELECT symbol, exchange, mode, source, created_at, 100 AS priority_score FROM priority_symbols
          UNION ALL
          SELECT symbol, exchange, mode, source, updated_at AS created_at, CASE WHEN COALESCE(pinned,0)=1 THEN 95 ELSE 88 END AS priority_score FROM manual_watch
          UNION ALL
          SELECT symbol, exchange, mode, 'opportunity_memory' AS source, updated_at AS created_at, COALESCE(priority_score,0) AS priority_score
          FROM opportunity_memory
          WHERE UPPER(stage) IN ('POTENTIAL','QUALIFIED','ARMED')
        ) ORDER BY priority_score DESC, created_at DESC LIMIT ?
        """, (db_limit,)).fetchall()
        merged: List[Dict[str, Any]] = [dict(r) for r in rows] + self.auto_live_priority(universe, limit=3)
        best: Dict[tuple, Dict[str, Any]] = {}
        for r in merged:
            sym = str(r.get("symbol") or "").upper().strip()
            mode = str(r.get("mode") or "").lower().strip()
            if not sym or not mode:
                continue
            key = (sym, mode)
            cur = best.get(key)
            if cur is None or int(r.get("priority_score") or 0) > int(cur.get("priority_score") or 0):
                r["symbol"] = sym
                r["mode"] = mode
                best[key] = r
        ordered = list(best.values())
        ordered.sort(key=lambda r: (int(r.get("priority_score") or 0), str(r.get("created_at") or "")), reverse=True)
        return ordered[:limit]

    def clear_priority_symbols(self, source_like: str = "") -> int:
        """Clear searched/queued priority symbols. Keeps selected signal ledger and watch queue intact.
        When source_like is provided, only matching priority sources are removed.
        """
        if source_like:
            cur = self.conn.execute("DELETE FROM priority_symbols WHERE source LIKE ?", (f"%{source_like}%",))
        else:
            cur = self.conn.execute("DELETE FROM priority_symbols")
        self.conn.commit(); return cur.rowcount

    def sticky_selected_merge(self, promoted: list, ttl_seconds: int = 90, dismiss_keys: Optional[set] = None) -> list:
        """Merge this cycle's freshly-promoted rows into the sticky ledger
        and return the still-live set (fresh + recently-dropped-but-within-TTL),
        newest/highest-score first. Safe to call every scan cycle."""
        with self.write_lock:
            state = self.get_kv(self._STICKY_KEY, {}) or {}
            now = time.time()
            seen_keys = set()
            for row in promoted or []:
                sym = str(row.get("symbol") or "").upper().strip()
                mode = str(row.get("mode") or "").lower().strip()
                if not sym or not mode:
                    continue
                key = f"{sym}|{mode}"
                seen_keys.add(key)
                entry = state.get(key) or {}
                entry["payload"] = row
                entry["first_seen"] = entry.get("first_seen", now)
                entry["last_seen"] = now
                entry["missing_since"] = None
                state[key] = entry
            dismiss_keys = dismiss_keys or set()
            live_rows = []
            for key, entry in list(state.items()):
                if key in dismiss_keys:
                    del state[key]
                    continue
                if key not in seen_keys:
                    if entry.get("missing_since") is None:
                        entry["missing_since"] = now
                    if now - entry["missing_since"] > ttl_seconds:
                        del state[key]
                        continue
                    payload = dict(entry["payload"])
                    payload["_sticky_stale"] = True
                    live_rows.append((entry["last_seen"], payload))
                else:
                    live_rows.append((entry["last_seen"], entry["payload"]))
            self.set_kv(self._STICKY_KEY, state)
            live_rows.sort(key=lambda t: t[0], reverse=True)
            return [r for _, r in live_rows]

    def sticky_selected_dismiss(self, symbol: str, mode: str) -> None:
        """Explicitly remove one entry (e.g. promoted to an active
        position, or invalidated) rather than waiting for TTL expiry.

        NOTE: preserved verbatim from storage.py, including a pre-existing
        bug -- `return default` below references an undefined name `default`,
        so every call to this method raises NameError. Not fixed here; flagged
        for a separate decision rather than silently changed during an
        architecture-only extraction."""
        key = f"{str(symbol).upper().strip()}|{str(mode).lower().strip()}"
        with self.write_lock:
            state = self.get_kv(self._STICKY_KEY, {}) or {}
            if key in state:
                del state[key]
                self.set_kv(self._STICKY_KEY, state)
