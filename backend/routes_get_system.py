"""Project Laddu GET handlers: system."""
from __future__ import annotations
import math
import os
import secrets
from routes_get_dependencies import *

_PROCESS_BOOT_ID = secrets.token_hex(16)
_PROCESS_STARTED_AT = now_iso()
def r_ready(app, qs, q, mode):
    """Ultra-light process-readiness probe.
    This endpoint deliberately performs no database, broker, token or worker
    inspection.  Windows service scripts use it only to prove that the newly
    deployed Python process is listening and serving the expected build.
    Operational detail remains available from the cache-only pipeline/risk
    endpoints after readiness is established.
    """
    return {
        "ok": True,
        "ready": True,
        "version": APP_VERSION,
        "build_marker": BUILD_MARKER,
        "time": now_iso(),
        "process_id": os.getpid(),
        "process_boot_id": _PROCESS_BOOT_ID,
        "process_started_at": _PROCESS_STARTED_AT,
        "probe": "process_memory_only",
        "product_mode": PRODUCT_MODE,
        "broker_order_execution": BROKER_ORDER_EXECUTION_ENABLED,
        "execution_boundary": "automatic_paper_simulation_no_broker_orders",
        "frontend_identity_endpoint": "/api/frontend-identity",
    }
def r_frontend_identity(app, qs, q, mode):
    """Attest that the served frontend is the exact frontend paired with this runtime."""
    identity_path = FRONTEND_DIR / "release-identity.json"
    expected_owner = f"standalone-{APP_VERSION}"
    mismatches = []
    manifest = {}
    try:
        manifest = json.loads(identity_path.read_text(encoding="utf-8"))
    except Exception as exc:
        mismatches.append(f"IDENTITY_MANIFEST_UNREADABLE:{type(exc).__name__}")

    declared_assets = dict(manifest.get("assets") or {})
    actual_assets = {}
    for relative, declared in declared_assets.items():
        safe_relative = str(relative).replace("\\", "/").lstrip("/")
        target = (FRONTEND_DIR / safe_relative).resolve()
        try:
            target.relative_to(FRONTEND_DIR.resolve())
        except ValueError:
            mismatches.append(f"ASSET_PATH_OUTSIDE_FRONTEND:{safe_relative}")
            continue
        if not target.is_file():
            mismatches.append(f"ASSET_MISSING:{safe_relative}")
            continue
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        actual_assets[safe_relative] = digest
        if str(declared).lower() != digest.lower():
            mismatches.append(f"ASSET_HASH_MISMATCH:{safe_relative}")

    manifest_version = str(manifest.get("version") or "")
    frontend_owner = str(manifest.get("frontend_owner") or "")
    if manifest_version != APP_VERSION:
        mismatches.append(f"VERSION_MISMATCH:{manifest_version or 'missing'}")
    manifest_build_marker = str(manifest.get("build_marker") or "")
    if manifest_build_marker != BUILD_MARKER:
        mismatches.append(f"BUILD_MARKER_MISMATCH:{manifest_build_marker or 'missing'}")
    if frontend_owner != expected_owner:
        mismatches.append(f"OWNER_MISMATCH:{frontend_owner or 'missing'}")
    for required in ("index.html", "app.css", "app.js", "assets/lightweight-charts.js"):
        if required not in declared_assets:
            mismatches.append(f"ASSET_NOT_DECLARED:{required}")

    return {
        "ok": not mismatches,
        "version": APP_VERSION,
        "manifest_version": manifest_version,
        "frontend_owner": frontend_owner or expected_owner,
        "expected_frontend_owner": expected_owner,
        "build_marker": manifest_build_marker,
        "expected_build_marker": BUILD_MARKER,
        "declared_assets": declared_assets,
        "assets": actual_assets,
        "mismatches": mismatches,
        "time": now_iso(),
    }


def r_architecture(app, qs, q, mode):
    """Structured authority proof; never inferred from rendered frontend text."""
    data_plane = getattr(app, "production_data_plane", None)
    settings = getattr(data_plane, "settings", None)
    production = getattr(settings, "mode", "") == "production"
    repository = getattr(app, "universe_authority_repository", None)
    authority = {"ok": False, "authority": "POSTGRESQL", "reason": "PRODUCTION_AUTHORITY_NOT_ACTIVE"}
    if production and repository is not None:
        try:
            authority = repository.authority_status()
        except Exception as exc:
            authority = {"ok": False, "authority": "POSTGRESQL", "reason": f"{type(exc).__name__}: {exc}"[:240]}
    runtime = getattr(getattr(app, "store", None), "runtime_market_state", None)
    try:
        runtime_health = dict(runtime.canonical_bar_health() or {}) if runtime is not None else {}
    except Exception as exc:
        runtime_health = {"ok": False, "error": str(exc)}
    storage_engine = str(runtime_health.get("storage_engine") or "unavailable")
    universe_proof = dict(getattr(app, "status", {}).get("universe_authority") or {})
    compatibility_runtime_dependency = storage_engine != "in_process_memory"
    return {
        "ok": bool(production and authority.get("ok") and not compatibility_runtime_dependency),
        "version": APP_VERSION,
        "product_mode": PRODUCT_MODE,
        "broker_authority": "NONE",
        "frontend_owner": f"standalone-{APP_VERSION}",
        "dom_derived_authority": False,
        "inactive_page_polling": False,
        "production_data_plane": production,
        "compatibility_runtime_dependency": compatibility_runtime_dependency,
        "runtime_authority": runtime_health,
        "universe_authority": universe_proof,
        "universe_rule": "one-isin-one-security-nse-first-bse-only-fallback",
        "incremental_data_rule": "requested-range-minus-verified-local-coverage",
        "scanner_rule": "immutable-snapshot-one-terminal-state-per-security",
        "authority": authority,
        "time": now_iso(),
    }

def r_health(app, qs, q, mode):
    return app.health()

def r_system_health(app, qs, q, mode):
    return app.system_health()

def r_product_readiness(app, qs, q, mode):
    """Failure-first installed-product readiness; cache-only."""
    return app.product_readiness()


def r_product_state(app, qs, q, mode):
    """One immutable cache-only product-state envelope for every operator surface."""
    service = getattr(app, "product_state_envelope", None)
    if service is None:
        return ({"ok": False, "state": "UNAVAILABLE", "error": "product state envelope unavailable"}, 503)
    return service.snapshot()

def r_first_useful_mode_status(app, qs, q, mode):
    size_raw = qs.get("cohort_size", [96])[0]
    try:
        size = int(size_raw or 96)
    except (TypeError, ValueError):
        size = 96
    return app.first_useful_mode.status(size)

def r_pipeline_health(app, qs, q, mode):
    """Compact operator diagnosis used by the Windows status script."""
    try:
        reader = getattr(app, "health_status_snapshot", None)
        if callable(reader):
            status = reader()
        else:
            legacy_reader = getattr(app, "snapshot_status", None)
            status = legacy_reader() if callable(legacy_reader) else dict(getattr(app, "status", {}) or {})
    except Exception:
        status = dict(getattr(app, "status", {}) or {})
    fast = dict(status.get("fast_lane") or {})
    if not fast or not any(fast.get(key) is not None for key in ("scanned", "promoted", "data_missing", "below_threshold")):
        modes = status.get("mode_scanners") or {}
        intraday = modes.get("intraday") or {}
        fast = dict(intraday)
    scanned = int(fast.get("scanned") or fast.get("deep_scanned") or 0)
    promoted = int(fast.get("promoted") or 0)
    data_missing = int(fast.get("data_missing") or 0)
    below_threshold = int(fast.get("below_threshold") or 0)
    diagnosis = str(fast.get("diagnosis") or (
        "market_closed" if str(fast.get("state") or "").lower() == "market_closed" else
        "no_data" if scanned == 0 and data_missing > 0 else
        "no_qualifying_setups" if promoted == 0 and below_threshold > 0 else
        "ok" if promoted > 0 else "warming"
    ))
    # Operator health must remain cache-only. A previous implementation read
    # signal_ledger here, so restart/status could also inherit SQLite lock waits.
    try:
        cache = getattr(getattr(app, "dashboard", None), "_cards_cache", {}) or {}
        cards = cache.get("all") or next(iter(cache.values()), {})
        selected_count = len(cards.get("active_positions") or cards.get("selected") or [])
    except Exception:
        selected_count = 0
    explanation = {
        "market_closed": "Live Intraday promotion is paused outside NSE hours; Delivery and evidence hydration continue.",
        "no_data": "The scanner could not obtain enough verified market evidence in the latest pass.",
        "no_qualifying_setups": "Verified data was analysed, but no setup passed the current promotion thresholds.",
        "ok": "The scanner is producing promoted signals.",
        "warming": "The scanner and universe coverage are warming up.",
    }.get(diagnosis, str(fast.get("message") or "Pipeline status available."))
    return {
        "ok": True,
        "fast_lane": {**fast, "scanned": scanned, "promoted": promoted, "data_missing": data_missing, "below_threshold": below_threshold, "diagnosis": diagnosis},
        "selected_count_now": selected_count,
        "explanation": explanation,
        "market_radar": (lambda radar_status, radar_snapshot: {
            "state": str(radar_status.get("state") or radar_snapshot.get("projection_state") or "warming"),
            "coverage": int(radar_status.get("coverage") or ((radar_snapshot.get("market_radar") or {}).get("coverage") or 0)),
            "verified_coverage": int(radar_status.get("verified_coverage") or ((radar_snapshot.get("market_radar") or {}).get("verified_coverage") or 0)),
            "change_ready": int(radar_status.get("change_ready") or 0),
            "volume_ready": int(radar_status.get("volume_ready") or 0),
            "heat_rows": int(radar_status.get("heat_rows") or len(radar_snapshot.get("heatmap") or [])),
            "last_run": radar_status.get("last_run") or radar_snapshot.get("time"),
            "worker_projection_elapsed_ms": radar_status.get("projection_elapsed_ms") or radar_snapshot.get("projection_elapsed_ms"),
            "worker_projection_slo_ms": 1500,
            "route_read_slo_ms": 250,
            "projection_policy": "HTTP serves the last immutable worker snapshot; it performs no provider, SQLite or analytical calculation",
        })(dict(status.get("market_radar") or {}), dict(getattr(app, "_market_radar_snapshot", {}) or {})),
        "time": now_iso(),
    }

