"""Focused R50 canonical Final Signal authority end-to-end source gate."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from core.dashboard_readmodel_service import DashboardReadModelService
from routes_get_system import _trader_candidate_projection


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check(condition: bool, name: str, failures: list[str], passes: list[str]) -> None:
    (passes if condition else failures).append(name)


class _Repo:
    def active_decisions(self, mode="all", limit=120):
        rows = [
            {
                "decision_id": "DEC-1", "signal_id": "SIG-1", "symbol": "EXACT", "mode": "delivery",
                "entry": 100.0, "t1": 110.0, "sl": 95.0, "generated_at": "2026-08-18T10:00:00+05:30",
                "horizon": "10d", "publication_authority": "MODEL_PAPER", "canonical_state": "CONFIRMED",
                "score": 90,
            },
            {
                "decision_id": "DEC-2", "signal_id": "SIG-2", "symbol": "NO_SYMBOL_JOIN", "mode": "delivery",
                "entry": 200.0, "t1": 220.0, "sl": 190.0, "generated_at": "2026-08-18T11:00:00+05:30",
                "max_holding_period": "1-6 months", "publication_authority": "MODEL_PAPER", "canonical_state": "PREPARED",
                "score": 80,
            },
        ]
        return rows if mode == "all" else [row for row in rows if row["mode"] == mode]


class _Store:
    production_canonical_decision_repository = _Repo()


class _Portfolio:
    def open_positions(self):
        return [
            {
                "position_id": "POS-1", "decision_id": "DEC-1", "source_signal_id": "SIG-1",
                "symbol": "EXACT", "mode": "delivery", "original_entry": 101.0,
                "original_target": 110.0, "original_stop": 95.0, "managed_stop": 98.0,
                "opened_at": "2026-08-18T12:00:00+05:30", "last_price": 105.0,
                "quantity": 10, "net_pnl": 40.0, "action": "HOLD",
            },
            # Same symbol as DEC-2 but unrelated IDs: must NEVER symbol/time join.
            {
                "position_id": "POS-ORPHAN", "decision_id": "UNRELATED", "source_signal_id": "SIG-X",
                "symbol": "NO_SYMBOL_JOIN", "mode": "delivery", "original_entry": 201.0,
                "original_target": 225.0, "original_stop": 188.0, "managed_stop": 191.0,
                "opened_at": "2026-08-18T12:30:00+05:30", "last_price": 205.0,
                "quantity": 5, "net_pnl": 20.0, "action": "HOLD",
            },
            # Canonical signal id deliberately placed in decision_id namespace. Namespaced
            # identity must prevent a cross-field accidental match to DEC-2/SIG-2.
            {
                "position_id": "POS-CROSSFIELD", "decision_id": "SIG-2", "source_signal_id": "OTHER-SIGNAL",
                "symbol": "OTHER", "mode": "delivery", "original_entry": 50.0,
                "original_target": 55.0, "original_stop": 48.0, "managed_stop": 49.0,
                "opened_at": "2026-08-18T13:00:00+05:30", "last_price": 51.0,
            },
        ]


class _Runtime:
    model_portfolio = _Portfolio()


def _service() -> DashboardReadModelService:
    svc = object.__new__(DashboardReadModelService)
    svc.store = _Store()
    svc.runtime = _Runtime()
    svc._last_errors = {}
    svc.record_error = lambda *args, **kwargs: None
    return svc


def main() -> int:
    failures: list[str] = []
    passes: list[str] = []

    frozen = json.loads((ROOT / "validation" / "r50_frozen_r49_hashes.json").read_text(encoding="utf-8"))
    check(frozen.get("exact_parent_sha256") == "36086dd2dd06de2bb3f46162cdf2636a757df8adf364d2250d85e912be7c76ea", "exact R49 parent SHA frozen", failures, passes)
    changed, missing = [], []
    for rel, expected in dict(frozen.get("hashes") or {}).items():
        path = ROOT / rel
        if not path.is_file():
            missing.append(rel)
        elif sha(path) != expected:
            changed.append(rel)
    check(not missing, f"R49 frozen files present ({len(frozen.get('hashes') or {})})" if not missing else "missing frozen parent files: " + ",".join(missing[:8]), failures, passes)
    check(not changed, "all non-R50 parent bytes frozen" if not changed else "unexpected parent changes: " + ",".join(changed[:8]), failures, passes)

    rows = _service()._final_signal_rows("delivery")
    by_join = {str(row.get("final_signal_join_state")): row for row in rows}
    linked = next((row for row in rows if row.get("decision_id") == "DEC-1"), {})
    canonical_only = next((row for row in rows if row.get("decision_id") == "DEC-2"), {})
    orphans = [row for row in rows if row.get("final_signal_join_state") == "ORPHAN_OPEN_POSITION_RECONCILIATION_REQUIRED"]
    check(linked.get("final_signal_join_state") == "EXACT_POSITION_LINKED" and linked.get("position_id") == "POS-1", "exact canonical decision to Model Paper position linkage", failures, passes)
    check(linked.get("entry") == 101.0 and linked.get("target") == 110.0 and linked.get("original_stop") == 95.0 and linked.get("active_stop") == 98.0, "position fill/lifecycle overlays preserve frozen target and stop lineage", failures, passes)
    check(linked.get("holding_period") == "10d" and linked.get("signal_age_seconds") is not None and linked.get("position_age_seconds") is not None, "signal age holding period and position age are independent authorities", failures, passes)
    check(canonical_only.get("final_signal_join_state") == "CANONICAL_SIGNAL_ONLY" and canonical_only.get("position_id") is None, "same symbol and cross-field IDs cannot create a false position join", failures, passes)
    check(canonical_only.get("holding_period") is None, "generic max_holding_period is not promoted to canonical holding period", failures, passes)
    check(len(orphans) == 2 and all(row.get("reconciliation_required") is True for row in orphans), "unlinked open positions remain visible only as reconciliation-required", failures, passes)

    open_row = {
        "decision_id": "DEC-X", "signal_id": "SIG-X", "position_id": "POS-X",
        "final_signal_authority": "POSTGRESQL_CANONICAL_DECISION+MODEL_PAPER_POSITION",
        "symbol": "CROSS", "mode": "delivery", "side": "LONG", "status": "OPEN",
        "entry": 100.0, "target": 110.0, "stop": 95.0, "active_stop": 97.0,
        "captured_price": 105.0, "generated_at": "2026-08-18T10:00:00+05:30",
        "opened_at": "2026-08-18T10:05:00+05:30",
    }
    target_cross = _trader_candidate_projection(open_row, {
        "ltp": 111.0, "freshness_state": "closed_market", "workspace_quote_authority": "TEST",
        "provider_timestamp": "2026-08-19T09:00:00+05:30",
    }, market_open=False)
    stop_cross = _trader_candidate_projection(open_row, {
        "ltp": 96.0, "freshness_state": "closed_market", "workspace_quote_authority": "TEST",
        "provider_timestamp": "2026-08-19T09:00:00+05:30",
    }, market_open=False)
    check(target_cross.get("display_stage") == "RECONCILIATION_REQUIRED" and target_cross.get("display_result") == "TARGET CROSSED", "verified target crossing cannot remain clean ACTIVE/HOLD", failures, passes)
    check(stop_cross.get("display_stage") == "RECONCILIATION_REQUIRED" and stop_cross.get("display_result") == "STOP CROSSED", "verified stop crossing cannot remain clean ACTIVE/HOLD", failures, passes)

    dashboard = (ROOT / "backend/core/dashboard_readmodel_service.py").read_text(encoding="utf-8")
    routes = (ROOT / "backend/routes_get_system.py").read_text(encoding="utf-8")
    quote_projection = (ROOT / "backend/core/decision_quote_projection_service.py").read_text(encoding="utf-8")
    worker = (ROOT / "backend/tools/quant_duckdb_lightgbm_worker.py").read_text(encoding="utf-8")
    coordinator = (ROOT / "backend/core/data_plane/coordinator.py").read_text(encoding="utf-8")
    index_scan = (ROOT / "backend/core/scan_orchestration_coverage.py").read_text(encoding="utf-8")
    runtime = (ROOT / "backend/application_runtime.py").read_text(encoding="utf-8")
    app = (ROOT / "frontend/app.js").read_text(encoding="utf-8-sig")
    index = (ROOT / "frontend/index.html").read_text(encoding="utf-8-sig")
    css = (ROOT / "frontend/ui-system.css").read_text(encoding="utf-8-sig")

    check('lambda: self.store.selected_signals' not in dashboard[dashboard.index('def _final_signal_rows'):dashboard.index('def _build_dashboard_cards_data')], "legacy selected_signals absent from Final authority builder", failures, passes)
    check('f"decision:{decision_id}"' in dashboard and 'f"signal:{signal_id}"' in dashboard, "decision and signal identities use separate exact namespaces", failures, passes)
    check('"final_signals": [dict(d) for d in final_signals[:80]]' in dashboard and '"active_positions": [dict(d) for d in active_positions[:20]]' in dashboard, "dashboard publishes explicit final and open authority projections", failures, passes)
    check('rows(payload?.final_signals).filter(workspaceFinalSignal)' in app and 'payload?.active' not in app[app.index('function workspaceFinalRows'):app.index('function renderWorkspaceFinalSignals')], "browser Final Signals reads only server canonical final_signals", failures, passes)
    check('final_signal_authority' in app[app.index('function workspaceFinalSignal'):app.index('function workspaceSignalScore')], "browser requires final signal authority marker", failures, passes)
    holding_slice = app[app.index('function workspaceHoldingPeriod'):app.index('function workspacePositionAge')]
    check('max_holding_period' not in holding_slice and "'holding_period','target_window','horizon','expected_horizon'" in holding_slice, "browser holding period excludes generic max-hold defaults", failures, passes)
    check('Signal Age' in index and 'Holding Period' in index and 'Position Age' in index and 'Age / Timeline' not in index, "three independent timeline columns replace combined Age/Timeline", failures, passes)
    check('emptyRow(17' in app and 'min-width:1360px' in css, "17-column Final table is explicit and horizontally contained", failures, passes)
    check('"contract_version": "trader-workspace-1.4.0-canonical-final-signal-authority"' in routes and '"final_signals": final_signals' in routes, "workspace API exposes canonical Final contract", failures, passes)
    check('"RECONCILIATION_REQUIRED"' in routes and '"TARGET CROSSED"' in routes and '"STOP CROSSED"' in routes, "verified target/stop crossing fails visibly pending lifecycle settlement", failures, passes)
    check('active_decisions' in quote_projection and 'selected_signals' not in quote_projection, "Model Paper quote admission reads canonical active decisions only", failures, passes)
    check('production_ready = bool(readiness["production_validation_ready"])' in worker and '\n    production_validation_ready = bool(' not in worker, "LightGBM production_validation_ready function is no longer shadowed by a local variable", failures, passes)
    check('role="interactive-read", min_size=2, max_size=8' in coordinator, "interactive PostgreSQL pool has dedicated 2-8 capacity", failures, passes)
    check('_stored_candles(' in index_scan and '_schedule_historical_refresh(' in index_scan and 'YIELDING_TO_HIGHER_PRIORITY' in index_scan and 'max_wait_sec=0.35' in index_scan, "Index Levels is cache-first, exact-gap scheduled and priority-yielding", failures, passes)
    check('"final_signals": []' in runtime and '"active_positions": []' in runtime, "startup read-model contract contains Final and active position books", failures, passes)
    check('app.css?v=131.0.0-r50' in index and 'ui-system.css?v=131.0.0-r50' in index and 'app.js?v=131.0.0-r50' in index, "R50 browser asset cache identity exact", failures, passes)

    identity = json.loads((ROOT / "RELEASE_IDENTITY.json").read_text(encoding="utf-8-sig"))
    attestation = json.loads((ROOT / "RELEASE_ATTESTATION.json").read_text(encoding="utf-8-sig"))
    check(identity.get("version") == "v131.0.0" and "R50" in str(identity.get("acceptance_state")), "R50 remains on v131.0.0 release line", failures, passes)
    check(identity.get("r50_exact_parent_sha256") == frozen.get("exact_parent_sha256"), "R50 identity binds exact R49 parent", failures, passes)
    check(identity.get("broker_authority") == "NONE" and identity.get("production_ready") is False, "broker and production-release boundary remains fail-closed", failures, passes)
    check(attestation.get("production_ready") is False and attestation.get("broker_authority") == "NONE", "R50 attestation remains installation candidate pending installed proof", failures, passes)

    result = {"ok": not failures, "passed": len(passes), "failed": len(failures), "passes": passes, "failures": failures}
    print(json.dumps(result, indent=2))
    return 0 if not failures else 2


if __name__ == "__main__":
    raise SystemExit(main())
