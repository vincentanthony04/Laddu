"""
SystemHealthService -- v51, Cluster 8 of the LadduRuntime extraction
(see EXTRACTION_HANDOFF_v51.md).

Owns every "is the system alive/producing fresh data" read: process/loop
status (health, health_light), data-truth status sourced from the store
(system_health), the scanner/instrument status endpoints, and the API-error
noise filter (_visible_api_errors).

This is the last facade cluster -- every method here is a pure read over
state LadduRuntime already owns (status dict, store, client, supervisor,
rate controller, fundamentals store). Nothing here mutates anything or
does I/O beyond what those objects already expose, so it takes a single
`host` reference (the owning LadduRuntime) rather than a pile of
individually-injected callables -- a fake narrow interface would just be
the same host access with extra steps.
"""
from __future__ import annotations

from typing import Any, Dict
import threading
import time

from core.local_projection_dispatcher import for_app as local_projection_dispatcher_for_app


class SystemHealthService:
    def __init__(self, host: Any):
        self.host = host

    def _cached_token_status(self, status: Dict[str, Any]) -> Dict[str, Any]:
        """No-I/O token view for health endpoints.

        Decrypting the Windows token can launch PowerShell and wait six seconds;
        a process health probe must never trigger that path.
        """
        client = self.host.client
        cached = bool(getattr(client, "_token_cache", None))
        auth = dict(status.get("auth") or {})
        auth_state = str(auth.get("state") or "unknown")
        if cached:
            return {"ok": True, "state": "Token cached", "message": "In-memory token available", "source": "memory"}
        if auth_state in {"ok", "ready", "authenticated", "quote_rate_limited", "historical_rate_limited"} or auth.get("quote_ok") is True or auth.get("historical_ok") is True:
            return {"ok": True, "state": "Token previously verified", "message": "Cached authentication state", "source": "health_registry"}
        return {"ok": False, "state": "Token state pending", "message": "Use /api/auth-test for an explicit token check", "source": "health_registry"}

    @staticmethod
    def _fundamental_count(row: Dict[str, Any]) -> int:
        values = []
        for key in ("count", "symbols", "available_symbol_count", "live_cache_count"):
            try:
                values.append(int(row.get(key) or 0))
            except Exception:
                values.append(0)
        return max(values or [0])

    def _fundamentals_snapshot(self, status: Dict[str, Any]) -> Dict[str, Any]:
        """Return the strongest cache-only fundamentals authority.

        ProductReadinessService already used the governed provider-chain cache,
        while /api/health exposed only a lagging startup metadata dict.  The two
        endpoints could therefore report 1,584 READY and zero rows at the same
        instant.  Health and readiness now select from the same published
        sources without performing network work.
        """
        candidates = []
        for row in (
            status.get("fundamentals"),
            getattr(self.host, "_fundamental_health_meta", None),
            getattr(self.host, "status", {}).get("fundamentals") if isinstance(getattr(self.host, "status", {}), dict) else None,
        ):
            if isinstance(row, dict) and row:
                candidates.append(dict(row))
        reference_data = getattr(self.host, "reference_data", None)
        provider_reader = getattr(reference_data, "fundamental_provider_status", None)
        if callable(provider_reader):
            try:
                row = dict(provider_reader() or {})
                if row:
                    row["readiness_source"] = "reference_data_provider_chain"
                    candidates.append(row)
            except Exception:
                pass
        local_store = getattr(self.host, "fundamentals", None)
        local_reader = getattr(local_store, "status", None)
        if callable(local_reader):
            try:
                row = dict(local_reader() or {})
                if row:
                    row["readiness_source"] = "fundamental_store_local_import"
                    candidates.append(row)
            except Exception:
                pass
        if not candidates:
            return {"loaded": False, "ready": False, "count": 0, "state": "not_loaded", "source": None}
        selected = max(
            candidates,
            key=lambda row: (
                1 if row.get("loaded") or row.get("ready") else 0,
                self._fundamental_count(row),
            ),
        )
        count = self._fundamental_count(selected)
        ready = bool((selected.get("loaded") or selected.get("ready")) and count > 0)
        return {
            **selected,
            "count": count,
            "symbols": count,
            "loaded": ready,
            "ready": ready,
            "state": selected.get("state") or ("READY" if ready else "not_loaded"),
        }

    def health(self) -> Dict[str, Any]:
        """Fast operational health; cache/memory only with a strict lock budget."""
        from config import APP_VERSION
        from runtime_shared import PORT
        from core.runtime_primitives import is_india_market_open, minutes_to_close
        from models import now_iso
        host = self.host
        status = host.health_status_snapshot()
        snapshot_meta = dict(status.pop("_health_snapshot", {}) or {})
        instruments = dict(getattr(host, "_instrument_health_meta", {}) or {})
        fundamentals = self._fundamentals_snapshot(status)
        mins = minutes_to_close()
        return {
            "app": "Project Laddu",
            "version": APP_VERSION,
            "service": status.get("service"),
            "time": now_iso(),
            "market_timezone": "Asia/Kolkata",
            "market_open": is_india_market_open(),
            "minutes_to_close": mins,
            "late_session_block": (mins is not None and mins <= 30),
            "hard_late_session_block": (mins is not None and mins <= 15),
            "token": self._cached_token_status(status),
            "auth": status.get("auth"),
            "instruments": instruments,
            "fundamentals": fundamentals,
            "scanner": status,
            # Stable top-level authority projections for installed verifiers and
            # operator tooling. The scanner projection remains for compatibility.
            "production_data_plane": status.get("production_data_plane"),
            "quant_research_plane": status.get("quant_research_plane"),
            "startup_phases": status.get("startup_phases"),
            "health_snapshot": snapshot_meta,
            "urls": {"local": f"http://127.0.0.1:{PORT}", "lan_hint": f"http://<this-pc-ip>:{PORT}"},
            "reliability": {
                "loops": host.supervisor.snapshot(),
                "rate_controller": host.rate.snapshot(),
            },
            "probe": "memory_and_cached_metadata_only",
        }

    def _compute_system_health(self) -> Dict[str, Any]:
        """Authoritative cross-plane data truth producer for installed validation.

        Production must not expose the legacy SQLite compatibility projection
        as candle/ledger authority.  This method therefore reconciles the
        Parquet catalogue, QuestDB writer, canonical PostgreSQL ledger, and
        connection-pool health.  A missing authority is reported as
        unavailable rather than silently converted to zero/ready.
        """
        from core.runtime_primitives import is_india_market_open
        from models import now_iso
        host = self.host
        store_facts = dict(host.store.system_health_snapshot() or {})
        status = host.health_status_snapshot()

        lake_repo = getattr(host.store, "production_candle_repository", None)
        lake_reader = getattr(lake_repo, "physical_summary", None)
        try:
            lake = dict(lake_reader() or {}) if callable(lake_reader) else {}
        except Exception as exc:
            lake = {"state": "UNAVAILABLE", "error": f"{type(exc).__name__}: {exc}"[:240]}

        data_plane = getattr(host, "production_data_plane", None)
        questdb_obj = getattr(data_plane, "questdb", None)
        try:
            questdb = dict(questdb_obj.status() or {}) if questdb_obj is not None else {}
        except Exception as exc:
            questdb = {"state": "UNAVAILABLE", "error": f"{type(exc).__name__}: {exc}"[:240]}

        operational = getattr(data_plane, "operational", None)
        governance = getattr(data_plane, "governance", None)
        def pool_health(authority):
            reader = getattr(authority, "pool_health", None)
            try:
                return dict(reader() or {}) if callable(reader) else {"state": "UNAVAILABLE"}
            except Exception as exc:
                return {"state": "UNAVAILABLE", "error": f"{type(exc).__name__}: {exc}"[:240]}

        ledger = dict(store_facts.get("signal_ledger") or {})
        journal = dict(store_facts.get("trade_journal") or {})
        if not ledger:
            ledger = {
                "last": store_facts.get("last_ledger_write"),
                "open": store_facts.get("open_ledger_rows"),
                "total": store_facts.get("ledger_rows_total"),
                "authority": "COMPATIBILITY",
            }
        if not journal:
            journal = {"last": store_facts.get("last_journal_write"), "authority": "COMPATIBILITY"}

        durable_rows = lake.get("durable_rows")
        durable_latest = lake.get("durable_latest")
        questdb_written = questdb.get("written")
        ingestion_available = durable_rows is not None or questdb_written is not None
        return {
            "time": now_iso(),
            "market_open": is_india_market_open(),
            "ingestion": {
                "authority": "PARQUET_CATALOG+QUESTDB_WRITER",
                "available": ingestion_available,
                "last_quote_stored": store_facts.get("last_quote_stored"),
                "last_candle_stored": durable_latest,
                "candles_total": durable_rows,
                "restart_safe": bool(lake.get("restart_safe")),
                "parquet": lake,
                "questdb": questdb,
                "last_price_refresh_status_field": status.get("last_price_refresh"),
                "last_historical_fetch_status_field": status.get("last_historical_fetch"),
                "compatibility_projection": {
                    "last_candle_stored": store_facts.get("last_candle_stored"),
                    "candles_total": store_facts.get("candles_total"),
                },
            },
            "signal_ledger": {
                "last_write": ledger.get("last"),
                "open_rows": ledger.get("open"),
                "total_rows": ledger.get("total"),
                "authority": ledger.get("authority") or "UNAVAILABLE",
                "available": ledger.get("total") is not None,
                "note": "Canonical PostgreSQL decisions are the only production ledger authority; unavailable is never converted to zero.",
            },
            "trade_journal": {
                "last_write": journal.get("last"),
                "total_rows": journal.get("total"),
                "authority": journal.get("authority") or "UNAVAILABLE",
            },
            "database_pools": {
                "operational": pool_health(operational),
                "governance": pool_health(governance),
            },
            "loops": host.supervisor.snapshot(),
            "rate_controller": host.rate.snapshot(),
            "reference_data": {
                "runs": host.store.reference_run_status(),
                "delivery_auto_sync": status.get("delivery_data_sync"),
                "latest_breadth": host.store.get_latest_market_breadth("NIFTY250_CORE"),
                "earnings_calendar": status.get("earnings_calendar") or "not_materialised",
            },
        }

    def system_health(self) -> Dict[str, Any]:
        """Return one completed health projection; physical truth is producer work.

        The HTTP thread never performs Parquet, QuestDB or PostgreSQL aggregate
        reconciliation. A coalesced local producer refreshes the last-completed
        snapshot on the dedicated read-model lane. This is deliberately the same
        two-lane architecture used by selected-stock/chart materialization: local
        computation is isolated from provider/repair work.
        """
        from models import now_iso
        host = self.host
        lock = getattr(host, "_system_health_projection_lock", None)
        if lock is None:
            lock = threading.RLock()
            setattr(host, "_system_health_projection_lock", lock)
        now_mono = time.monotonic()
        with lock:
            cached = dict(getattr(host, "_system_health_projection", {}) or {})
            completed_at = float(getattr(host, "_system_health_projection_completed_mono", 0.0) or 0.0)
            age = max(0.0, now_mono - completed_at) if completed_at else None

        def produce() -> None:
            payload = dict(self._compute_system_health() or {})
            with lock:
                setattr(host, "_system_health_projection", payload)
                setattr(host, "_system_health_projection_completed_mono", time.monotonic())

        refreshing = False
        if age is None or age >= 5.0:
            submit = local_projection_dispatcher_for_app(host).submit("system-health:v3", produce)
            refreshing = bool(submit.accepted or submit.state == "COALESCED")

        if cached:
            cached["projection"] = {
                "state": "CURRENT" if age is not None and age < 5.0 else "REFRESHING",
                "age_sec": round(age, 3) if age is not None else None,
                "refreshing": refreshing,
                "serving_policy": "IN_MEMORY_COMPLETED_SNAPSHOT_ONLY",
            }
            cached["local_projection_lane"] = local_projection_dispatcher_for_app(host).status()
            return cached

        status = host.health_status_snapshot()
        return {
            "time": now_iso(),
            "state": "WARMING",
            "market_open": None,
            "ingestion": {
                "authority": "BACKGROUND_HEALTH_PROJECTION",
                "available": None,
                "last_quote_stored": status.get("last_quote_stored"),
                "last_candle_stored": None,
                "candles_total": None,
                "restart_safe": None,
                "parquet": {"state": "WARMING"},
                "questdb": {"state": "WARMING"},
            },
            "signal_ledger": {"authority": "POSTGRESQL_CANONICAL_DECISIONS", "available": None, "state": "WARMING"},
            "trade_journal": {"authority": "POSTGRESQL", "state": "WARMING"},
            "database_pools": {"operational": {"state": "WARMING"}, "governance": {"state": "WARMING"}},
            "loops": host.supervisor.snapshot(),
            "rate_controller": host.rate.snapshot(),
            "reference_data": {"state": "WARMING"},
            "projection": {
                "state": "WARMING",
                "age_sec": None,
                "refreshing": refreshing,
                "serving_policy": "IN_MEMORY_COMPLETED_SNAPSHOT_ONLY",
            },
            "local_projection_lane": local_projection_dispatcher_for_app(host).status(),
        }

    def visible_api_errors(self) -> list[Dict[str, Any]]:
        out = []
        for e in (self.host.health_status_snapshot().get("api_errors") or []):
            msg = str(e.get("error") or "").lower()
            module = str(e.get("module") or "")
            if module in ("deep_scan", "index_historical") and ("bad parameter" in msg or "400" in msg):
                continue
            out.append(e)
        return out[-5:]

    def health_light(self) -> Dict[str, Any]:
        """Compatibility alias for the decomposed, cache-only health plane."""
        payload = self.health()
        scanner = dict(payload.get("scanner") or {})
        payload["scanner"] = {
            "service": scanner.get("service"),
            "fast_lane": scanner.get("fast_lane"),
            "deep_scan": scanner.get("deep_scan"),
            "mode_scanners": scanner.get("mode_scanners"),
            "last_price_refresh": scanner.get("last_price_refresh"),
            "last_historical_fetch": scanner.get("last_historical_fetch"),
            "last_fundamental_refresh": scanner.get("last_fundamental_refresh"),
            "last_delivery_refresh": scanner.get("last_delivery_refresh"),
            "delivery_data_sync": scanner.get("delivery_data_sync"),
            "last_ai_validation": scanner.get("last_ai_validation"),
            "api_errors": self.visible_api_errors(),
        }
        return payload

    def scanner_status(self) -> Dict[str, Any]:
        from core.runtime_primitives import is_india_market_open, minutes_to_close
        from models import now_iso
        host = self.host
        status = host.health_status_snapshot()
        return {
            "service": status.get("service"),
            "market_open": is_india_market_open(),
            "minutes_to_close": minutes_to_close(),
            "token": self._cached_token_status(status),
            "auth": status.get("auth"),
            "instruments": dict(getattr(host, "_instrument_health_meta", {}) or {}),
            "scanner": status,
            "events": host.store.events(limit=40),
            "time": now_iso(),
            "contract_version": "scanner-status-v2-snapshot",
        }

    def instruments_status(self) -> Dict[str, Any]:
        from models import now_iso
        host = self.host
        meta = host.client._cached_instrument_meta("status-cache")
        return {"ok": bool(meta.get("loaded")), "count": host.store.instrument_count(), "meta": meta, "refreshing": bool(getattr(host.client, "_instrument_refreshing", False)), "time": now_iso()}