def r_delivery_data(app, qs, q, mode):
    sym = str(qs.get("symbol", [""])[0] or "")
    days = _qint(qs, "days", 10)
    return {"ok": True, "rows": app.store.get_delivery_data(sym, days), "sync": app.status.get("delivery_data_sync")}

def r_delivery_sync(app, qs, q, mode):
    force = _flag(qs, "force")
    return {"ok": True, "delivery": app.ensure_live_delivery_data(force=force)}

def r_bulk_deals(app, qs, q, mode):
    sym = str(qs.get("symbol", [""])[0] or "")
    days = _qint(qs, "days", 5)
    return {"ok": True, "rows": app.store.get_bulk_block_deals(sym, days)}


def r_market_breadth(app, qs, q, mode):
    universe = str(qs.get("universe", ["NIFTY250_CORE"])[0] or "NIFTY250_CORE")
    projections = getattr(app, "operator_read_models", None)
    if projections is not None:
        breadth = projections.market_breadth(universe)
        cache_only = True
    else:  # compatibility for isolated route tests/legacy embedders
        breadth = app.store.get_latest_market_breadth(universe)
        cache_only = False
    return {"ok": True, "breadth": breadth, "projection_state": "ready" if breadth else "warming", "cache_only": cache_only}

def r_reference_data_runs(app, qs, q, mode):
    return {"ok": True, "runs": app.store.reference_run_status()}

def r_institutional_flow(app, qs, q, mode):
    days = _qint(qs, "days", 20, min_val=1, max_val=120)
    return {"ok": True, "institutional_flow": app.reference_data.institutional_flow_context(days)}

def r_analytical_projection_status(app, qs, q, mode):
    service = getattr(app, "analytical_projection", None)
    return {
        "ok": bool(service is not None),
        "analytical_projection": service.status() if service is not None else {"state": "UNAVAILABLE"},
        "time": now_iso(),
    }

def r_event_calendar(app, qs, q, mode):
    sym = str(qs.get("symbol", [""])[0] or "")
    within = _qint(qs, "within_days", 3)
    projections = getattr(app, "operator_read_models", None)
    if projections is not None:
        result = projections.event_calendar(within, sym)
        result["cache_only"] = True
        return result
    events = app.earnings_calendar.event_risk_map(within) or {}
    if sym:
        symbol = sym.upper().strip()
        return {"ok": True, "symbol": symbol, "event": events.get(symbol), "requested_within_days": within, "cache_only": False}
    return {"ok": True, "events": events, "requested_within_days": within, "cache_only": False}

def r_auth_test(app, qs, q, mode):
    return app.auth_test(force=True)

def r_instruments_status(app, qs, q, mode):
    return app.instruments_status()

def r_scanner_audit(app, qs, q, mode):
    return app.store.events(limit=100)

def r_scanner_status(app, qs, q, mode):
    return app.scanner_status()


def r_dashboard_state(app, qs, q, mode):
    return app.dashboard_data(mode)

def r_dashboard_cards(app, qs, q, mode):
    return app.dashboard_cards_data(mode)



def _workspace_quote_projection(app, symbols, *, market_open: bool):
    """Memory-only quote overlay for trader workspace rows.

    This helper deliberately performs no provider call and no database read.
    It prefers the canonical live-market gateway snapshot, then already-retained
    in-memory quote caches. Candidate snapshot prices remain immutable and are
    exposed separately as captured_price when no current/verified quote exists.
    """
    requested = []
    seen = set()
    for value in symbols or []:
        symbol = str(value or "").strip().upper()
        if symbol and symbol not in seen:
            seen.add(symbol)
            requested.append(symbol)
        if len(requested) >= 60:
            break
    out = {}
    if not requested:
        return out
    try:
        quotes = getattr(getattr(app, "live_market", None), "quotes", None)
        if quotes is not None:
            snapshot = quotes.snapshot(requested, market_open=market_open, max_age_sec=8.0) or {}
            for symbol, raw in snapshot.items():
                row = dict(raw or {})
                if row.get("ltp") is None or row.get("identity_verified") is not True:
                    continue
                state = str(row.get("freshness_state") or "").lower()
                if state not in {"live", "closed_market"}:
                    continue
                row["workspace_quote_authority"] = "CANONICAL_LIVE_MARKET_GATEWAY"
                out[str(symbol).upper()] = row
    except Exception:
        pass
    caches = [
        getattr(app, "_coverage_quote_cache", {}) or {},
        getattr(getattr(app, "market_data", None), "_quote_delta_cache", {}) or {},
    ]
    for cache in caches:
        if not isinstance(cache, dict):
            continue
        for symbol in requested:
            if symbol in out:
                continue
            raw = cache.get(symbol)
            if not isinstance(raw, dict) or raw.get("ltp") is None:
                continue
            row = dict(raw)
            row.setdefault("workspace_quote_authority", "RETAINED_IN_MEMORY_QUOTE_CACHE")
            out[symbol] = row
    return out


def _workspace_market_context_projection(app, existing_rows, *, market_open: bool):
    """Overlay canonical index/sector rows with hot-runtime stream quotes.

    This path is intentionally provider-I/O-free.  The market_heat worker still
    owns historical/breadth/direction enrichment, while the trader workspace
    gets current LTP/change/freshness immediately from the canonical live
    gateway when those governed index identities are subscribed.
    """
    try:
        from core.heatmap_index_catalog import canonical_index_rows
        catalog = canonical_index_rows()
    except Exception:
        catalog = []
    existing = [dict(row or {}) for row in list(existing_rows or []) if isinstance(row, dict)]
    by_key = {str(row.get("instrument_key") or ""): row for row in existing if row.get("instrument_key")}
    by_name = {str(row.get("display_name") or row.get("name") or row.get("trading_symbol") or "").upper(): row for row in existing}

    stream_rows = []
    try:
        quote_store = getattr(getattr(app, "live_market", None), "quotes", None)
        if quote_store is not None and callable(getattr(quote_store, "snapshot", None)):
            stream_rows = list((quote_store.snapshot(None, market_open=market_open, max_age_sec=8.0) or {}).values())
    except Exception:
        stream_rows = []
    retained = []
    for cache in (getattr(app, "_coverage_quote_cache", {}) or {}, getattr(getattr(app, "market_data", None), "_quote_delta_cache", {}) or {}):
        if isinstance(cache, dict):
            retained.extend(dict(row or {}) for row in cache.values() if isinstance(row, dict))
    quote_by_key = {}
    quote_by_symbol = {}
    for raw in [*stream_rows, *retained]:
        row = dict(raw or {})
        key = str(row.get("instrument_key") or "").strip()
        symbol = str(row.get("symbol") or row.get("trading_symbol") or "").upper().strip()
        if row.get("ltp") is None:
            continue
        if key and key not in quote_by_key:
            quote_by_key[key] = row
        if symbol and symbol not in quote_by_symbol:
            quote_by_symbol[symbol] = row

    preferred_codes = ["NIFTY", "SENSEX", "VIX", "BANK", "IT", "AUTO", "PHARMA", "METAL", "FMCG", "PSUBANK", "PVTBANK", "REALTY", "ENERGY", "OILGAS", "HEALTHCARE", "CONSUMDUR", "MEDIA", "MIDCAP", "SMALLCAP", "N500"]
    order = {code: index for index, code in enumerate(preferred_codes)}
    out = []
    seen = set()
    for identity in sorted(catalog, key=lambda row: order.get(str(row.get("catalog_code") or ""), 999)):
        key = str(identity.get("instrument_key") or "")
        display = str(identity.get("display_name") or identity.get("trading_symbol") or identity.get("catalog_code") or "")
        symbol = str(identity.get("trading_symbol") or display).upper()
        base = dict(by_key.get(key) or by_name.get(display.upper()) or identity)
        quote = quote_by_key.get(key) or quote_by_symbol.get(symbol) or quote_by_symbol.get(display.upper())
        if quote:
            ltp = quote.get("ltp")
            previous = quote.get("previous_close") or quote.get("close")
            change_abs = quote.get("rupee_change")
            change_pct = quote.get("change_pct")
            try:
                if change_abs is None and ltp is not None and previous not in (None, 0, ""):
                    change_abs = float(ltp) - float(previous)
                if change_pct is None and change_abs is not None and previous not in (None, 0, ""):
                    change_pct = float(change_abs) * 100.0 / float(previous)
            except (TypeError, ValueError, ZeroDivisionError):
                pass
            base.update({
                "name": identity.get("catalog_code") or base.get("name") or display,
                "display_name": display,
                "trading_symbol": identity.get("trading_symbol"),
                "instrument_key": key,
                "exchange": identity.get("exchange"),
                "segment": identity.get("segment"),
                "instrument_type": "INDEX",
                "type": "Sector" if str(identity.get("catalog_code") or "") not in {"NIFTY","SENSEX","VIX","BANK","NXT50","N100","N200","N500","MIDCAP","SMALLCAP"} else "Index",
                "ltp": ltp, "close": ltp, "previous_close": previous,
                "rupee_change": change_abs, "point_change": change_abs, "change_pct": change_pct,
                "source_time": quote.get("provider_timestamp") or quote.get("source_time") or quote.get("timestamp"),
                "timestamp": quote.get("provider_timestamp") or quote.get("source_time") or quote.get("timestamp"),
                "freshness_state": quote.get("freshness_state"),
                "freshness_reason": quote.get("freshness_reason"),
                "identity_verified": quote.get("identity_verified") is True,
                "stale": str(quote.get("freshness_state") or "").lower() not in {"live", "closed_market"},
                "workspace_live_overlay": True,
            })
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        out.append(base)

    # Keep any authoritative heat rows not represented in the canonical catalogue.
    for row in existing:
        key = str(row.get("instrument_key") or "")
        if key and key in seen:
            continue
        out.append(row)
    return out


