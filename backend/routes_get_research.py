"""Project Laddu GET handlers: research."""
from __future__ import annotations
from routes_get_dependencies import *
from core.forward_progress_service import ForwardProgressService
from core.market_regime_change_service import MarketRegimeChangeService
from core.nse_cash_data_authority_service import NseCashDataAuthorityService
from core.instrument_brand_asset_service import InstrumentBrandAssetService
from core.research_lifecycle_reconciliation_service import ResearchLifecycleReconciliationService
from core.timeframe import storage_interval, public_interval
from core.market_radar_http_projection import project_market_radar_http


def r_research_lifecycle_reconciliation(app, qs, q, mode):
    return ResearchLifecycleReconciliationService(app.store).status()

def r_market_intelligence(app, qs, q, mode):
    symbol = str(qs.get("symbol", [q])[0] or q)
    if not symbol:
        return app.market_intelligence()
    refresh = _flag(qs, "refresh")
    return app.symbol_market_intelligence(symbol, mode if mode != "all" else "delivery", refresh=refresh)

def r_mtf_trend(app, qs, q, mode):
    """Materialized local MTF projection; never recompute technicals on HTTP."""
    from core.technical_snapshot_service import TechnicalSnapshotService
    symbol = str(qs.get("symbol", [q])[0] or q).strip().upper()
    if not symbol:
        return ({"ok": False, "error": "symbol_required"}, 400)
    try:
        instrument = app._index_instrument_for_chart(symbol) or app._first_instrument(symbol)
    except Exception:
        instrument = app._first_instrument(symbol) if hasattr(app, "_first_instrument") else None
    if not instrument:
        return ({"ok": False, "state": "IDENTITY_UNAVAILABLE", "symbol": symbol, "mtf_trend": []}, 404)
    snapshot = TechnicalSnapshotService(app).read(instrument)
    rows = list(snapshot.get("mtf") or [])
    return {
        "ok": bool(rows),
        "state": "READY" if rows else str(snapshot.get("state") or "WARMING"),
        "symbol": symbol,
        "instrument_key": instrument.get("instrument_key"),
        "mtf_trend": rows,
        "mtf": rows,
        "snapshot_id": snapshot.get("snapshot_id"),
        "as_of": snapshot.get("as_of"),
        "freshness": snapshot.get("freshness"),
        "refreshing": snapshot.get("refreshing"),
        "source": snapshot.get("source") or "MATERIALIZED_TECHNICAL_SNAPSHOT",
        "service_version": snapshot.get("read_model_version") or snapshot.get("version"),
        "policy": "MTF HTTP is materialized local read-only; canonical candle/technical producer owns computation.",
    }

def r_decision_ledger(app, qs, q, mode):
    symbol = str(qs.get("symbol", [q])[0] or q)
    mode2 = str(qs.get("mode", [mode if mode != "all" else "delivery"])[0]).lower()
    return app.decision_ledger.latest(symbol, mode2)

def r_canonical_decision(app, qs, q, mode):
    decision_id = str(qs.get("decision_id", qs.get("signal_id", [q]))[0] or q).strip()
    if not decision_id:
        return {"ok": False, "error": "decision_id is required"}, 400
    record = app.store.canonical_decision(decision_id)
    if not record:
        return {"ok": False, "error": "canonical decision not found", "decision_id": decision_id}, 404
    return {
        "ok": True,
        "contract_version": record.get("contract_version") or "canonical-decision-record-1.0.0",
        "decision": record,
        "events": app.store.canonical_decision_events(decision_id),
    }

def r_historical_readiness(app, qs, q, mode):
    instrument_key = str(qs.get("instrument_key", [""])[0] or "").strip()
    symbol = str(qs.get("symbol", [q])[0] or q).strip().upper()
    interval = str(qs.get("interval", ["1d"])[0] or "1d")
    years_raw = qs.get("years", ["10"])[0]
    try:
        years = max(1, min(20, int(years_raw)))
    except (TypeError, ValueError):
        years = 10
    if not instrument_key and symbol:
        resolver = getattr(app, "_first_instrument", None)
        if callable(resolver):
            try:
                instrument = resolver(symbol) or {}
            except TypeError:
                instrument = resolver(symbol) or {}
            instrument_key = str(instrument.get("instrument_key") or "").strip()
    if not instrument_key:
        return {"ok": False, "error": "instrument_key or resolvable symbol is required", "symbol": symbol}, 400
    result = app.store.historical_readiness(instrument_key, interval, years)
    return {"ok": True, "symbol": symbol or None, **result}

def r_research_libraries(app, qs, q, mode):
    refresh = _flag(qs, "refresh")
    caps = app.research_libraries.capabilities(refresh=refresh)
    caps["research_adapter"] = app.research_adapter.available()
    return caps

def r_research_adapter(app, qs, q, mode):
    """Lightweight isolated-research runtime status by default.

    Installed readiness must never launch the research subprocess on the HTTP
    request thread. An explicit ``run=true`` retains the legacy diagnostic run
    for operator use, while the normal route cleanly separates runtime
    availability from model readiness/production influence.
    """
    if not _flag(qs, "run"):
        return app.research_adapter.available()
    from reference_catalog import final_fallback_instrument
    symbol = str(qs.get("symbol", [q or "NIFTY 50"])[0] or "NIFTY 50")
    mode2 = str(qs.get("mode", [mode if mode != "all" else "delivery"])[0]).lower()
    inst = app._first_instrument(symbol) or final_fallback_instrument(symbol) or {
        "trading_symbol": symbol, "symbol": symbol, "exchange": "NSE",
    }
    hist = app.historical_for_symbol(symbol, "day", 180, False, refresh=False)
    return app.research_adapter.run(
        symbol=symbol, mode=mode2, inst=inst, hist=hist,
        candles=hist.get("candles") or [], selected_truth={"manual_run": True},
    )

