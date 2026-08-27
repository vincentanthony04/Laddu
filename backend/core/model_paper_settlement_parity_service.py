"""Parity gate between the execution ledger and canonical settlement projection.

The Model Paper position ledger owns economic settlement. The canonical
DecisionRecord mirrors that settlement after commit. This service proves that
the mirror has not drifted; it never settles positions and never infers values.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, Mapping

from core.outcome_accuracy_taxonomy import DEFAULT_OUTCOME_ACCURACY_TAXONOMY


class ModelPaperSettlementParityService:
    NAME = "ModelPaperSettlementParityAuthority"
    VERSION = "1.1.0"
    NUMERIC_FIELDS = (
        ("quantity", "quantity"),
        ("gross_pnl", "gross_pnl"),
        ("total_cost", "costs"),
        ("net_pnl", "net_pnl"),
        ("exit_price", "exit"),
    )
    SEMANTIC_FIELDS = ("exit_reason", "signal_outcome", "economic_outcome", "result")

    def __init__(self, model_portfolio_repository: Any, canonical_repository: Any):
        self.positions = model_portfolio_repository
        self.canonical = canonical_repository

    @staticmethod
    def _number(value: Any) -> float | None:
        if isinstance(value, Mapping):
            value = value.get("total")
        try:
            number = float(value)
            return number if number == number and abs(number) != float("inf") else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _close(a: Any, b: Any, *, tolerance: float = 0.005) -> bool:
        left = ModelPaperSettlementParityService._number(a)
        right = ModelPaperSettlementParityService._number(b)
        if left is None or right is None:
            return left is None and right is None
        return abs(left - right) <= tolerance

    @staticmethod
    def _canonical_map(rows: Iterable[Mapping[str, Any]]) -> Dict[str, Dict[str, Any]]:
        out: Dict[str, Dict[str, Any]] = {}
        for raw in rows:
            row = dict(raw or {})
            for key in (row.get("decision_id"), row.get("signal_id")):
                if key:
                    out[str(key)] = row
        return out

    def report(self, *, limit: int = 5000) -> Dict[str, Any]:
        if self.positions is None or self.canonical is None:
            return {
                "ok": False,
                "state": "AUTHORITY_UNAVAILABLE",
                "authority": self.NAME,
                "authority_version": self.VERSION,
                "checked": 0,
                "mismatches": [],
            }
        closed = [dict(row or {}) for row in self.positions.list_positions("CLOSED")]
        canonical_rows = self.canonical.latest_decisions("all", limit=max(1, min(int(limit), 5000)))
        by_id = self._canonical_map(canonical_rows)
        mismatches: list[Dict[str, Any]] = []
        checked = 0
        for position in closed:
            source_id = str(position.get("source_signal_id") or "").strip()
            decision = by_id.get(source_id)
            if decision is None:
                mismatches.append({
                    "position_id": position.get("position_id"),
                    "source_signal_id": source_id or None,
                    "field": "settlement_lineage",
                    "state": "CANONICAL_PROJECTION_MISSING",
                })
                continue
            checked += 1
            if str(decision.get("settlement_id") or decision.get("position_id") or "") != str(position.get("position_id") or ""):
                mismatches.append({
                    "position_id": position.get("position_id"),
                    "source_signal_id": source_id,
                    "field": "settlement_id",
                    "position_value": position.get("position_id"),
                    "canonical_value": decision.get("settlement_id") or decision.get("position_id"),
                })
            for position_field, decision_field in self.NUMERIC_FIELDS:
                left = position.get(position_field)
                right = decision.get(decision_field)
                if not self._close(left, right):
                    mismatches.append({
                        "position_id": position.get("position_id"),
                        "source_signal_id": source_id,
                        "field": position_field,
                        "position_value": left,
                        "canonical_value": right,
                    })
            taxonomy = DEFAULT_OUTCOME_ACCURACY_TAXONOMY
            semantic_expected = {
                "exit_reason": str(position.get("exit_reason") or "").upper().strip() or None,
                "signal_outcome": taxonomy.normalize_signal(position.get("signal_outcome")),
                "economic_outcome": taxonomy.normalize_economic(position.get("economic_outcome")),
                "result": taxonomy.compatibility_result(position.get("signal_outcome")),
            }
            semantic_actual = {
                "exit_reason": str(decision.get("exit_reason") or "").upper().strip() or None,
                "signal_outcome": taxonomy.normalize_signal(decision.get("signal_outcome")),
                "economic_outcome": taxonomy.normalize_economic(decision.get("economic_outcome")),
                "result": str(decision.get("result") or "").upper().strip() or None,
            }
            for field in self.SEMANTIC_FIELDS:
                if semantic_expected[field] != semantic_actual[field]:
                    mismatches.append({
                        "position_id": position.get("position_id"),
                        "source_signal_id": source_id,
                        "field": field,
                        "position_value": semantic_expected[field],
                        "canonical_value": semantic_actual[field],
                    })
            position_cost_version = str(position.get("cost_version") or "").strip()
            decision_costs = decision.get("costs") if isinstance(decision.get("costs"), Mapping) else {}
            canonical_cost_version = str((decision_costs or {}).get("version") or "").strip()
            if position_cost_version and canonical_cost_version != position_cost_version:
                mismatches.append({
                    "position_id": position.get("position_id"),
                    "source_signal_id": source_id,
                    "field": "cost_version",
                    "position_value": position_cost_version,
                    "canonical_value": canonical_cost_version or None,
                })
        return {
            "ok": not mismatches,
            "state": "PARITY" if not mismatches else "DRIFT",
            "authority": self.NAME,
            "authority_version": self.VERSION,
            "execution_authority": "POSTGRESQL_MODEL_PAPER_POSITIONS",
            "projection_authority": "POSTGRESQL_CANONICAL_DECISIONS",
            "closed_positions": len(closed),
            "checked": checked,
            "mismatch_count": len(mismatches),
            "mismatches": mismatches[:100],
            "outcome_taxonomy_authority": DEFAULT_OUTCOME_ACCURACY_TAXONOMY.authority,
            "outcome_taxonomy_version": DEFAULT_OUTCOME_ACCURACY_TAXONOMY.authority_version,
            "policy": "Model Paper settlement owns truth; canonical outcomes must mirror settlement identity, signal/economic semantics, quantity, costs and rupee economics exactly.",
        }
