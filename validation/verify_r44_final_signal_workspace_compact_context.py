from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
FROZEN = json.loads((ROOT / "validation/r44_frozen_r43_hashes.json").read_text(encoding="utf-8"))
EXPECTED_PARENT_SHA = "ae222e2f0991375928963278af33a90a345ef2ca8baa2eb86793514ad455f704"

FUNCTIONAL_ALLOWED = {"frontend/app.js", "frontend/index.html", "frontend/ui-system.css"}
METADATA_ALLOWED = {
    "RELEASE_IDENTITY.json", "RELEASE_ATTESTATION.json", "frontend/release-identity.json",
    "validation/package_allowlist.json", "validation/package_manifest.sha256",
    "validation/r44_frozen_r43_hashes.json",
    "validation/verify_r44_final_signal_workspace_compact_context.py",
    "validation/validate_deployable_candidate.py",
    "docs/R44_FINAL_SIGNAL_WORKSPACE_COMPACT_CONTEXT.md",
}
ALLOWED = FUNCTIONAL_ALLOWED | METADATA_ALLOWED


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    failures=[]; checks=[]
    def check(name, ok, detail):
        checks.append({"gate":name,"state":"PASS" if ok else "FAIL","detail":detail})
        if not ok: failures.append(f"{name}:{detail}")

    check("EXACT_R43_PARENT_ARCHIVE", FROZEN.get("parent_archive_sha256") == EXPECTED_PARENT_SHA, "R44 bound to exact R43 archive")
    missing=[]; unexpected=[]; changed=[]
    for rel,digest in dict(FROZEN.get("hashes") or {}).items():
        path=ROOT/rel
        if not path.is_file(): missing.append(rel); continue
        if sha(path)!=digest:
            if rel in ALLOWED: changed.append(rel)
            else: unexpected.append(rel)
    check("R43_NON_REGRESSION", not missing and not unexpected, f"missing={len(missing)} unexpected_changed={unexpected}")
    check("FRONTEND_ONLY_FUNCTIONAL_BOUNDARY", set(changed).issubset(ALLOWED), f"allowed_changed={sorted(changed)}")

    protected=[
      "INSTALL_UPDATE.cmd","installer/install.ps1","installer/register_research_tasks.ps1",
      "backend/routes_get_system.py","backend/routes_get_performance.py",
      "backend/core/decision_row_projection_service.py",
      "backend/core/scan_orchestration_lifecycle.py","backend/core/scan_orchestration_fast_lane.py",
      "backend/core/scan_orchestration_coverage.py","backend/core/intraday_session_policy.py",
      "backend/core/historical_pit_sweep_service.py","backend/core/trust_state_service.py",
      "backend/core/selection_walk_forward_replay_service.py",
    ]
    bad=[]
    for rel in protected:
        expected=(FROZEN.get("hashes") or {}).get(rel); path=ROOT/rel
        if not expected or not path.is_file() or sha(path)!=expected: bad.append(rel)
    check("BACKEND_INSTALLER_R43_FROZEN", not bad, f"changed={bad}")

    index=(ROOT/"frontend/index.html").read_text(encoding="utf-8")
    app=(ROOT/"frontend/app.js").read_text(encoding="utf-8")
    css=(ROOT/"frontend/ui-system.css").read_text(encoding="utf-8")
    check("ROUTE_ISOLATION_INHERITED", ".page[data-page-panel]{display:none!important}" in css and ".page[data-page-panel].active{display:block!important}" in css, "R43 top-level route isolation retained")
    check("FINAL_SIGNAL_PRIMARY_SURFACE", ">FINAL SIGNALS<" in index and "SELECTED CANDIDATES" not in index, "Workspace names the customer surface Final Signals")
    check("FINAL_SIGNAL_FILTERS", 'data-workspace-signal-mode' in index and all(f'data-mode="{x}"' in index for x in ('all','intraday','delivery')), "All/Intraday/Delivery filter restored")
    check("TOP5_TOP10_BOUND", 'data-workspace-signal-limit' in index and 'data-limit="5"' in index and 'data-limit="10"' in index and "workspaceSignalLimit: 5" in app, "Top 5 default with bounded Top 10 view")
    check("FINAL_ONLY_ADMISSION", "function workspaceFinalSignal(row)" in app and "/OPEN|OPENED|SIGNAL_OPEN|FINAL|PROMOTED|ACTIONABLE/.test(stage)" in app and "row?.research_only === true" in app, "research/prepared candidates cannot enter Final Signals")
    check("COMPLETE_GEOMETRY_REQUIRED", "entry !== null && target !== null && stop !== null" in app and "PENDING_LIVE_CONFIRMATION" in app, "Final row requires positive Entry/Target/SL and cannot be pending geometry")
    headers=["Stock","Mode","Score","LTP","₹ Chg","% Chg","Entry","Target","SL","Age / Timeline","Hit","Next","Status","Net P&amp;L"]
    check("FINAL_SIGNAL_COLUMNS", all(f">{h}</th>" in index for h in headers), "one-line final lifecycle fields present")
    check("NO_SECOND_LINE_STOCK_META", "stock-subline" not in app[app.index("function renderWorkspaceFinalSignals"):app.index("function renderWorkspaceSummary")], "Final Signals stock cell has no second-line mode/stage copy")
    check("INTRADAY_TIMELINE_VISIBLE", "Entry ≤14:30" in app and "Flat ≤15:00" in app, "agreed Intraday timing contract is visible without pretending backend enforcement changed")
    check("ONE_MARKET_RAIL", 'id="marketDecisionRail"' in index and 'market-panel' not in index[index.index('data-page-panel="workspace"'):index.index('data-page-panel="report"')] and 'sector-panel' not in index[index.index('data-page-panel="workspace"'):index.index('data-page-panel="report"')], "market/sector support collapsed to one rail")
    check("NO_MARKET_SCROLL", "market-decision-rail" in css and "overflow:hidden!important" in css[css.rfind("Project Laddu R44"):], "R44 market rail is non-scrolling")
    check("ONE_SCANNER_RAIL", 'class="scanner-health-rail"' in index and "scanner-rail-item" in app, "Intraday and Delivery health share one compact support line")
    check("R44_CACHE_IDENTITY", all(token in index for token in ["/app.css?v=131.0.0-r44","/ui-system.css?v=131.0.0-r44","/app.js?v=131.0.0-r44"]), "browser cache identity R44")
    node=subprocess.run(["node","--check",str(ROOT/"frontend/app.js")],capture_output=True,text=True,timeout=20)
    check("FRONTEND_JS_SYNTAX",node.returncode==0,node.stderr.strip()[-220:] if node.returncode else "node --check PASS")

    report={"ok":not failures,"scope":"R44_FINAL_SIGNAL_WORKSPACE_COMPACT_CONTEXT","checks":checks,"passed":sum(c["state"]=="PASS" for c in checks),"failed":sum(c["state"]=="FAIL" for c in checks),"failures":failures,"functional_change_boundary":sorted(FUNCTIONAL_ALLOWED),"production_ready":False,"broker_authority":"NONE"}
    print(json.dumps(report,indent=2)); return 0 if not failures else 2

if __name__=="__main__": raise SystemExit(main())