def _as_reason_list(*values):
    result = []
    seen = set()
    for value in values:
        items = value if isinstance(value, list) else [value] if value not in (None, "") else []
        for item in items:
            text_value = str(item or "").strip()
            if not text_value or text_value in seen:
                continue
            seen.add(text_value)
            result.append(text_value)
    return result


def _candidate_age_seconds(item):
    # Signal Age is anchored only to signal/decision creation. Observation or
    # last-seen timestamps describe Research/Watch recency and must never be
    # silently relabelled as the age of a generated trade signal.
    for key in ("generated_at", "decision_generated_at", "signal_generated_at", "created_at"):
        raw = item.get(key)
        if not raw:
            continue
        try:
            stamp = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            return max(0.0, (datetime.now(timezone.utc) - stamp.astimezone(timezone.utc)).total_seconds())
        except Exception:
            continue
    return None


def _first_candidate_time(item, *keys):
    for key in keys:
        raw = item.get(key)
        if not raw:
            continue
        try:
            stamp = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            if stamp.astimezone(timezone.utc) <= datetime.now(timezone.utc):
                return stamp.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        except Exception:
            continue
    return None


def _candidate_lifecycle_projection(item, *, pending_live: bool, market_open: bool):
    status = str(item.get("lifecycle_state") or item.get("canonical_state") or item.get("status") or "").upper()
    action = str(item.get("decision_action") or item.get("management_action") or item.get("current_action") or item.get("decision") or "").upper()
    reassessment = item.get("thesis_reassessment") or item.get("reassessment") or {}
    thesis_state = str((reassessment or {}).get("state") or item.get("reassessment_state") or item.get("thesis_state") or "").upper()
    signal_outcome = str(item.get("signal_outcome") or "").upper()
    economic_outcome = str(item.get("economic_outcome") or "").upper()
    exit_reason = str(item.get("exit_reason") or item.get("outcome") or item.get("result") or "").upper()

    terminal = (
        status in {"REJECTED", "BLOCKED", "INVALIDATED", "FAILED", "FAIL", "EXPIRED", "SETTLED", "CLOSED"}
        or action in {"REJECT", "AVOID", "AVOID_LONG", "EXIT"}
        or thesis_state in {"INVALIDATED", "THESIS_INVALIDATED"}
        or signal_outcome in {"SUCCESS", "FAILURE", "NEUTRAL", "UNSCORABLE"}
        or bool(economic_outcome)
        or bool(exit_reason and status in {"SETTLED", "CLOSED"})
    )
    if terminal:
        stage = "SETTLED" if status in {"SETTLED", "CLOSED"} or signal_outcome or economic_outcome else "FAILED"
        current_action = "MOVED TO HISTORY"
    elif status in {"OPEN", "OPENED", "SIGNAL_OPEN", "MODEL_PAPER_OPEN"}:
        stage = "OPEN"
        current_action = action or "CONTINUE TO HOLD"
    elif status in {"FINAL", "PROMOTED", "ACTIONABLE"}:
        stage = "FINAL"
        current_action = action or "AWAIT MODEL PAPER / CURRENT VALIDATION"
    elif pending_live:
        stage = "VALIDATING" if market_open else "PREPARED"
        current_action = "LIVE VALIDATION" if market_open else "REASSESS 09:15 IST"
    elif status in {"RESEARCH", "UNDER_REVIEW"}:
        stage = "RESEARCH"
        current_action = action or "CONTINUE RESEARCH"
    else:
        stage = "PREPARED" if status in {"WATCH", "WATCHING", "WAIT", "PREPARING", "POTENTIAL", "QUALIFIED", "ARMED", ""} else status
        current_action = action or "WATCH"

    result = item.get("signal_outcome") or item.get("economic_outcome") or item.get("exit_reason") or item.get("outcome") or item.get("result")
    return {
        "stage": stage,
        "terminal": terminal,
        "current_action": current_action,
        "result": result,
        "reassessment_state": thesis_state or ("VALID" if stage in {"FINAL", "OPEN"} else None),
    }