def r_fundamentals(app, qs, q, mode):
    symbol = str(qs.get("symbol", [q])[0] or q)
    return app.fundamentals_for_symbol(symbol)

def r_market_layers(app, qs, q, mode):
    symbol = str(qs.get("symbol", [q])[0] or q)
    mode2 = mode if mode != "all" else "delivery"
    if not symbol:
        return app.market_intelligence()
    refresh = _flag(qs, "refresh")
    return app.symbol_market_intelligence(symbol, mode2, refresh=refresh)

def r_deep_scan(app, qs, q, mode):
    if _flag(qs, "trigger"):
        request = app.scan_orchestration.request_scan("delivery")
        return {"ok": True, "message": "Delivery scan requested", "request": request, "scanner": app.scanner_status(), "time": now_iso()}
    return app.scanner_status()

def r_refresh(app, qs, q, mode):
    if _flag(qs, "trigger"):
        request = app.scan_orchestration.request_scan("intraday")
        return {"ok": True, "message": "Intraday refresh requested", "request": request, "scanner": app.scanner_status(), "time": now_iso()}
    return app.scanner_status()

def _evidence_regime(app):
    latest = MarketRegimeChangeService(app.store).latest()
    confirmed = str(latest.get("confirmed_regime") or "UNKNOWN").upper()
    if confirmed != "UNKNOWN":
        state = "supportive" if confirmed == "BULL" else "hostile" if confirmed in {"BEAR", "VOLATILE"} else "neutral"
        return {
            "state": state,
            "regime": confirmed,
            "candidate_regime": latest.get("candidate_regime"),
            "transition_state": latest.get("transition_state"),
            "confidence": latest.get("confidence"),
            "change_probability": latest.get("change_probability"),
            "observed_at": latest.get("observed_at"),
            "authority": "POINT_IN_TIME_REGIME_CHANGE_SERVICE",
        }
    rows = app.heatmap_snapshot() or []
    values = []
    for row in rows:
        try:
            if row.get("change_pct") is not None:
                values.append(float(row["change_pct"]))
        except (TypeError, ValueError):
            pass
    mean = (sum(values) / len(values)) if values else 0.0
    state = "supportive" if mean >= 0.15 else "hostile" if mean <= -0.15 else "neutral"
    return {"state": state, "regime": "UNKNOWN", "mean_index_change_pct": round(mean, 2), "index_count": len(values), "authority": "DESCRIPTIVE_FALLBACK"}


def r_regime_change_status(app, qs, q, mode):
    return MarketRegimeChangeService(app.store).status()

def r_market_radar(app, qs, q, mode):
    """Memory-only Market Radar read; compact by default, full on demand."""
    started = time.perf_counter()
    full = str(qs.get("detail", [""])[0] or "").strip().lower() in {"full", "diagnostic", "debug"}
    # Compact route is an atomic memory lookup of a producer-precomputed shape.
    # No lock, repository read, list projection or ranking occurs here.
    if not full:
        compact = getattr(app, "_market_radar_http_snapshot", None)
        if isinstance(compact, dict) and compact:
            result = dict(compact)
            result["route_elapsed_ms"] = round((time.perf_counter() - started) * 1000.0, 3)
            result["cache_only"] = True
            result["_cached_json_bytes"] = getattr(app, "_market_radar_http_bytes", None)
            return result
    try:
        snapshot = dict(getattr(app, "_market_radar_snapshot", {}) or {})
    except Exception:
        snapshot = {}

    if snapshot.get("market_radar") is not None:
        result = project_market_radar_http(snapshot, full=full)
        result["route_elapsed_ms"] = round((time.perf_counter() - started) * 1000.0, 3)
        result["cache_only"] = True
        return result

    # Compatibility/warm-start fallback for tests and older runtime facades:
    # project only the in-memory coverage/heat fields. Never query app.store.
    try:
        current_rows = [dict(row, mode=row.get("mode") or "intraday", radar_source="verified_coverage_memory")
                        for row in (getattr(app, "_coverage_quote_cache", {}) or {}).values()
                        if isinstance(row, dict)]
    except Exception:
        current_rows = []
    try:
        persisted = [dict(row) for row in (getattr(app, "_market_radar_persisted_rows", []) or []) if isinstance(row, dict)]
    except Exception:
        persisted = []
    try:
        heat = [dict(row) for row in (getattr(app, "_heatmap_cache", []) or []) if isinstance(row, dict)]
    except Exception:
        heat = []
    radar = MarketRadarService(max_rows=5).build(current_rows, persisted_rows=persisted, heatmap=heat)
    result = project_market_radar_http({
        "ok": True, "counts": {}, "opportunities": [],
        "market_radar": radar, "heatmap": radar.get("heatmap") or [],
        "projection_state": "ready" if radar.get("coverage") else "warming",
        "time": now_iso(),
    })
    result["route_elapsed_ms"] = round((time.perf_counter() - started) * 1000.0, 3)
    result["cache_only"] = True
    return result

