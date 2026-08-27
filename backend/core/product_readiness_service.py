"""Cache-only installed-product readiness and failure challenge plane.

This service deliberately does not touch SQLite, DuckDB, the broker, model
training, or any scanner calculation.  It answers a different question from
``/api/ready`` and ``/api/health``:

    Is the installed product currently capable of producing useful, fresh,
    explainable operator output?

A responsive process is not an operational product.  The report separates
process readiness, operational readiness, usefulness, and empirical edge so a
release cannot be called successful merely because it starts or installs.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional

from models import now_iso
from config import APP_VERSION, BUILD_MARKER
from core.instrument_universe_policy import ACTIVE_UNIVERSE_REVISION
from core.operational_acceptance_contract import (
    REQUIRED_RUNTIME_STATUS_KEYS, evaluate_installation_acceptance,
)
from core.startup_phase_contract import startup_phase_summary

SERVICE_VERSION = "product-readiness-2.1.0-exact-build-browser-proof"


@dataclass(frozen=True)
class Check:
    key: str
    state: str
    title: str
    detail: str
    action: str
    owner: str
    critical: bool = True

    def payload(self) -> Dict[str, Any]:
        return asdict(self)


def _lower(value: Any) -> str:
    return str(value or "").strip().lower()


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except Exception:
        return 0


def _float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except Exception:
        return 0.0


class ProductReadinessService:
    """Build a bounded readiness report from in-memory/cached state only."""

    def __init__(self, host: Any, *, market_open_fn: Callable[[], bool]):
        self.host = host
        self.market_open_fn = market_open_fn

    def _status(self) -> Dict[str, Any]:
        reader = getattr(self.host, "health_status_snapshot", None)
        if callable(reader):
            try:
                status = dict(reader() or {})
            except Exception:
                status = dict(getattr(self.host, "status", {}) or {})
        else:
            status = dict(getattr(self.host, "status", {}) or {})

        # Installed usefulness gates consume the same bounded authorities as
        # /api/system-health.  Infrastructure metadata alone cannot prove a
        # persisted candle, ledger continuity, or database-pool health.
        system_health = getattr(self.host, "system_health_service", None)
        health_reader = getattr(system_health, "system_health", None)
        if callable(health_reader):
            try:
                truth = dict(health_reader() or {})
                for key in ("ingestion", "signal_ledger", "trade_journal", "database_pools"):
                    if key in truth:
                        status[key] = truth[key]
            except Exception as exc:
                status.setdefault("ingestion", {"available": False, "error": f"{type(exc).__name__}: {exc}"[:240]})

        priority = getattr(self.host, "priority_pipeline", None)
        recovery_reader = getattr(priority, "recovery_status", None)
        if callable(recovery_reader):
            try:
                status["priority_pipeline_recovery"] = dict(recovery_reader() or {})
            except Exception as exc:
                status["priority_pipeline_recovery"] = {"state": "UNAVAILABLE", "error": f"{type(exc).__name__}: {exc}"[:240]}

        store = getattr(self.host, "store", None)
        kv_reader = getattr(store, "get_kv", None)
        if callable(kv_reader):
            try:
                status["browser_proof"] = dict(kv_reader("level4_browser_proof:last", {}) or {})
            except Exception:
                status.setdefault("browser_proof", {})
        return status

    def _fundamentals(self, status: Mapping[str, Any]) -> Dict[str, Any]:
        """Choose the strongest already-published fundamentals authority.

        The health registry can lag the live FundamentalStore during startup.
        This merge remains cache-only and prevents a valid 1,500+ symbol live
        cache from being rendered as 0% merely because one projection is old.
        """
        candidates: List[Dict[str, Any]] = []
        host_status = getattr(self.host, "status", {})
        if not isinstance(host_status, Mapping):
            host_status = {}
        for row in (
            status.get("fundamentals"),
            host_status.get("fundamentals"),
            getattr(self.host, "_fundamental_health_meta", None),
        ):
            if isinstance(row, Mapping) and row:
                candidates.append(dict(row))

        # The authoritative provider-chain count lives on ReferenceDataService.
        # FundamentalStore.status() only describes the optional local import and
        # can legitimately remain zero while 1,500+ verified Upstox cache rows
        # are available.  Read the already-published provider status directly;
        # this is cache-only and performs no network or analytical work.
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

        store = getattr(self.host, "fundamentals", None)
        reader = getattr(store, "status", None)
        if callable(reader):
            try:
                row = dict(reader() or {})
                if row:
                    row["readiness_source"] = "fundamental_store_local_import"
                    candidates.append(row)
            except Exception:
                pass
        if not candidates:
            return {}
        def count(row):
            return max(_int(row.get("count")), _int(row.get("symbols")), _int(row.get("available_symbol_count")), _int(row.get("live_cache_count")))
        selected = max(candidates, key=lambda row: (1 if row.get("loaded") or row.get("ready") else 0, count(row)))
        resolved_count = count(selected)
        return {**selected, "count": resolved_count, "loaded": bool((selected.get("loaded") or selected.get("ready")) and resolved_count > 0)}

    def _instruments(self, status: Mapping[str, Any]) -> Dict[str, Any]:
        """Resolve one authoritative instrument readiness snapshot.

        Product readiness is an HTTP snapshot and must not touch SQLite.
        Merge only already-published health-registry and host-memory states,
        preferring the strongest usable observation while preserving provenance.
        """
        candidates: List[Dict[str, Any]] = []
        status_row = dict(status.get("instruments") or {})
        if status_row:
            candidates.append({**status_row, "readiness_source": "health_status"})
        host_row = dict(getattr(self.host, "_instrument_health_meta", {}) or {})
        if host_row:
            candidates.append({**host_row, "readiness_source": "host_memory"})
        if not candidates:
            return {}
        def rank(row: Mapping[str, Any]) -> tuple:
            count = _int(row.get("count"))
            stats = dict(row.get("universe_stats") or {})
            usable = bool(row.get("loaded") and row.get("cache_usable") and count > 0)
            focused = bool(
                usable
                and row.get("universe_revision") == ACTIVE_UNIVERSE_REVISION
                and _int(stats.get("derivatives")) == 0
            )
            # A smaller focused catalogue is stronger evidence than a larger
            # stale provider-wide count. This prevents the old 138K status row
            # from overriding the freshly published NSE/BSE active universe.
            return (1 if focused else 0, 1 if usable else 0, count)
        selected = max(candidates, key=rank)
        return {
            **selected,
            "count": _int(selected.get("count")),
            "loaded": bool(selected.get("loaded")),
            "cache_usable": bool(selected.get("cache_usable")),
            "observations": [
                {
                    "source": row.get("readiness_source"),
                    "loaded": bool(row.get("loaded")),
                    "cache_usable": bool(row.get("cache_usable")),
                    "count": _int(row.get("count")),
                    "universe_revision": row.get("universe_revision"),
                    "derivatives": _int(dict(row.get("universe_stats") or {}).get("derivatives")),
                }
                for row in candidates
            ],
        }

    def _loops(self) -> Dict[str, Any]:
        supervisor = getattr(self.host, "supervisor", None)
        if supervisor is None:
            return {}
        try:
            return dict(supervisor.snapshot() or {})
        except Exception:
            return {}

    def _gateway(self) -> Dict[str, Any]:
        live_market = getattr(self.host, "live_market", None)
        if live_market is None:
            return {}
        try:
            return dict(live_market.status() or {})
        except Exception:
            return {}

    @staticmethod
    def _loop_check(loops: Mapping[str, Any], name: str, *, title: str, action: str, critical: bool = True) -> Check:
        row = dict(loops.get(name) or {})
        alive = row.get("alive") is True
        stale = row.get("stale") is True
        if alive and not stale:
            return Check(name, "READY", title, "worker heartbeat is current", "none", "runtime", critical)
        if alive and stale:
            return Check(name, "DEGRADED", title, f"worker heartbeat is stale ({row.get('heartbeat_age_sec')}s)", action, "runtime", critical)
        return Check(name, "BLOCKED", title, "worker is not alive", action, "runtime", critical)

    def assess(self) -> Dict[str, Any]:
        status = self._status()
        loops = self._loops()
        gateway = self._gateway()
        market_open = bool(self.market_open_fn())
        checks: List[Check] = []

        missing_runtime_status_keys = [key for key in REQUIRED_RUNTIME_STATUS_KEYS if key not in status]

        data_plane = dict(status.get("production_data_plane") or {})
        data_plane_mode = _lower(data_plane.get("mode"))
        data_plane_ready = (
            "production_data_plane" in status
            and data_plane_mode == "production"
            and data_plane.get("production_ready") is True
        )
        data_plane_blockers = list(data_plane.get("blockers") or [])
        if "production_data_plane" not in status:
            data_plane_detail = "REQUIRED_READINESS_STATUS_MISSING: production_data_plane"
        elif data_plane_ready:
            data_plane_detail = "PostgreSQL operational/governance authorities and QuestDB market plane ready"
        else:
            data_plane_detail = f"mode={data_plane_mode or 'unknown'}; blockers={','.join(data_plane_blockers) or 'production authority not enabled'}"
        checks.append(Check(
            "production_data_plane",
            "READY" if data_plane_ready else "BLOCKED",
            "Production data plane",
            data_plane_detail,
            "restore the dedicated PostgreSQL and QuestDB services; no SQLite production fallback is permitted",
            "architecture",
            critical=True,
        ))

        research_migration = dict(status.get("research_governance_migration") or {})
        research_migration_ready = bool(
            research_migration.get("count_verified") is True
            and research_migration.get("hash_verified") is True
            and research_migration.get("quarantine_verified") is True
        )
        checks.append(Check(
            "research_governance_migration",
            "READY" if research_migration_ready else "BLOCKED",
            "Research governance migration",
            (
                "retired SQLite research evidence has a verified PostgreSQL migration checkpoint"
                if research_migration_ready else
                f"state={research_migration.get('state') or 'UNVERIFIED'}; historical research authority is not admitted"
            ),
            "run the installer-owned legacy research governance migration while runtime is quiescent",
            "architecture",
            critical=True,
        ))

        research_plane = dict(status.get("quant_research_plane") or {})
        research_ready = (
            "quant_research_plane" in status
            and research_plane.get("ok") is True
            and str(research_plane.get("state") or "").upper() == "READY"
        )
        research_blockers = list(research_plane.get("blockers") or [])
        if "quant_research_plane" not in status:
            research_detail = "REQUIRED_READINESS_STATUS_MISSING: quant_research_plane"
        elif research_ready:
            research_detail = (
                "isolated mathematics/AI runtime ready; Parquet/DuckDB training, "
                "PostgreSQL governance publication, active capped Hybrid authority and deterministic fallback enforced"
            )
        else:
            research_detail = ";".join(research_blockers) or "authoritative Quant/AI research plane is not ready"
        checks.append(Check(
            "quant_research_plane",
            "READY" if research_ready else "BLOCKED",
            "Quant/AI research plane",
            research_detail,
            "repair the isolated research runtime and rerun the installed verifier; live quote/stop/risk loops remain isolated",
            "research",
            critical=False,
        ))

        startup = dict(status.get("startup_phases") or {})
        startup_summary = startup_phase_summary(startup)
        startup_state = str(startup_summary.get("state") or "MISSING").upper()
        startup_ready = (
            "startup_phases" in status
            and startup_state == "COMPLETE"
            and startup_summary.get("required_complete") is True
            and not startup_summary.get("required_failures")
        )
        bulk_state = str((startup.get("bulk") or {}).get("state") or "PENDING").upper()
        if "startup_phases" not in status:
            detail = "REQUIRED_READINESS_STATUS_MISSING: startup_phases"
        elif startup_ready:
            detail = (
                "HTTP, critical risk/data workers and bounded operational read models are ready; "
                f"optional bulk hydration={bulk_state.lower()}"
            )
        else:
            failures = list(startup_summary.get("required_failures") or [])
            pending = list(startup_summary.get("required_pending") or [])
            detail = f"state={startup_state}; required={','.join(failures + pending) or 'unknown'}; bulk={bulk_state}"
        checks.append(Check(
            "startup_phases",
            "READY" if startup_ready else "BLOCKED",
            "Phased startup",
            detail,
            "repair the exact required phase reported; bulk hydration remains observable but does not block a safe installation",
            "runtime",
            critical=True,
        ))

        instruments = self._instruments(status)
        instrument_count = _int(instruments.get("count"))
        universe_revision = str(instruments.get("universe_revision") or "")
        stats = dict(instruments.get("universe_stats") or {})
        derivatives = _int(stats.get("derivatives"))
        out_of_policy_rows = _int(stats.get("out_of_policy_rows"))
        nse_equities = _int(stats.get("nse_equities"))
        bse_only_equities = _int(stats.get("bse_only_equities"))
        indices = _int(stats.get("indices"))
        instrument_ready = bool(
            instruments.get("loaded")
            and instruments.get("cache_usable")
            and instrument_count > 0
            and universe_revision == ACTIVE_UNIVERSE_REVISION
            and nse_equities > 0
            and bse_only_equities > 0
            and indices > 0
            and derivatives == 0
            and out_of_policy_rows == 0
        )
        if instrument_ready:
            detail = (
                f"NSE {nse_equities:,} · "
                f"BSE-only {bse_only_equities:,} · "
                f"indices {indices:,} · derivatives 0 · out-of-policy 0"
            )
        elif instrument_count > 0 and universe_revision != ACTIVE_UNIVERSE_REVISION:
            detail = f"legacy/provider-wide catalogue still active ({instrument_count:,} rows); focused refresh required"
        elif derivatives > 0:
            detail = f"active catalogue contains {derivatives:,} derivative rows; binding universe violated"
        elif out_of_policy_rows > 0:
            detail = f"active catalogue contains {out_of_policy_rows:,} non-stock cash rows; binding universe violated"
        elif nse_equities <= 0 or bse_only_equities <= 0 or indices <= 0:
            detail = f"focused catalogue incomplete: NSE={nse_equities:,}; BSE-only={bse_only_equities:,}; indices={indices:,}"
        else:
            detail = "focused NSE/BSE cash-equity catalogue is not usable"
        checks.append(Check(
            "instrument_identity",
            "READY" if instrument_ready else "BLOCKED",
            "Instrument identity",
            detail,
            "wait for the atomic NSE+BSE refresh; if unchanged, run the installed operational verifier",
            "data",
        ))

        # Search remains available from a small trusted recovery catalogue while
        # the full master warms, but company-name/prefix discovery needs the RAM
        # index built from the downloaded master.
        search_state = "READY" if instrument_ready else "DEGRADED"
        checks.append(Check(
            "search",
            search_state,
            "Symbol search",
            "full local symbol/company index ready" if instrument_ready else "exact recovery symbols only; broad search is unavailable",
            "do not treat an empty search result as a valid stock conclusion",
            "identity",
            critical=False,
        ))

        subscriptions = dict(gateway.get("subscriptions") or {})
        desired = _int(subscriptions.get("desired_total"))
        applied = _int(subscriptions.get("applied_total"))
        connected = gateway.get("connected") is True
        feed_age = gateway.get("feed_age_sec")
        feed_stale = gateway.get("stale") is True
        feed_state = _lower(gateway.get("operational_state") or gateway.get("state"))
        if not market_open:
            feed_check = Check(
                "live_feed", "PAUSED", "Live price feed",
                f"market closed; last gateway state {feed_state or 'unknown'}",
                "use verified completed-session data only", "market-data", False,
            )
        elif connected and applied > 0 and not feed_stale:
            feed_check = Check(
                "live_feed", "READY", "Live price feed",
                f"connected · {applied}/{desired or applied} subscriptions · feed age {feed_age if feed_age is not None else 'warming'}s",
                "none", "market-data",
            )
        elif connected:
            feed_check = Check(
                "live_feed", "DEGRADED", "Live price feed",
                f"connected but not producing a fresh subscribed stream ({applied}/{desired} applied; age {feed_age})",
                "watchdog will force a clean reconnect; block new entries meanwhile", "market-data",
            )
        else:
            feed_check = Check(
                "live_feed", "BLOCKED", "Live price feed",
                f"gateway {feed_state or 'offline'}; {applied}/{desired} subscriptions applied",
                "verify token and Upstox connectivity; HTTP fallback remains labelled degraded", "market-data",
            )
        checks.append(feed_check)

        checks.append(self._loop_check(
            loops, "intraday_lifecycle", title="Intraday lifecycle and forced-flatten monitor",
            action="restart the supervised Intraday lifecycle loop; Intraday stays blocked", critical=True,
        ))
        checks.append(self._loop_check(
            loops, "delivery_lifecycle", title="Delivery Model Paper lifecycle monitor",
            action="restart the supervised Delivery lifecycle loop; Delivery stays blocked", critical=True,
        ))

        scanner_status = dict(status.get("mode_scanners") or (status.get("scanner") or {}).get("mode_scanners") or {})
        intraday = dict(scanner_status.get("intraday") or {})
        delivery = dict(scanner_status.get("delivery") or {})
        intraday_loop = dict(loops.get("intraday_scanner") or {})
        delivery_loop = dict(loops.get("delivery_scanner") or {})

        intraday_loop_state = str(intraday_loop.get("state") or "").upper()
        intraday_loop_failed = any(token in intraday_loop_state for token in ("FAILED", "STUCK", "NO_PROGRESS", "CIRCUIT_OPEN", "DEAD"))
        intraday_coverage = dict(intraday.get("coverage") or {})
        intraday_last_completed = dict(intraday_coverage.get("last_completed") or {})
        intraday_population = _int(intraday_coverage.get("universe_size"))
        intraday_sweep_complete = bool(
            intraday_coverage.get("sweep_complete")
            or (
                intraday_population > 0
                and _int(intraday_last_completed.get("attempted")) >= intraday_population
            )
        )
        intraday_ready = (
            not market_open
            or (
                instrument_ready
                and feed_check.state == "READY"
                and intraday_loop.get("alive") is True
                and intraday_loop.get("stale") is not True
                and not intraday_loop_failed
                and intraday_sweep_complete
            )
        )
        checks.append(Check(
            "intraday_desk",
            "PAUSED" if not market_open else "READY" if intraday_ready else "BLOCKED",
            "Intraday desk",
            "market closed" if not market_open else f"state {_lower(intraday.get('state')) or 'warming'} · {_int(intraday.get('scanned'))} scanned",
            "release only after identity, feed, bars and risk checks are ready",
            "intraday",
        ))

        delivery_coverage = dict(delivery.get("analysis") or {})
        scanner_summary = {
            "intraday": {
                "state": intraday.get("state") or intraday_coverage.get("state") or "unknown",
                "coverage_pct": _float(intraday_coverage.get("coverage_pct")),
                "population_count": _int(intraday_coverage.get("universe_size")),
                "sweep_complete": bool(intraday_coverage.get("sweep_complete")),
            },
            "delivery": {
                "state": delivery.get("state") or delivery_coverage.get("state") or "unknown",
                "coverage_pct": _float(delivery_coverage.get("coverage_pct")),
                "population_count": _int(delivery_coverage.get("universe_size")),
                "sweep_complete": bool(delivery_coverage.get("sweep_complete")),
                "data_missing": _int(delivery_coverage.get("cycle_data_missing")),
            },
        }

        delivery_state = _lower(delivery.get("state"))
        universe_state = dict(status.get("universe_authority") or getattr(self.host, "status", {}).get("universe_authority") or {})
        delivery_population = _int(
            delivery_coverage.get("universe_size")
            or dict(dict(universe_state.get("snapshots") or {}).get("delivery") or {}).get("population_count")
            or dict(universe_state.get("delivery") or {}).get("population_count")
            or universe_state.get("delivery_population")
        )
        # Desk operational readiness is authority + supervised worker + a
        # bounded immutable population. History/fundamental hydration and
        # actual opportunity output are separate usefulness gates; treating
        # them as installer safety would cause healthy upgrades to roll back
        # during normal cache warming.
        delivery_progress = dict(delivery.get("progress_contract") or {})
        delivery_completed = _int(
            delivery_progress.get("last_completed_sweep_count")
            or (delivery.get("analysis") or {}).get("last_completed_sweep_count")
            or 0
        )
        loop_state = str(delivery_loop.get("state") or "").upper()
        expected_idle = delivery_loop.get("expected_idle") is True or loop_state in {
            "EXPECTED_IDLE", "YIELDING_TO_HIGHER_PRIORITY", "YIELDING_TO_SELECTED_STOCK"
        }
        heartbeat_age = float(delivery_loop.get("heartbeat_age_sec") or 0.0)
        live_or_recent_idle = bool(
            delivery_loop.get("alive") is True
            and delivery_loop.get("stale") is not True
            and (not expected_idle or heartbeat_age <= 150.0)
        )
        delivery_ready = bool(
            instrument_ready
            and delivery_population > 0
            and live_or_recent_idle
            and not any(token in loop_state for token in ("FAILED", "STUCK", "NO_PROGRESS", "CIRCUIT_OPEN", "DEAD"))
            and delivery_completed >= delivery_population
        )
        checks.append(Check(
            "delivery_desk",
            "READY" if delivery_ready else "BLOCKED",
            "Delivery desk",
            (lambda progress, completed: f"state {delivery_state or 'warming'} · population {delivery_population} · last full {completed}/{delivery_population} · current {progress}/{delivery_population}")(
                _int(delivery_progress.get("current_sweep_scanned") or delivery.get('sweep_scanned') or delivery.get('current_cycle_scanned') or delivery.get('scanned')),
                delivery_completed,
            ),
            "restore the bounded Delivery universe/worker; continue cache-first hydration outside the live-risk path",
            "delivery",
        ))

        fundamentals = self._fundamentals(status)
        fundamental_count = _int(fundamentals.get("count"))
        fundamentals_ready = fundamentals.get("loaded") is True and fundamental_count > 0
        checks.append(Check(
            "fundamentals_authority",
            "READY" if fundamentals_ready else "DEGRADED",
            "Fundamentals authority",
            f"{fundamental_count} point-in-time rows loaded" if fundamentals_ready else "no point-in-time fundamentals are currently loaded",
            "load/refresh the governed fundamentals cache; Delivery strategies that declare fundamentals mandatory must remain research-only",
            "fundamentals",
            critical=False,
        ))

        radar = dict(status.get("market_radar") or {})
        radar_loop = dict(loops.get("market_radar_projection") or {})
        heat_rows = _int(radar.get("heat_rows"))
        verified_coverage = _int(radar.get("verified_coverage"))
        verified_actionable = _int(radar.get("verified_actionable"))
        next_session_watchlist = _int(radar.get("next_session_watchlist"))
        projection_ready = radar_loop.get("alive") is True and radar_loop.get("stale") is not True
        output_ready = projection_ready
        customer_useful = bool(
            projection_ready
            and verified_coverage > 0
            and (verified_actionable > 0 if market_open else next_session_watchlist > 0)
        )
        checks.append(Check(
            "operator_output",
            "READY" if output_ready else "BLOCKED",
            "Decision output",
            f"projection current; {heat_rows} market rows; {verified_coverage} verified observations; {verified_actionable} actionable; {next_session_watchlist} watchlist",
            "restart the bounded read-model projection and show explicit no-result reasons; never render a blank shell",
            "presentation",
        ))
        checks.append(Check(
            "customer_usefulness",
            "READY" if customer_useful else "BLOCKED" if market_open else "DEGRADED",
            "Customer usefulness",
            "verified actionable/watchlist evidence available" if customer_useful else "infrastructure is running but verified opportunity evidence is not yet sufficient",
            "continue exact-gap hydration and scanning; never relabel unverified radar rows as actionable",
            "product",
            critical=True,
        ))

        # Installed usefulness is a binding release gate whenever the runtime
        # publishes the corresponding telemetry.  Database reachability and a
        # live thread are not substitutes for a persisted candle, a reconciled
        # recovery path and a browser-proven vertical slice.
        ingestion = dict(status.get("ingestion") or {})
        persisted = bool(
            ingestion.get("last_candle_stored")
            and _int(ingestion.get("candles_total")) > 0
        )
        checks.append(Check(
            "persisted_market_data",
            "READY" if persisted else "BLOCKED",
            "Persisted market-data continuity",
            "physical candle row-count and last-write telemetry are present" if persisted else "authoritative physical candle row-count/last-write telemetry is absent",
            "repair provider → physical store → coverage manifest telemetry; a fetch timestamp or database reachability is not persistence proof",
            "market-data",
            critical=True,
        ))
        recovery = dict(status.get("priority_pipeline_recovery") or status.get("pipeline_recovery") or {})
        recovery_state = str(recovery.get("state") or "UNAVAILABLE").upper()
        recovery_ready = bool(recovery) and recovery_state not in {"BLOCKED", "FAILED", "UNAVAILABLE", "CIRCUIT_OPEN"} and _int(recovery.get("blocked")) == 0
        checks.append(Check(
            "priority_pipeline_recovery",
            "READY" if recovery_ready else "BLOCKED",
            "Priority pipeline recovery",
            f"state {recovery_state}; blocked {_int(recovery.get('blocked'))}",
            "diagnose the exact dependency, execute an allow-listed action and verify progress before claiming operational readiness",
            "runtime-controller",
            critical=True,
        ))
        browser = dict(status.get("browser_proof") or status.get("browser_validation") or {})
        browser_ready = bool(
            browser.get("passed") is True
            and str(browser.get("build") or "") == APP_VERSION
            and str(browser.get("build_marker") or "") == BUILD_MARKER
        )
        checks.append(Check(
            "installed_browser_vertical_slice",
            "READY" if browser_ready else "BLOCKED",
            "Installed browser vertical slice",
            f"exact {BUILD_MARKER} browser workflow proof is current" if browser_ready else f"exact-build browser proof is absent, failed, or belongs to another build (expected {BUILD_MARKER})",
            "run the exact-build installed browser proof; source Chromium fixtures are insufficient",
            "presentation",
            critical=True,
        ))

        # Research libraries being installed is not empirical edge.  This check
        # intentionally reports the distinction rather than declaring success.
        checks.append(Check(
            "edge_evidence",
            "NOT_PROVEN",
            "Empirical edge",
            "software and dependencies may be installed; post-cost forward evidence is not implied",
            "run finite Delivery and Intraday tournaments, then promote or reject",
            "research",
            critical=False,
        ))

        critical = [row for row in checks if row.critical]
        blocked = [row for row in critical if row.state == "BLOCKED"]
        degraded = [row for row in critical if row.state == "DEGRADED"]
        if blocked:
            product_state = "BLOCKED"
            level = "PROCESS_READY"
        elif degraded:
            product_state = "DEGRADED"
            level = "OPERATIONAL_CANDIDATE"
        else:
            product_state = "OPERATIONAL"
            level = "OPERATIONAL"

        blockers = [
            {
                "code": row.key,
                "state": row.state,
                "message": row.detail,
                "action": row.action,
                "owner": row.owner,
            }
            for row in checks if row.state in {"BLOCKED", "DEGRADED"}
        ]
        primary = blockers[0] if blockers else None
        check_payloads = [row.payload() for row in checks]
        installation_acceptance = evaluate_installation_acceptance(
            product_state=product_state,
            checks=check_payloads,
            missing_runtime_status_keys=missing_runtime_status_keys,
        )
        return {
            "ok": product_state == "OPERATIONAL",
            "service_version": SERVICE_VERSION,
            "product_state": product_state,
            "truth_level": level,
            "market_open": market_open,
            "checks": check_payloads,
            "blockers": blockers,
            "primary_blocker": primary,
            "runtime_contract": {
                "required_status_keys": list(REQUIRED_RUNTIME_STATUS_KEYS),
                "missing_status_keys": missing_runtime_status_keys,
                "complete": not missing_runtime_status_keys,
            },
            "installation_acceptance": installation_acceptance,
            "claims": {
                "built": True,
                "installs": True,
                "operational": product_state == "OPERATIONAL",
                "useful": customer_useful and product_state == "OPERATIONAL",
                "edge_validated": False,
            },
            "customer_usefulness": {
                "state": "READY" if customer_useful else "DEGRADED",
                "verified_coverage": verified_coverage,
                "verified_actionable": verified_actionable,
                "next_session_watchlist": next_session_watchlist,
            },
            "fundamentals_authority": {
                **fundamentals,
                "count": fundamental_count,
                "loaded": fundamentals_ready,
                "state": "READY" if fundamentals_ready else "DEGRADED",
            },
            "scanner": scanner_summary,
            "time": now_iso(),
            "probe": "memory_and_cached_metadata_only",
        }