def _trader_candidate_projection(row, quote, *, market_open: bool):
    """Human-facing projection only; never recomputes ranking or trade math."""
    item = dict(row or {})
    symbol = str(item.get("symbol") or item.get("trading_symbol") or "").strip().upper()
    mode = str(item.get("mode") or "delivery").strip().lower()
    captured_price = next((item.get(key) for key in (
        "captured_price", "observed_price", "ltp", "current_price", "close", "price"
    ) if item.get(key) not in (None, "")), None)
    def _positive_trade_value(value):
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None
        return parsed if parsed > 0 else None

    q = dict(quote or {})
    current_price = q.get("ltp") if q.get("ltp") is not None else item.get("current_price")
    if current_price is None:
        current_price = item.get("ltp")
    quote_state = str(q.get("freshness_state") or item.get("price_freshness") or "candidate_snapshot").lower()
    quote_verified = bool(q and q.get("ltp") is not None and quote_state in {"live", "closed_market"})
    item.update({
        "symbol": symbol,
        "captured_price": captured_price,
        "current_price": current_price,
        "display_price": current_price,
        "current_price_authority": q.get("workspace_quote_authority") if quote_verified else "CANDIDATE_SNAPSHOT",
        "current_price_state": quote_state,
        "current_price_as_of": q.get("provider_timestamp") or q.get("source_time") or q.get("timestamp") or item.get("quote_as_of"),
        "score_semantics": "Evidence/ranking score; not probability of profit",
    })
    context = " ".join(str(item.get(key) or "") for key in (
        "setup", "reason", "candidate_stage", "opportunity_stage", "prepared_state", "status", "decision"
    )).lower()
    lifecycle = str(item.get("status") or item.get("decision") or item.get("lifecycle_state") or "").upper()
    final_or_open = lifecycle in {"PROMOTED", "FINAL", "OPEN", "SIGNAL_OPEN", "OPENED", "SETTLED", "CLOSED"}
    pending_live = mode == "intraday" and not final_or_open and (
        not market_open
        or item.get("execution_quote_required") is True
        or item.get("research_only") is True
        or item.get("execution_price_authority") is False
        or "pre-market" in context
        or "premarket" in context
        or "live validation required" in context
        or "live confirmation" in context
    )
    if pending_live:
        item.update({
            "display_entry": None,
            "display_target": None,
            "display_stop": None,
            "display_rr": None,
            "trade_geometry_display_state": "PENDING_LIVE_CONFIRMATION",
        })
    else:
        display_entry = _positive_trade_value(item.get("entry")) or _positive_trade_value(item.get("planned_entry"))
        display_target = _positive_trade_value(item.get("target")) or _positive_trade_value(item.get("t1")) or _positive_trade_value(item.get("planned_t1"))
        display_stop = _positive_trade_value(item.get("stop")) or _positive_trade_value(item.get("sl")) or _positive_trade_value(item.get("planned_sl"))
        display_rr = (
            _positive_trade_value(item.get("rr"))
            or _positive_trade_value(item.get("planned_rr"))
            or _positive_trade_value(item.get("reward_risk"))
            or _positive_trade_value(item.get("room_rr"))
            or _positive_trade_value(item.get("intended_rr"))
        )
        item.update({
            "display_entry": display_entry,
            "display_target": display_target,
            "display_stop": display_stop,
            "display_rr": display_rr,
            "trade_geometry_display_state": "AVAILABLE" if all(value is not None for value in (display_entry, display_target, display_stop)) else "UNAVAILABLE",
        })
    components = []
    for raw in list(item.get("rank_components") or [])[:8]:
        if not isinstance(raw, dict):
            continue
        components.append({
            "name": raw.get("name"),
            "points": raw.get("points"),
            "max_points": raw.get("max_points"),
            "status": raw.get("status"),
            "data_quality": raw.get("data_quality"),
            "reason": raw.get("reason"),
        })
    blockers = _as_reason_list(
        item.get("rank_veto_reasons"), item.get("promotion_blocked_by"),
        item.get("rank_gate_failures"), item.get("rank_conflicts"),
        item.get("qualification_blocker"), item.get("waiting_for"),
    )
    # ₹ change and % change must always be a coherent pair derived from the same
    # current-price / previous-close authority. Never combine a stale stored
    # percentage with a different quote price (for example ₹0.00 +7.49%).
    previous_close = q.get("previous_close") if q.get("previous_close") is not None else item.get("previous_close")
    change_abs = None
    change_pct = None
    change_state = "UNAVAILABLE"
    try:
        current_numeric = float(current_price) if current_price not in (None, "") else None
        previous_numeric = float(previous_close) if previous_close not in (None, "", 0) else None
        if current_numeric is not None and previous_numeric is not None and previous_numeric > 0:
            change_abs = current_numeric - previous_numeric
            change_pct = (change_abs / previous_numeric) * 100.0
            change_state = "CURRENT_PRICE_PREVIOUS_CLOSE"
    except (TypeError, ValueError, OverflowError):
        change_abs = None
        change_pct = None
        change_state = "UNAVAILABLE"
    lifecycle_projection = _candidate_lifecycle_projection(item, pending_live=pending_live, market_open=market_open)
    # R50 fail-safe: a verified current quote that has already crossed the frozen
    # target/active stop of an OPEN Model Paper position may not be rendered as a
    # clean ACTIVE/HOLD row while lifecycle settlement is lagging.  This read path
    # never settles a trade; it exposes the reconciliation requirement explicitly.
    if item.get("final_signal_authority") and item.get("position_id") and quote_verified and lifecycle_projection.get("stage") == "OPEN":
        try:
            px = float(current_price)
            entry_px = float(item.get("entry") if item.get("entry") is not None else item.get("entry_price"))
            target_px = float(item.get("target") if item.get("target") is not None else item.get("original_target"))
            stop_px = float(item.get("active_stop") if item.get("active_stop") is not None else item.get("stop") if item.get("stop") is not None else item.get("original_stop"))
            side = str(item.get("side") or "LONG").upper()
            hit_raw = str(item.get("hit_status") or "").upper()
            terminal_hit = any(token in hit_raw for token in ("TARGET", "STOP", "SL_HIT", "CLOSED", "EXIT"))
            crossed_target = (side == "LONG" and px >= target_px) or (side == "SHORT" and px <= target_px)
            crossed_stop = (side == "LONG" and px <= stop_px) or (side == "SHORT" and px >= stop_px)
            if not terminal_hit and (crossed_target or crossed_stop):
                lifecycle_projection = dict(lifecycle_projection)
                lifecycle_projection.update({
                    "stage": "RECONCILIATION_REQUIRED",
                    "terminal": False,
                    "current_action": "AWAIT LIFECYCLE RECONCILIATION",
                    "result": "TARGET CROSSED" if crossed_target else "STOP CROSSED",
                    "reassessment_state": "RECONCILIATION_REQUIRED",
                })
                item["reconciliation_required"] = True
                item["price_cross_reconciliation"] = "TARGET" if crossed_target else "STOP"
        except (TypeError, ValueError, OverflowError):
            pass
    item.update({
        "display_stage": lifecycle_projection["stage"],
        "display_terminal": lifecycle_projection["terminal"],
        "display_action": lifecycle_projection["current_action"],
        "display_result": lifecycle_projection["result"],
        "display_reassessment_state": lifecycle_projection["reassessment_state"],
        "signal_age_seconds": item.get("signal_age_seconds") if item.get("signal_age_seconds") is not None else _candidate_age_seconds(item),
        "display_change_abs": change_abs,
        "display_change_pct": change_pct,
        "display_change_state": change_state,
    })
    generated_at = _first_candidate_time(
        item, "generated_at", "decision_generated_at", "signal_generated_at", "created_at"
    )
    first_seen_at = _first_candidate_time(
        item, "first_seen_at", "occurred_at", "observed_at", "created_at", "generated_at", "last_seen_at"
    )
    last_seen_at = _first_candidate_time(
        item, "last_seen_at", "updated_at", "last_update", "observed_at", "finalized_at", "generated_at", "created_at"
    )
    holding_period = next((
        item.get(key) for key in ("holding_period", "target_window", "horizon", "expected_horizon", "max_holding_period")
        if item.get(key) not in (None, "")
    ), None)
    has_signal_identity = bool(
        str(item.get("decision_id") or item.get("signal_id") or "").strip()
        and lifecycle_projection["stage"] in {"FINAL", "OPEN", "RECONCILIATION_REQUIRED", "SETTLED"}
    )
    evidence_state = str(
        item.get("evidence_state") or item.get("rank_readiness") or item.get("evidence_readiness")
        or item.get("feature_freshness") or item.get("freshness_state") or "UNPROVEN"
    ).upper()
    signal_age = item.get("signal_age_seconds") if item.get("signal_age_seconds") is not None else _candidate_age_seconds(item)
    item.update({
        "generated_at": generated_at or item.get("generated_at"),
        "first_seen_at": first_seen_at or item.get("first_seen_at"),
        "last_seen_at": last_seen_at or item.get("last_seen_at"),
        "holding_period": holding_period,
        "signal_age_seconds": signal_age,
        "time_semantics": {
            "generated_at": generated_at,
            "first_seen_at": first_seen_at,
            "last_seen_at": last_seen_at,
            "signal_age_seconds": signal_age,
            "signal_age_state": "AVAILABLE" if signal_age is not None else "MISSING" if has_signal_identity else "NOT_A_SIGNAL",
            "display_age_kind": "SIGNAL" if has_signal_identity else "OBSERVATION",
            "holding_period": holding_period,
            "holding_period_state": "AVAILABLE" if holding_period else "MISSING" if has_signal_identity else "PENDING_FINAL_ADMISSION",
            "lifecycle_state": lifecycle_projection["stage"],
            "evidence_state": evidence_state,
            "reason": blockers[0] if blockers else None,
        },
    })
    item["trader_explanation"] = {
        "score": item.get("rank_score") if item.get("rank_score") is not None else item.get("evidence_score") if item.get("evidence_score") is not None else item.get("score"),
        "score_semantics": item["score_semantics"],
        "readiness": item.get("rank_readiness") or item.get("candidate_stage") or item.get("opportunity_stage") or item.get("status"),
        "scoring_state": item.get("rank_scoring_state"),
        "components": components,
        "blockers": blockers[:8],
        "missing_inputs": list(item.get("rank_missing_inputs") or [])[:8],
        "model": {
            "state": item.get("model_state") or item.get("model_ranking_stage") or item.get("research_factor_state"),
            "model_id": item.get("model_id"),
            "score": item.get("model_score"),
            "authority_pct": item.get("model_ranking_authority_pct"),
            "influence_applied": bool(item.get("model_influence_applied")),
            "rank_contribution": item.get("model_rank_contribution") if item.get("model_rank_contribution") is not None else item.get("research_factor_points"),
        },
        "ranking_explanation": item.get("ranking_explanation"),
        "ranking_trace_id": item.get("ranking_trace_id"),
    }
    return item


def _workspace_mode_coverage(mode_status):
    """Small cache-only scan completeness contract for customer ranking semantics."""
    out = {}
    for desk in ("delivery", "intraday"):
        row = dict((mode_status or {}).get(desk) or {})
        contract = dict(row.get("progress_contract") or {})
        def first_number(*values):
            for value in values:
                try:
                    if value is None or isinstance(value, bool):
                        continue
                    number = float(value)
                    if math.isfinite(number) and number >= 0:
                        return number
                except (TypeError, ValueError, OverflowError):
                    continue
            return None
        total = first_number(contract.get("population_count"), row.get("universe_count"), row.get("universe_size"), row.get("total"), row.get("expected"))
        processed = first_number(contract.get("current_sweep_scanned"), row.get("scanned"), row.get("processed"), row.get("completed"))
        analysis = dict(row.get("analysis") or {})
        coverage = dict(row.get("coverage") or {})
        if processed is None:
            processed = first_number(analysis.get("current_sweep_scanned"), analysis.get("sweep_scanned"), analysis.get("cycle_scanned"))
        last_completed = dict(coverage.get("last_completed") or analysis.get("last_completed_sweep") or {})
        last_completed_processed = first_number(
            contract.get("last_completed_sweep_count"), analysis.get("last_completed_sweep_count"),
            last_completed.get("attempted"), last_completed.get("processed"), last_completed.get("scanned"),
        )
        # A new rotating sweep resets the current cursor immediately.  Full-
        # universe scope is durable evidence from the last terminal sweep, not a
        # millisecond-wide state that disappears when the next cycle starts.
        # Attempted members include explicit missing/unverified terminal blockers;
        # those rows remain fail-closed and are not assigned a normal score.
        scope_processed = max(
            [value for value in (processed, last_completed_processed) if value is not None],
            default=None,
        )
        pct = None
        if total and scope_processed is not None:
            pct = max(0.0, min(100.0, scope_processed * 100.0 / total))
        state = str(contract.get("state") or row.get("state") or row.get("status") or "UNKNOWN").upper()
        # Full-universe rank is only truthful when numeric coverage proves every
        # member was processed.  Never infer completion from text such as
        # INCOMPLETE or COMPLETE_WITH_EXPLICIT_BLOCKERS.
        complete = bool(total is not None and total > 0 and scope_processed is not None and scope_processed >= total)
        out[desk] = {
            "processed": int(scope_processed) if scope_processed is not None else None,
            "current_sweep_processed": int(processed) if processed is not None else None,
            "last_completed_sweep_processed": int(last_completed_processed) if last_completed_processed is not None else None,
            "total": int(total) if total is not None else None,
            "pct": round(pct, 3) if pct is not None else None,
            "complete": bool(complete),
            "state": state,
            "as_of": last_completed.get("completed_at") or contract.get("as_of") or row.get("as_of") or row.get("last_progress_at"),
            "ranking_scope": "FULL_UNIVERSE" if complete else "EVALUATED_SUBSET_ONLY",
        }
    return out


def r_trader_live_state(app, qs, q, mode):
    """Ultra-light current customer truth. Same trust authority, no retained workspace rows."""
    from core.runtime_primitives import is_india_market_open
    market_open = bool(is_india_market_open())
    trust = app.trust_state_service.snapshot() if getattr(app, "trust_state_service", None) is not None else {
        "state": "DEGRADED", "decision_admission_allowed": False, "reason": "trust projection warming", "evaluated_at": now_iso()
    }
    return {
        "ok": True,
        "contract_version": "trader-live-state-1.0.0",
        "server_time": now_iso(),
        "market_open": market_open,
        "market_state": "LIVE" if market_open else "CLOSED",
        "trust": trust,
        "policy": "Current runtime trust is projected independently of retained workspace rows; stale workspace evidence can never inherit current admission authority.",
    }