def r_evidence_today(app, qs, q, mode):
    limit = _qint(qs, "limit", 15, min_val=1, max_val=200)
    requested_modes = (normalise_mode(mode),)
    rows = []
    for requested_mode in requested_modes:
        rows += app.store.latest_decisions(requested_mode, limit=300) or []
        try:
            rows += app.store.opportunity_candidates(requested_mode, limit=120) or []
        except Exception:
            pass
    service = EvidenceEngineService(app.store)
    # Evidence scoring used to perform delivery-history work for hundreds of
    # duplicate scanner rows before returning a 15-row desk.  That could exceed
    # the browser timeout and displayed "Evidence desk unavailable".  Keep the
    # strongest current row per symbol and bound enrichment to a useful pool.
    best = {}
    for row in rows:
        symbol = str(row.get("symbol") or row.get("tradingsymbol") or "").upper().strip()
        if not symbol:
            continue
        score = float(row.get("rank_score") or row.get("score") or row.get("priority") or 0)
        key = (symbol, normalise_mode(row.get("mode")))
        old = best.get(key)
        old_score = float((old or {}).get("rank_score") or (old or {}).get("score") or (old or {}).get("priority") or 0)
        if old is None or score > old_score:
            best[key] = row
    rows = sorted(best.values(), key=lambda r: float(r.get("rank_score") or r.get("score") or r.get("priority") or 0), reverse=True)[:60]
    result = service.build_today(rows, delivery_lookup=lambda symbol: app.delivery_context(symbol, record=False), regime=_evidence_regime(app), limit=limit)
    radar_rows = list(best.values())
    def _number(value, default=0.0):
        try: return float(value)
        except (TypeError, ValueError): return default
    def _first_number(row, *keys):
        for key in keys:
            value = _number(row.get(key), None)
            if value is not None:
                return value
        return None
    def _radar_item(row):
        ltp = _first_number(row, "ltp", "current_price", "last_price", "close")
        previous_close = _first_number(row, "previous_close", "prev_close")
        change_pct = _first_number(row, "change_pct", "pChange")
        rupee_change = _first_number(row, "rupee_change", "day_change_abs", "point_change", "change")
        if rupee_change is None and ltp is not None and previous_close is not None:
            rupee_change = ltp - previous_close
        if change_pct is None and ltp is not None and previous_close not in (None, 0):
            change_pct = (ltp / previous_close - 1.0) * 100.0
        return {
            "symbol": str(row.get("symbol") or row.get("tradingsymbol") or "").upper(),
            "mode": row.get("mode") or "delivery",
            "change_pct": None if change_pct is None else round(change_pct, 4),
            "rupee_change": None if rupee_change is None else round(rupee_change, 4),
            "previous_close": previous_close,
            "change_source": row.get("change_source") or ("ltp_plus_previous_close" if previous_close is not None and ltp is not None else "stored_decision"),
            "relative_volume": row.get("recent_volume_vs_base") or row.get("relative_volume") or row.get("rvol"),
            "score": row.get("rank_score") or row.get("score") or row.get("priority"),
            "sector": row.get("sector") or row.get("sector_label") or "Sector pending",
            "ltp": ltp,
        }
    projected_radar_rows = [_radar_item(row) for row in radar_rows]
    result["market_radar"] = {
        "coverage": len(projected_radar_rows),
        "top_gainers": [row for row in sorted(projected_radar_rows, key=lambda row: _number(row.get("change_pct")), reverse=True)[:5] if _number(row.get("change_pct")) > 0],
        "top_losers": [row for row in sorted(projected_radar_rows, key=lambda row: _number(row.get("change_pct")))[:5] if _number(row.get("change_pct")) < 0],
        "volume_shockers": [row for row in sorted(projected_radar_rows, key=lambda row: _number(row.get("relative_volume")), reverse=True)[:5] if _number(row.get("relative_volume")) > 0],
        "intraday_trending": sorted([row for row in projected_radar_rows if str(row.get("mode") or "").lower() == "intraday"], key=lambda row: _number(row.get("score")), reverse=True)[:5],
        "delivery_trending": sorted([row for row in projected_radar_rows if str(row.get("mode") or "").lower() == "delivery"], key=lambda row: _number(row.get("score")), reverse=True)[:5],
    }
    return result

def r_evidence_history(app, qs, q, mode):
    limit = _qint(qs, "limit", 20, min_val=1, max_val=500)
    return EvidenceEngineService(app.store).history(limit=limit)

def r_market_object(app, qs, q, mode):
    projections = getattr(app, "operator_read_models", None)
    if projections is not None:
        result = projections.market_object()
        result["cache_only"] = True
        return result
    return {"ok": True, "market_object": {"state": "warming"}, "projection_state": "warming", "cache_only": False}

def r_learning_health(app, qs, q, mode):
    from core.outcome_learning_service import OutcomeLearningService
    return OutcomeLearningService(app.store).summary()

def r_winrate_diagnostics(app, qs, q, mode):
    projections = getattr(app, "operator_read_models", None)
    if projections is not None:
        result = projections.winrate_diagnostics()
        result["cache_only"] = True
        return result
    from core.win_rate_diagnostics_service import WinRateDiagnosticsService
    rows = app.store.outcome_learning_rows(limit=5000) or []
    result = WinRateDiagnosticsService().analyze(rows)
    result["projection_state"] = "direct_compatibility"
    result["cache_only"] = False
    return result

