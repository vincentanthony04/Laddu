"""Deterministic Model Paper -> canonical DecisionRecord reconciliation.

There is one execution/settlement authority: ``trading.model_paper_positions``.
The position lifecycle writes the economic settlement first.  The canonical
DecisionRecord is a post-commit projection used by Signal Accuracy, Performance
and Research lineage.  This worker repairs a missed projection from durable
PostgreSQL state; it never infers an outcome from quotes or candles and never
closes a position itself.
"""
from __future__ import annotations

import time
from typing import Any, Dict


class ModelPaperSettlementReconciliationService:
    VERSION = "model-paper-settlement-reconciliation-1.0.0"

    def __init__(self, repository: Any, settlement_sink: Any, event_fn: Any | None = None):
        self.repository = repository
        self.settlement_sink = settlement_sink
        self.event_fn = event_fn
        self.last_result: Dict[str, Any] = {
            "state": "STARTING", "reconciled": 0, "remaining": None, "version": self.VERSION,
        }

    def run_once(self, *, limit: int = 100) -> Dict[str, Any]:
        if self.repository is None or self.settlement_sink is None:
            self.last_result = {
                "state": "NOT_REQUIRED", "candidates": 0, "reconciled": 0, "failures": [],
                "version": self.VERSION, "time": time.time(),
                "authority": "TEST_OR_NONPRODUCTION",
            }
            return dict(self.last_result)
        rows = self.repository.settlement_lineage_candidates(limit=max(1, min(int(limit), 500)))
        repaired = 0
        failures = []
        for row in rows:
            try:
                result = self.settlement_sink.record(row)
                if result.get("ok"):
                    repaired += 1
                else:
                    failures.append({
                        "position_id": row.get("position_id"),
                        "decision_id": row.get("source_signal_id"),
                        "state": result.get("state"),
                    })
            except Exception as exc:
                failures.append({
                    "position_id": row.get("position_id"),
                    "decision_id": row.get("source_signal_id"),
                    "error": str(exc)[:240],
                })
        state = "RECONCILED" if not rows else ("REPAIRED" if repaired else "BLOCKED")
        self.last_result = {
            "state": state,
            "candidates": len(rows),
            "reconciled": repaired,
            "failures": failures[:20],
            "version": self.VERSION,
            "time": time.time(),
            "authority": "POSTGRESQL_MODEL_PAPER_POSITIONS",
            "projection": "POSTGRESQL_CANONICAL_DECISIONS",
            "policy": "durable settlement replay only; no quote/candle inferred closure",
        }
        if failures and self.event_fn is not None:
            try:
                self.event_fn("WARN", "settlement_reconciliation", "Model Paper settlement projection requires attention", {
                    "failures": failures[:10], "candidate_count": len(rows),
                })
            except Exception:
                pass
        return dict(self.last_result)

    def status(self) -> Dict[str, Any]:
        return dict(self.last_result)

    def loop(self, sup=None, *, running_fn):
        while running_fn() and (sup is None or sup.running):
            if sup:
                sup.beat("settlement_reconciliation")
            try:
                result = self.run_once(limit=100)
                work = int(result.get("candidates") or 0)
                repaired = int(result.get("reconciled") or 0)
                if sup:
                    sup.progress(
                        "settlement_reconciliation",
                        token=f"{result.get('state')}:{work}:{repaired}",
                        stage="settlement_lineage_reconciliation",
                        completed_units=repaired,
                        total_units=work if work else 0,
                        expected_idle=(work == 0),
                        waiting_on="no missing settlement projections" if work == 0 else None,
                    )
                time.sleep(2.0 if work else 15.0)
            except Exception as exc:
                if self.event_fn is not None:
                    try:
                        self.event_fn("ERROR", "settlement_reconciliation", "Settlement reconciliation cycle failed", {"error": str(exc)[:240]})
                    except Exception:
                        pass
                time.sleep(3.0)
