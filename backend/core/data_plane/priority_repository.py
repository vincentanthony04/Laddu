from __future__ import annotations

import time
from typing import Any, Callable, Dict, List, Optional

from core.production_mode_policy import normalise_mode
from .postgres import PostgresAuthority


class ProductionPriorityRepository:
    """PostgreSQL authority for scanner priorities and sticky selection state."""

    production_authority = True
    _STICKY_KEY = "sticky_selected:v1"

    def __init__(self, operational: PostgresAuthority, get_kv: Callable, set_kv: Callable):
        self.operational = operational
        self.get_kv = get_kv
        self.set_kv = set_kv

    @staticmethod
    def _mode(value: str, *, allow_all: bool = True) -> str:
        mode = normalise_mode(value or "all")
        allowed = {"intraday", "delivery"} | ({"all"} if allow_all else set())
        if mode not in allowed:
            raise ValueError(f"unsupported production mode: {value!r}")
        return mode

    def add_priority(self, symbol: str, exchange: str, mode: str, source: str) -> None:
        self.operational.execute(
            """INSERT INTO trading.priority_symbols(symbol,exchange,mode,source,created_at)
               VALUES(%s,%s,%s,%s,now())
               ON CONFLICT(symbol,exchange,mode) DO UPDATE SET
                 source=EXCLUDED.source,created_at=now()""",
            (str(symbol or "").upper().strip(), str(exchange or "NSE").upper(), self._mode(mode), str(source or "search")),
        )

    @staticmethod
    def auto_live_priority(universe: List[str], limit: int = 6, cursor: int = 0) -> List[Dict[str, Any]]:
        n = len(universe)
        window = max(1, min(limit, n)) if n else 0
        start = (cursor % n) if n else 0
        names = [universe[(start + i) % n] for i in range(window)] if n else []
        return [
            {"symbol": sym, "exchange": "NSE", "mode": "intraday", "source": "auto_liquid_seed", "created_at": None, "priority_score": 86}
            for sym in names
        ]

    def priority_list(self, universe: List[str], limit: int = 100) -> List[Dict[str, Any]]:
        db_limit = max(int(limit) * 4, 120)
        rows = self.operational.execute(
            """SELECT symbol,exchange,mode,source,created_at,priority_score FROM (
                 SELECT symbol,exchange,mode,source,created_at,100 AS priority_score
                   FROM trading.priority_symbols
                 UNION ALL
                 SELECT symbol,exchange,mode,source,updated_at AS created_at,
                        CASE WHEN pinned THEN 95 ELSE 88 END AS priority_score
                   FROM trading.manual_watch
                 UNION ALL
                 SELECT symbol,exchange,mode,'opportunity_memory' AS source,updated_at AS created_at,
                        COALESCE(priority_score,0) AS priority_score
                   FROM trading.opportunity_memory
                  WHERE UPPER(stage) IN ('POTENTIAL','QUALIFIED','ARMED')
               ) ranked
               ORDER BY priority_score DESC,created_at DESC LIMIT %s""",
            (db_limit,), fetch="all", statement_timeout_ms=1800,
        ) or []
        merged: List[Dict[str, Any]] = [dict(row) for row in rows] + self.auto_live_priority(universe, limit=3)
        best: Dict[tuple[str, str], Dict[str, Any]] = {}
        for source in merged:
            row = dict(source)
            symbol = str(row.get("symbol") or "").upper().strip()
            mode = self._mode(str(row.get("mode") or "all"))
            if not symbol:
                continue
            key = (symbol, mode)
            current = best.get(key)
            if current is None or int(row.get("priority_score") or 0) > int(current.get("priority_score") or 0):
                row.update({"symbol": symbol, "mode": mode})
                best[key] = row
        ordered = list(best.values())
        ordered.sort(key=lambda row: (int(row.get("priority_score") or 0), str(row.get("created_at") or "")), reverse=True)
        return ordered[: max(1, int(limit))]

    def clear_priority_symbols(self, source_like: str = "") -> int:
        if source_like:
            return int(self.operational.execute(
                "DELETE FROM trading.priority_symbols WHERE source ILIKE %s",
                (f"%{source_like}%",), statement_timeout_ms=1800,
            ) or 0)
        return int(self.operational.execute("DELETE FROM trading.priority_symbols", statement_timeout_ms=1800) or 0)

    def sticky_selected_merge(self, promoted: list, ttl_seconds: int = 90, dismiss_keys: Optional[set] = None) -> list:
        state = self.get_kv(self._STICKY_KEY, {}) or {}
        now = time.time()
        seen_keys = set()
        for source in promoted or []:
            row = dict(source or {})
            symbol = str(row.get("symbol") or "").upper().strip()
            mode = self._mode(str(row.get("mode") or "all"))
            if not symbol or mode == "all":
                continue
            key = f"{symbol}|{mode}"
            seen_keys.add(key)
            entry = dict(state.get(key) or {})
            entry.update({"payload": row, "first_seen": entry.get("first_seen", now), "last_seen": now, "missing_since": None})
            state[key] = entry
        live_rows = []
        for key, raw in list(state.items()):
            entry = dict(raw or {})
            if key in (dismiss_keys or set()):
                state.pop(key, None)
                continue
            if key not in seen_keys:
                if entry.get("missing_since") is None:
                    entry["missing_since"] = now
                    state[key] = entry
                if now - float(entry.get("missing_since") or now) > max(1, int(ttl_seconds)):
                    state.pop(key, None)
                    continue
                payload = dict(entry.get("payload") or {})
                payload["_sticky_stale"] = True
                live_rows.append((float(entry.get("last_seen") or 0), payload))
            else:
                live_rows.append((float(entry.get("last_seen") or 0), dict(entry.get("payload") or {})))
        self.set_kv(self._STICKY_KEY, state)
        live_rows.sort(key=lambda item: item[0], reverse=True)
        return [row for _, row in live_rows]

    def sticky_selected_dismiss(self, symbol: str, mode: str) -> None:
        key = f"{str(symbol or '').upper().strip()}|{self._mode(mode, allow_all=False)}"
        state = self.get_kv(self._STICKY_KEY, {}) or {}
        if key in state:
            state.pop(key, None)
            self.set_kv(self._STICKY_KEY, state)