def r_action_objects(app, qs, q, mode):
    """v61 architecture: ActionObject list -- the intelligence-object read
    model. Same candidate pool and scoring as /api/evidence/today; this route
    only reshapes that result (plus the MarketObject) into the eight-state
    ActionObject contract so the dashboard/cockpit can read one object per
    symbol instead of stitching together components/institutional/fundamental
    fields itself."""
    evidence_today = r_evidence_today(app, qs, q, mode)
    try:
        breadth = app.store.get_latest_market_breadth("NIFTY250_CORE")
    except Exception:
        breadth = None
    try:
        institutional_flow = app.reference_data.institutional_flow_context()
    except Exception:
        institutional_flow = None
    market = build_market_object(
        regime=_evidence_regime(app), breadth=breadth, institutional_flow=institutional_flow
    )
    actions = build_action_objects(evidence_today, market=market)
    result = {
        "ok": True,
        "as_of": evidence_today.get("as_of"),
        "contract_version": "action-object-v1",
        "market_object": market,
        "counts": evidence_today.get("counts"),
        "action_objects": actions,
    }
    try:
        persist_action_objects(app.store, result)
    except Exception:
        pass
    return result

def r_action_objects_history(app, qs, q, mode):
    limit = _qint(qs, "limit", 20, min_val=1, max_val=200)
    return action_object_history(app.store, limit=limit)

def r_action_object_symbol(app, qs, q, mode):
    """v61 architecture: single-symbol ActionObject, enriched with
    DeliveryObject (institutional_signal_service.analyze reshaped into the
    accumulation/distribution/weak-rally/panic-selling classification) and
    FundamentalObject (fundamentals.py score plus quarterly momentum read
    off the same FundamentalStore rows). If the symbol has no current
    evidence-engine candidate row, the delivery/fundamental objects are
    still returned on their own -- they don't depend on the scanner having
    picked the symbol up."""
    symbol = str(qs.get("symbol", [q])[0] or q).upper().strip()
    if not symbol:
        return ({"ok": False, "error": "symbol query parameter required"}, 400)

    payload = r_action_objects(app, qs, q, mode)
    match = next((a for a in payload.get("action_objects") or [] if a.get("symbol") == symbol), None)

    try:
        delivery_ctx = app.delivery_context(symbol, record=False)
    except Exception:
        delivery_ctx = None
    delivery_obj = build_delivery_object(delivery_ctx)

    fundamental_obj = None
    try:
        inst = app._first_instrument(symbol)
        if inst:
            score = app.fundamentals.score(inst)
            rows = app.fundamentals.rows.get(symbol) or []
            fundamental_obj = build_fundamental_object(score, quarterly_rows=rows)
    except Exception:
        fundamental_obj = None

    if match:
        enriched = dict(match)
        enriched["delivery_object"] = delivery_obj
        if fundamental_obj:
            enriched["fundamental_object"] = fundamental_obj
        return {"ok": True, "action_object": enriched, "market_object": payload.get("market_object")}

    return {
        "ok": True,
        "action_object": None,
        "note": "No current evidence-engine candidate row for this symbol; showing delivery/fundamental objects on their own.",
        "delivery_object": delivery_obj,
        "fundamental_object": fundamental_obj,
        "market_object": payload.get("market_object"),
    }

def r_validation_status(app, qs, q, mode):
    model_id = str(qs.get("model_id", [""])[0] or "")
    profile = str(qs.get("profile", [""])[0] or "")
    return WalkForwardValidationService(app.store).status(model_id=model_id, profile=profile)

def r_selection_fairness(app, qs, q, mode):
    desk = str(qs.get("desk", [mode or "all"])[0] or "all").lower()
    persist = _flag(qs, "persist", "false")
    return app.scan_orchestration.selection_fairness_snapshot(desk, persist=persist)

def r_risk_authority(app, qs, q, mode):
    return ProductionRiskAuthorityService(app.store, runtime_status=app.status).status()

def r_capital_readiness(app, qs, q, mode):
    return CapitalReadinessService(app.store, app.status).assess()

def r_engineering_quality(app, qs, q, mode):
    return CapitalReadinessService(app.store, app.status).engineering_quality()

def r_strategy_validation(app, qs, q, mode):
    result = StrategyValidationStatusService(app.store).status()
    validator = getattr(app, "evidence_score_validation", None) or EvidenceScoreValidationService(app.store)
    result["evidence_score_validation"] = validator.status()
    result["primary_selector_state"] = result["evidence_score_validation"].get("overall_state")
    return result

def r_evidence_score_validation(app, qs, q, mode):
    desk = str(qs.get("mode", [mode or ""])[0] or "").lower()
    persist = _flag(qs, "persist", "false")
    if desk in ("intraday", "delivery"):
        validator = getattr(app, "evidence_score_validation", None) or EvidenceScoreValidationService(app.store)
        return validator.validate(desk, persist=persist)
    validator = getattr(app, "evidence_score_validation", None) or EvidenceScoreValidationService(app.store)
    return validator.status()

def r_real_walk_forward(app, qs, q, mode):
    # Compatibility route retained for older clients.  A v42 97-symbol report
    # is legacy/partial evidence and must never be presented as a full-universe
    # production-equivalent result.
    legacy = StrategyValidationStatusService(app.store).legacy_report()
    return {
        "ok": True,
        "state": legacy.get("state"),
        "full_universe": False,
        "production_policy_replay": False,
        "capital_authority": "NONE",
        "scope": legacy.get("scope") or "No legacy report is installed.",
        "report": legacy.get("report"),
        "policy": "Legacy/partial research evidence is not a validated win-rate claim.",
    }


