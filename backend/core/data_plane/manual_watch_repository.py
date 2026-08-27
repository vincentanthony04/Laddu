from __future__ import annotations

from core.production_mode_policy import require_production_mode

"""Operational manual-watchlist authority for production (PostgreSQL-backed).

storage.py's Store.upsert_manual_watch/remove_manual_watch/clear_manual_watch/
pin_manual_watch/manual_watch_rows previously delegated unconditionally to
core.manual_watch_repository.ManualWatchRepository, which is built directly
on a raw sqlite3 connection with no production delegate. This repository
closes that gap against trading.manual_watch (see
infra/postgres/operational/004_manual_watch_authority.sql), keeping the exact
external contract (method names, return shapes, row normalisation) so
callers -- the radar, opportunity views, and API routes -- do not change.
"""

import json
from typing import Any, Dict, List

from core.production_mode_policy import require_production_mode

from .postgres import PostgresAuthority


class ProductionManualWatchRepository:
    """Operational PostgreSQL persistence for user-managed watch rows."""

    production_authority = True

    def __init__(self, operational: PostgresAuthority):
        self.operational = operational

    def upsert(self, decision: Dict[str, Any], source: str = "manual_search") -> None:
        sym = str(decision.get("symbol") or "").upper().strip()
        mode = require_production_mode(decision.get("mode") or "delivery")
        if not sym:
            return
        side = str(decision.get("side") or "WAIT").upper()
        if side == "LONG":
            waiting_for = "breakout / reclaim / retest confirmation"
            trigger = str(decision.get("resistance") or decision.get("entry") or "above setup level")
            invalid = str(decision.get("support") or decision.get("sl") or "below support")
        elif side == "SHORT":
            waiting_for = "breakdown / retest confirmation"
            trigger = str(decision.get("support") or decision.get("entry") or "below setup level")
            invalid = str(decision.get("resistance") or decision.get("sl") or "above resistance")
        else:
            waiting_for, trigger, invalid = "clear mode trigger", str(decision.get("entry") or "pending"), str(decision.get("sl") or "pending")
        self.operational.execute(
            """
            INSERT INTO trading.manual_watch
                (symbol, exchange, mode, side, state, waiting_for, trigger, invalidation,
                 reason, pinned, source, payload_json, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s,
                    COALESCE((SELECT pinned FROM trading.manual_watch WHERE symbol=%s AND mode=%s), false),
                    %s, %s, now(), now())
            ON CONFLICT (symbol, mode) DO UPDATE SET
                exchange = excluded.exchange, side = excluded.side, state = excluded.state,
                waiting_for = excluded.waiting_for, trigger = excluded.trigger,
                invalidation = excluded.invalidation, reason = excluded.reason,
                source = excluded.source, payload_json = excluded.payload_json, updated_at = now()
            """,
            (
                sym, decision.get("exchange") or "NSE", mode, side,
                str(decision.get("status") or decision.get("decision") or "WATCH"),
                waiting_for, trigger, invalid, decision.get("reason") or "manual watch",
                sym, mode, source, json.dumps(decision, sort_keys=True, default=str),
            ),
        )

    def remove(self, symbol: str, mode: str = "all") -> int:
        sym, mode = str(symbol or "").upper().strip(), str(mode or "all").lower().strip()
        if mode != "all":
            mode = require_production_mode(mode)
        if mode == "all":
            return self.operational.execute(
                "DELETE FROM trading.manual_watch WHERE symbol=%s", (sym,),
            )
        return self.operational.execute(
            "DELETE FROM trading.manual_watch WHERE symbol=%s AND mode=%s", (sym, mode),
        )

    def remove_generated(self, symbol: str, mode: str) -> int:
        sym = str(symbol or "").upper().strip()
        canonical_mode = require_production_mode(mode)
        if not sym:
            return 0
        return int(self.operational.execute(
            "DELETE FROM trading.manual_watch WHERE symbol=%s AND mode=%s AND COALESCE(pinned,false)=false AND source NOT ILIKE 'manual%%'",
            (sym, canonical_mode),
        ) or 0)

    def clear(self, keep_pinned: bool = True) -> int:
        if keep_pinned:
            return self.operational.execute(
                "DELETE FROM trading.manual_watch WHERE COALESCE(pinned, false) = false"
            )
        return self.operational.execute("DELETE FROM trading.manual_watch")

    def pin(self, symbol: str, mode: str, pinned: bool = True) -> int:
        mode = require_production_mode(mode)
        return self.operational.execute(
            "UPDATE trading.manual_watch SET pinned=%s, updated_at=now() WHERE symbol=%s AND mode=%s",
            (bool(pinned), str(symbol or "").upper().strip(), mode),
        )

    def rows(self, mode: str = "all", limit: int = 60) -> List[Dict[str, Any]]:
        sql, params = "SELECT * FROM trading.manual_watch", []
        if mode != "all":
            sql += " WHERE mode=%s"
            params.append(mode)
        sql += " ORDER BY pinned DESC, updated_at DESC LIMIT %s"
        params.append(limit)
        fetched = self.operational.execute(sql, tuple(params), fetch="all") or []
        out: List[Dict[str, Any]] = []
        for row in fetched:
            payload = row.get("payload_json") or {}
            decision = payload if isinstance(payload, dict) else json.loads(payload or "{}")
            try:
                canonical_mode = require_production_mode(row.get("mode"))
            except ValueError:
                continue
            decision.update({
                "symbol": row.get("symbol"), "exchange": row.get("exchange"), "mode": canonical_mode,
                "side": row.get("side"), "decision": "WATCH", "status": row.get("state"),
                "waiting_for": row.get("waiting_for"), "trigger": row.get("trigger"),
                "invalidation": row.get("invalidation"), "reason": row.get("reason"),
                "pinned": bool(row.get("pinned")),
                "watch_type": ("manual" if str(row.get("source") or "").startswith("manual") else str(row.get("source") or "auto_discovery")),
                "updated_at": row.get("updated_at"),
            })
            out.append(decision)
        return out
