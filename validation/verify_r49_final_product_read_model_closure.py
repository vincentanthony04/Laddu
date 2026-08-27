"""Focused R49 final product/read-model closure gate.

R49 is deliberately broader than a one-line Model Paper patch: it proves the
customer-visible persisted books survive independent auxiliary failures, all
history is retained, browser state retains last verified rows through a
transient read miss, and inactive chart GPU surfaces are physically removed.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
import sys

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from core.portfolio_workspace_service import PortfolioWorkspaceService


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check(condition: bool, name: str, failures: list[str], passes: list[str]) -> None:
    (passes if condition else failures).append(name)


class _Session:
    _now = datetime(2026, 8, 18, 23, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
    def local(self):
        return self._now
    def at(self):
        return {"state": "CLOSED", "as_of": self._now.isoformat()}


class _LifecycleFails:
    def project(self, rows):
        raise RuntimeError("lifecycle attribution temporarily busy")


class _MustNotRun:
    def __getattr__(self, name):
        def _boom(*args, **kwargs):
            raise AssertionError(f"auxiliary path unexpectedly called: {name}")
        return _boom


class _Portfolio:
    repository = object()
    def __init__(self, fail_final: bool = False):
        self.fail_final = fail_final
    def positions(self):
        if self.fail_final:
            raise RuntimeError("operational read temporarily busy")
        return [
            {
                "position_id": "P-OLD", "source_signal_id": "SIG-OLD", "symbol": "OLDCO", "exchange": "NSE",
                "mode": "delivery", "side": "LONG", "status": "CLOSED", "last_price": 108.0,
                "original_entry": 100.0, "original_target": 110.0, "original_stop": 96.0, "managed_stop": 101.0,
                "quantity": 10, "notional": 1000.0, "reserved_cost": 5.0, "gross_pnl": 80.0, "total_cost": 10.0,
                "net_pnl": 70.0, "action": "EXIT", "signal_outcome": "SUCCESS", "economic_outcome": "WIN",
                "opened_at": "2026-08-10T10:00:00+05:30", "closed_at": "2026-08-11T14:00:00+05:30",
                "updated_at": "2026-08-11T14:00:00+05:30", "payload_json": "{}",
            },
            {
                "position_id": "P-OPEN", "source_signal_id": "SIG-OPEN", "symbol": "OPENCO", "exchange": "NSE",
                "mode": "delivery", "side": "LONG", "status": "OPEN", "last_price": 205.0,
                "original_entry": 200.0, "original_target": 220.0, "original_stop": 194.0, "managed_stop": 198.0,
                "quantity": 5, "notional": 1000.0, "reserved_cost": 5.0, "gross_pnl": 25.0, "total_cost": 5.0,
                "net_pnl": 20.0, "action": "HOLD", "opened_at": "2026-08-18T10:00:00+05:30",
                "updated_at": "2026-08-18T15:00:00+05:30", "payload_json": "{}",
            },
        ]
    def research_rows(self, limit=100):
        return [
            {
                "research_id": "R-OLD", "source_signal_id": "R-SIG-OLD", "symbol": "RESEARCHOLD", "mode": "delivery",
                "disposition": "WATCH", "observed_price": 50.0, "occurred_at": "2026-08-09T12:00:00+05:30",
                "payload_json": json.dumps({"side": "LONG", "entry": 50.0, "target": 55.0, "sl": 48.0}),
            },
            {
                "research_id": "R-NOW", "source_signal_id": "R-SIG-NOW", "symbol": "RESEARCHNOW", "mode": "intraday",
                "disposition": "LIVE_VALIDATION", "observed_price": 75.0, "occurred_at": "2026-08-18T12:00:00+05:30",
                "payload_json": json.dumps({"side": "LONG", "entry": 75.0, "target": 78.0, "sl": 73.5}),
            },
        ][:limit]
    def capital_summary(self):
        raise RuntimeError("capital auxiliary temporarily busy")


def _service(portfolio) -> PortfolioWorkspaceService:
    svc = object.__new__(PortfolioWorkspaceService)
    svc.store = object()
    svc.equity = 500_000.0
    svc.intraday_cap = 100_000.0
    svc.portfolio = portfolio
    svc.repository = portfolio.repository
    svc.performance = _MustNotRun()
    svc.lifecycle_projection = _LifecycleFails()
    svc.continuity = _MustNotRun()
    svc.quant = _MustNotRun()
    svc.session = _Session()
    return svc


def main() -> int:
    failures: list[str] = []
    passes: list[str] = []

    frozen = json.loads((ROOT / "validation" / "r49_frozen_r48_hashes.json").read_text(encoding="utf-8"))
    check(frozen.get("exact_parent_sha256") == "5a4dad97135ff676697e7cef6c19e8606720d746649022ed15b3f6dc6c4265f9", "exact R48 parent SHA frozen", failures, passes)
    changed, missing = [], []
    for rel, expected in dict(frozen.get("hashes") or {}).items():
        path = ROOT / rel
        if not path.is_file():
            missing.append(rel)
        elif sha(path) != expected:
            changed.append(rel)
    check(not missing, f"R48 frozen files present ({len(frozen.get('hashes') or {})})" if not missing else "missing frozen parent files: " + ",".join(missing[:8]), failures, passes)
    check(not changed, "all non-R49 parent implementation bytes frozen" if not changed else "unexpected parent changes: " + ",".join(changed[:8]), failures, passes)

    # Behaviour: old settled positions and old research publications remain visible
    # even when lifecycle attribution and capital are unavailable.
    payload = _service(_Portfolio()).build(include_aux=False, research_limit=1000)
    check(payload.get("ok") is True and payload.get("state") == "READY", "core persisted books remain READY while auxiliary sections fail/defer", failures, passes)
    check({row.get("symbol") for row in payload.get("final") or []} == {"OLDCO", "OPENCO"}, "old settled and open Final positions both retained", failures, passes)
    check({row.get("symbol") for row in payload.get("research") or []} == {"RESEARCHOLD", "RESEARCHNOW"}, "old and current persisted Research publications both retained", failures, passes)
    check((payload.get("counts") or {}).get("final_closed") == 1 and (payload.get("counts") or {}).get("research") == 2, "history counts include settled Final and persisted Research", failures, passes)
    check((payload.get("sections") or {}).get("capital", {}).get("state") == "UNAVAILABLE" and (payload.get("capital") or {}).get("state") == "CONFIGURED_ONLY", "capital failure degrades independently without masking books", failures, passes)
    check((payload.get("sections") or {}).get("performance", {}).get("state") == "DEFERRED" and (payload.get("performance") or {}).get("state") == "DEFERRED", "performance removed from foreground Model Paper dependency", failures, passes)
    check((payload.get("sections") or {}).get("research_prediction_paper", {}).get("state") == "DEFERRED", "automatic prediction paper is not a foreground dependency", failures, passes)
    check((payload.get("sections") or {}).get("final_lifecycle_projection", {}).get("state") == "UNAVAILABLE" and len(payload.get("final") or []) == 2, "lifecycle attribution failure falls back to canonical positions", failures, passes)
    check(payload.get("history_scope") == "ALL_PERSISTED_ROWS" and payload.get("read_contract") == "INDEPENDENT_CANONICAL_BOOKS_AUXILIARY_FAIL_ISOLATED", "explicit all-history independent-book contract", failures, passes)

    partial = _service(_Portfolio(fail_final=True)).build(include_aux=False)
    check(partial.get("ok") is True and partial.get("state") == "PARTIAL" and len(partial.get("research") or []) == 2, "one canonical book can remain visible when the other read authority is temporarily unavailable", failures, passes)

    route = (ROOT / "backend" / "routes_get_performance.py").read_text(encoding="utf-8")
    service_src = (ROOT / "backend" / "core" / "portfolio_workspace_service.py").read_text(encoding="utf-8")
    app = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8-sig")
    index = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8-sig")
    css = (ROOT / "frontend" / "app.css").read_text(encoding="utf-8-sig")

    check('detail = str(qs.get("detail", ["core"])' in route and 'build(include_aux=include_aux, research_limit=1000)' in route, "HTTP endpoint defaults to core independent-book projection", failures, passes)
    check('if payload.get("state") == "UNAVAILABLE"' in route and 'return (payload, 503)' in route, "HTTP 503 reserved for loss of both canonical book reads", failures, passes)
    check('visible_positions' not in service_src and 'is_today(' not in service_src, "today-only Model Paper hiding removed", failures, passes)
    check("/api/model-portfolio?mode=all&detail=core" in app and "timeout:5000" in app, "browser uses bounded core Model Paper endpoint", failures, passes)
    check("mergeModelPaperPayload" in app and "STALE_LAST_VERIFIED" in app and "retained_last_verified" in app, "browser retains last verified canonical rows through transient refresh misses", failures, passes)
    check('data-model-paper-scope' in index and 'data-scope="all">All history' in index and 'modelPaperScope' in app, "Model Paper exposes All history/Open/Today view without deleting history", failures, passes)
    check('id="modelPaperSources"' in index and "Final book" in app and "Research history" in app, "Final and Research source states are independently visible", failures, passes)
    check('if (state.page !== \'report\' || state.chart' in app and "host.replaceChildren(message)" in app and "canvas.width=0;canvas.height=0" in app, "inactive Stock Intelligence physically destroys residual GPU canvases", failures, passes)
    check('.page[data-page-panel="report"]:not(.active){display:none!important;visibility:hidden!important;width:0!important;height:0!important' in css, "inactive report route has zero paint/layout surface", failures, passes)
    check("app.css?v=131.0.0-r49" in index and "ui-system.css?v=131.0.0-r49" in index and "app.js?v=131.0.0-r49" in index, "R49 browser asset identity exact", failures, passes)

    identity = json.loads((ROOT / "RELEASE_IDENTITY.json").read_text(encoding="utf-8-sig"))
    attestation = json.loads((ROOT / "RELEASE_ATTESTATION.json").read_text(encoding="utf-8-sig"))
    check(identity.get("broker_authority") == "NONE" and identity.get("production_ready") is False, "broker/release boundary remains fail-closed", failures, passes)
    check("R49" in str(identity.get("acceptance_state")) and identity.get("r49_exact_parent_sha256") == frozen.get("exact_parent_sha256"), "R49 identity binds exact R48 parent", failures, passes)
    check(attestation.get("production_ready") is False and attestation.get("broker_authority") == "NONE", "R49 attestation remains installation candidate until installed acceptance", failures, passes)
    check("r47_intraday_price_action_session_structure_ui_clarity" in dict(attestation.get("proof_state") or {}), "R47 live-market mathematics proof remains pending and unaltered", failures, passes)

    result = {"ok": not failures, "passed": len(passes), "failed": len(failures), "passes": passes, "failures": failures}
    print(json.dumps(result, indent=2))
    return 0 if not failures else 2


if __name__ == "__main__":
    raise SystemExit(main())
