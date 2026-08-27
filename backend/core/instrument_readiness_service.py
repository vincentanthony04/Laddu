from __future__ import annotations

import threading
import time
from typing import Any, Callable, Iterable

from core.instrument_universe_policy import ACTIVE_UNIVERSE_REVISION


def run_instrument_readiness(
    host: Any,
    sup: Any = None,
    *,
    running_fn: Callable[[], bool],
    cleanup_symbols: Iterable[str],
) -> None:
    """Bounded instrument authority readiness loop.

    Catalogue liveness must never be held hostage by optional fundamentals/auth,
    symbol-index warming, universe freezing, or provider-side reconciliation.
    Those tasks are detached once and publish their own evidence/events.
    """
    last_loaded = False
    optional_started = False
    post_ready_thread: threading.Thread | None = None
    optional_thread: threading.Thread | None = None

    def schedule_post_ready(meta: dict) -> None:
        nonlocal post_ready_thread
        if post_ready_thread is not None and post_ready_thread.is_alive():
            return
        def work():
            symbol_index_count = 0
            try:
                symbol_index_count = int(host.store.warm_symbol_index())
            except Exception as exc:
                host.event("WARN", "instruments", "Focused symbol index warm-up failed", {"error": str(exc)[:240]})
            try:
                proof = host.freeze_authoritative_universe()
                host.event("INFO", "universe", "Immutable Delivery and Intraday snapshots frozen", proof)
            except Exception as exc:
                host.event("ERROR", "universe", "Authoritative universe freeze failed", {"error": str(exc)[:240]})
            host.event("INFO", "instruments", "Post-ready catalogue work completed", {"symbol_index_count": symbol_index_count, "count": meta.get("count")})
        post_ready_thread = threading.Thread(target=work, name="LadduInstrumentPostReady", daemon=True)
        post_ready_thread.start()

    def schedule_optional_bootstrap() -> None:
        nonlocal optional_started, optional_thread
        if optional_started:
            return
        optional_started = True
        def work():
            try:
                host.fundamentals.load(force=False)
                fmeta = host.reference_data.fundamental_provider_status()
                host._fundamental_health_meta = dict(fmeta or {})
                host.health_registry.publish_component("fundamentals", host._fundamental_health_meta)
                try:
                    cleaned = host.store.cleanup_scanner_artifacts(cleanup_symbols)
                    if cleaned.get("decisions") or cleaned.get("signals"):
                        host.event("INFO", "scanner", "Legacy A-list scanner artifacts cleaned", cleaned)
                except Exception as exc:
                    host.event("WARN", "scanner", "Scanner artifact cleanup skipped", {"error": str(exc)[:240]})
                host._set_status("last_fundamental_refresh", fmeta.get("last_refresh"))
                host.event(
                    "INFO" if fmeta.get("loaded") else "WARN", "fundamentals",
                    "Fundamental provider ready" if fmeta.get("loaded") else "Local fundamentals file missing; Upstox API fallback will be used when ISIN is available",
                    fmeta,
                )
                auth = host.auth_test(force=False)
                host.event("INFO" if auth.get("historical", {}).get("ok") else "WARN", "auth", "Upstox auth preflight completed", auth)
            except Exception as exc:
                host.event("WARN", "instruments", "Optional fundamentals/auth bootstrap failed", {"error": str(exc)[:240]})
        optional_thread = threading.Thread(target=work, name="LadduOptionalInstrumentBootstrap", daemon=True)
        optional_thread.start()

    while running_fn() and (sup is None or sup.running):
        if sup:
            sup.beat("instrument_bootstrap")
        try:
            meta = host.client._cached_instrument_meta("readiness-loop")
            stats = dict(meta.get("universe_stats") or {})
            revision_current = meta.get("universe_revision") == ACTIVE_UNIVERSE_REVISION
            loaded = bool(
                meta.get("loaded")
                and int(meta.get("count") or 0) > 0
                and revision_current
                and int(stats.get("derivatives") or 0) == 0
            )
            host._instrument_health_meta = dict(meta or {})
            host.health_registry.publish_component("instruments", host._instrument_health_meta)
            if loaded and not last_loaded:
                cleared = 0
                try:
                    cleared = int(host.instrument_resolver.clear_negative_cache())
                except Exception:
                    cleared = 0
                host.event("INFO", "instruments", "Focused NSE/BSE catalogue ready; search, subscriptions and scanners released", {**dict(meta or {}), "negative_resolutions_released": cleared})
                try:
                    host._refresh_live_subscription_plan(force=True)
                except Exception as exc:
                    host.event("WARN", "live_market_gateway", "Initial live subscription plan refresh deferred", {"error": str(exc)[:240]})
                schedule_post_ready(dict(meta or {}))
            elif not loaded and not getattr(host.client, "_instrument_refreshing", False):
                reason = "focused universe migration required" if not revision_current and int(meta.get("count") or 0) > 0 else "instrument master unavailable"
                host.event("WARN", "instruments", f"{reason}; bounded background refresh started", meta)
                host.client.refresh_instruments_background(force=not revision_current)
            last_loaded = loaded
            schedule_optional_bootstrap()
            if sup:
                count = int(meta.get("count") or 0)
                sup.progress(
                    "instrument_bootstrap", token=f"{count}:{meta.get('universe_revision')}:{int(loaded)}",
                    stage="catalogue_ready" if loaded else "catalogue_refresh",
                    completed_units=count, total_units=count if loaded and count > 0 else None,
                    waiting_on=None if loaded else "focused instrument authority", expected_idle=loaded,
                )
        except Exception as exc:
            host.event("WARN", "instruments", "Instrument readiness loop failed", {"error": str(exc)[:240]})
        # Frequent enough to remain observable; no synchronous provider/storage
        # work is allowed in this supervisor-owned loop.
        time.sleep(15 if not last_loaded else 30)