def r_trader_workspace(app, qs, q, mode):
    """Cache-only product BFF for the primary trader workspace.

    The former browser surface composed up to ten domain/diagnostic routes and
    inherited the slowest PostgreSQL or research query.  This contract reads
    only already-published in-memory projections.  Missing components stay
    explicit and independently recover in their owning background plane.
    """
    started = time.perf_counter()
    selected_mode = mode if mode in {"delivery", "intraday"} else "all"
    try:
        cards = dict(app.dashboard_cards_data(selected_mode) or {})
    except Exception as exc:
        cards = {"projection_state": "UNAVAILABLE", "error": str(exc)[:240]}
    try:
        scanner_response = dict(app.scanner_status() or {})
    except Exception as exc:
        scanner_response = {"state": "UNAVAILABLE", "error": str(exc)[:240]}
    scanner = scanner_response.get("scanner") if isinstance(scanner_response.get("scanner"), dict) else scanner_response
    mode_status = dict(scanner.get("mode_scanners") or scanner.get("modes") or {})
    try:
        heatmap = [dict(row or {}) for row in list(app.heatmap_snapshot() or [])[:48]]
    except Exception:
        heatmap = []
    radar = dict(getattr(app, "_market_radar_http_snapshot", {}) or {})
    if not radar:
        radar = dict(getattr(app, "_market_radar_snapshot", {}) or {})
    market_radar = dict(radar.get("market_radar") or radar)
    # The compact HTTP radar deliberately omits mover lists.  Workspace is already
    # inside the runtime process, so it can safely project the bounded mover rows
    # from the full in-memory radar without provider/DB I/O.  This is display-only
    # market breadth context; it never changes ranking or decision authority.
    full_radar_snapshot = dict(getattr(app, "_market_radar_snapshot", {}) or {})
    full_market_radar = dict(full_radar_snapshot.get("market_radar") or {})
    market_movers = {
        "top_gainers": [dict(row or {}) for row in list(full_market_radar.get("top_gainers") or [])[:8] if isinstance(row, dict)],
        "top_losers": [dict(row or {}) for row in list(full_market_radar.get("top_losers") or [])[:8] if isinstance(row, dict)],
        "coverage": full_market_radar.get("coverage"),
        "verified_coverage": full_market_radar.get("verified_coverage"),
        "verified_coverage_pct": full_market_radar.get("verified_coverage_pct"),
        "advances": full_market_radar.get("advances"),
        "declines": full_market_radar.get("declines"),
        "unchanged": full_market_radar.get("unchanged"),
        "change_unknown": full_market_radar.get("change_unknown"),
        "breadth_measured": full_market_radar.get("breadth_measured"),
        "breadth_policy": full_market_radar.get("breadth_policy"),
        "data_state": full_market_radar.get("data_state"),
        "reason": full_market_radar.get("reason"),
    }

    final_signals = list(cards.get("final_signals") or [])[:80]
    active = list(cards.get("active_positions") or [])[:20]
    preparing = list((cards.get("discovery") or {}).get("near_qualified") or cards.get("watch_queue") or cards.get("decision_list") or [])[:24]
    # Preserve independent desk visibility. A single 30-row global cap could
    # let Delivery fill the candidate projection and make valid next-session
    # Intraday preparation disappear from the Intraday desk. Keep a bounded 30
    # rows per desk from the same already-published in-memory sources.
    candidates = []
    desk_candidate_counts = {"delivery": 0, "intraday": 0}
    candidate_sources = [
        cards.get("selected_memory"), cards.get("watch_queue"), cards.get("decision_list"),
        market_radar.get("opportunities"), market_radar.get("next_session_watchlist"),
        radar.get("opportunities"), radar.get("next_session_watchlist"),
    ]
    def candidate_stage_rank(row):
        stage = str(row.get("candidate_stage") or row.get("opportunity_stage") or row.get("lifecycle_state") or row.get("canonical_state") or row.get("status") or row.get("decision") or "").upper()
        if any(token in stage for token in ("OPEN", "FINAL", "PROMOTED", "ACTIONABLE")):
            return 60
        if any(token in stage for token in ("RESEARCH", "QUALIFIED", "ARMED", "VALIDATING")):
            return 50
        if any(token in stage for token in ("PREPARED", "WATCH", "UNDER_REVIEW", "SCREENED")):
            return 30
        if any(token in stage for token in ("REJECT", "FAILED", "INVALID", "BLOCKED")):
            return 5
        return 10
    def candidate_time_rank(row):
        for key in ("last_seen_at", "updated_at", "last_update", "finalized_at", "generated_at", "observed_at", "created_at"):
            raw = row.get(key)
            if not raw:
                continue
            try:
                stamp = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
                if stamp.tzinfo is None:
                    stamp = stamp.replace(tzinfo=timezone.utc)
                return stamp.timestamp()
            except Exception:
                continue
        return 0.0
    best_candidates = {}
    for source_index, source in enumerate(candidate_sources):
        for raw in list(source or []):
            row = dict(raw or {})
            symbol = str(row.get("symbol") or row.get("trading_symbol") or "").strip().upper()
            desk = str(row.get("mode") or "delivery").strip().lower()
            if not symbol or desk not in desk_candidate_counts:
                continue
            key = (symbol, desk)
            try:
                rank_score = float(row.get("rank_score") if row.get("rank_score") is not None else row.get("evidence_score") if row.get("evidence_score") is not None else row.get("score") or 0.0)
                if not math.isfinite(rank_score):
                    rank_score = 0.0
            except Exception:
                rank_score = 0.0
            quality = (candidate_stage_rank(row), candidate_time_rank(row), rank_score, -source_index)
            prior = best_candidates.get(key)
            if prior is None or quality > prior[0]:
                best_candidates[key] = (quality, row)
    for desk in ("delivery", "intraday"):
        desk_rows = [value for (symbol, row_desk), value in best_candidates.items() if row_desk == desk]
        desk_rows.sort(key=lambda item: item[0], reverse=True)
        selected = [dict(item[1]) for item in desk_rows[:30]]
        desk_candidate_counts[desk] = len(selected)
        candidates.extend(selected)
    status = dict(getattr(app, "status", {}) or {})
    from core.runtime_primitives import is_india_market_open
    market_open = is_india_market_open()
    heatmap = _workspace_market_context_projection(app, heatmap, market_open=market_open)
    symbols = [
        str(row.get("symbol") or row.get("trading_symbol") or "").strip().upper()
        for row in [*final_signals, *active, *preparing, *candidates]
        if isinstance(row, dict)
    ]
    quote_by_symbol = _workspace_quote_projection(app, symbols, market_open=market_open)
    final_signals = [
        _trader_candidate_projection(row, quote_by_symbol.get(str(row.get("symbol") or row.get("trading_symbol") or "").strip().upper()), market_open=market_open)
        for row in final_signals
    ]
    active = [
        _trader_candidate_projection(row, quote_by_symbol.get(str(row.get("symbol") or row.get("trading_symbol") or "").strip().upper()), market_open=market_open)
        for row in active
    ]
    preparing = [
        _trader_candidate_projection(row, quote_by_symbol.get(str(row.get("symbol") or row.get("trading_symbol") or "").strip().upper()), market_open=market_open)
        for row in preparing
    ]
    candidates = [
        _trader_candidate_projection(row, quote_by_symbol.get(str(row.get("symbol") or row.get("trading_symbol") or "").strip().upper()), market_open=market_open)
        for row in candidates
    ]
    # Terminal/rejected rows are never allowed to linger on the active attention
    # surface.  Their scanner/event/settlement evidence remains authoritative in
    # history/Accuracy, but the trader workspace only shows work that can still act.
    moved_rows = [row for row in [*active, *preparing, *candidates] if row.get("display_terminal") is True]
    active = [row for row in active if row.get("display_terminal") is not True]
    preparing = [row for row in preparing if row.get("display_terminal") is not True]
    candidates = [row for row in candidates if row.get("display_terminal") is not True]
    next_session_intraday = []
    seen_next = set()
    for row in [*preparing, *candidates]:
        symbol = str(row.get("symbol") or "").upper()
        if not symbol or str(row.get("mode") or "").lower() != "intraday":
            continue
        if str(row.get("trade_geometry_display_state") or "").upper() != "PENDING_LIVE_CONFIRMATION":
            continue
        if symbol in seen_next:
            continue
        seen_next.add(symbol); next_session_intraday.append(row)
    if not market_open:
        intraday_status = dict(mode_status.get("intraday") or {})
        contract = dict(intraday_status.get("progress_contract") or {})
        contract["next_session_prepared"] = len(next_session_intraday)
        existing_detail = str(contract.get("display_detail") or "").strip()
        if next_session_intraday:
            prepared_text = f"{len(next_session_intraday)} prepared for next session"
            contract["display_detail"] = (
                f"{existing_detail} · {prepared_text}" if existing_detail else
                f"{prepared_text} · live validation resumes 09:15 IST"
            )
        elif not existing_detail:
            contract["display_detail"] = "Off-market preparation continues; live Intraday validation resumes 09:15 IST"
        intraday_status["progress_contract"] = contract
        intraday_status["next_session_prepared"] = len(next_session_intraday)
        mode_status["intraday"] = intraday_status
    return {
        "ok": True,
        "contract_version": "trader-workspace-1.5.0-live-truth-and-ranking-scope",
        "cache_only": True,
        "server_time": now_iso(),
        "market_state": "LIVE" if market_open else "CLOSED",
        "market_open": market_open,
        "as_of": cards.get("time") or cards.get("as_of") or radar.get("time") or now_iso(),
        "projection_state": cards.get("projection_state") or cards.get("state") or "READY",
        "indices": heatmap,
        "market_movers": market_movers,
        "mode_status": mode_status,
        "coverage": _workspace_mode_coverage(mode_status),
        "final_signals": final_signals,
        "active": active,
        "preparing": preparing,
        "candidates": candidates,
        "counts": {
            "final_signals": len(final_signals), "active": len(active), "preparing": len(preparing), "candidates": len(candidates),
            "next_session_intraday": len(next_session_intraday), "moved_from_active": len(moved_rows),
            "universe": int(((scanner_response.get("instruments") or {}).get("universe_count") or (scanner_response.get("instruments") or {}).get("count") or 0)),
        },
        "trust": app.trust_state_service.snapshot() if getattr(app, "trust_state_service", None) is not None else {"state": "DEGRADED", "decision_admission_allowed": False, "reason": "trust projection warming"},
        "historical_pit": app.historical_pit_sweep.snapshot() if getattr(app, "historical_pit_sweep", None) is not None else {"state": "STARTING"},
        "health": {
            "service": scanner_response.get("service") or status.get("service") or "running",
            "auth": scanner_response.get("auth") or status.get("auth") or {},
            "live_stream": status.get("live_stream") or status.get("live_market") or {},
            "scanner": scanner.get("state") or scanner_response.get("service") or "UNKNOWN",
        },
        "route_elapsed_ms": round((time.perf_counter() - started) * 1000.0, 3),
        "policy": "Foreground workspace is an atomic in-memory projection. Final Signals comes only from canonical PostgreSQL decisions plus exact-ID Model Paper positions; quote overlay may update only current price/change and can never supply Entry/Target/SL, signal time, holding period or lifecycle state.",
    }


