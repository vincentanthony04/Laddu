"""P0-03 regression: the monitoring/recovery agent must not report HEALTHY
when Research is semantically blocked (FEATURES_INCOMPLETE /
THREE_ARM_CAPTURE_INCOMPLETE), even though no supervisor worker has crashed.

Run: python validation/verify_pl46_monitoring_semantic_blocker_closure.py
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from core.operations_control_service import OperationsControlService  # noqa: E402
import core.research_lifecycle_reconciliation_service as reconciliation_mod  # noqa: E402
import core.research_lifecycle_advance_service as advance_mod  # noqa: E402


class FakePriorityPipeline:
    def recover_stale(self, max_recoveries=3):
        return {"ok": True, "recovered": 0}


class FakeAutonomicController:
    def request_evaluation(self, allow_action=True, reason=""):
        return {"ok": True, "state": "OBSERVED"}


class FakeApp:
    def __init__(self):
        self.priority_pipeline = FakePriorityPipeline()
        self.autonomic_controller = FakeAutonomicController()
        self.store = object()


class FakeReconciliation:
    """No worker liveness problem, but Research is semantically blocked."""

    def __init__(self, store):
        pass

    def status(self):
        return {
            "by_desk": {
                "delivery": {"state": "THREE_ARM_INCOMPLETE"},
                "intraday": {"state": "SETTLEMENT_ACTIVE"},
            }
        }


class FakeAdvanceOk:
    def __init__(self, app):
        pass

    def run(self, **kwargs):
        return {"ok": True, "state": "REPAIRED"}


def _build_service():
    svc = OperationsControlService.__new__(OperationsControlService)
    svc.app = FakeApp()
    svc._supervisor_jobs = lambda: []  # no crashed workers at all
    return svc


def test_no_longer_reports_healthy_when_research_semantically_blocked():
    original_recon = reconciliation_mod.ResearchLifecycleReconciliationService
    original_advance = advance_mod.ResearchLifecycleAdvanceService
    reconciliation_mod.ResearchLifecycleReconciliationService = FakeReconciliation  # type: ignore[assignment]
    advance_mod.ResearchLifecycleAdvanceService = FakeAdvanceOk  # type: ignore[assignment]
    try:
        svc = _build_service()
        result = svc._monitoring_agent_pass(reason="test")
    finally:
        reconciliation_mod.ResearchLifecycleReconciliationService = original_recon  # type: ignore[assignment]
        advance_mod.ResearchLifecycleAdvanceService = original_advance  # type: ignore[assignment]

    assert result["state"] != "HEALTHY", f"must not be HEALTHY while Research is blocked: {result}"
    assert result["active_actionable_count"] == 0, "no supervisor worker was actually down"
    assert any(a.get("component") == "research_lifecycle" for a in result["attempts"]), (
        "bounded repair must be attempted for the recoverable THREE_ARM_INCOMPLETE desk"
    )


def test_still_reports_healthy_when_nothing_is_wrong():
    class FakeReconciliationClean:
        def __init__(self, store):
            pass

        def status(self):
            return {"by_desk": {
                "delivery": {"state": "SETTLEMENT_ACTIVE"},
                "intraday": {"state": "MONITORING"},
            }}

    original_recon = reconciliation_mod.ResearchLifecycleReconciliationService
    reconciliation_mod.ResearchLifecycleReconciliationService = FakeReconciliationClean  # type: ignore[assignment]
    try:
        svc = _build_service()
        result = svc._monitoring_agent_pass(reason="test")
    finally:
        reconciliation_mod.ResearchLifecycleReconciliationService = original_recon  # type: ignore[assignment]

    assert result["state"] == "HEALTHY", f"expected HEALTHY when nothing is wrong: {result}"


if __name__ == "__main__":
    test_no_longer_reports_healthy_when_research_semantically_blocked()
    test_still_reports_healthy_when_nothing_is_wrong()
    print("PASS: P0-03 monitoring semantic blocker regression")