def r_cost_model(app, qs, q, mode):
    return {
        "ok": True,
        "profiles": {
            desk: IndiaCashCostModel.for_mode(desk).round_trip(100, 100, 1)["config"]
            for desk in ("intraday", "delivery")
        },
        "policy": "Desk-specific configurable estimates; not tax advice",
    }

def r_institutional_performance(app, qs, q, mode):
    return InstitutionalOutcomeService(app.store).performance()

def r_ai_governance(app, qs, q, mode):
    return AIGovernanceService(app.store).status()

def r_storage_architecture(app, qs, q, mode):
    service = getattr(app, "storage_architecture_service", None)
    if service is None:
        runtime = getattr(getattr(app, "store", None), "runtime_market_state", None)
        data_plane = getattr(app, "production_data_plane", None)
        service = StorageArchitectureService(
            runtime_market_state=runtime,
            production_data_plane=data_plane,
        )
        app.storage_architecture_service = service
        service.refresh_async()
    return service.status(max_wait_sec=1.5)

def r_binding_mtf_contract(app, qs, q, mode):
    return BindingMtfContractService.status()

def r_canonical_bars(app, qs, q, mode):
    instrument_key = str(qs.get("instrument_key", [""])[0] or "").strip()
    raw_interval = str(qs.get("interval", ["1m"])[0] or "1m").strip()
    interval = storage_interval(raw_interval)
    include_forming = _flag(qs, "include_forming", "true")
    limit = _qint(qs, "limit", 500, min_val=1, max_val=5000)
    runtime = getattr(getattr(app, "store", None), "runtime_market_state", None)
    if runtime is None:
        return ({"ok": False, "error": "runtime_market_state_unavailable"}, 503)
    if not instrument_key:
        return {
            "ok": True,
            "health": runtime.canonical_bar_health(),
            "message": "Provide instrument_key and interval to read canonical bars.",
        }
    bars = runtime.canonical_bars(instrument_key, interval, limit=limit, include_forming=include_forming)
    return {
        "ok": True,
        "contract_version": "canonical-market-bars-1.0.0",
        "instrument_key": instrument_key,
        "interval": interval,
        "public_interval": public_interval(raw_interval),
        "include_forming": include_forming,
        "rows": len(bars),
        "bars": bars,
        "health": runtime.canonical_bar_health(instrument_key),
    }

def r_simulation_robustness(app, qs, q, mode):
    desk = str(qs.get("desk", [mode or "all"])[0] or "all").lower()
    paths = _qint(qs, "paths", 2000, min_val=100, max_val=20000)
    horizon = _qint(qs, "horizon", 100, min_val=1, max_val=1000)
    seed_raw = str(qs.get("seed", [""])[0] or "").strip()
    try:
        seed = int(seed_raw) if seed_raw else None
    except ValueError:
        seed = None
    return SimulationRobustnessService(app.store).status(desk=desk, paths=paths, horizon=horizon, seed=seed)

def r_nse_feature_manifest(app, qs, q, mode):
    return {
        "ok": True, "manifest": feature_manifest(), "manifest_hash": FEATURE_MANIFEST_HASH,
        "production_authority": "UNCHANGED_HEURISTIC_BASELINE",
    }

def r_selection_platform(app, qs, q, mode):
    desk = str(qs.get("desk", [mode or ""])[0] or "").lower().strip()
    if desk not in ("", "intraday", "delivery"):
        return ({"ok": False, "error": "desk must be intraday or delivery"}, 400)
    service = getattr(app, "selection_platform", None)
    if service is None:
        try:
            service = SelectionPlatformService(app.store)
        except Exception as exc:
            return ({
                "ok": False,
                "state": "UNAVAILABLE",
                "error": str(exc),
                "production_authority": "UNCHANGED_HEURISTIC_BASELINE",
            }, 503)
    return service.latest_summary(desk or None)

def r_selection_research_validation(app, qs, q, mode):
    desk = str(qs.get("desk", [mode or ""])[0] or "").lower().strip()
    horizon = str(qs.get("horizon", ["20d" if desk == "delivery" else "session"])[0] or "").lower().strip()
    if desk not in ("intraday", "delivery"):
        return ({"ok": False, "error": "desk must be intraday or delivery"}, 400)
    if not horizon:
        return ({"ok": False, "error": "horizon is required"}, 400)
    try:
        return SelectionResearchValidationService(app.store).report(mode=desk, horizon=horizon)
    except Exception as exc:
        return ({
            "ok": False,
            "state": "UNAVAILABLE",
            "error": str(exc),
            "production_authority": "UNCHANGED_HEURISTIC_BASELINE",
        }, 503)

def r_forward_progress(app, qs, q, mode):
    service = getattr(app, "research_control_projection", None)
    if service is None:
        return {"ok": False, "state": "WARMING", "by_desk": {}, "production_change_allowed": False, "read_model": "RESEARCH_CONTROL_PROJECTION"}
    return service.forward_progress()

def r_forward_evidence_clock(app, qs, q, mode):
    service = getattr(app, "research_control_projection", None)
    if service is None:
        return {"ok": False, "state": "WARMING", "by_desk": {}, "production_ml_influence": 0.0, "broker_authority": "NONE", "read_model": "RESEARCH_CONTROL_PROJECTION"}
    return service.forward_clock()