def r_trader_research(app, qs, q, mode):
    """Lightweight governed-method registry for the trader surface.

    Dependency discovery and tournaments remain isolated research operations;
    opening this page must not inspect the research virtual environment or run
    analytical SQL.
    """
    from dataclasses import asdict
    from core.active_research_method_registry import METHODS, SERVICE_VERSION
    rows = []
    for method in METHODS:
        row = asdict(method)
        row.update({
            "status": "ACTIVE" if method.lifecycle == "ACTIVE_RUNTIME" else "SHADOW",
            "production_influence": False,
            "dependency_state": "OWNED_BY_ISOLATED_RESEARCH_RUNTIME",
        })
        rows.append(row)
    return {
        "ok": True,
        "contract_version": "trader-strategy-registry-1.0.0",
        "source_version": SERVICE_VERSION,
        "methods": rows,
        "counts": {
            "active": sum(row["status"] == "ACTIVE" for row in rows),
            "shadow": sum(row["status"] == "SHADOW" for row in rows),
            "research": 0, "rejected": 0,
        },
        "as_of": now_iso(),
        "policy": "Only separately approved production strategy versions may influence a FINAL decision; this read performs no discovery, training or promotion.",
    }

def r_today_entries(app, qs, q, mode):
    """Canonical, cache-only contract for the Today Entries workspace."""
    from core.india_time import trading_date_ist
    from core.production_mode_policy import require_production_mode
    from actionability import is_actionable_signal, is_publishable_research_signal
    trading_date = trading_date_ist()
    cards = dict(app.dashboard_cards_data(mode) or {})
    # v65.26.13 continuity recovery: the async cards cache may briefly be an
    # empty/starting snapshot after service restart or a completed zero-promotion
    # scan. Never interpret that transient cache as an instruction to erase open
    # ledger positions or accumulated performance. Hydrate those durable sections
    # directly from SQLite when the cache has no rows.
    if not list(cards.get("active_positions") or []):
        try:
            cards["active_positions"] = list(app.store.selected_signals(mode, limit=20) or [])
        except Exception:
            cards["active_positions"] = list(cards.get("active_positions") or [])
    if not list(cards.get("daily_performance") or []):
        try:
            cards["daily_performance"] = list(app.store.daily_performance("2000-01-01", trading_date) or [])
        except Exception:
            cards["daily_performance"] = list(cards.get("daily_performance") or [])
    if not list(cards.get("trade_journal") or []):
        try:
            cards["trade_journal"] = list(app.store.trade_journal(limit=20, mode=mode) or [])
        except Exception:
            cards["trade_journal"] = list(cards.get("trade_journal") or [])
    try:
        scanner = app.scanner_status() or {}
    except Exception:
        scanner = {}
    # scanner_status() wraps the immutable runtime snapshot under `scanner`.
    # v65.26.32 incorrectly read mode_scanners from the response root, which
    # made Today Entries permanently render "starting · 0 scanned".
    scanner_snapshot = scanner.get("scanner") if isinstance(scanner.get("scanner"), dict) else scanner
    mode_scanners = scanner_snapshot.get("mode_scanners") or {}
    try:
        canonical_entries = list(app.store.canonical_today_entries(mode, trading_date, limit=100) or [])
    except Exception:
        canonical_entries = []
    # Clean Core presentation boundary: provider instrument keys remain machine
    # identities and may never leak into Today Entries. Resolution is local-only
    # and does not mutate or rewrite canonical decision/research history.
    from core.canonical_presentation_service import CanonicalPresentationService
    presentation = CanonicalPresentationService(app.store)
    canonical_entries = [row for row in presentation.decorate_rows(canonical_entries) if row.get("customer_visible", True)]
    confirmed = []
    confirmed_seen = set()
    def current_desk_rows(rows):
        output = []
        for source in rows or []:
            row = dict(source or {})
            try:
                row["mode"] = require_production_mode(row.get("mode"))
            except ValueError:
                continue
            output.append(row)
        return output

    canonical_capital = current_desk_rows([row for row in canonical_entries if str(row.get("publication_authority") or "") == "CAPITAL"])
    legacy_confirmed_source = [] if canonical_entries else current_desk_rows(list(cards.get("active_positions") or []))
    for row in canonical_capital + legacy_confirmed_source:
        key = (
            str(row.get("symbol") or "").upper(),
            str(row.get("mode") or "").lower(),
            str(row.get("signal_id") or row.get("opened_at") or ""),
        )
        if not key[0] or key in confirmed_seen:
            continue
        confirmed_seen.add(key)
        confirmed.append(row)
    preparing = list((cards.get("discovery") or {}).get("near_qualified") or [])
    if not preparing:
        preparing = list(cards.get("watch_queue") or cards.get("decision_list") or [])
    confirmed_keys = {
        (str(row.get("symbol") or "").upper(), str(row.get("mode") or "").lower())
        for row in confirmed
    }
    preparing = [
        row for row in preparing
        if (str(row.get("symbol") or "").upper(), str(row.get("mode") or "").lower()) not in confirmed_keys
    ]
    paper_entries = []
    paper_seen = set()
    canonical_paper = current_desk_rows([row for row in canonical_entries if str(row.get("publication_authority") or "") == "MODEL_PAPER"])
    legacy_paper_source = [] if canonical_entries else current_desk_rows(list(cards.get("decision_list") or []) + list(cards.get("watch_queue") or []) + list(preparing or []))
    for row in canonical_paper + legacy_paper_source:
        if not canonical_entries and not is_publishable_research_signal(row):
            continue
        key = (str(row.get("symbol") or "").upper(), str(row.get("mode") or "").lower(), str(row.get("decision_id") or row.get("signal_id") or ""))
        if not key[0] or key in paper_seen or key[:2] in confirmed_keys:
            continue
        paper_seen.add(key)
        paper = dict(row)
        paper.update({
            "paper_only": True,
            "book": "MODEL_PAPER",
            "signal_status": "PAPER_RESEARCH",
            "status": "PAPER_RESEARCH",
            "decision": str(row.get("proposed_decision") or row.get("pre_risk_decision") or row.get("decision") or "WATCH").upper(),
            "capital_execution_allowed": False,
        })
        paper_entries.append(paper)
    ledger_diagnostics = {"trading_date": trading_date, "open": len(confirmed), "paper_research": len(paper_entries), "decisions_today": 0, "actionable_today": 0, "recent_rejections": []}
    try:
        rows = [
            dict(row or {}) for row in app.store.latest_decisions("all", limit=200)
            if str((row or {}).get("trading_date") or "")[:10] == trading_date
        ][:30]
        ledger_diagnostics["decisions_today"] = len(rows)
        for detail in rows:
            if is_actionable_signal(detail):
                ledger_diagnostics["actionable_today"] += 1
            elif len(ledger_diagnostics["recent_rejections"]) < 8:
                rejection_reasons = list(detail.get("rejection_reasons") or [])
                ledger_diagnostics["recent_rejections"].append({
                    "symbol": detail.get("symbol"), "mode": detail.get("mode"),
                    "decision": detail.get("decision"), "status": detail.get("status") or detail.get("canonical_state"),
                    "reason": detail.get("qualification_blocker") or detail.get("reason") or (rejection_reasons[0] if rejection_reasons else "not actionable"),
                    "created_at": detail.get("created_at"),
                })
    except Exception:
        pass
    confirmed = [row for row in presentation.decorate_rows(confirmed) if row.get("customer_visible", True)]
    # AC-072: Today Entries Final lifecycle attribution is joined only by the
    # immutable source signal ID to canonical PostgreSQL Model Paper.  Missing
    # lifecycle evidence stays explicit; no symbol/time/price inference.
    from core.today_entries_lifecycle_projection_service import TodayEntriesLifecycleProjectionService
    confirmed = TodayEntriesLifecycleProjectionService(
        getattr(app, "model_portfolio_repository", None)
    ).project(confirmed)
    paper_entries = [row for row in presentation.decorate_rows(paper_entries) if row.get("customer_visible", True)]
    preparing = [row for row in presentation.decorate_rows(preparing) if row.get("customer_visible", True)]

    def _rank_value(row):
        try:
            return float(row.get("rank_score") if row.get("rank_score") is not None else row.get("score") or -1.0)
        except (TypeError, ValueError):
            return -1.0
    confirmed.sort(key=lambda row: (-_rank_value(row), str(row.get("symbol") or "")))
    paper_entries.sort(key=lambda row: (-_rank_value(row), str(row.get("symbol") or "")))
    preparing.sort(key=lambda row: (-_rank_value(row), str(row.get("symbol") or "")))
    from core.production_ranking_service import RANKING_VERSION, RANKING_CONTRACT_VERSION
    ranking_rows = list(confirmed) + list(paper_entries) + list(preparing)
    trace_complete = [
        row for row in ranking_rows
        if row.get("ranking_version") == RANKING_VERSION
        and row.get("ranking_contract_version") == RANKING_CONTRACT_VERSION
        and row.get("ranking_trace_id")
        and row.get("ranking_input_hash")
        and row.get("ranking_result_hash")
    ]
    trace_by_decision = {}
    for row in trace_complete:
        decision_key = str(row.get("decision_id") or row.get("signal_id") or "").strip()
        if decision_key:
            trace_by_decision.setdefault(decision_key, set()).add(str(row.get("ranking_trace_id")))
    trace_conflicts = [
        {"decision_id": key, "ranking_trace_ids": sorted(values)}
        for key, values in trace_by_decision.items() if len(values) > 1
    ]
    ranking_reconciliation = {
        "state": "PASS" if ranking_rows and len(trace_complete) == len(ranking_rows) and not trace_conflicts else "PENDING_EVIDENCE",
        "ranking_version": RANKING_VERSION,
        "contract_version": RANKING_CONTRACT_VERSION,
        "rows": len(ranking_rows),
        "trace_complete_rows": len(trace_complete),
        "conflicts": trace_conflicts,
        "same_ranker_consumers": ["TODAY_ENTRY_SCANNER", "REASSESSMENT_SCANNER", "MANUAL_ANALYSIS"],
    }
    payload = dict(cards)
    payload.update({
        "ok": True,
        "authoritative": True,
        "trading_date": trading_date,
        "contract_version": "today-entries-v4-ac072-lifecycle-attribution",
        "model_ranking_contract": {
            "consumer": "TODAY_ENTRY_SCANNER",
            "calculation": "shadow and governed model scores are persisted on every evaluated candidate",
            "authority": "active only for a healthy governed champion; effective weight is recorded and capped at 15%, otherwise deterministic fallback is used",
            "reassessment_uses_same_ranker": True,
        },
        "decision_contract_version": "canonical-decision-record-1.0.0",
        "lifecycle_projection_contract": "today-entries-lifecycle-projection-1.0.0-ac072",
        "ranking_reconciliation": ranking_reconciliation,
        "confirmed": confirmed,
        "paper_entries": paper_entries[:20],
        "preparing": preparing[:20],
        "rejected": [],
        "selected": confirmed,
        "counts": {"confirmed": len(confirmed), "paper": min(20, len(paper_entries)), "preparing": min(20, len(preparing)), "rejected": len(ledger_diagnostics.get("recent_rejections") or [])},
        "ledger_diagnostics": ledger_diagnostics,
        "mode_status": {
            "intraday": mode_scanners.get("intraday") or {},
            "delivery": mode_scanners.get("delivery") or {},
        },
        "intraday_engineering": {
            "model": "live_same_day_confluence",
            "history": "50 closed 5-minute candles",
            "indicators": ["EMA", "MACD", "RSI", "ADX", "ATR"],
            "session_layers": ["ORB maturity and confirmation", "VWAP alignment", "relative-volume expansion"],
            "context_layers": ["market structure", "volume profile", "participation", "index and sector regime"],
            "promotion_gates": ["live quote and fresh candle", "two of ORB/VWAP/RVOL", "R:R >= 1.30", "45+ minutes to close", "validated entry/stop/target"],
        },
        "message": "Today Entries is projected from the canonical DecisionRecord when available; legacy dashboard rows are fallback-only during migration. Paper rows never grant broker or capital authority.",
    })
    return payload

