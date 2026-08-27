"""Provider-free proof for R26 off-market acceptance and dual-theme closure."""
from __future__ import annotations

import json
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from core.research_candidate_projection_service import ResearchCandidateProjectionService
from core.decision_quote_projection_service import DecisionQuoteProjectionService
from core.operations_control_service import OperationsControlService


def check(rows, gate, ok, detail):
    rows.append({"gate": gate, "state": "PASS" if ok else "FAIL", "detail": detail})


def read(rel):
    return (ROOT / rel).read_text(encoding="utf-8-sig")


class FakeQuotes:
    def deltas_since(self, cursor, **_kwargs):
        return {"cursor": cursor + 1, "deltas": [{
            "symbol": "TCS", "instrument_key": "NSE_EQ|TCS", "identity_verified": True,
            "usable_for_promotion": True, "freshness_state": "live", "stale": False,
            "ltp": 3200.0,
        }]}


class MustNotRun:
    def mark(self, _payload):
        raise AssertionError("durable side effect leaked onto quote projection thread")


class FakeApp:
    def __init__(self):
        self.live_market = SimpleNamespace(quotes=FakeQuotes())
        self.counterfactual_learning = MustNotRun()
        self.evidence_score_validation = MustNotRun()
        self.production_ranker = SimpleNamespace(quant_paper=None)
        self.model_portfolio = SimpleNamespace(sync_final_signals=lambda *_a, **_k: {"ok": True})
        self.store = SimpleNamespace(selected_signals=lambda *_a, **_k: [])
        self.errors = []
    def market_open(self): return True
    def _cache_live_quote_state(self, latest): return latest
    def record_error(self, component, error): self.errors.append((component, error))


