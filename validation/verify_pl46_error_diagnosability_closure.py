"""P0-05 partial closure: capital WFA (and every other route) previously
logged/returned only str(exc) on failure -- no file/line/function -- which is
exactly why the PL44 installed 503s could not be diagnosed from evidence.
This does not (and must not) change any statistical/risk/cost gate; it only
makes a genuine failure's location visible so it can actually be fixed on the
next installed run.

Run: python validation/verify_pl46_error_diagnosability_closure.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from http_server import Handler  # noqa: E402


def _diagnostic_location(exc: BaseException) -> str:
    return Handler._diagnostic_location(exc)



def test_diagnostic_location_points_at_backend_frame():
    def inner_backend_call():
        raise ValueError("simulated capital WFA failure")

    try:
        inner_backend_call()
    except ValueError as exc:
        location = _diagnostic_location(exc)

    assert "verify_pl46_error_diagnosability_closure.py" in location or "inner_backend_call" in location, (
        f"expected a real file:line/function pointer, got: {location!r}"
    )
    assert location != "unknown_location"


def test_selection_walk_forward_replay_enriches_error_with_location():
    import routes_get_research as rgr

    class ExplodingService:
        def __init__(self, store):
            pass

        def replay(self, **kwargs):
            raise RuntimeError("simulated deep WFA math failure")

    class FakeApp:
        store = object()

        def event(self, *a, **k):
            pass

    original = rgr.SelectionWalkForwardReplayService
    rgr.SelectionWalkForwardReplayService = ExplodingService  # type: ignore[assignment]
    try:
        body, status = rgr.r_selection_walk_forward_replay(FakeApp(), {"desk": ["delivery"]}, "", "delivery")
    finally:
        rgr.SelectionWalkForwardReplayService = original  # type: ignore[assignment]

    assert status == 503
    assert body["error_type"] == "RuntimeError"
    assert body["error_location"] != "unknown_location"
    assert "error_diagnosability_closure" in body["error_location"] or ".py" in body["error_location"]


if __name__ == "__main__":
    test_diagnostic_location_points_at_backend_frame()
    test_selection_walk_forward_replay_enriches_error_with_location()
    print("PASS: P0-05 error diagnosability regression")
