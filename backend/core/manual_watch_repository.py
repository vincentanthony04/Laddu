"""Bounded persistence context for user-managed watch rows.

v60.14 P0 fix: upsert/remove/clear/pin execute+commit directly on the shared
connection without going through Store.write_lock, unlike
MarketDataRepository/SignalLedgerRepository/SystemHealthRepository after
their v60.13/v60.14 fixes -- same "database is locked" contention risk.
Same fallback-lock pattern applied here: Store always passes its shared
write_lock; a private Lock is used only if this repo is ever constructed
standalone (e.g. in a test)."""
from __future__ import annotations

from core.production_mode_policy import require_production_mode

import json
import threading
from typing import Any, Dict, List

from core.production_mode_policy import require_production_mode


class ManualWatchRepository:
    def __init__(self, connection, write_lock=None):
        self.conn = connection
        self.write_lock = write_lock or threading.Lock()

    def upsert(self, decision: Dict[str, Any], source: str = "manual_search") -> None:
        sym = str(decision.get("symbol") or "").upper().strip()
        mode = require_production_mode(decision.get("mode") or "delivery")
        if not sym:
            return
        side = str(decision.get("side") or "WAIT").upper()
        if side == "LONG":
            waiting_for, trigger, invalid = "breakout / reclaim / retest confirmation", str(decision.get("resistance") or decision.get("entry") or "above setup level"), str(decision.get("support") or decision.get("sl") or "below support")
        elif side == "SHORT":
            waiting_for, trigger, invalid = "breakdown / retest confirmation", str(decision.get("support") or decision.get("entry") or "below setup level"), str(decision.get("resistance") or decision.get("sl") or "above resistance")
        else:
            waiting_for, trigger, invalid = "clear mode trigger", str(decision.get("entry") or "pending"), str(decision.get("sl") or "pending")
        with self.write_lock:
            self.conn.execute("""INSERT INTO manual_watch(symbol,exchange,mode,side,state,waiting_for,trigger,invalidation,reason,pinned,source,payload_json,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,COALESCE((SELECT pinned FROM manual_watch WHERE symbol=? AND mode=?),0),?,?,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)
            ON CONFLICT(symbol,mode) DO UPDATE SET exchange=excluded.exchange, side=excluded.side, state=excluded.state, waiting_for=excluded.waiting_for, trigger=excluded.trigger, invalidation=excluded.invalidation, reason=excluded.reason, source=excluded.source, payload_json=excluded.payload_json, updated_at=CURRENT_TIMESTAMP""",
            (sym, decision.get("exchange") or "NSE", mode, side, str(decision.get("status") or decision.get("decision") or "WATCH"), waiting_for, trigger, invalid, decision.get("reason") or "manual watch", sym, mode, source, json.dumps(decision)))
            self.conn.commit()

    def remove(self, symbol: str, mode: str = "all") -> int:
        sym, mode = str(symbol or "").upper().strip(), str(mode or "all").lower().strip()
        if mode != "all":
            mode = require_production_mode(mode)
        with self.write_lock:
            cur = self.conn.execute("DELETE FROM manual_watch WHERE symbol=?" if mode == "all" else "DELETE FROM manual_watch WHERE symbol=? AND mode=?", (sym,) if mode == "all" else (sym, mode))
            self.conn.commit()
            return cur.rowcount

    def remove_generated(self, symbol: str, mode: str) -> int:
        sym = str(symbol or "").upper().strip()
        canonical_mode = require_production_mode(mode)
        if not sym:
            return 0
        with self.write_lock:
            cur = self.conn.execute(
                "DELETE FROM manual_watch WHERE symbol=? AND mode=? AND COALESCE(pinned,0)=0 AND source NOT LIKE 'manual%'",
                (sym, canonical_mode),
            )
            self.conn.commit()
            return int(cur.rowcount or 0)

    def clear(self, keep_pinned: bool = True) -> int:
        with self.write_lock:
            cur = self.conn.execute("DELETE FROM manual_watch WHERE COALESCE(pinned,0)=0" if keep_pinned else "DELETE FROM manual_watch")
            self.conn.commit()
            return cur.rowcount

    def pin(self, symbol: str, mode: str, pinned: bool = True) -> int:
        mode = require_production_mode(mode)
        with self.write_lock:
            cur = self.conn.execute("UPDATE manual_watch SET pinned=?, updated_at=CURRENT_TIMESTAMP WHERE symbol=? AND mode=?", (1 if pinned else 0, str(symbol or "").upper().strip(), mode))
            self.conn.commit()
            return cur.rowcount

    def rows(self, mode: str = "all", limit: int = 60) -> List[Dict[str, Any]]:
        sql, params = "SELECT * FROM manual_watch", []
        if mode != "all":
            sql += " WHERE mode=?"
            params.append(mode)
        sql += " ORDER BY pinned DESC, updated_at DESC LIMIT ?"
        params.append(limit)
        out = []
        for row in self.conn.execute(sql, tuple(params)).fetchall():
            decision = json.loads(row["payload_json"] or "{}")
            try:
                canonical_mode = require_production_mode(row["mode"])
            except ValueError:
                continue
            decision.update({"symbol": row["symbol"], "exchange": row["exchange"], "mode": canonical_mode, "side": row["side"], "decision": "WATCH", "status": row["state"], "waiting_for": row["waiting_for"], "trigger": row["trigger"], "invalidation": row["invalidation"], "reason": row["reason"], "pinned": bool(row["pinned"]), "watch_type": ("manual" if str(row["source"] or "").startswith("manual") else str(row["source"] or "auto_discovery")), "updated_at": row["updated_at"]})
            out.append(decision)
        return out
