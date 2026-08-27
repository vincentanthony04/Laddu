"""Replay durable Signal Lifecycle evidence without owning trade execution.

Model Paper position/canonical-decision state remains the execution truth. Exact
REASSESSED/MANAGED intents are committed to the PostgreSQL transactional outbox
with the position mutation; GENERATED/OPENED/SETTLED can additionally be
reconstructed from durable canonical/position state. This worker only repairs
the append-only evidence projection and never infers prices, closes positions or
changes broker/risk authority.
"""
from __future__ import annotations

import time
from typing import Any, Dict


class SignalLifecycleReconciliationService:
    VERSION = "signal-lifecycle-reconciliation-1.0.0"

    def __init__(self, repository: Any, event_fn: Any | None = None):
        self.repository = repository
        self.event_fn = event_fn
        self.last_result: Dict[str, Any] = {
            "state": "STARTING", "candidates": 0, "replayed": 0,
            "failures": [], "version": self.VERSION,
        }

    def run_once(self, *, limit: int = 200) -> Dict[str, Any]:
        if self.repository is None:
            self.last_result = {
                "state": "NOT_REQUIRED", "candidates": 0, "replayed": 0,
                "failures": [], "version": self.VERSION,
                "authority": "TEST_OR_NONPRODUCTION", "time": time.time(),
            }
            return dict(self.last_result)
        rows = self.repository.lifecycle_replay_candidates(limit=max(1, min(int(limit), 500)))
        replayed = 0
        already_present = 0
        failures = []
        by_type: dict[str, int] = {}
        for row in rows:
            event_type = str(row.get("event_type") or "UNKNOWN").upper()
            by_type[event_type] = by_type.get(event_type, 0) + 1
            try:
                inserted = self.repository.append_signal_lifecycle_event(row)
                if inserted:
                    replayed += 1
                else:
                    already_present += 1
            except Exception as exc:
                failures.append({
                    "event_type": event_type,
                    "signal_id": row.get("signal_id"),
                    "position_id": row.get("position_id"),
                    "error": str(exc)[:240],
                })
        if failures:
            state = "BLOCKED"
        elif rows:
            state = "REPLAYED"
        else:
            state = "RECONCILED"
        self.last_result = {
            "state": state, "candidates": len(rows), "replayed": replayed,
            "already_present": already_present, "by_type": by_type,
            "failures": failures[:20], "version": self.VERSION, "time": time.time(),
            "authority": "POSTGRESQL_CANONICAL_DECISIONS_MODEL_PAPER_AND_TRANSACTIONAL_OUTBOX",
            "projection": "POSTGRESQL_APPEND_ONLY_SIGNAL_LIFECYCLE",
            "execution_authority": "NONE",
            "broker_authority": "NONE",
            "policy": "evidence replay only; never infer price/crossing/closure; exact reassessment-management intent comes from same position transaction",
        }
        if failures and self.event_fn is not None:
            try:
                self.event_fn("WARN", "signal_lifecycle_reconciliation", "Signal lifecycle replay requires attention", {
                    "candidate_count": len(rows), "failures": failures[:10],
                })
            except Exception:
                pass
        return dict(self.last_result)

    def status(self) -> Dict[str, Any]:
        return dict(self.last_result)

    def loop(self, sup=None, *, running_fn):
        while running_fn() and (sup is None or sup.running):
            if sup:
                sup.beat("signal_lifecycle_reconciliation")
            try:
                result = self.run_once(limit=200)
                work = int(result.get("candidates") or 0)
                replayed = int(result.get("replayed") or 0)
                if sup:
                    sup.progress(
                        "signal_lifecycle_reconciliation",
                        token=f"{result.get('state')}:{work}:{replayed}",
                        stage="signal_lifecycle_replay",
                        completed_units=replayed,
                        total_units=work if work else 0,
                        expected_idle=(work == 0),
                        waiting_on="no missing lifecycle evidence" if work == 0 else None,
                    )
                time.sleep(2.0 if work else 15.0)
            except Exception as exc:
                if self.event_fn is not None:
                    try:
                        self.event_fn("ERROR", "signal_lifecycle_reconciliation", "Signal lifecycle replay cycle failed", {"error": str(exc)[:240]})
                    except Exception:
                        pass
                time.sleep(3.0)