def r_quote_delta(app, qs, q, mode):
    symbols = str(qs.get("symbols", [""])[0] or "")
    return app.live_quotes(symbols, allow_cached=not _flag(qs, "live_only"), network_refresh=False)

def r_live_market_status(app, qs, q, mode):
    return {"ok": True, "live_market_gateway": app.live_market.status(), "time": now_iso()}

def r_live_deltas(app, qs, q, mode):
    since = _qint(qs, "since", 0, min_val=0)
    symbols = str(qs.get("symbols", [""])[0] or "")
    return app.live_deltas(since=since, symbols_csv=symbols)

def r_suggest(app, qs, q, mode):
    result = app.suggest(q)
    try:
        from core.canonical_presentation_service import CanonicalPresentationService
        rows = (result or {}).get("matches") if isinstance(result, dict) else result if isinstance(result, list) else []
        CanonicalPresentationService(app.store).prime_authority_rows(rows or [])
    except Exception:
        pass
    return result

def r_backfill_progress(app, qs, q, mode):
    return {"ok": True, "backfill": app.status.get("deep_history_backfill") or {"state": "starting", "done": 0, "total": 0}}

def r_daily_learning(app, qs, q, mode):
    rows = app.store.latest_daily_learning(limit=_qint(qs, "limit", 5, min_val=1, max_val=60))
    return {"ok": True, "daily_learning": rows}

def r_heatmap(app, qs, q, mode):
    from reference_catalog import final_heatmap_payload
    return final_heatmap_payload(app)

def _calculate_index_evidence_scores(app, row):
    """Compatibility facade over the canonical core direction-evidence authority."""
    from core.index_direction_evidence_authority import DEFAULT_INDEX_DIRECTION_EVIDENCE_AUTHORITY
    key = str(row.get("instrument_key") or "").strip()
    if not key:
        return DEFAULT_INDEX_DIRECTION_EVIDENCE_AUTHORITY.scores([])
    try:
        # Read-model boundary: this GET performs zero provider I/O.
        candles = list(app._stored_candles(key, "day", limit=260) or [])
    except Exception:
        candles = []
    return DEFAULT_INDEX_DIRECTION_EVIDENCE_AUTHORITY.scores(candles)


_INDEX_EVIDENCE_CACHE = {}

def _index_evidence_scores(app, row):
    key = str(row.get("instrument_key") or "").strip()
    generation = str(row.get("source_time") or row.get("timestamp") or row.get("last_refresh") or "")
    cache_key = (key, generation)
    cached = _INDEX_EVIDENCE_CACHE.get(cache_key)
    if cached is not None:
        return dict(cached)
    result = _calculate_index_evidence_scores(app, row)
    if key:
        if len(_INDEX_EVIDENCE_CACHE) > 256:
            _INDEX_EVIDENCE_CACHE.clear()
        _INDEX_EVIDENCE_CACHE[cache_key] = dict(result)
    return result

def r_indices(app, qs, q, mode):
    from core.runtime_primitives import is_india_market_open
    live = _flag(qs, "refresh")
    market_open = is_india_market_open()
    # GET is a bounded read. Network/provider work belongs to the background
    # market projection worker, never the browser request path.
    raw_rows = app.heatmap_snapshot()
    rows = []
    for raw in list(raw_rows or []):
        row = dict(raw or {})
        row.setdefault("symbol", row.get("name"))
        row.setdefault("trading_symbol", row.get("name"))
        from core.market_snapshot_service import market_group
        row["market_group"] = market_group(row)
        row["type"] = "Sector" if row["market_group"] == "sector" else "Index"
        # Direction/conviction evidence is already materialized by the
        # background heatmap producer. GET is projection-only.
        row.setdefault("direction_authority_ready", False)
        if row.get("ltp") is None:
            row["freshness_state"] = "unavailable"
            row["freshness_reason"] = row.get("reason") or "completed-close data unavailable"
            row["stale"] = True
        elif market_open:
            row["freshness_state"] = "live" if not row.get("stale") else "stale"
        else:
            # The main heatmap authority has already session-validated the close.
            row["freshness_state"] = "current_at_close" if not row.get("stale") else "stale"
        rows.append(row)
    from core.market_snapshot_service import stable_market_snapshot, snapshot_id as market_snapshot_id
    rows = stable_market_snapshot(rows, store=getattr(app, "store", None))
    snapshot_id = market_snapshot_id(rows)
    return {
        "ok": True,
        "state": "LIVE" if market_open else "CLOSED",
        "market_open": market_open,
        "indices": rows,
        "heatmap": rows,
        "verified_count": sum(1 for row in rows if row.get("ltp") is not None),
        "population_count": len(rows),
        "snapshot_id": snapshot_id,
        "snapshot_policy": "newer_non_empty_rows_replace; partial responses retain prior verified identities",
        "required_market_groups": {"indices": ["NIFTY 50", "SENSEX", "INDIA VIX"], "sectors": ["NIFTY BANK", "NIFTY IT", "NIFTY AUTO", "NIFTY FMCG", "NIFTY PHARMA", "NIFTY METAL", "NIFTY REALTY", "NIFTY ENERGY"]},
        "time": now_iso(),
        "auth": app.status.get("auth"),
        "live": live,
        "refresh_policy": "background_projection_only_no_provider_io_in_get",
    }

def r_market_depth(app, qs, q, mode):
    symbol = str(qs.get("symbol", [q])[0] or q or "").strip().upper()
    refresh = _flag(qs, "refresh")
    payload = app.selected_market_depth(symbol, refresh=refresh)
    quote = dict(payload.get("quote") or {})
    market_open = bool(payload.get("market_open"))
    return {
        **payload,
        "market_depth": _market_depth_contract(quote, market_open) if quote else {
            "state": payload.get("state") or "UNAVAILABLE",
            "actionable": False,
            "levels": [],
            "message": payload.get("message") or "Market depth unavailable",
        },
    }

def r_market_status(app, qs, q, mode):
    from core.runtime_primitives import is_india_market_open, minutes_to_close
    return {"market_open": is_india_market_open(), "minutes_to_close": minutes_to_close(), "time": now_iso()}

_COVERAGE_INTERVALS = (
    ("1m", "1minute"), ("3m", "3minute"), ("5m", "5minute"),
    ("15m", "15minute"), ("30m", "30minute"), ("1H", "60minute"),
    ("4H", "240minute"), ("1D", "day"), ("1W", "week"), ("1M", "month"),
)

