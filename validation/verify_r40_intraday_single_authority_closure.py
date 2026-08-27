from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
FROZEN = json.loads((ROOT / "validation/r40_frozen_r39_hashes.json").read_text(encoding="utf-8"))
EXPECTED_PARENT_SHA = "51c3041450f62df7625ef37535c97fa996b5602f75df80bb2c3a74731c248c16"
ALLOWED = {
    "backend/core/desk_analysis_executor_router.py",
    "backend/core/scan_orchestration_service.py",
    "backend/core/desk_runtime_authority.py",
    "backend/core/scan_orchestration_coverage.py",
    "backend/core/scan_orchestration_lifecycle.py",
    "backend/core/operations_control_service.py",
    "RELEASE_IDENTITY.json", "RELEASE_ATTESTATION.json", "frontend/release-identity.json",
    "validation/package_allowlist.json", "validation/package_manifest.sha256",
    "validation/validate_deployable_candidate.py",
    "validation/r40_frozen_r39_hashes.json",
    "validation/verify_r40_intraday_authority_closure.py",
    "validation/verify_r40_intraday_single_authority_closure.py",
    "docs/R40_INTRADAY_SINGLE_AUTHORITY_CLOSURE.md",
}


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    failures = []
    checks = []

    def check(name, ok, detail):
        checks.append({"gate": name, "state": "PASS" if ok else "FAIL", "detail": detail})
        if not ok:
            failures.append(f"{name}:{detail}")

    check(
        "EXACT_R39_PARENT_ARCHIVE",
        FROZEN.get("parent_archive_sha256") == EXPECTED_PARENT_SHA,
        "R40 is bound to the exact installed-candidate R39 archive SHA",
    )
    missing, changed = [], []
    for rel, digest in dict(FROZEN.get("hashes") or {}).items():
        if rel in ALLOWED:
            continue
        path = ROOT / rel
        if not path.is_file():
            missing.append(rel)
        elif sha(path) != digest:
            changed.append(rel)
    check(
        "R39_NON_REGRESSION",
        not missing and not changed,
        f"parent members protected; missing={len(missing)} changed={len(changed)}",
    )

    # Installer and all production authorities outside the declared boundary are
    # protected by the frozen-parent check above. Reproduce the installer import
    # preflight again because R38 once escaped source-only QC on exactly this gate.
    with tempfile.TemporaryDirectory(prefix="laddu-r40-import-") as td:
        env = dict(os.environ)
        env["PROJECT_LADDU_HOME"] = td
        env["PROJECT_LADDU_DATA_PLANE_MODE"] = "test"
        env["PROJECT_LADDU_BACKEND_DIR"] = str(ROOT / "backend")
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        code = (
            "import os,sys; sys.path.insert(0,os.environ['PROJECT_LADDU_BACKEND_DIR']); "
            "import main; print(main.APP_VERSION)"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code], cwd=str(ROOT), env=env,
            capture_output=True, text=True, timeout=60,
        )
        check(
            "INSTALLER_EQUIVALENT_BACKEND_IMPORT",
            proc.returncode == 0 and "v131.0.0" in proc.stdout,
            f"returncode={proc.returncode}; stderr={proc.stderr.strip()[-240:]}",
        )

    focused = subprocess.run(
        [sys.executable, "validation/verify_r40_intraday_authority_closure.py"],
        cwd=str(ROOT), capture_output=True, text=True, timeout=30,
    )
    focused_payload = {}
    try:
        focused_payload = json.loads(focused.stdout)
    except Exception:
        pass
    check(
        "INTRADAY_AUTHORITY_BEHAVIOUR",
        focused.returncode == 0 and bool(focused_payload.get("ok")),
        f"focused_returncode={focused.returncode}; failures={focused_payload.get('failures')}",
    )

    # Release-boundary proof: no UI/installer/chart/model/PIT source belongs to
    # this candidate's functional-change set.
    forbidden_changed = []
    for rel, digest in dict(FROZEN.get("hashes") or {}).items():
        path = ROOT / rel
        if not path.is_file() or sha(path) == digest:
            continue
        if rel.startswith(("frontend/", "installer/")) or rel in {
            "backend/core/historical_pit_sweep_service.py",
            "backend/core/trust_state_service.py",
            "backend/core/selection_walk_forward_replay_service.py",
        }:
            forbidden_changed.append(rel)
    check(
        "NO_UI_INSTALLER_PIT_WFA_CHANGE",
        not forbidden_changed,
        f"forbidden_changed={forbidden_changed}",
    )

    report = {
        "ok": not failures,
        "scope": "R40_INTRADAY_SINGLE_AUTHORITY_CLOSURE",
        "checks": checks,
        "passed": sum(c["state"] == "PASS" for c in checks),
        "failed": sum(c["state"] == "FAIL" for c in checks),
        "failures": failures,
        "production_ready": False,
        "broker_authority": "NONE",
    }
    print(json.dumps(report, indent=2))
    return 0 if not failures else 2


if __name__ == "__main__":
    raise SystemExit(main())
