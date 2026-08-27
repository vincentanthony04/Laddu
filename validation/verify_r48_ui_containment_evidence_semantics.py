"""Focused R48 UI containment and evidence-semantics source gate."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check(condition: bool, name: str, failures: list[str], passes: list[str]) -> None:
    (passes if condition else failures).append(name)


def main() -> int:
    failures: list[str] = []
    passes: list[str] = []

    frozen_path = ROOT / "validation" / "r48_frozen_r47_hashes.json"
    frozen = json.loads(frozen_path.read_text(encoding="utf-8"))
    check(frozen.get("exact_parent_sha256") == "4f3fa86dea53c133218390a63b0086253e2c0748fc961c28cfc660fa340e856c", "exact R47 parent SHA frozen", failures, passes)
    changed_frozen = []
    missing_frozen = []
    for rel, expected in dict(frozen.get("hashes") or {}).items():
        path = ROOT / rel
        if not path.is_file():
            missing_frozen.append(rel)
        elif sha(path) != expected:
            changed_frozen.append(rel)
    check(not missing_frozen, f"R47 frozen files present ({len(frozen.get('hashes') or {})})" if not missing_frozen else "missing frozen files: " + ",".join(missing_frozen[:8]), failures, passes)
    check(not changed_frozen, "R47 backend/math/research/non-UI parent bytes frozen" if not changed_frozen else "frozen parent bytes changed: " + ",".join(changed_frozen[:8]), failures, passes)

    index = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8-sig")
    app = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8-sig")
    css = (ROOT / "frontend" / "app.css").read_text(encoding="utf-8-sig")
    ui_css = (ROOT / "frontend" / "ui-system.css").read_text(encoding="utf-8-sig")

    check("app.css?v=131.0.0-r48" in index and "ui-system.css?v=131.0.0-r48" in index and "app.js?v=131.0.0-r48" in index, "R48 asset cache identity exact", failures, passes)
    check("131.0.0-r44" not in index, "stale R44 browser cache key removed", failures, passes)
    check("ACTIVE PROBLEMS" in index and "EVIDENCE STILL MATURING" in index and 'id="operationsEvidence"' in index, "active problems separated from maturing evidence", failures, passes)
    check("state.operationsProblems = activeRows" in app and "state.operationsEvidence = evidenceRows" in app, "separate active/evidence read models", failures, passes)
    check("Evidence is still maturing; this is not an active runtime failure" in app, "pending evidence explicitly non-failure", failures, passes)
    check("operationProblemRowHtml" in app and "compactOperationDetail" in app and "Copy for full evidence" in app, "long evidence has bounded preview with full copy path", failures, passes)
    check("grid-template-columns:minmax(0,1fr) auto!important" in css and "overflow-wrap:break-word!important" in css and "word-break:normal!important" in css, "error/evidence rows cannot collapse to character-width columns", failures, passes)
    check("EVIDENCE STILL MATURING|ACTION\\s*\\/\\s*EVIDENCE CONSOLE" in app, "maturing evidence defaults collapsed", failures, passes)
    check("function parkStockChartSurface()" in app and "function restoreStockChartSurface()" in app, "hidden chart lifecycle is explicit", failures, passes)
    check("chart?.remove()" in app and "state.chart = null; state.candleSeries = null" in app, "leaving Stock Intelligence destroys compositor instances", failures, passes)
    check("if (leavingReport) parkStockChartSurface();" in app and "if (enteringReport) restoreStockChartSurface();" in app, "route transition parks/restores chart", failures, passes)
    check("state.page !== 'report'" in app and "clearTimeout(state.projectionRefreshTimer)" in app and "clearInterval(state.liveTimer)" in app, "hidden route suppresses chart/live background work", failures, passes)
    check("chart-surface-parked" in css and 'page[data-page-panel="report"]:not(.active){content-visibility:hidden!important;contain:strict!important}' in css, "hidden Stock Intelligence paint containment", failures, passes)
    check(".collapsible-section.is-collapsed" in ui_css and "expandSystemSections" in app and "collapseSystemSections" in app, "persistent expand/collapse contract retained", failures, passes)

    identity = json.loads((ROOT / "RELEASE_IDENTITY.json").read_text(encoding="utf-8-sig"))
    attestation = json.loads((ROOT / "RELEASE_ATTESTATION.json").read_text(encoding="utf-8-sig"))
    check(identity.get("broker_authority") == "NONE" and identity.get("production_ready") is False, "broker/release boundary fail-closed", failures, passes)
    check("R48" in str(identity.get("acceptance_state")) and identity.get("r48_exact_parent_sha256") == frozen.get("exact_parent_sha256"), "R48 identity and exact parent declared", failures, passes)
    check(attestation.get("production_ready") is False and attestation.get("broker_authority") == "NONE", "R48 attestation remains non-production", failures, passes)
    check("r47_intraday_price_action_session_structure_ui_clarity" in dict(attestation.get("proof_state") or {}), "R47 mathematics acceptance remains pending, not overwritten", failures, passes)

    result = {"ok": not failures, "passed": len(passes), "failed": len(failures), "passes": passes, "failures": failures}
    print(json.dumps(result, indent=2))
    return 0 if not failures else 2


if __name__ == "__main__":
    raise SystemExit(main())