def r_improvement_review(app, qs, q, mode):
    desk = str(qs.get("desk", [mode or ""])[0] or "").lower().strip()
    if desk not in ("intraday", "delivery"):
        return ({"ok": False, "error": "desk must be intraday or delivery"}, 400)
    default_horizon = "20d" if desk == "delivery" else "session"
    horizon = str(qs.get("horizon", [default_horizon])[0] or "").lower().strip()
    try:
        return ImprovementReviewService(app.store).review(mode=desk, horizon=horizon)
    except ValueError as exc:
        return ({"ok": False, "error": str(exc)}, 400)
    except Exception as exc:
        return ({
            "ok": False,
            "state": "UNAVAILABLE",
            "error": str(exc),
            "automatic_production_mutation": False,
            "production_ml_influence": 0.0,
            "broker_authority": "NONE",
        }, 503)

def r_improvement_proposals(app, qs, q, mode):
    desk = str(qs.get("desk", [""])[0] or "").lower().strip() or None
    if desk not in (None, "intraday", "delivery"):
        return ({"ok": False, "error": "desk must be intraday or delivery"}, 400)
    try:
        limit = int(qs.get("limit", ["50"])[0] or 50)
    except ValueError:
        return ({"ok": False, "error": "limit must be numeric"}, 400)
    try:
        return ImprovementProposalService(app.store).list(mode=desk, limit=limit)
    except Exception as exc:
        return ({"ok": False, "state": "UNAVAILABLE", "error": str(exc),
                 "production_ml_influence": 0.0, "broker_authority": "NONE"}, 503)

def r_forward_evidence_lifecycle(app, qs, q, mode):
    service = getattr(app, "forward_evidence_lifecycle", None)
    if service is None:
        return ({"ok": False, "state": "UNAVAILABLE", "error": "lifecycle service not initialized",
                 "production_ml_influence": 0.0, "broker_authority": "NONE"}, 503)
    return service.status()

def r_selection_walk_forward_replay(app, qs, q, mode):
    desk = str(qs.get("desk", [mode or ""])[0] or "").lower().strip()
    if desk not in ("intraday", "delivery"):
        return ({"ok": False, "error": "desk must be intraday or delivery"}, 400)
    horizon = str(qs.get("horizon", [DEFAULT_HORIZON[desk]])[0] or "").lower().strip()
    top_fraction_raw = str(qs.get("top_fraction", ["0.20"])[0] or "0.20")
    try:
        top_fraction = float(top_fraction_raw)
    except ValueError:
        return ({"ok": False, "error": "top_fraction must be numeric"}, 400)
    if not 0.01 <= top_fraction <= 1.0:
        return ({"ok": False, "error": "top_fraction must be between 0.01 and 1.0"}, 400)
    try:
        return SelectionWalkForwardReplayService(app.store).replay(
            mode=desk, horizon=horizon, top_fraction=top_fraction,
            min_train_days=_qint(qs, "min_train_days", 252, min_val=20),
            test_days=_qint(qs, "test_days", 63, min_val=5),
            max_folds=_qint(qs, "max_folds", 8, min_val=1, max_val=20),
            embargo_days=_qint(qs, "embargo_days", 1, min_val=0, max_val=60),
            min_samples=_qint(qs, "min_samples", 300, min_val=30),
            profile=str(qs.get("profile", ["capital"])[0] or "capital").lower(),
        )
    except ValueError as exc:
        return ({"ok": False, "error": str(exc)}, 400)
    except Exception as exc:
        # P0-05: a bare str(exc) here made an installed capital-WFA 503
        # undiagnosable -- log where it actually happened (app.event already
        # persists to the operator log), without weakening any statistical
        # gate. This is diagnosability only; approved/rejected math is
        # untouched.
        import traceback as _tb
        location = "unknown_location"
        try:
            frames = _tb.extract_tb(exc.__traceback__)
            if frames:
                last = frames[-1]
                location = f"{last.filename.split('/')[-1]}:{last.lineno} in {last.name}"
        except Exception:
            pass
        try:
            app.event("ERROR", "selection_walk_forward_replay", "capital WFA replay failed", {
                "desk": desk, "error": str(exc)[:500], "error_type": type(exc).__name__,
                "error_location": location, "traceback": _tb.format_exc()[-4000:],
            })
        except Exception:
            pass
        return ({"ok": False, "state": "UNAVAILABLE", "error": str(exc),
                 "error_type": type(exc).__name__, "error_location": location}, 503)

def r_calibrated_challenger_status(app, qs, q, mode):
    desk = str(qs.get("desk", [mode or ""])[0] or "").lower().strip()
    if desk not in ("intraday", "delivery"):
        return ({"ok": False, "error": "desk must be intraday or delivery"}, 400)
    horizon = str(qs.get("horizon", [DEFAULT_HORIZON[desk]])[0] or "").lower().strip()
    return NseCalibratedChallengerService(app.store).status(mode=desk, horizon=horizon)

def r_research_maturity(app, qs, q, mode):
    try:
        result = ResearchMaturityStatusService(app.store).status()
        result["market_cycle_and_sector_rotation"] = MarketCycleMaturityService(app).status()
        return result
    except Exception as exc:
        return ({"ok": False, "state": "UNAVAILABLE", "error": str(exc)}, 503)

def r_market_cycle_maturity(app, qs, q, mode):
    try:
        return MarketCycleMaturityService(app).status()
    except Exception as exc:
        return ({"ok": False, "state": "UNAVAILABLE", "error": str(exc)}, 503)

