from __future__ import annotations

from core.production_mode_policy import require_production_mode

"""Operational opportunity-memory authority for production (PostgreSQL-backed).

storage.py's Store.upsert_opportunity_memory/opportunity_candidates/
opportunity_summary previously delegated unconditionally to
core.opportunity_memory_repository.OpportunityMemoryRepository, built
directly on a raw sqlite3 connection with no production delegate. This
repository closes that gap against trading.opportunity_memory (see
infra/postgres/operational/005_opportunity_memory_authority.sql), keeping
the exact external contract so callers do not change.
"""

import json
from typing import Any, Callable, Dict, List

from core.production_mode_policy import require_production_mode

from .postgres import PostgresAuthority

_STAGE_ORDER_SQL = "CASE UPPER(stage) WHEN 'ARMED' THEN 0 WHEN 'QUALIFIED' THEN 1 WHEN 'POTENTIAL' THEN 2 ELSE 3 END"


class ProductionOpportunityMemoryRepository:
    """Operational PostgreSQL persistence for auto-discovered candidate memory."""

    production_authority = True

    def __init__(self, operational: PostgresAuthority, desk_modes: Callable[[str], tuple]):
        self.operational = operational
        self._desk_modes = desk_modes

    def upsert(self, d: Dict[str, Any], source: str = "auto_discovery") -> None:
        sym = str(d.get("symbol") or "").upper().strip()
        mode = require_production_mode(d.get("mode") or "delivery")
        if not sym:
            return
        stage = str(d.get("opportunity_stage") or d.get("candidate_stage") or d.get("stage") or "Potential").title()
        if stage.upper() == "WATCH":
            stage = "Potential"
        score = int(d.get("priority_score") or d.get("score") or 0)
        reason = d.get("priority_reason") or d.get("opportunity_bucket") or d.get("reason") or "Potential candidate under priority watch"
        themes = d.get("themes") or []
        self.operational.execute(
            """
            INSERT INTO trading.opportunity_memory
                (symbol, exchange, mode, stage, priority_score, sector, themes_json,
                 priority_reason, trigger, invalidation, target_window, next_scan_at,
                 last_seen_at, payload_json, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now(), %s, now())
            ON CONFLICT (symbol, mode) DO UPDATE SET
                exchange = excluded.exchange, stage = excluded.stage,
                priority_score = GREATEST(trading.opportunity_memory.priority_score, excluded.priority_score),
                sector = excluded.sector, themes_json = excluded.themes_json,
                priority_reason = excluded.priority_reason, trigger = excluded.trigger,
                invalidation = excluded.invalidation, target_window = excluded.target_window,
                next_scan_at = excluded.next_scan_at, last_seen_at = now(),
                payload_json = excluded.payload_json, updated_at = now()
            """,
            (
                sym, d.get("exchange") or "NSE", mode, stage, score, d.get("sector"),
                json.dumps(themes, sort_keys=True, default=str), reason, d.get("trigger"), d.get("invalidation"),
                d.get("target_window"), d.get("next_scan_at"), json.dumps(d, sort_keys=True, default=str),
            ),
        )

    def candidates(self, mode: str = "all", limit: int = 60) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM trading.opportunity_memory"
        params: List[Any] = []
        if mode != "all":
            modes = self._desk_modes(mode)
            marks = ",".join("%s" for _ in modes)
            sql += f" WHERE mode IN ({marks})"
            params.extend(modes)
        sql += f" ORDER BY {_STAGE_ORDER_SQL}, priority_score DESC, updated_at DESC LIMIT %s"
        params.append(limit)
        fetched = self.operational.execute(sql, tuple(params), fetch="all") or []
        out: List[Dict[str, Any]] = []
        for r in fetched:
            payload = r.get("payload_json") or {}
            d = payload if isinstance(payload, dict) else json.loads(payload or "{}")
            themes_raw = r.get("themes_json")
            if isinstance(themes_raw, list):
                themes = themes_raw
            else:
                try:
                    themes = json.loads(themes_raw or "[]")
                except Exception:
                    themes = []
            try:
                canonical_mode = require_production_mode(r.get("mode"))
            except ValueError:
                continue
            d.update({
                "symbol": r.get("symbol"), "exchange": r.get("exchange"), "mode": canonical_mode,
                "opportunity_stage": r.get("stage"), "candidate_stage": r.get("stage"),
                "priority_score": r.get("priority_score"), "sector": r.get("sector"), "themes": themes,
                "priority_reason": r.get("priority_reason"), "trigger": r.get("trigger"),
                "invalidation": r.get("invalidation"), "target_window": r.get("target_window"),
                "last_seen_at": r.get("last_seen_at"), "next_scan_at": r.get("next_scan_at"),
                "watch_type": "potential_memory", "status": "WATCH", "decision": "WATCH",
            })
            out.append(d)
        return out

    def remove(self, symbol: str, mode: str) -> int:
        sym = str(symbol or "").upper().strip()
        canonical_mode = require_production_mode(mode)
        if not sym:
            return 0
        return int(self.operational.execute(
            "DELETE FROM trading.opportunity_memory WHERE symbol=%s AND mode=%s",
            (sym, canonical_mode),
        ) or 0)

    def summary(self) -> Dict[str, Any]:
        rows = self.candidates("all", 300)
        by_stage: Dict[str, int] = {}
        by_sector: Dict[str, int] = {}
        for d in rows:
            st = str(d.get("opportunity_stage") or "Potential")
            by_stage[st] = by_stage.get(st, 0) + 1
            sec = str(d.get("sector") or "broad")
            by_sector[sec] = by_sector.get(sec, 0) + 1
        return {
            "count": len(rows),
            "by_stage": by_stage,
            "by_sector": dict(sorted(by_sector.items(), key=lambda kv: (-kv[1], kv[0]))[:12]),
        }
