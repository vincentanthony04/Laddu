"""Market Radar and next-session research projection collaborator.

This service reads already acquired/persisted observations and publishes an
in-memory read model. It performs no market-data network I/O and has no
promotion, decision or order authority.
"""
from __future__ import annotations

import time
import json
from typing import Any, Dict

from core.india_time import india_now
from core.market_clock import is_india_market_open
from core.market_radar_service import MarketRadarService
from core.market_radar_http_projection import project_market_radar_http
from core.quote_integrity_service import classify_quote
from models import now_iso


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _first(row: Dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = row.get(key)
        if value not in (None, "", [], {}):
            return value
    return None


def _actionable_setup(row: Dict[str, Any], *, market_open: bool) -> bool:
    mode = str(row.get("mode") or "").lower()
    status = str(row.get("status") or row.get("state") or "").upper()
    decision = str(row.get("decision") or row.get("decision_action") or "").upper()
    if row.get("observation_only") is True or row.get("next_session_only") is True:
        return False
    if status not in {"PROMOTED", "SIGNAL_OPEN", "TRIGGERED", "CONFIRMED"}:
        return False
    if decision in {"", "WAIT", "WATCH", "RESEARCH", "AVOID", "REJECT"}:
        return False
    if row.get("identity_verified") is not True:
        return False
    freshness = str(row.get("freshness_state") or "").lower()
    if mode == "intraday" and (not market_open or freshness != "live"):
        return False
    if freshness not in {"live", "closed_market"}:
        return False
    values = (
        _number(_first(row, "entry", "entry_price", "planned_entry", "trigger_level")),
        _number(_first(row, "target", "target_price", "t1", "planned_target", "planned_t1")),
        _number(_first(row, "stop", "stop_price", "sl", "planned_stop", "planned_sl")),
        _number(_first(row, "rr_after_costs", "planned_rr", "rr")),
    )
    return all(value is not None and value > 0 for value in values)


class MarketRadarProjectionService:
    def __init__(self, host):
        self.host = host

    def _projection_lock(self):
        # Full runtime owns a dedicated radar lock. Minimal test/embedded hosts
        # may only expose the legacy lock, so retain compatibility without
        # moving any repository I/O under that lock.
        return getattr(self.host, "_market_radar_lock", getattr(self.host, "lock"))

    def refresh(self) -> Dict[str, Any]:
        started = time.perf_counter()
        now = india_now()
        market_open = is_india_market_open(now)
        # Load persisted restart evidence once, in the worker lane. Never make
        # an HTTP request wait on SQLite's busy_timeout.
        if not bool(getattr(self.host, "_market_radar_persisted_loaded", False)):
            try:
                persisted = self.host.store.get_kv("market_radar_coverage:last", []) or []
            except Exception as exc:
                persisted = []
                self.host.record_error("market_radar_persisted_load", str(exc))
            with self._projection_lock():
                self.host._market_radar_persisted_rows = [dict(r) for r in persisted if isinstance(r, dict)]
                self.host._market_radar_persisted_loaded = True

        with self._projection_lock():
            cached_current = [
                dict(r) for r in (getattr(self.host, "_coverage_quote_cache", {}) or {}).values()
                if isinstance(r, dict)
            ]
            persisted = [dict(r) for r in (getattr(self.host, "_market_radar_persisted_rows", []) or []) if isinstance(r, dict)]
            heat = [dict(r) for r in (getattr(self.host, "_heatmap_cache", []) or []) if isinstance(r, dict)]
        current = []
        for raw in cached_current:
            integrity = classify_quote(
                raw,
                now=now,
                market_open=market_open,
                max_live_age_sec=45.0,
            )
            state = str(integrity.get("state") or "unverified")
            source_time = integrity.get("source_time")
            current.append(dict(
                raw,
                radar_source=raw.get("radar_source") or "verified_coverage_memory",
                source_time=source_time,
                freshness_state=state,
                freshness_reason=integrity.get("reason"),
                stale=state not in {"live", "closed_market"},
                usable_for_promotion=False,
                freshness=(
                    f"{state.replace('_', ' ')} @ {source_time}"
                    if source_time
                    else f"{state.replace('_', ' ')} · provider timestamp unavailable"
                ),
            ))

        # Heat persistence is also loaded only from this worker lane.
        if not heat:
            try:
                heat = [dict(r) for r in (self.host.store.get_kv("heatmap_cache", []) or []) if isinstance(r, dict)]
            except Exception:
                heat = []

        # After-market Delivery research is projected in the worker lane, not
        # the HTTP route.  Merge completed-daily/VCP/breakout/fundamental rows
        # with verified closed-market quotes so the user can review next-session
        # setups without mislabelling them as Today's Entries.
        delivery_candidates = []
        latest_delivery = getattr(self.host.store, "latest_decisions", None)
        if callable(latest_delivery):
            try:
                delivery_candidates.extend(latest_delivery("delivery", limit=160) or [])
            except Exception as exc:
                self.host.record_error("market_radar_delivery_decisions", str(exc))
        opportunity_delivery = getattr(self.host.store, "opportunity_candidates", None)
        if callable(opportunity_delivery):
            try:
                delivery_candidates.extend(opportunity_delivery("delivery", limit=120) or [])
            except Exception as exc:
                self.host.record_error("market_radar_delivery_opportunities", str(exc))

        intraday_candidates = []
        latest_intraday = getattr(self.host.store, "latest_decisions", None)
        if callable(latest_intraday):
            try:
                intraday_candidates.extend(latest_intraday("intraday", limit=160) or [])
            except Exception as exc:
                self.host.record_error("market_radar_intraday_decisions", str(exc))
        opportunity_intraday = getattr(self.host.store, "opportunity_candidates", None)
        if callable(opportunity_intraday):
            try:
                intraday_candidates.extend(opportunity_intraday("intraday", limit=120) or [])
            except Exception as exc:
                self.host.record_error("market_radar_intraday_opportunities", str(exc))
        try:
            intraday_candidates.extend(self.host.store.get_kv("fair_analysis_queue:last", []) or [])
        except Exception as exc:
            self.host.record_error("market_radar_intraday_fair_queue", str(exc))

        radar = MarketRadarService(max_rows=5).build(
            current, persisted_rows=persisted, heatmap=heat,
            delivery_candidates=delivery_candidates, intraday_candidates=intraday_candidates,
        )
        delivery_setups = list(radar.get("delivery_setups") or [])
        intraday_setups = list(radar.get("intraday_setups") or [])
        projected_rows = intraday_setups + delivery_setups
        actionable = [dict(row, actionability_verified=True) for row in projected_rows if _actionable_setup(dict(row), market_open=market_open)]
        watchlist = [dict(row, actionability_verified=False, research_state="WATCH", publish_section="next_session_watchlist") for row in projected_rows if not _actionable_setup(dict(row), market_open=market_open)]
        elapsed_ms = round((time.perf_counter() - started) * 1000.0, 3)
        snapshot = {
            "ok": True,
            "counts": {"READY": len(actionable), "WATCH": len(watchlist), "EXTENDED": 0, "AVOID": 0},
            "opportunities": actionable,
            "next_session_watchlist": watchlist,
            "publication_policy": "Only complete identity/freshness/entry/target/stop/post-cost-RR rows may appear as actionable; all other radar setups are research watchlist rows.",
            "market_radar": radar,
            "heatmap": radar.get("heatmap") or [],
            "projection_state": "ready" if radar.get("coverage") else "warming",
            "projection_elapsed_ms": elapsed_ms,
            "price_refresh_contract": {
                "market_open": market_open,
                "visible_quote_poll_seconds": 3.5,
                "radar_projection_seconds": 15,
                "live_max_provider_age_seconds": 45,
                "live_requires": "exact instrument identity + provider market timestamp",
                "closed_market_label": "CLOSE",
                "fallback_label": "LKG",
            },
            "time": now_iso(),
        }
        # Precompute the customer HTTP shape and its JSON bytes in the producer.
        # Request threads perform no projection/copy/serialization work. Python
        # object assignment is atomic, so readers can take this immutable
        # generation without contending on the producer lock.
        http_snapshot = project_market_radar_http(snapshot, full=False)
        http_snapshot["cache_only"] = True
        http_bytes = json.dumps(http_snapshot, ensure_ascii=False, default=str).encode("utf-8")
        with self._projection_lock():
            self.host._market_radar_snapshot = snapshot
            self.host._market_radar_http_snapshot = http_snapshot
            self.host._market_radar_http_bytes = http_bytes
            self.host._market_radar_snapshot_ts = time.time()
        with self.host.lock:
            health = self.host.status.setdefault("market_radar", {})
            health.update({
                "state": snapshot["projection_state"],
                "last_run": snapshot["time"],
                "projection_elapsed_ms": elapsed_ms,
                "coverage": int(radar.get("coverage") or 0),
                "verified_coverage": int(radar.get("verified_coverage") or 0),
                "verified_coverage_pct": float(radar.get("verified_coverage_pct") or 0.0),
                "change_ready": len(radar.get("top_gainers") or []) + len(radar.get("top_losers") or []),
                "volume_ready": len(radar.get("volume_shockers") or []),
                "delivery_setups": len(delivery_setups),
                "intraday_setups": len(intraday_setups),
                "verified_actionable": len(actionable),
                "next_session_watchlist": len(watchlist),
                "heat_rows": len(snapshot["heatmap"]),
                "market_open": market_open,
                "visible_quote_poll_seconds": 3.5,
                "live_max_provider_age_seconds": 45,
            })
        return snapshot

