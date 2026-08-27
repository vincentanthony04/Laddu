"""Persistence-time duplicate and unsupported-mode filter.

This module is deliberately not a decision or capital authority.  Final policy
is applied inside DecisionEngineService/ProductionRiskAuthorityService before a
row reaches persistence.
"""
from core.production_mode_policy import is_production_mode


class DecisionWriteDedupFilter:
    @staticmethod
    def reject_unsupported_mode(decision) -> bool:
        return not is_production_mode((decision or {}).get("mode"))

    @staticmethod
    def suppress_duplicate_stale(conn, decision) -> bool:
        reason = str(decision.get("reason") or "").lower()
        if "stale-data guard" not in reason or str(decision.get("status") or "").upper() != "BLOCKED":
            return False
        return bool(conn.execute(
            """SELECT 1 FROM decisions WHERE symbol=? AND mode=? AND status='BLOCKED'
               AND created_at>=datetime('now','-90 seconds') ORDER BY id DESC LIMIT 1""",
            (decision.get("symbol"), decision.get("mode")),
        ).fetchone())