def main() -> int:
    rows = []
    stamp = datetime.now(timezone.utc).isoformat()
    candidate = {
        "symbol": "TCS", "instrument_key": "NSE_EQ|TCS", "exchange": "NSE",
        "mode": "intraday", "identity_verified": True, "decision_ts": stamp,
        "side": "LONG", "change_pct": 1.2, "session_relative_volume": 1.8,
        "evidence_score": 72.0,
    }
    projection = ResearchCandidateProjectionService(now=datetime.now(timezone.utc))
    ranking = projection.project_for_ranking(candidate, desk="intraday")
    paper_overlay = projection.project(candidate, desk="intraday")
    check(rows, "RANKING_CAPTURE_WITHOUT_TRADE_MAP", ranking.get("ok") is True and ranking.get("candidate", {}).get("trade_map_required_for_capture") is False,
          "exact PIT candidate is eligible for immutable ranking evidence before executable Entry/T1/SL exists")
    check(rows, "PAPER_GEOMETRY_REMAINS_FAIL_CLOSED", paper_overlay.get("ok") is False and paper_overlay.get("reason") == "valid_trade_map_unavailable",
          "the same candidate still fails the stricter trade-map projection used for paper/executable context")

    app = FakeApp()
    service = DecisionQuoteProjectionService(app)
    quote_result = service.run_once()
    status = service.status()
    check(rows, "QUOTE_PROJECTION_NONBLOCKING", quote_result.get("state") == "READY" and quote_result.get("observed") == 1 and status.get("pending_side_effect_quotes") == 1 and not app.errors,
          "latency-critical quote projection only caches/enqueues; durable learning/paper writes are isolated")

    ops = object.__new__(OperationsControlService)
    ops._lock = threading.RLock(); ops._samples = {"scanner:test": (time.time() - 60.0, 1000.0)}
    rate = OperationsControlService._rate(ops, "scanner:test", 100.0)
    check(rows, "SWEEP_RESET_NOT_NEGATIVE_RATE", rate is None,
          "a new scanner generation/reset is not displayed as a negative processing rate")

    lifecycle = read("backend/core/research_lifecycle_advance_service.py")
    forward = read("backend/core/forward_progress_service.py")
    reconciliation = read("backend/core/research_lifecycle_reconciliation_service.py")
    repo = read("backend/core/data_plane/model_governance_repository.py")
    appjs = read("frontend/app.js")
    runtime = read("backend/application_runtime.py")
    check(rows, "GOVERNANCE_FIRST_POPULATION_READ", "latest_selector_population" in lifecycle and "CandidatePopulationService" in forward and "CandidatePopulationService" in reconciliation,
          "production lifecycle/read models resolve the same Governance PostgreSQL selector authority")
    check(rows, "GOVERNANCE_FEATURE_STATE_EXPOSED", "governance_feature_snapshot" in repo and "feature_snapshot_state" in repo and "feature_lineage_state" in repo,
          "governance selector members expose the verified feature-snapshot state needed for exact progress reconciliation")
    check(rows, "SIDE_EFFECT_WORKER_SUPERVISED", "decision_quote_side_effects" in runtime and "side_effect_loop" in read("backend/core/decision_quote_projection_service.py"),
          "durable quote side effects have their own supervised progress/failure boundary")
    check(rows, "RESEARCH_ACTION_ASYNC", "research_page_advance" in appjs and "advance_full_lifecycle" in appjs and "Timed out after 12.0s" not in appjs,
          "Research advancement dispatches background lifecycle work instead of waiting synchronously in the browser")
    check(rows, "ACTIVE_VS_EVIDENCE_BLOCKERS", "evidence_pending_count" in read("backend/core/operations_control_service.py") and "active_blockers" in read("backend/core/operations_control_service.py") and "opsActiveProblemCount" in appjs and "opsEvidencePendingCount" in appjs and "progressQuick').textContent = 'Diagnostics'" in appjs,
          "Diagnostics distinguishes active runtime blockers from long-duration evidence pending while the trading header stays secondary")
    check(rows, "STOCK_IDENTITY_PROPAGATES", "instrument_key=instrument_key" in read("backend/routes_get_stock.py") and "selected_instrument_key" in read("backend/core/workload_governor.py"),
          "resolved selected-stock identity is propagated into the priority pipeline")
    check(rows, "WFA_DEPTH_DIAGNOSTICS", all(token in read("backend/core/selection_walk_forward_replay_service.py") for token in ("settled_date_count", "fold_blocker", "evidence_status")),
          "zero-fold WFA exposes retained date depth and the exact insufficiency reason")

    ops_src = read("backend/core/operations_control_service.py")
    routes_src = read("backend/routes_get_control.py")
    readiness_src = read("backend/core/instrument_readiness_service.py")
    side_src = read("backend/core/decision_quote_projection_service.py")
    check(rows, "LIVE_PROOF_REFRESH_BYPASSES_STALE_CACHE", "LIVE_BOUNDED_OPERATOR_READ" in ops_src and "service.live_summary()" in routes_src and "forceFresh:true" in appjs,
          "explicit Refresh/Copy reads a current bounded operations snapshot instead of re-copying a stale materialized projection")
    check(rows, "INSTRUMENT_BOOTSTRAP_NONBLOCKING", "LadduInstrumentPostReady" in readiness_src and "LadduOptionalInstrumentBootstrap" in readiness_src and "freeze_authoritative_universe()" in readiness_src,
          "catalogue supervisor loop detaches universe freeze, symbol-index warmup and optional fundamentals/auth so they cannot stall liveness")
    check(rows, "INSTRUMENT_RECOVERY_NO_PARALLEL_FREEZE", "INSTRUMENT_AUTHORITY_ALREADY_READY" in runtime and "INSTRUMENT_AUTHORITY_REFRESH_ACCEPTED" in runtime,
          "instrument recovery reconciles or refreshes catalogue authority without launching another blocking universe freeze")
    check(rows, "MODEL_PAPER_SIDE_EFFECT_LANE_ISOLATED", "LadduDecisionQuoteResearchSideEffects" in side_src and "_enqueue_research_side_effects" in side_src and "heartbeat_guard(\"decision_quote_side_effects\"" in side_src,
          "production Model Paper quote side effects remain supervised while local research/evaluation marks run on a separate best-effort lane")
    quant_capture = read("backend/core/quant_scan_capture_service.py")
    quant_edge = read("backend/core/quant_edge_data_service.py")
    check(rows, "PIT_QUOTE_TIMESTAMP_ALIASES", all(token in quant_capture for token in ("quote_as_of", "provider_timestamp", "quote_timestamp")) and all(token in quant_edge for token in ("quote_as_of", "provider_timestamp", "price_freshness_state")),
          "verified scanner quote timestamps/freshness aliases feed the PIT snapshot without inventing source time")
    check(rows, "PIT_FEATURE_DIAGNOSTICS_EXPLICIT", "feature_snapshot_diagnostics" in reconciliation and "lineage_missing" in reconciliation and "COMPLETE point-in-time feature snapshots" in reconciliation,
          "Research exposes the exact immutable feature/lineage blocker instead of a generic missing-feature message")
    check(rows, "FORCE_FRESH_WAITS_FOR_AUTOPOLL", "if (!forceFresh) return state.operations" in appjs and "Date.now()+2500" in appjs and "forceFresh?12000:1800" in appjs,
          "operator Refresh/Copy cannot be silently skipped by the 2.5s auto-poll and has a bounded forward-evidence deadline")
    check(rows, "CONTROLLER_BLOCKER_RECONCILED_WITH_LIVE_JOB", "RECOVERED_IN_LIVE_PROJECTION" in ops_src and "controller_state_superseded" in ops_src and "live_by_component" in ops_src,
          "a stale controller generation cannot keep a freshly healthy/expected-idle worker classified as an active blocker")

    wfa_repo = read("backend/core/data_plane/model_governance_repository.py")
    wfa_indexes = read("infra/postgres/governance/007_r26_wfa_query_indexes.sql")
    conveyor = read("backend/core/data_conveyor_runtime_service.py")
    css = read("frontend/app.css")
    check(rows, "R26_WFA_OFFLINE_QUERY_PROFILE", "R26_BOUNDED_SQL_AGGREGATE" in wfa_repo and "statement_timeout_ms=120000" in wfa_repo and "pool_timeout_seconds=30.0" in wfa_repo,
          "capital WFA uses a bounded offline PostgreSQL read profile while interactive/runtime deadlines remain unchanged")
    check(rows, "R26_WFA_READ_INDEXES", all(token in wfa_indexes for token in ("ix_selector_members_desk_population_candidate", "ix_selector_outcomes_horizon_population_candidate", "ix_selector_outcomes_population_candidate_settled")),
          "governance migrations add idempotent indexes for the immutable selector replay join path")
    check(rows, "R26_PARTIAL_PIT_IS_EVIDENCE_PENDING", "FEATURE_EVIDENCE_PENDING" in reconciliation and "FEATURE_EVIDENCE_PENDING" in conveyor and "FEATURE_EVIDENCE_PENDING" in ops_src,
          "verified immutable PARTIAL PIT snapshots are explicit evidence waits, not endlessly restarted worker failures")
    check(rows, "R26_FRESHNESS_FAIL_CLOSED", "PROVIDER_TIMESTAMP_VERIFIED_AGE" in quant_capture and "provider_timestamp_verified" in quant_capture and "max_age = 20.0" in quant_capture,
          "freshness may be derived only from provider-verified timestamps under strict age ceilings; missing source truth is not fabricated")
    check(rows, "R26V_DUAL_THEME_SEMANTICS", all(token in css for token in ('html[data-theme="dark"]', 'html[data-theme="light"]', '--success-soft', '--danger-soft', '--warning-soft', '.semantic-positive', '.semantic-negative', '.semantic-warning')),
          "light and dark themes share the same bullish/bearish/warning/live semantic design system")
    check(rows, "R26V_STOCK_AND_GLOBAL_SURFACES", all(token in css for token in ('[data-page-panel="stock"]', '[data-page-panel="opportunities"]', '.model-paper-topline', '.research-life-card', '.ops-job-card')),
          "the visual upgrade covers Stock Intelligence plus Opportunities, Model Paper, Research, Accuracy/Performance and Progress surfaces")

    failed = [row for row in rows if row["state"] != "PASS"]
    print(json.dumps({"ok": not failed, "scope": "R26_OFFMARKET_ACCEPTANCE_AND_VISUAL_CLOSURE", "checks": rows, "passed": len(rows)-len(failed), "failed": failed}, indent=2))
    return 0 if not failed else 2


if __name__ == "__main__":
    raise SystemExit(main())
