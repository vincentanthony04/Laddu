"""Small, failure-isolated runtime health projection.

The live scanner owns a large mutable status tree.  HTTP health/readiness must
not wait for that tree, an analytical database, or an outbound token helper.
This registry stores a deliberately small immutable projection and provides a
bounded refresh path: if the runtime status lock is busy, callers receive the
last good snapshot immediately with an explicit cached/contention marker.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import threading
from typing import Any, Dict, Mapping

REGISTRY_VERSION = "runtime-health-registry-1.2.0-governance-migration-projection"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class RuntimeHealthRegistry:
    RUNTIME_KEYS = (
        "service", "started_at", "active_mode", "auth", "fast_lane",
        "deep_scan", "mode_scanners", "quote_delta", "opportunity_memory",
        "last_price_refresh", "last_historical_fetch", "last_ai_validation",
        "last_fundamental_refresh", "last_delivery_refresh",
        "delivery_data_sync", "last_market_data_maintenance",
        "storage_maintenance", "live_market_gateway",
        "analytical_projection", "market_radar", "scan_lanes", "api_errors",
        "production_data_plane", "quant_research_plane", "research_governance_migration", "startup_phases", "instruments",
    )

    def __init__(self, initial_status: Mapping[str, Any] | None = None):
        self._lock = threading.RLock()
        self._snapshot: Dict[str, Any] = {
            "registry_version": REGISTRY_VERSION,
            "snapshot_state": "starting",
            "snapshot_at": _now(),
            "status": {},
            "components": {},
        }
        if initial_status is not None:
            self.publish_runtime(initial_status, state="initial")

    @classmethod
    def project_runtime(cls, status: Mapping[str, Any] | None) -> Dict[str, Any]:
        src = status or {}
        projected: Dict[str, Any] = {}
        for key in cls.RUNTIME_KEYS:
            if key not in src:
                continue
            try:
                projected[key] = copy.deepcopy(src.get(key))
            except Exception:
                projected[key] = {"state": "snapshot_error"}
        return projected

    def publish_runtime(self, status: Mapping[str, Any] | None, *, state: str = "fresh") -> Dict[str, Any]:
        projected = self.project_runtime(status)
        with self._lock:
            self._snapshot = {
                **self._snapshot,
                "snapshot_state": state,
                "snapshot_at": _now(),
                "status": projected,
            }
            return copy.deepcopy(self._snapshot)

    def publish_component(self, name: str, payload: Mapping[str, Any] | None) -> None:
        with self._lock:
            components = dict(self._snapshot.get("components") or {})
            components[str(name)] = {**copy.deepcopy(dict(payload or {})), "published_at": _now()}
            self._snapshot = {**self._snapshot, "components": components, "snapshot_at": _now()}

    def snapshot(self, *, state: str | None = None) -> Dict[str, Any]:
        with self._lock:
            result = copy.deepcopy(self._snapshot)
        if state:
            result["snapshot_state"] = state
        return result


def bounded_runtime_health_snapshot(runtime: Any, timeout_seconds: float = 0.02) -> Dict[str, Any]:
    """Return the compact runtime projection without waiting on scanner work."""
    acquired = runtime.lock.acquire(timeout=max(0.0, float(timeout_seconds)))
    if acquired:
        try:
            registry = runtime.health_registry.publish_runtime(runtime.status, state="fresh")
        finally:
            runtime.lock.release()
    else:
        registry = runtime.health_registry.snapshot(state="cached_due_to_contention")
    projected = dict(registry.get("status") or {})
    projected["_health_snapshot"] = {
        "state": registry.get("snapshot_state"),
        "at": registry.get("snapshot_at"),
        "registry_version": registry.get("registry_version"),
    }
    return projected