def _coverage_timestamp(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except Exception:
        return None

def _coverage_state(label: str, count: int, first, last) -> str:
    if count <= 0 or not first or not last:
        return "MISSING"
    stamp = _coverage_timestamp(last)
    if stamp is None:
        return "PARTIAL"
    age_days = max(0.0, (datetime.now(timezone.utc) - stamp).total_seconds() / 86400.0)
    tolerance = {"1m": 5, "3m": 5, "5m": 5, "15m": 5, "30m": 5, "1H": 5, "4H": 8, "1D": 10, "1W": 24, "1M": 70}.get(label, 10)
    return "CURRENT" if age_days <= tolerance else "STALE"

def r_data_coverage(app, qs, q, mode):
    """Fast, measurable historical coverage for UI and installer evidence.

    It never mutates or scans the full Parquet lake.  Selected-symbol coverage
    comes from the authoritative repository manifest/index; aggregate counts
    come from the existing PostgreSQL coverage authority when available.
    """
    symbol = str(qs.get("symbol", [q])[0] or q or "").strip().upper()
    rows = []
    instrument = None
    if symbol:
        try:
            instrument = app._index_instrument_for_chart(symbol)
        except Exception:
            instrument = None
        if not instrument:
            try:
                instrument = app._first_instrument(symbol)
            except Exception:
                instrument = None
        key = str((instrument or {}).get("instrument_key") or "")
        candle_repository = getattr(app.store, "production_candle_repository", None)
        if key and candle_repository is not None:
            for label, interval in _COVERAGE_INTERVALS:
                coverage = candle_repository.candle_coverage(key, interval) or {}
                count = int(coverage.get("count") or 0)
                first = coverage.get("first")
                last = coverage.get("last")
                first_dt, last_dt = _coverage_timestamp(first), _coverage_timestamp(last)
                years = None
                if first_dt and last_dt and last_dt >= first_dt:
                    years = round((last_dt - first_dt).total_seconds() / (365.2425 * 86400.0), 2)
                catalog_state = str(coverage.get("catalog_state") or "MISSING").upper()
                state = "INDEXING" if catalog_state in {"MISSING", "BUILDING", "INVALID"} else _coverage_state(label, count, first, last)
                rows.append({
                    "label": label, "interval": interval, "count": count,
                    "first": first, "last": last, "years": years,
                    "last_received_at": coverage.get("last_received_at"),
                    "source": coverage.get("source"),
                    "state": state, "catalog_state": catalog_state,
                    "file_count": int(coverage.get("file_count") or 0),
                    "indexed": bool(coverage.get("indexed")),
                })
    aggregate = []
    repository = getattr(app, "universe_authority_repository", None)
    if repository is not None and hasattr(repository, "coverage_summary"):
        try:
            aggregate = repository.coverage_summary()
        except Exception:
            aggregate = []
    return {
        "ok": bool(instrument) if symbol else True,
        "symbol": symbol or None,
        "instrument": instrument or None,
        "timeframes": rows,
        "aggregate": aggregate,
        "backfill": dict(getattr(app, "status", {}).get("deep_history_backfill") or {"state": "starting", "done": 0, "total": 0}),
        "candle_catalog": candle_repository.catalog_status() if symbol and candle_repository is not None and hasattr(candle_repository, "catalog_status") else None,
        "protected_asset": True,
        "installer_policy": "PRESERVE_AND_FAIL_ON_UNEXPLAINED_COVERAGE_REGRESSION",
        "incremental_rule": "REQUESTED_RANGE_MINUS_VERIFIED_LOCAL_COVERAGE",
        "time": now_iso(),
    }


def r_priority_pipeline(app, qs, q, mode):
    symbol = str(qs.get("symbol", [q])[0] or q or "").strip().upper()
    desk = str(qs.get("mode", [mode if mode != "all" else "delivery"])[0] or "delivery").strip().lower()
    if not symbol:
        return ({"ok": False, "error": "symbol_required"}, 400)
    try:
        return (getattr(app, "priority_pipeline", None) or PriorityPipelineService(app)).snapshot(symbol=symbol, mode=desk)
    except Exception as exc:
        return ({"ok": False, "state": "PIPELINE_AUTHORITY_UNAVAILABLE", "error": str(exc)[:300]}, 503)



def r_priority_pipeline_recovery(app, qs, q, mode):
    try:
        return (getattr(app, "priority_pipeline", None) or PriorityPipelineService(app)).recovery_status()
    except Exception as exc:
        return ({"ok": False, "state": "PIPELINE_RECOVERY_UNAVAILABLE", "error": str(exc)[:300]}, 503)


def r_evidence_snapshot(app, qs, q, mode):
    symbol = str(qs.get("symbol", [q])[0] or q or "").strip().upper()
    desk = str(qs.get("mode", [mode if mode != "all" else "delivery"])[0] or "delivery").strip().lower()
    if not symbol:
        return ({"ok": False, "error": "symbol_required"}, 400)
    try:
        service = getattr(app, "evidence_snapshots", None) or CanonicalEvidenceSnapshotService(app)
        payload = service.latest(symbol=symbol, mode=desk)
        if payload.get("payload_hash"):
            payload["integrity"] = service.verify(payload)
        return payload
    except Exception as exc:
        return ({"ok": False, "state": "EVIDENCE_SNAPSHOT_UNAVAILABLE", "error": str(exc)[:300]}, 503)


def r_cross_plane_reconciliation(app, qs, q, mode):
    symbol = str(qs.get("symbol", [q])[0] or q or "").strip().upper()
    interval = str(qs.get("interval", ["day"])[0] or "day")
    if not symbol:
        return ({"ok": False, "error": "symbol_required"}, 400)
    try:
        instrument = app._index_instrument_for_chart(symbol) or app._first_instrument(symbol)
    except Exception:
        instrument = None
    key = str((instrument or {}).get("instrument_key") or "")
    if not key:
        return ({"ok": False, "state": "IDENTITY_UNAVAILABLE", "error": "verified instrument identity required"}, 404)
    try:
        service = getattr(app, "cross_plane_reconciliation", None) or CrossPlaneReconciliationService(app)
        return service.reconcile(symbol=symbol, instrument_key=key, interval=interval)
    except Exception as exc:
        return ({"ok": False, "state": "RECONCILIATION_UNAVAILABLE", "error": str(exc)[:300]}, 503)


def r_level5_evidence_matrix(app, qs, q, mode):
    """Fast operator matrix derived from canonical background projections."""
    try:
        return Level5EvidenceMatrixService.materialized(app)
    except Exception as exc:
        return ({"ok": False, "state": "LEVEL5_MATRIX_UNAVAILABLE", "error": str(exc)[:300]}, 503)

def _market_depth_contract(quote, market_open):
    q = dict(quote or {})
    depth = dict(q.get("depth") or {})
    buys = [dict(row) for row in (depth.get("buy") or []) if isinstance(row, dict)][:5]
    sells = [dict(row) for row in (depth.get("sell") or []) if isinstance(row, dict)][:5]
    def _total(rows, key):
        total = 0.0
        for row in rows:
            try:
                total += float(row.get(key) or 0.0)
            except (TypeError, ValueError):
                pass
        return total
    buy_qty = q.get("buy_quantity")
    sell_qty = q.get("sell_quantity")
    try: buy_qty = float(buy_qty) if buy_qty is not None else _total(buys, "quantity")
    except (TypeError, ValueError): buy_qty = _total(buys, "quantity")
    try: sell_qty = float(sell_qty) if sell_qty is not None else _total(sells, "quantity")
    except (TypeError, ValueError): sell_qty = _total(sells, "quantity")
    buy_orders = _total(buys, "orders")
    sell_orders = _total(sells, "orders")
    total = buy_qty + sell_qty
    imbalance = q.get("depth_imbalance_pct")
    if imbalance is None and total:
        imbalance = round((buy_qty - sell_qty) * 100.0 / total, 1)
    best_bid = q.get("best_bid") or (buys[0].get("price") if buys else None)
    best_ask = q.get("best_ask") or (sells[0].get("price") if sells else None)
    spread = q.get("spread")
    try:
        if spread is None and best_bid is not None and best_ask is not None:
            spread = round(float(best_ask) - float(best_bid), 2)
    except (TypeError, ValueError):
        spread = None
    midpoint = None
    spread_bps = None
    try:
        midpoint = (float(best_bid) + float(best_ask)) / 2.0
        spread_bps = round(float(spread) * 10000.0 / midpoint, 2) if midpoint and spread is not None else None
    except (TypeError, ValueError):
        pass
    has_depth = bool(buys or sells or buy_qty or sell_qty)
    state = "LIVE" if market_open and has_depth else "CLOSED" if not market_open and has_depth else "UNAVAILABLE"
    return {
        "state": state,
        "actionable": bool(market_open and has_depth),
        "buy_quantity": buy_qty or None,
        "sell_quantity": sell_qty or None,
        "buy_orders": int(buy_orders) if buy_orders else None,
        "sell_orders": int(sell_orders) if sell_orders else None,
        "imbalance_pct": imbalance,
        "best_bid": best_bid,
        "best_ask": best_ask,
        "spread": spread,
        "spread_bps": spread_bps,
        "levels": [{"bid": buys[i] if i < len(buys) else {}, "ask": sells[i] if i < len(sells) else {}} for i in range(max(len(buys), len(sells)))],
        "message": "Live selected-stock market depth; evidence only" if market_open and has_depth else "Market depth closed · last session snapshot · non-actionable" if has_depth else "Market depth unavailable; select Sync during market hours",
    }


def r_level5_operational_proof(app, qs, q, mode):
    """Current-build target evidence gate; never fabricates missing proof."""
    return Level5OperationalProofService(app).status()
