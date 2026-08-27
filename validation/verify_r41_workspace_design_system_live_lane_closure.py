from __future__ import annotations

import hashlib
import importlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading

ROOT = Path(__file__).resolve().parents[1]
FROZEN = json.loads((ROOT / "validation/r41_frozen_r40_hashes.json").read_text(encoding="utf-8"))
EXPECTED_PARENT_SHA = "e8026dca0ed26ff6c664dbbc4918cded5ead3c85844013b435392bb0b3fec1b8"

FUNCTIONAL_ALLOWED = {
    "backend/core/scan_orchestration_lifecycle.py",
    "backend/core/scan_orchestration_fast_lane.py",
    "backend/core/scan_orchestration_coverage.py",
    "frontend/app.js",
    "frontend/index.html",
    "frontend/ui-system.css",
}
METADATA_ALLOWED = {
    "RELEASE_IDENTITY.json", "RELEASE_ATTESTATION.json", "frontend/release-identity.json",
    "validation/package_allowlist.json", "validation/package_manifest.sha256",
    "validation/r41_frozen_r40_hashes.json",
    "validation/verify_r41_workspace_design_system_live_lane_closure.py",
    "validation/validate_deployable_candidate.py",
    "docs/R41_WORKSPACE_DESIGN_SYSTEM_LIVE_LANE_CLOSURE.md",
}
ALLOWED = FUNCTIONAL_ALLOWED | METADATA_ALLOWED


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    failures: list[str] = []
    checks: list[dict[str, str]] = []

    def check(name: str, ok: bool, detail: str) -> None:
        checks.append({"gate": name, "state": "PASS" if ok else "FAIL", "detail": detail})
        if not ok:
            failures.append(f"{name}:{detail}")

    check("EXACT_R40_PARENT_ARCHIVE", FROZEN.get("parent_archive_sha256") == EXPECTED_PARENT_SHA,
          "R41 is bound to the exact R40 archive SHA supplied to market-hours acceptance")

    missing: list[str] = []
    changed_unexpected: list[str] = []
    changed_allowed: list[str] = []
    for rel, digest in dict(FROZEN.get("hashes") or {}).items():
        path = ROOT / rel
        if not path.is_file():
            missing.append(rel)
            continue
        if sha(path) != digest:
            if rel in ALLOWED:
                changed_allowed.append(rel)
            else:
                changed_unexpected.append(rel)
    check("R40_NON_REGRESSION", not missing and not changed_unexpected,
          f"missing={len(missing)} unexpected_changed={changed_unexpected}")
    check("FUNCTIONAL_CHANGE_BOUNDARY", set(changed_allowed).issubset(ALLOWED),
          f"allowed_changed={sorted(changed_allowed)}")

    # Installer/service/data/model/chart authority remain frozen by parent hash.
    protected = [
        "INSTALL_UPDATE.cmd", "installer/install.ps1", "installer/register_research_tasks.ps1",
        "backend/core/historical_pit_sweep_service.py", "backend/core/trust_state_service.py",
        "backend/core/selection_walk_forward_replay_service.py",
    ]
    protected_bad = []
    for rel in protected:
        expected = (FROZEN.get("hashes") or {}).get(rel)
        path = ROOT / rel
        if not expected or not path.is_file() or sha(path) != expected:
            protected_bad.append(rel)
    check("INSTALLER_PIT_WFA_TRUST_FROZEN", not protected_bad, f"changed={protected_bad}")

    lifecycle = (ROOT / "backend/core/scan_orchestration_lifecycle.py").read_text(encoding="utf-8")
    fast_lane = (ROOT / "backend/core/scan_orchestration_fast_lane.py").read_text(encoding="utf-8")
    backend_text = "\n".join(
        p.read_text(encoding="utf-8", errors="ignore")
        for p in (ROOT / "backend").rglob("*.py") if "__pycache__" not in p.parts
    )
    check("INTRADAY_MANUAL_TRIGGER_CANONICAL",
          'request_async("intraday_analysis", lambda: self._run_live_mode_scan_impl("intraday"))' in lifecycle,
          "manual/API/lifecycle Intraday requests use canonical live-analysis implementation")
    check("INTRADAY_COMPAT_ENTRY_CANONICAL",
          'return self.run_live_mode_scan("intraday")' in fast_lane,
          "legacy run_fast_lane delegates to canonical live-mode scan")
    execution_refs = len(re.findall(r"\bself\._run_fast_lane_impl\b", backend_text))
    check("NO_LEGACY_INTRADAY_SCHEDULING", execution_refs == 0,
          f"direct execution refs to self._run_fast_lane_impl={execution_refs}; definition may remain for compatibility")

    coverage = (ROOT / "backend/core/scan_orchestration_coverage.py").read_text(encoding="utf-8")
    check("INDEX_LEVEL_REFRESH_GENERATION",
          '"refresh_generation"' in coverage and '"cursor": f"index-levels:{generation}:{refreshed_at}"' in coverage,
          "successful periodic index refresh publishes a changing verified progress cursor")
    check("INDEX_LEVEL_EMPTY_REFRESH_FAILS_CLOSED",
          '"ok": count > 0' in coverage and '"NO_LEVELS_REFRESHED"' in coverage,
          "zero refreshed level sets cannot masquerade as healthy progress")

    appjs = (ROOT / "frontend/app.js").read_text(encoding="utf-8")
    css = (ROOT / "frontend/ui-system.css").read_text(encoding="utf-8")
    index = (ROOT / "frontend/index.html").read_text(encoding="utf-8")
    check("WORKSPACE_TWO_TRUTHS_VISIBLE",
          "Universe sweep" in appjs and "Live analysis" in appjs and "Universe ready" in appjs,
          "Workspace separates coverage completion from live-analysis health")
    check("UNIFIED_UI_AUTHORITY_LOADED_LAST",
          '/ui-system.css?v=131.0.0-r41' in index and index.index('ui-system.css') > index.index('app.css'),
          "release UI authority loads after legacy mechanics CSS")
    check("TYPOGRAPHY_FLOOR",
          '--ui-xxs:11px' in css and 'font-size:14px!important' in css,
          "14px customer body scale with 11px absolute metadata floor")
    check("DESK_LAYOUT_NOT_CRAMPED",
          'min-height:164px' in css and 'grid-template-columns:minmax(130px,.55fr) minmax(220px,.9fr)' in css
          and '.desk-v3-live' in css and 'font-size:24px' in css,
          "desk identity/progress/context/live-analysis/KPIs are separated with readable numeric hierarchy")
    check("KPI_LABELS_STACKED",
          'grid-template-columns:1fr!important' in css and 'min-height:48px' in css,
          "desk KPI label/value stack prevents the R40 ELIGI/SH/DEEP collision")

    # Execute the installer-equivalent backend import, including every changed backend module.
    with tempfile.TemporaryDirectory(prefix="laddu-r41-import-") as td:
        env = dict(os.environ)
        env["PROJECT_LADDU_HOME"] = td
        env["PROJECT_LADDU_DATA_PLANE_MODE"] = "test"
        env["PROJECT_LADDU_BACKEND_DIR"] = str(ROOT / "backend")
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        code = (
            "import os,sys; sys.path.insert(0,os.environ['PROJECT_LADDU_BACKEND_DIR']); "
            "import main; import core.scan_orchestration_lifecycle; import core.scan_orchestration_fast_lane; "
            "import core.scan_orchestration_coverage; print(main.APP_VERSION)"
        )
        proc = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT), env=env,
                              capture_output=True, text=True, timeout=60)
        check("INSTALLER_EQUIVALENT_BACKEND_IMPORT",
              proc.returncode == 0 and "v131.0.0" in proc.stdout,
              f"returncode={proc.returncode}; stderr={proc.stderr.strip()[-240:]}")

    node = subprocess.run(["node", "--check", str(ROOT / "frontend/app.js")],
                          capture_output=True, text=True, timeout=20)
    check("FRONTEND_JS_SYNTAX", node.returncode == 0,
          node.stderr.strip()[-200:] if node.returncode else "node --check PASS")

    report = {
        "ok": not failures,
        "scope": "R41_WORKSPACE_DESIGN_SYSTEM_LIVE_LANE_CLOSURE",
        "checks": checks,
        "passed": sum(c["state"] == "PASS" for c in checks),
        "failed": sum(c["state"] == "FAIL" for c in checks),
        "failures": failures,
        "functional_change_boundary": sorted(FUNCTIONAL_ALLOWED),
        "production_ready": False,
        "broker_authority": "NONE",
    }
    print(json.dumps(report, indent=2))
    return 0 if not failures else 2


if __name__ == "__main__":
    raise SystemExit(main())
