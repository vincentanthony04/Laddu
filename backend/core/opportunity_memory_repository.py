"""Opportunity memory: auto-discovered candidates staged Potential/Qualified/
Armed. Extracted verbatim from storage.py's Store class (v51 storage split,
cluster 8).

v60.14 P0 fix: upsert() wrote without Store.write_lock; same fallback-lock
pattern applied here as the other repos fixed for "database is locked"."""
from __future__ import annotations

from core.production_mode_policy import require_production_mode

import json
import threading
from typing import Any, Callable, Dict, List

from core.production_mode_policy import require_production_mode


class OpportunityMemoryRepository:
    def __init__(self, connection, desk_modes: Callable[[str], tuple], write_lock=None):
        self.conn = connection
        self._desk_modes = desk_modes
        self.write_lock = write_lock or threading.Lock()

    def upsert(self, d: Dict[str, Any], source: str = "auto_discovery") -> None:
        sym = str(d.get("symbol") or "").upper().strip(); mode = require_production_mode(d.get("mode") or "delivery")
        if not sym:
            return
        stage = str(d.get("opportunity_stage") or d.get("candidate_stage") or d.get("stage") or "Potential").title()
        if stage.upper() == "WATCH":
            stage = "Potential"
        score = int(d.get("priority_score") or d.get("score") or 0)
        reason = d.get("priority_reason") or d.get("opportunity_bucket") or d.get("reason") or "Potential candidate under priority watch"
        themes = d.get("themes") or []
        with self.write_lock:
            self.conn.execute("""INSERT INTO opportunity_memory(symbol,exchange,mode,stage,priority_score,sector,themes_json,priority_reason,trigger,invalidation,target_window,next_scan_at,last_seen_at,payload_json,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP,?,CURRENT_TIMESTAMP)
            ON CONFLICT(symbol,mode) DO UPDATE SET exchange=excluded.exchange, stage=excluded.stage, priority_score=MAX(opportunity_memory.priority_score, excluded.priority_score), sector=excluded.sector, themes_json=excluded.themes_json, priority_reason=excluded.priority_reason, trigger=excluded.trigger, invalidation=excluded.invalidation, target_window=excluded.target_window, next_scan_at=excluded.next_scan_at, last_seen_at=CURRENT_TIMESTAMP, payload_json=excluded.payload_json, updated_at=CURRENT_TIMESTAMP""",
            (sym, d.get("exchange") or "NSE", mode, stage, score, d.get("sector"), json.dumps(themes), reason, d.get("trigger"), d.get("invalidation"), d.get("target_window"), d.get("next_scan_at"), json.dumps(d)))
            self.conn.commit()

    def candidates(self, mode: str = "all", limit: int = 60) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM opportunity_memory"; params: List[Any] = []
        if mode != "all":
            modes=self._desk_modes(mode); marks=",".join("?" for _ in modes); sql += f" WHERE mode IN ({marks})"; params.extend(modes)
        sql += " ORDER BY CASE UPPER(stage) WHEN 'ARMED' THEN 0 WHEN 'QUALIFIED' THEN 1 WHEN 'POTENTIAL' THEN 2 ELSE 3 END, priority_score DESC, updated_at DESC LIMIT ?"; params.append(limit)
        rows = self.conn.execute(sql, tuple(params)).fetchall()
        out=[]
        for r in rows:
            d = json.loads(r["payload_json"] or "{}")
            try: themes=json.loads(r["themes_json"] or "[]")
            except Exception: themes=[]
            try:
                canonical_mode = require_production_mode(r["mode"])
            except ValueError:
                continue
            d.update({"symbol":r["symbol"],"exchange":r["exchange"],"mode":canonical_mode,"opportunity_stage":r["stage"],"candidate_stage":r["stage"],"priority_score":r["priority_score"],"sector":r["sector"],"themes":themes,"priority_reason":r["priority_reason"],"trigger":r["trigger"],"invalidation":r["invalidation"],"target_window":r["target_window"],"last_seen_at":r["last_seen_at"],"next_scan_at":r["next_scan_at"],"watch_type":"potential_memory","status":"WATCH","decision":"WATCH"})
            out.append(d)
        return out

    def remove(self, symbol: str, mode: str) -> int:
        sym = str(symbol or "").upper().strip()
        canonical_mode = require_production_mode(mode)
        if not sym:
            return 0
        with self.write_lock:
            cur = self.conn.execute("DELETE FROM opportunity_memory WHERE symbol=? AND mode=?", (sym, canonical_mode))
            self.conn.commit()
            return int(cur.rowcount or 0)

    def summary(self) -> Dict[str, Any]:
        rows = self.candidates("all", 300)
        by_stage: Dict[str, int] = {}; by_sector: Dict[str, int] = {}
        for d in rows:
            st=str(d.get("opportunity_stage") or "Potential"); by_stage[st]=by_stage.get(st,0)+1
            sec=str(d.get("sector") or "broad"); by_sector[sec]=by_sector.get(sec,0)+1
        return {"count":len(rows), "by_stage":by_stage, "by_sector":dict(sorted(by_sector.items(), key=lambda kv:(-kv[1],kv[0]))[:12])}
