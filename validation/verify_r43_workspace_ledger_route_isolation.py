from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
FROZEN = json.loads((ROOT / "validation/r43_frozen_r42_hashes.json").read_text(encoding="utf-8"))
EXPECTED_PARENT_SHA = "135562531b996a39a4f8122747230b1e847626802333139d8de188c063fad756"

FUNCTIONAL_ALLOWED = {"frontend/app.js", "frontend/index.html", "frontend/ui-system.css"}
METADATA_ALLOWED = {
    "RELEASE_IDENTITY.json", "RELEASE_ATTESTATION.json", "frontend/release-identity.json",
    "validation/package_allowlist.json", "validation/package_manifest.sha256",
    "validation/r43_frozen_r42_hashes.json",
    "validation/verify_r43_workspace_ledger_route_isolation.py",
    "validation/validate_deployable_candidate.py",
    "docs/R43_WORKSPACE_LEDGER_ROUTE_ISOLATION.md",
}
ALLOWED = FUNCTIONAL_ALLOWED | METADATA_ALLOWED

def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()

def main() -> int:
    failures=[]; checks=[]
    def check(name, ok, detail):
        checks.append({"gate":name,"state":"PASS" if ok else "FAIL","detail":detail})
        if not ok: failures.append(f"{name}:{detail}")

    check("EXACT_R42_PARENT_ARCHIVE", FROZEN.get("parent_archive_sha256") == EXPECTED_PARENT_SHA, "R43 bound to exact R42 archive")
    missing=[]; unexpected=[]; changed=[]
    for rel,digest in dict(FROZEN.get("hashes") or {}).items():
        path=ROOT/rel
        if not path.is_file():
            missing.append(rel); continue
        if sha(path)!=digest:
            if rel in ALLOWED: changed.append(rel)
            else: unexpected.append(rel)
    check("R42_NON_REGRESSION", not missing and not unexpected, f"missing={len(missing)} unexpected_changed={unexpected}")
    check("FRONTEND_ONLY_FUNCTIONAL_BOUNDARY", set(changed).issubset(ALLOWED), f"allowed_changed={sorted(changed)}")
    protected=[
      "INSTALL_UPDATE.cmd","installer/install.ps1","installer/register_research_tasks.ps1",
      "backend/routes_get_system.py","backend/routes_get_performance.py",
      "backend/core/decision_row_projection_service.py",
      "backend/core/scan_orchestration_lifecycle.py","backend/core/scan_orchestration_fast_lane.py",
      "backend/core/scan_orchestration_coverage.py","backend/core/historical_pit_sweep_service.py",
      "backend/core/trust_state_service.py","backend/core/selection_walk_forward_replay_service.py",
    ]
    bad=[]
    for rel in protected:
        expected=(FROZEN.get("hashes") or {}).get(rel); path=ROOT/rel
        if not expected or not path.is_file() or sha(path)!=expected: bad.append(rel)
    check("BACKEND_INSTALLER_R42_FROZEN", not bad, f"changed={bad}")

    index=(ROOT/"frontend/index.html").read_text(encoding="utf-8")
    app=(ROOT/"frontend/app.js").read_text(encoding="utf-8")
    css=(ROOT/"frontend/ui-system.css").read_text(encoding="utf-8")
    check("ROUTE_ISOLATION", ".page[data-page-panel]{display:none!important}" in css and ".page[data-page-panel].active{display:block!important}" in css and '[data-page-panel="workspace"]{display:block!important}' not in css, "exactly one top-level page visible")
    headers=["Stock","LTP","₹ Chg","% Chg","Entry","Target","SL","Hit","Next","Status","Net P&amp;L"]
    check("WORKSPACE_LEDGER_COLUMNS", all(f">{h}</th>" in index for h in headers), "selected list exposes agreed trading lifecycle columns")
    check("NO_DUPLICATE_FINAL_DECISIONS_PANEL", "FINAL DECISIONS" not in index, "selected ledger is the single primary Workspace lifecycle surface")
    check("LIFECYCLE_POOL", "const lifecyclePool = [...candidatePool, ...rows(payload.preparing), ...rows(payload.active)];" in app, "selected list follows candidate/preparing/active canonical rows")
    check("PRICE_CHANGE_PAIR", "display_change_abs" in app and "display_change_pct" in app, "LTP change ₹/% rendered from coherent backend projection")
    check("TARGET_STOP_HIT", "candidateHitState" in app and "TARGET HIT" in app and "SL HIT" in app, "target/stop outcome state visible")
    check("NEXT_ACTION", "candidateNextAction" in app and "CONTINUE" in app and "HOLD" in app and "EXIT" in app, "next state supports Continue/Hold/Exit")
    check("SUCCESS_FAILURE", "candidateOutcomeState" in app and "SUCCESS" in app and "FAILURE" in app, "settlement status visible when authoritative outcome exists")
    check("ACCURACY_LINK", 'data-open-page="accuracy"' in index and "settled outcomes move to Accuracy &amp; Performance" in index, "settled lifecycle destination is explicit")
    check("R43_CACHE_IDENTITY", all(token in index for token in ["/app.css?v=131.0.0-r43","/ui-system.css?v=131.0.0-r43","/app.js?v=131.0.0-r43"]), "browser cache identity R43")
    node=subprocess.run(["node","--check",str(ROOT/"frontend/app.js")],capture_output=True,text=True,timeout=20)
    check("FRONTEND_JS_SYNTAX",node.returncode==0,node.stderr.strip()[-220:] if node.returncode else "node --check PASS")
    report={"ok":not failures,"scope":"R43_WORKSPACE_LEDGER_ROUTE_ISOLATION","checks":checks,"passed":sum(c["state"]=="PASS" for c in checks),"failed":sum(c["state"]=="FAIL" for c in checks),"failures":failures,"functional_change_boundary":sorted(FUNCTIONAL_ALLOWED),"production_ready":False,"broker_authority":"NONE"}
    print(json.dumps(report,indent=2)); return 0 if not failures else 2

if __name__=="__main__": raise SystemExit(main())
