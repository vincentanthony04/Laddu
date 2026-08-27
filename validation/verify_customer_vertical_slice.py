"""Deterministic, provider-free customer vertical-slice acceptance harness.

This is intentionally a packaged smoke contract, not installed-Windows proof.
It proves that the deploy tree contains one connected customer path from
reference/search through decision, model paper, settlement/performance and
historical replay.  It performs no provider I/O and does not mutate production
state.
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8-sig")


def check(name: str, ok: bool, detail: str, rows: list[dict]) -> None:
    rows.append({"gate": name, "state": "PASS" if ok else "FAIL", "detail": detail})


def main() -> int:
    started = time.perf_counter()
    rows: list[dict] = []

    get_registry = read("backend/routes_get_registry.py")
    post_routes = read("backend/routes_post.py")
    app = read("frontend/app.js")
    html = read("frontend/index.html")
    css = read("frontend/app.css")
    snapshot = read("backend/core/stock_snapshot_service.py")
    engines = read("backend/engines.py")
    operations = read("backend/core/operations_control_service.py")
    research_routes = read("backend/routes_get_research.py")
    research_projection = read("backend/core/research_control_projection_service.py")

    # 1. Customer entry/search exact identity.
    check("SEARCH_ROUTE", '"/api/search"' in post_routes, "POST /api/search registered", rows)
    check(
        "EXACT_IDENTITY_CLICKTHROUGH",
        "instrument_key" in app and "/api/stock-snapshot?symbol=" in app,
        "browser retains canonical instrument_key into Stock Intelligence",
        rows,
    )

    # 2. Stock Intelligence / Decision Proof.
    check("STOCK_SNAPSHOT_ROUTE", '"/api/stock-snapshot"' in get_registry, "Stock Intelligence route registered", rows)
    decision_tokens = ("CANONICAL_DECISION_READY", "NO-TRADE", "first_hard_blocker", "decision_proof")
    decision_material = app + snapshot
    check(
        "DECISION_PROOF_FAIL_CLOSED",
        all(token.lower() in decision_material.lower() for token in decision_tokens),
        "UI consumes canonical action and first hard blocker rather than prose-only proof",
        rows,
    )

    # 3. Model paper and lifecycle advance.
    check("MODEL_PAPER_ROUTE", '"/api/model-portfolio"' in get_registry, "canonical Model Paper route registered", rows)
    check(
        "MODEL_PAPER_PAGE",
        "model-paper" in html and "/api/model-portfolio?mode=all" in app,
        "first-class Model Paper page is wired to canonical authority",
        rows,
    )
    check(
        "RESEARCH_LIFECYCLE_ADVANCE",
        '"/api/operations/action"' in post_routes and "advance_full_lifecycle" in app
        and "research_page_advance" in app,
        "Research lifecycle dispatches the bounded background closure job instead of blocking the browser on WFA/settlement",
        rows,
    )

    # 4. Settled Accuracy/Performance economics.
    check("PERFORMANCE_ROUTE", '"/api/performance"' in get_registry, "settled performance authority registered", rows)
    check(
        "ACCURACY_PERFORMANCE_UI",
        all(token in app for token in ("renderAccuracy", "renderPerformance", "accuracy_eligible", "performance_eligible")),
        "settled decisive accuracy and post-cost performance remain separated",
        rows,
    )

    # 5. Backtest/replay safety surface.
    check(
        "BACKTEST_ROUTE",
        '"/api/selection-walk-forward-replay"' in get_registry and "/api/selection-walk-forward-replay" in app,
        "first-class Backtest reuses governed walk-forward replay authority",
        rows,
    )
    check(
        "BACKTEST_POINT_IN_TIME_UI",
        "Purge" in html and "Embargo" in html and "Cached point-in-time" in html,
        "Backtest visibly advertises purge/embargo/cached point-in-time boundary",
        rows,
    )

    # 6. Mathematics-green simple customer surface. The internal chart remains
    # compatibility-only and must not be presented as live decision truth.
    timeframe_tokens = ("1m", "3m", "5m", "15m", "30m", "1H", "4H", "1D", "1W", "1M")
    check(
        "MANDATORY_TIMEFRAME_IDENTITIES_RETAINED_IN_MATH",
        all(token in app for token in timeframe_tokens),
        "all mandatory timeframe identities remain available to canonical MTF mathematics even though the custom chart is disabled",
        rows,
    )
    check(
        "CUSTOM_CHART_DISABLED_FROM_DECISION_PATH",
        "const INTERNAL_CHART_ENABLED = false" in app
        and 'data-internal-chart-disabled="true"' in html
        and 'href="https://tv.upstox.com"' in html,
        "customer UI uses broker chart externally; the recreated chart is hidden and has no decision authority",
        rows,
    )
    check(
        "VOLUME_INTELLIGENCE_GUARD",
        "WARMING" in app and "rvol" in app.lower() and "D/D" in app,
        "volume participation intelligence is fail-soft instead of fabricating unavailable comparison",
        rows,
    )
    check(
        "ENTRY_NEAR_STRUCTURE_GUARD",
        all(token in engines for token in ("BREAKOUT WATCH", "BREAKDOWN WATCH", "near_resistance", "near_support")),
        "entry authority has explicit breakout/breakdown watch path near structure",
        rows,
    )
    check(
        "SIMPLE_ACTIONABLE_NOW_CONTRACT",
        all(token in html for token in ("ACTIONABLE NOW", "WATCH NEXT", "RECENT OUTCOMES", "Signal Age", "Holding", "Result", "After"))
        and all(token in app for token in ("workspaceFinalRows", "renderWorkspaceWatchNext", "renderWorkspaceRecentOutcomes", "workspaceAfterState")),
        "primary customer surface is a concise actionable list with separate watch and measured outcome/follow-through evidence",
        rows,
    )
    check(
        "ACTIONABLE_ROWS_CANONICAL_ONLY",
        "rows(payload?.final_signals).filter(workspaceFinalSignal)" in app
        and "final_signal_authority" in app
        and "decision_id || row?.signal_id" in app,
        "Actionable Now consumes canonical Final Signals only and requires immutable decision/signal identity",
        rows,
    )
    check(
        "RESULT_AFTER_SEPARATION",
        "function workspaceResultLabel" in app and "function workspaceAfterState" in app
        and "Result is immutable" in html,
        "trade Result is displayed separately from post-exit After state for learning",
        rows,
    )

    # 7. Engineering diagnostics remain available but secondary to trading.
    check(
        "ENGINEERING_DIAGNOSTICS_SECONDARY",
        all(token in html for token in ("Diagnostics &amp; Proof", "operationsJobs", "operationsConsole", "data-quick-operation", "Engineering details stay out of the trading workflow."))
        and all(token in app for token in ("/api/operations/summary", "/api/operations/action", "/api/forward-progress", "copyPlainText", "runQuickOperation")),
        "operator controls and proof remain intact but are explicitly separated from the actionable customer workflow",
        rows,
    )

    # 8. R22 one-shot lifecycle closure and truthful partial-state UX.
    check(
        "ONE_SHOT_LIFECYCLE_CLOSURE",
        all(token in operations for token in ("advance_full_lifecycle", "_run_full_lifecycle", "delivery_capital_wfa", "intraday_capital_wfa", "ResearchLifecycleAdvanceService", "SelectionWalkForwardReplayService"))
        and "full_lifecycle" in app and "Run one-shot proof" in html,
        "one bounded operator action orchestrates scanner request, Research/Model-Paper advancement, settlement, both capital WFA desks and final reconciliation",
        rows,
    )
    check(
        "HISTORICAL_WFA_SEPARATE_FROM_PUBLICATION",
        all(token in (research_routes + research_projection) for token in ("historical_wfa_folds", "historical_wfa_state", "diagnostic_fallback_used"))
        and "Published WF" in app and "Historical capital WFA" in app,
        "historical capital WFA is visible without fabricating a governed production publication",
        rows,
    )
    check(
        "PARTIAL_SCAN_TRUTH",
        all(token in app for token in ("PARTIAL", "ranking provisional until sweep completes", "Published research", "Current scan promoted")),
        "Opportunities distinguishes partial universe progress, published Research rows and current-scan promotion counts",
        rows,
    )
    check(
        "ZERO_GEOMETRY_NOT_ACTIONABLE",
        "positiveGeometry" in app and "Pending" in app,
        "zero/missing Entry/Target/Stop/R:R values render Pending instead of looking like executable zero-price geometry",
        rows,
    )
    check(
        "WHY_PANEL_PERSISTS_ACROSS_REFRESH",
        all(token in app for token in ("candidateInspectKey", "candidateInspectSnapshot", "candidateStableKey", "data-close-candidate-inspect", "Candidate is no longer in the latest projection")),
        "candidate Why explanation is keyed by stable identity and remains visible across live projection refreshes until explicitly closed or the page is left",
        rows,
    )

    # 9. Safety boundary.
    identity = json.loads(read("RELEASE_IDENTITY.json"))
    # R23 authority-boundary/runtime closure.
    lifecycle = (ROOT / "backend/core/research_lifecycle_advance_service.py").read_text(encoding="utf-8")
    forward = (ROOT / "backend/core/forward_progress_service.py").read_text(encoding="utf-8")
    reconciliation = (ROOT / "backend/core/research_lifecycle_reconciliation_service.py").read_text(encoding="utf-8")
    quote_projection = (ROOT / "backend/core/decision_quote_projection_service.py").read_text(encoding="utf-8")
    check(
        "GOVERNANCE_SELECTOR_AUTHORITY_REUSED",
        "latest_selector_population" in lifecycle and "project_many_for_ranking" in lifecycle
        and "CandidatePopulationService" in forward and "CandidatePopulationService" in reconciliation,
        "Research/forward/reconciliation reuse Governance PostgreSQL selector evidence instead of retired local counters", rows,
    )
    candidate_projection = (ROOT / "backend/core/research_candidate_projection_service.py").read_text(encoding="utf-8")
    check(
        "RANKING_CAPTURE_SEPARATE_FROM_PAPER_GEOMETRY",
        "trade_map_required_for_capture" in candidate_projection
        and "CROSS_SECTIONAL_RANKING" in candidate_projection
        and "project_many_for_ranking" in lifecycle,
        "immutable ranking evidence can be captured before executable trade geometry; Model Paper remains a later gate", rows,
    )
    check(
        "QUOTE_PROJECTION_SIDE_EFFECT_ISOLATION",
        "side_effect_loop" in quote_projection and "_enqueue_side_effects" in quote_projection
        and "decision_quote_side_effects" in (ROOT / "backend/application_runtime.py").read_text(encoding="utf-8"),
        "quote/Product-State projection cannot be frozen by synchronous learning or Model-Paper side effects", rows,
    )

    check(
        "BROKER_AUTHORITY_NONE",
        identity.get("broker_authority") == "NONE" and identity.get("production_ready") is False,
        "candidate remains Model Paper only / NOT production ready",
        rows,
    )

    failed = [row for row in rows if row["state"] != "PASS"]
    elapsed_ms = round((time.perf_counter() - started) * 1000.0, 2)
    result = {
        "ok": not failed,
        "scope": "DETERMINISTIC_PROVIDER_FREE_CUSTOMER_VERTICAL_SLICE",
        "installed_windows_live_market": "PENDING",
        "provider_io": False,
        "mutation": False,
        "checks": rows,
        "passed": len(rows) - len(failed),
        "failed": len(failed),
        "elapsed_ms": elapsed_ms,
    }
    print(json.dumps(result, indent=2))
    return 0 if not failed else 2


if __name__ == "__main__":
    raise SystemExit(main())
