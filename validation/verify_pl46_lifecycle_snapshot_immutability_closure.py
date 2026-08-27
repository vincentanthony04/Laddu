"""P0-04 regression: a published lifecycle snapshot must be causally frozen
at publish time -- later mutation of the same "results"/"agents" dict object
by the caller must never leak into an earlier snapshot.

Run: python validation/verify_pl46_lifecycle_snapshot_immutability_closure.py
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path
from typing import Any, Dict

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from core.operations_control_service import OperationsControlService  # noqa: E402


class FakeStore:
    def set_kv(self, key, payload):
        pass


class FakeApp:
    def __init__(self):
        self.store = FakeStore()


def _build_service():
    svc = OperationsControlService.__new__(OperationsControlService)
    svc.app = FakeApp()
    svc._lifecycle_lock = threading.RLock()
    svc._lifecycle_status = {}
    return svc


def test_earlier_snapshot_does_not_see_later_mutation():
    svc = _build_service()

    # Mimic _run_full_lifecycle: ONE dict object mutated in place across stages.
    results: Dict[str, Any] = {"scan_requests": {"delivery": "accepted"}}
    svc._publish_lifecycle(stage="research_reconciliation", completed=1, total=8, results=results)

    early_snapshot = svc.lifecycle_status()
    assert early_snapshot["stage"] == "research_reconciliation"
    assert early_snapshot["results"] == {"scan_requests": {"delivery": "accepted"}}

    # Later stage mutates the SAME results dict object further, as the real
    # lifecycle runner does.
    results["research_advance"] = {"state": "THREE_ARM_EVALUATED"}
    svc._publish_lifecycle(stage="settlement", completed=3, total=8, results=results)

    # The snapshot taken BEFORE this mutation must remain exactly as it was.
    assert early_snapshot["stage"] == "research_reconciliation", "stage field must not drift on a held snapshot"
    assert "research_advance" not in early_snapshot["results"], (
        f"earlier snapshot leaked later stage's nested results: {early_snapshot}"
    )

    later_snapshot = svc.lifecycle_status()
    assert later_snapshot["stage"] == "settlement"
    assert "research_advance" in later_snapshot["results"]


def test_reads_are_independent_copies_too():
    svc = _build_service()
    results: Dict[str, Any] = {"a": 1}
    svc._publish_lifecycle(stage="x", results=results)
    read_1 = svc.lifecycle_status()
    read_1["results"]["a"] = 999  # a caller mutating its own copy
    read_2 = svc.lifecycle_status()
    assert read_2["results"]["a"] == 1, "mutating a returned snapshot must not affect internal state"


if __name__ == "__main__":
    test_earlier_snapshot_does_not_see_later_mutation()
    test_reads_are_independent_copies_too()
    print("PASS: P0-04 lifecycle snapshot immutability regression")
