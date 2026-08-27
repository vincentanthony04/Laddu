"""One canonical Model Paper settlement lineage.

The model-paper position ledger owns simulated execution economics.  This
service projects a completed position back to the canonical DecisionRecord only
after the position transaction has committed.  Signal accuracy and rupee
performance therefore share the same decision/position/settlement identity
without allowing a points-only signal lifecycle to masquerade as portfolio P&L.
"""
from __future__ import annotations

from typing import Any, Mapping

from core.outcome_accuracy_taxonomy import DEFAULT_OUTCOME_ACCURACY_TAXONOMY


class ModelPaperSettlementLineageService:
    VERSION = "model-paper-settlement-lineage-1.1.0"

    def __init__(self, canonical_repository: Any, event_fn: Any | None = None):
        self.canonical = canonical_repository
        self.event_fn = event_fn

    @staticmethod
    def _accuracy_result(signal_outcome: Any, economic_outcome: Any) -> str:
        # Compatibility result is derived from signal quality only. Economic
        # P&L is deliberately independent: a profitable managed/time exit is
        # still NEUTRAL for signal accuracy and may never be backfilled to WIN.
        return DEFAULT_OUTCOME_ACCURACY_TAXONOMY.compatibility_result(signal_outcome)

    def record(self, settlement: Mapping[str, Any]) -> dict[str, Any]:
        row = dict(settlement or {})
        decision_id = str(row.get("source_signal_id") or row.get("decision_id") or "").strip()
        if not decision_id:
            return {"ok": False, "state": "NO_CANONICAL_DECISION_ID"}
        total_cost = row.get("total_cost")
        payload = {
            "status": "CLOSED",
            "result": self._accuracy_result(row.get("signal_outcome"), row.get("economic_outcome")),
            "signal_outcome": row.get("signal_outcome"),
            "economic_outcome": row.get("economic_outcome"),
            "exit_reason": row.get("exit_reason"),
            "entry": row.get("entry_price"),
            "target": row.get("original_target"),
            "stop": row.get("original_stop"),
            "managed_stop": row.get("managed_stop"),
            "quantity": row.get("quantity"),
            "exit": row.get("exit_price"),
            "exit_price": row.get("exit_price"),
            "gross_pnl": row.get("gross_pnl"),
            "costs": {"total": total_cost, "version": row.get("cost_version")},
            "charges": total_cost,
            "net_pnl": row.get("net_pnl"),
            "settlement_id": row.get("position_id"),
            "position_id": row.get("position_id"),
            "publication_authority": "MODEL_PAPER",
            "settlement_authority": "POSTGRESQL_MODEL_PAPER_POSITIONS",
            "settlement_lineage_version": self.VERSION,
            "outcome_taxonomy_authority": DEFAULT_OUTCOME_ACCURACY_TAXONOMY.authority,
            "outcome_taxonomy_version": DEFAULT_OUTCOME_ACCURACY_TAXONOMY.authority_version,
            "closed_at": row.get("closed_at") or row.get("updated_at"),
        }
        updated = self.canonical.record_outcome(decision_id, payload)
        if self.event_fn is not None:
            try:
                self.event_fn("INFO", "settlement_lineage", "Model Paper settlement projected to canonical decision", {
                    "decision_id": updated.get("decision_id") or decision_id,
                    "position_id": row.get("position_id"),
                    "symbol": row.get("symbol"),
                    "result": payload["result"],
                    "net_pnl": payload.get("net_pnl"),
                })
            except Exception:
                pass
        return {"ok": bool(updated), "state": "CANONICAL_SETTLEMENT_RECORDED" if updated else "CANONICAL_DECISION_NOT_FOUND", "decision": updated}
