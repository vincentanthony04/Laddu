"""P0-02 regression: an existing immutable population with incomplete
three-arm predictions must attempt a bounded, same-identity repair instead
of returning THREE_ARM_CAPTURE_INCOMPLETE forever.

Uses fakes for CandidatePopulationService/SelectionPlatformService/etc so the
control-flow fix in ResearchLifecycleAdvanceService._advance_desk can be
proven deterministically without the full production Postgres/NSE stack.

Run: python validation/verify_pl46_three_arm_self_repair_closure.py
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from core.research_lifecycle_advance_service import ResearchLifecycleAdvanceService  # noqa: E402

FINGERPRINT = "pop-fp-abc123"
CANDIDATES = ["c1", "c2", "c3"]


def _row(cid: str) -> Dict[str, Any]:
    return {"candidate_id": cid, "symbol": cid.upper()}


def _prediction(cid: str, arm: str) -> Dict[str, Any]:
    return {"candidate_id": cid, "arm": arm}


class FakePopulations:
    def __init__(self, rows):
        self._rows = rows

    def rows(self, fingerprint):
        return self._rows if fingerprint == FINGERPRINT else []


class FakePlatform:
    """Starts with only 'heuristic' predictions captured (quant/hybrid
    missing) -- the exact THREE_ARM_CAPTURE_INCOMPLETE scenario in P0-02.
    evaluate_population() simulates the real idempotent repair: same
    fingerprint back, and it backfills the missing arms in place.
    """

    def __init__(self):
        self.predictions_by_fp = {
            FINGERPRINT: [_prediction(cid, "heuristic") for cid in CANDIDATES]
        }
        self.evaluate_calls: List[Dict[str, Any]] = []

    def predictions(self, fingerprint):
        return list(self.predictions_by_fp.get(fingerprint, []))

    def evaluate_population(self, rows, *, mode, observed_at, universe_id,
                             dataset_fingerprint, feature_manifest_hash):
        self.evaluate_calls.append({"mode": mode, "observed_at": observed_at})
        # Simulate INSERT OR IGNORE backfill of the missing arms only.
        existing = self.predictions_by_fp.setdefault(FINGERPRINT, [])
        have = {(p["candidate_id"], p["arm"]) for p in existing}
        for cid in CANDIDATES:
            for arm in ("quant", "hybrid"):
                if (cid, arm) not in have:
                    existing.append(_prediction(cid, arm))
        return {"ok": True, "population_fingerprint": FINGERPRINT}


class FakePaper:
    def __init__(self, store):
        pass

    def process_selection_population(self, **kwargs):
        return {"ok": True, "state": "PAPER_OK", "processed": len(kwargs.get("candidates") or [])}


def _build_service(monkeypatch_paper=True):
    svc = ResearchLifecycleAdvanceService.__new__(ResearchLifecycleAdvanceService)
    svc.app = None
    svc.store = object()
    svc.populations = FakePopulations([_row(cid) for cid in CANDIDATES])
    svc.platform = FakePlatform()

    def fake_latest_population(desk):
        return {
            "population_fingerprint": FINGERPRINT,
            "observed_at": "2026-08-22T09:00:00Z",
            "universe_id": "canonical-scan:delivery:3",
            "dataset_fingerprint": "ds-1",
            "feature_manifest_hash": "fm-1",
        }

    svc._latest_population = fake_latest_population  # type: ignore[method-assign]
    return svc


def test_incomplete_three_arm_self_repairs_and_completes():
    import core.research_lifecycle_advance_service as mod
    original = mod.QuantPaperActivationService
    mod.QuantPaperActivationService = FakePaper  # type: ignore[assignment]
    try:
        svc = _build_service()
        result = svc._advance_desk("delivery")
    finally:
        mod.QuantPaperActivationService = original  # type: ignore[assignment]

    assert len(svc.platform.evaluate_calls) == 1, "repair must be attempted exactly once"
    assert result.get("ok") is True, f"expected repair to unblock the desk, got: {result}"
    assert result.get("state") != "THREE_ARM_CAPTURE_INCOMPLETE"
    assert result.get("population_fingerprint") == FINGERPRINT, "repair must never mint a new population identity"


def test_repair_refused_if_it_would_change_population_identity():
    svc = _build_service()

    def hostile_evaluate(rows, **kwargs):
        return {"ok": True, "population_fingerprint": "different-fp"}

    svc.platform.evaluate_population = hostile_evaluate  # type: ignore[method-assign]
    result = svc._advance_desk("delivery")
    assert result.get("state") == "THREE_ARM_CAPTURE_INCOMPLETE"
    assert result.get("missing_arm_repair", {}).get("ok") is False
    assert "different population identity" in str(result.get("missing_arm_repair", {}).get("error") or "")


if __name__ == "__main__":
    test_incomplete_three_arm_self_repairs_and_completes()
    test_repair_refused_if_it_would_change_population_identity()
    print("PASS: P0-02 three-arm self-repair regression")