def r_product_maturity(app, qs, q, mode):
    """Cache-only Product Maturity projection from the supervised worker."""
    try:
        projection = dict(app.maturity_projection.snapshot() or {})
        product = dict(projection.get("product") or {})
        if product:
            return {**product, "read_model": "MATURITY_PROJECTION", "projection_age_sec": projection.get("projection_age_sec")}
        return {"ok": False, "state": "WARMING", "maturity_level": 0, "read_model": "MATURITY_PROJECTION"}
    except Exception as exc:
        return ({"ok": False, "state": "UNAVAILABLE", "error": str(exc)}, 503)

def r_level5_forward_maturity(app, qs, q, mode):
    """Cache-only forward maturity; heavy qualification runs in background."""
    try:
        projection = dict(app.maturity_projection.snapshot() or {})
        forward = dict(projection.get("forward_maturity") or {})
        if forward:
            return {**forward, "read_model": "MATURITY_PROJECTION", "projection_age_sec": projection.get("projection_age_sec")}
        return {"ok": False, "state": "WARMING", "level5_ready": False, "read_model": "MATURITY_PROJECTION"}
    except Exception as exc:
        return ({"ok": False, "state": "UNAVAILABLE", "error": str(exc)}, 503)

def r_decision_surface_reconciliation(app, qs, q, mode):
    try:
        return DecisionSurfaceReconciliationService(app).status()
    except Exception as exc:
        return ({"ok": False, "state": "UNAVAILABLE", "error": str(exc)}, 503)

def r_model_learning_audit(app, qs, q, mode):
    try:
        return ModelLearningAuditService(app).status()
    except Exception as exc:
        return ({"ok": False, "state": "UNAVAILABLE", "error": str(exc)}, 503)

def r_operational_evidence_integrity(app, qs, q, mode):
    try:
        return OperationalEvidenceIntegrityService(app).status()
    except Exception as exc:
        return ({"ok": False, "state": "UNAVAILABLE", "error": str(exc)}, 503)

def r_quant_edge_status(app, qs, q, mode):
    try:
        result = QuantResearchOrchestratorService(app.store).status()
    except Exception as exc:
        return ({
            "ok": False,
            "state": "UNAVAILABLE",
            "error": str(exc),
            "selection_edge": "NOT_YET_STATISTICALLY_VALIDATED",
            "production_weight": 0.0,
        }, 503)
    # The analytical/research status is the primary route contract. Automatic
    # paper evaluation is an optional isolated projection and must not turn a
    # valid research status into HTTP 503 when its tables are not present yet.
    try:
        result["paper_activation"] = QuantPaperActivationService(app.store).status()
    except Exception as exc:
        result["paper_activation"] = {
            "ok": False,
            "state": "UNAVAILABLE",
            "reason": str(exc)[:240],
            "paper_weight": 0.0,
            "live_production_weight": 0.0,
            "broker_order_authority": "NONE",
        }
    return result

def r_quant_paper_status(app, qs, q, mode):
    try:
        return QuantPaperActivationService(app.store).status()
    except Exception as exc:
        return ({
            "ok": False,
            "state": "UNAVAILABLE",
            "error": str(exc)[:240],
            "live_production_weight": 0.0,
            "broker_order_authority": "NONE",
        }, 503)

def r_dual_desk_architecture(app, qs, q, mode):
    return DualDeskArchitectureService().status()

def r_active_research_methods(app, qs, q, mode):
    capabilities = app.research_libraries.capabilities(refresh=False)
    return ActiveResearchMethodRegistry().status(capabilities=capabilities)

def r_model_tournament(app, qs, q, mode):
    desk = str(qs.get("desk", [""])[0] or "").lower().strip()
    if desk not in ("", "intraday", "delivery"):
        return ({"ok": False, "error": "desk must be intraday or delivery"}, 400)
    try:
        return ModelTournamentService(app.store).status(mode=desk or None)
    except Exception as exc:
        return ({"ok": False, "state": "UNAVAILABLE", "error": str(exc)[:240]}, 503)

def r_positions(app, qs, q, mode):
    """Compatibility projection of the one canonical PostgreSQL Model Paper ledger."""
    desk = str(qs.get("desk", [mode if mode != "all" else ""])[0] or "").lower().strip()
    if desk not in ("", "intraday", "delivery"):
        return ({"ok": False, "error": "desk must be intraday or delivery"}, 400)
    try:
        service = getattr(app, "model_portfolio", None)
        if service is None:
            return ({"ok": False, "state": "UNAVAILABLE", "error": "canonical Model Paper authority unavailable"}, 503)
        rows = service.open_positions()
        if desk:
            rows = [row for row in rows if str(row.get("mode") or "").lower() == desk]
        return {
            "ok": True, "positions": rows, "count": len(rows),
            "authority": "POSTGRESQL_CANONICAL_MODEL_PAPER",
            "execution_boundary": "AUTOMATIC_MODEL_PAPER_ONLY; BROKER_AUTHORITY_NONE",
        }
    except Exception as exc:
        return ({"ok": False, "state": "UNAVAILABLE", "error": str(exc)[:240]}, 503)

def r_quant_research_plane(app, qs, q, mode):
    # HTTP is projection-only. Governance PostgreSQL fan-out is owned by the
    # supervised ResearchControlProjectionService background lane.
    service = getattr(app, "research_control_projection", None)
    if service is None:
        return {
            "ok": False, "state": "WARMING", "runtime": {},
            "publication_authority": {}, "model_lifecycle": {},
            "production_influence": False, "broker_authority": "NONE",
            "read_model": "RESEARCH_CONTROL_PROJECTION",
        }
    return service.quant_research_plane()

