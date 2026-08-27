from __future__ import annotations

import json
import time
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from core.desk_analysis_executor_router import DeskAnalysisExecutorRouter
from core.desk_runtime_authority import DeskCandidateScannerAuthority


def _prepared():
    return {
        "candles_override": [{"close": 100.0}],
        "prepared_analysis": {"context": {"local": True}},
    }


def main() -> int:
    checks = []

    def check(name: str, ok: bool, detail=None):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    # 1/2: Delivery remains deterministic; Intraday is bounded single-worker.
    def analyze(inst, quote, desk, **kwargs):
        if inst.get("sleep"):
            time.sleep(float(inst["sleep"]))
        return {"ok": True, "desk": desk, "symbol": inst.get("symbol")}

    router = DeskAnalysisExecutorRouter(analyze, enforce_local_input=True)
    dcap = router.capacity("delivery")
    icap = router.capacity("intraday")
    check("delivery_execution_policy_frozen", dcap.get("desk_execution_policy") == "DETERMINISTIC_LOCAL", dcap)
    check("intraday_execution_policy_bounded", icap.get("desk_execution_policy") == "BOUNDED_LOCAL_SINGLE_WORKER" and int(icap.get("workers") or 0) == 1, icap)

    # 3: A pathological pure/local Intraday calculation cannot hold the caller/lane.
    started = time.monotonic()
    value, state = router.run(
        {"symbol": "R40TIMEOUT", "sleep": 0.55}, None, "intraday", 0.25, **_prepared()
    )
    elapsed = time.monotonic() - started
    check("intraday_pathological_compute_times_out", state == "analysis_timeout" and elapsed < 0.45, {"state": state, "elapsed_sec": round(elapsed, 3)})

    # Let the finite test worker finish so the interpreter can exit cleanly.
    time.sleep(0.35)

    # 4: Lane-in-flight state is recognized only inside the bounded age window.
    class Lanes:
        def __init__(self, age): self.age = age
        def snapshot(self):
            return {"lanes": {"intraday_analysis": {"state": "running_coalesced", "last_started_at": time.time() - self.age}}, "pending_async": []}
    class SO:
        def __init__(self, age): self.lanes = Lanes(age)
    class Host:
        def __init__(self, age): self.scan_orchestration = SO(age)
    bounded = DeskCandidateScannerAuthority(Host(20), "intraday")._intraday_lane_inflight()
    stale = DeskCandidateScannerAuthority(Host(100), "intraday")._intraday_lane_inflight()
    check("coalesced_inflight_bounded_expected_idle", bounded[0] is True and stale[0] is False, {"bounded": bounded, "stale": stale})

    # 5/6/7: exact source contracts for one authority per concern.
    authority_src = (BACKEND / "core" / "desk_runtime_authority.py").read_text(encoding="utf-8")
    coverage_src = (BACKEND / "core" / "scan_orchestration_coverage.py").read_text(encoding="utf-8")
    ops_src = (BACKEND / "core" / "operations_control_service.py").read_text(encoding="utf-8")
    check("live_supervisor_uses_cycle_authority", '"intraday_live_analysis", self._completed_cycles' in authority_src and 'completed = self._completed_cycles' in authority_src)
    check("coverage_supervisor_gets_cumulative_sweep", '"scanned": attempted' in coverage_src and '"population_count": universe_size' in coverage_src)
    virtual_section = ops_src[ops_src.index('modes = dict(status.get("mode_scanners") or {})'):ops_src.index('selected = {}', ops_src.index('modes = dict(status.get("mode_scanners") or {})'))]
    check("no_duplicate_virtual_intraday_scanner", 'for desk in ("delivery",):' in virtual_section and 'scanner:intraday' not in virtual_section)

    # 8: critical unrelated release boundaries are unchanged by the focused gate.
    check("broker_authority_not_introduced", "broker authority" not in authority_src.lower() or "broker" not in authority_src.lower())

    failures = [row for row in checks if not row["ok"]]
    print(json.dumps({"contract": "r40-intraday-authority-closure-1.0.0", "ok": not failures, "checks": checks, "failures": failures}, indent=2, default=str))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
