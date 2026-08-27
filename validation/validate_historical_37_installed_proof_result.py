#!/usr/bin/env python3
"""Validate one exact-build Historical37InstalledClosureProof result."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT / "validation" / "historical_37_installed_proof_plan.json"
IDENTITY = ROOT / "RELEASE_IDENTITY.json"
ALLOWED = {"PASS", "FAIL", "TARGET_PENDING"}


def validate(proof_path: Path, *, require_closure: bool = False) -> dict[str, Any]:
    failures: list[str] = []
    proof = json.loads(proof_path.read_text(encoding="utf-8-sig"))
    plan = json.loads(PLAN.read_text(encoding="utf-8"))
    identity = json.loads(IDENTITY.read_text(encoding="utf-8-sig"))
    expected_build = str(identity.get("version") or "")
    if str(proof.get("authority") or "") != "Historical37InstalledClosureProof":
        failures.append("proof authority mismatch")
    if str(proof.get("exact_build") or "") != expected_build:
        failures.append(f"proof build mismatch: proof={proof.get('exact_build')} package={expected_build}")
    if str(proof.get("plan_content_sha256") or "") != str(plan.get("content_sha256") or ""):
        failures.append("proof plan hash does not match packaged installed-proof plan")

    gate_rows = list(proof.get("gates") or [])
    gate_map: dict[str, dict[str, Any]] = {}
    for row in gate_rows:
        gid = str(row.get("gate_id") or "")
        if not gid:
            failures.append("proof contains gate without gate_id")
            continue
        if gid in gate_map:
            failures.append(f"duplicate proof gate: {gid}")
        gate_map[gid] = row
        if str(row.get("status") or "") not in ALLOWED:
            failures.append(f"{gid} has invalid status {row.get('status')!r}")
    expected_gates = set(plan.get("gate_catalog") or {})
    actual_gates = set(gate_map)
    if actual_gates != expected_gates:
        failures.append(f"gate inventory mismatch: missing={sorted(expected_gates-actual_gates)} extra={sorted(actual_gates-expected_gates)}")

    defects = list(proof.get("defects") or [])
    expected_ids = [f"DL-{i:03d}" for i in range(1, 38)]
    if [str(row.get("defect_id") or "") for row in defects] != expected_ids:
        failures.append("proof defect inventory/order must be exactly DL-001..DL-037")
    plan_rows = list(plan.get("rows") or [])
    derived_closed = derived_pending = derived_failed = 0
    for idx, row in enumerate(defects[:37]):
        planned = plan_rows[idx]
        did = expected_ids[idx]
        required = list(planned.get("required_gates") or [])
        statuses = {gid: str((gate_map.get(gid) or {}).get("status") or "FAIL") for gid in required}
        passed = [gid for gid in required if statuses[gid] == "PASS"]
        pending = [gid for gid in required if statuses[gid] == "TARGET_PENDING"]
        failed = [gid for gid in required if statuses[gid] == "FAIL"]
        state = "FAILED_TARGET_PROOF" if failed else "TARGET_PENDING" if pending else "CLOSED_ELIGIBLE"
        if list(row.get("required_target_gates") or []) != required:
            failures.append(f"{did} required_target_gates drift from packaged plan")
        if list(row.get("passed_target_gates") or []) != passed:
            failures.append(f"{did} passed_target_gates inconsistent with gate statuses")
        if list(row.get("pending_target_gates") or []) != pending:
            failures.append(f"{did} pending_target_gates inconsistent with gate statuses")
        if list(row.get("failed_target_gates") or []) != failed:
            failures.append(f"{did} failed_target_gates inconsistent with gate statuses")
        if str(row.get("formal_status_candidate") or "") != state:
            failures.append(f"{did} formal_status_candidate inconsistent: declared={row.get('formal_status_candidate')} derived={state}")
        if state == "CLOSED_ELIGIBLE":
            derived_closed += 1
        elif state == "TARGET_PENDING":
            derived_pending += 1
        else:
            derived_failed += 1

    counts = dict(proof.get("counts") or {})
    expected_counts = {
        "tracked": 37,
        "closed_eligible": derived_closed,
        "target_pending": derived_pending,
        "failed_target_proof": derived_failed,
        "gates": len(expected_gates),
        "gate_pass": sum(1 for row in gate_map.values() if row.get("status") == "PASS"),
        "gate_pending": sum(1 for row in gate_map.values() if row.get("status") == "TARGET_PENDING"),
        "gate_fail": sum(1 for row in gate_map.values() if row.get("status") == "FAIL"),
    }
    for key, value in expected_counts.items():
        if int(counts.get(key, -1)) != value:
            failures.append(f"proof count {key} stale: declared={counts.get(key)} derived={value}")
    derived_state = "ALL_37_INSTALLED_PROVEN" if derived_closed == 37 else "BLOCKED" if derived_failed else "TARGET_PENDING"
    if str(proof.get("state") or "") != derived_state:
        failures.append(f"proof state stale: declared={proof.get('state')} derived={derived_state}")
    if bool(proof.get("ok")) != (derived_closed == 37):
        failures.append("proof ok flag does not equal 37/37 CLOSED_ELIGIBLE")
    if require_closure and derived_closed != 37:
        failures.append(f"installed closure required but only {derived_closed}/37 defects are CLOSED_ELIGIBLE")
    if str(proof.get("broker_authority") or "") != "NONE":
        failures.append("installed proof broker authority must remain NONE")

    return {
        "ok": not failures,
        "authority": "Historical37InstalledProofResultGate",
        "authority_version": "1.0.0",
        "exact_build": expected_build,
        "closed_eligible": derived_closed,
        "target_pending": derived_pending,
        "failed_target_proof": derived_failed,
        "require_closure": require_closure,
        "failures": failures,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--proof", type=Path, required=True)
    parser.add_argument("--require-closure", action="store_true")
    args = parser.parse_args()
    report = validate(args.proof, require_closure=args.require_closure)
    print(json.dumps(report, indent=2))
    return 0 if report["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