def r_quant_model_governance(app, qs, q, mode):
    return ModelChallengerGovernanceService().status()

def r_counterfactual_learning(app, qs, q, mode):
    service = getattr(app, "counterfactual_learning", None)
    if service is None:
        return {"ok": False, "state": "UNAVAILABLE", "policy": "observation only"}, 503
    return service.summary()

def r_factor_dedup_status(app, qs, q, mode):
    service = getattr(app, "factor_dedup", None)
    if service is None:
        return {"ok": False, "state": "UNAVAILABLE"}, 503
    return service.status()

def r_level5_learning_loop(app, qs, q, mode):
    service = getattr(app, "level5_learning_loop", None)
    if service is None:
        return {"ok": False, "state": "UNAVAILABLE", "broker_authority": "NONE"}, 503
    return service.status()


def r_winrate_controls(app, qs, q, mode):
    ranker = getattr(app, "production_ranker", None)
    counterfactual = getattr(app, "counterfactual_learning", None)
    return {
        "ok": bool(ranker is not None),
        "architecture": "governed-post-score-edge-admission",
        "controls": {
            "calibrated_edge": getattr(getattr(ranker, "calibrated_edge", None), "__class__", type(None)).__name__,
            "execution_quality": getattr(getattr(ranker, "execution_quality", None), "__class__", type(None)).__name__,
            "event_risk": getattr(getattr(ranker, "event_risk", None), "__class__", type(None)).__name__,
            "performance_drift": getattr(getattr(ranker, "drift_guard", None), "__class__", type(None)).__name__,
            "counterfactual_learning": getattr(counterfactual, "__class__", type(None)).__name__,
            "level5_edge_optimizer": getattr(getattr(app, "level5_learning_loop", None), "__class__", type(None)).__name__,
        },
        "counterfactual_summary": counterfactual.summary() if counterfactual is not None else None,
        "level5_learning_loop": getattr(app, "level5_learning_loop", None).status() if getattr(app, "level5_learning_loop", None) is not None else None,
        "policy": "Controls may veto/pause promotion. Level-5 optimization maximizes statistically defensible win rate only after positive post-cost edge gates; no control may create direction, inflate score, grant capital, or mutate production without approval.",
    }



def r_nse_data_authority(app, qs, q, mode):
    """Read-only official NSE cash-market data-source and target coverage authority."""
    from config import DATA_DIR
    return NseCashDataAuthorityService(getattr(app, "store", None), DATA_DIR).cached_status()


def r_instrument_brand_assets(app, qs, q, mode):
    """Return only locally cached, verified and hash-matching instrument logos."""
    from config import DATA_DIR
    raw = str(qs.get("symbols", [q])[0] or q or "")
    symbols = [item.strip().upper() for item in raw.split(",") if item.strip()][:200]
    return InstrumentBrandAssetService(DATA_DIR).status(symbols)

def r_ml_population_qualification(app, qs, q, mode):
    """Current multi-stock point-in-time ML training and influence qualification."""
    return MLPopulationQualificationService(app).status()


def r_decision_lifecycle(app, qs, q, mode):
    """One canonical decision-to-settlement authority for accuracy/performance."""
    from core.decision_lifecycle_read_model_service import DecisionLifecycleReadModelService
    selected_mode = str(qs.get("mode", [mode])[0] or mode or "all").lower()
    if selected_mode not in {"all", "delivery", "intraday"}:
        selected_mode = "all"
    payload = DecisionLifecycleReadModelService(app).status(
        mode=selected_mode,
        limit=_qint(qs, "limit", 5000, min_val=1, max_val=10000),
    )
    return payload if payload.get("ok") is True else (payload, 503)


def r_research_retention(app, qs, q, mode):
    """Cache-only retained Research evidence with coalesced background refresh."""
    from core.background_repair_dispatcher import for_app as repair_dispatcher_for_app
    from core.research_retention_service import ResearchRetentionService
    try:
        retained = dict(app.store.get_kv("research_retention:last", {}) or {})
    except Exception:
        retained = {}
    dispatch = repair_dispatcher_for_app(app).submit(
        "research-retention-refresh", lambda: ResearchRetentionService(app).status()
    )
    if retained:
        return {
            **retained,
            "read_state": "LAST_KNOWN",
            "refreshing": bool(dispatch.accepted or dispatch.state == "COALESCED"),
            "source": "RETAINED_RESEARCH_HIGH_WATER",
        }
    return {
        "ok": False, "state": "WARMING",
        "version": ResearchRetentionService.VERSION,
        "counts": {}, "desk_lineage": {},
        "refreshing": bool(dispatch.accepted or dispatch.state == "COALESCED"),
        "source": "RESEARCH_RETENTION_PENDING",
        "policy": "Foreground retention reads last-known evidence only; cross-plane recount runs in the bounded background lane.",
    }


def r_research_preservation_manifest(app, qs, q, mode):
    from core.research_preservation_manifest_service import ResearchPreservationManifestService
    return ResearchPreservationManifestService(app).status()


def r_evidence_pipeline_status(app, qs, q, mode):
    try:
        return EvidencePipelineStatusService(app).status()
    except Exception as exc:
        return ({"ok": False, "state": "UNAVAILABLE", "error": str(exc)[:400],
                 "production_ml_influence": 0.0, "broker_authority": "NONE"}, 503)
