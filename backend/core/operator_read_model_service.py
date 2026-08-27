"""Background projections for UI/operator endpoints.

Expensive SQLite joins and diagnostics belong to a worker lane. HTTP handlers
serve the latest immutable projection immediately, including an explicit stale
or warming state when the worker has not completed yet.
"""
from __future__ import annotations

import copy
import hashlib
import json
import threading
import time
from typing import Any, Dict

from intelligence.market_object import build_market_object
from core.win_rate_diagnostics_service import WinRateDiagnosticsService
from core.market_regime_change_service import MarketRegimeChangeService
from models import now_iso

SERVICE_VERSION = "operator-read-models-1.0.0"


class OperatorReadModelService:
    def __init__(self, host: Any):
        self.host = host
        self._lock = threading.RLock()
        self._cache: Dict[str, Any] = {
            "service_version": SERVICE_VERSION,
            "state": "warming",
            "last_refresh": None,
            "last_error": None,
            "market_breadth": {},
            "market_object": {"ok": True, "market_object": {"state": "warming"}, "projection_state": "warming"},
            "winrate_diagnostics": {"ok": True, "state": "collecting", "overall": {"samples": 0}, "root_causes": [], "projection_state": "warming"},
            "event_calendar_3d": {"ok": True, "events": {}, "projection_state": "warming"},
        }

    def _publish(self, **values: Any) -> None:
        with self._lock:
            self._cache = {**self._cache, **copy.deepcopy(values), "last_refresh": now_iso(), "state": "ready", "last_error": None}

    def _error(self, exc: Exception) -> None:
        with self._lock:
            self._cache = {**self._cache, "state": "degraded", "last_error": str(exc)[:240]}

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._cache)

    def market_breadth(self, universe: str = "NIFTY250_CORE") -> Dict[str, Any] | None:
        return self.snapshot().get("market_breadth", {}).get(str(universe))

    def market_object(self) -> Dict[str, Any]:
        return dict(self.snapshot().get("market_object") or {})

    def winrate_diagnostics(self) -> Dict[str, Any]:
        return dict(self.snapshot().get("winrate_diagnostics") or {})

    def event_calendar(self, within_days: int = 3, symbol: str = "") -> Dict[str, Any]:
        # Current UI consumes the 3-day projection. Other windows are labelled
        # as projected from the nearest available background snapshot rather
        # than triggering synchronous repository work.
        payload = dict(self.snapshot().get("event_calendar_3d") or {})
        events = dict(payload.get("events") or {})
        if symbol:
            sym = str(symbol).upper().strip()
            return {"ok": True, "symbol": sym, "event": events.get(sym), "projection_state": payload.get("projection_state", "warming"), "requested_within_days": int(within_days)}
        return {**payload, "requested_within_days": int(within_days)}

    def refresh(self) -> Dict[str, Any]:
        # Every repository/provider call is intentionally outside the cache
        # lock. A blocked analytical read can delay the next projection but can
        # never delay a health/UI HTTP response.
        try:
            if not getattr(self.host, "_heatmap_cache", None):
                try:
                    persisted_heat = self.host.store.get_kv("heatmap_cache", []) or []
                except Exception:
                    persisted_heat = []
                if persisted_heat:
                    self.host._heatmap_cache = [dict(row) for row in persisted_heat if isinstance(row, dict)]

            universe = "NIFTY250_CORE"
            breadth = self.host.store.get_latest_market_breadth(universe)
            try:
                flow = self.host.reference_data.institutional_flow_context()
            except Exception:
                flow = None
            heat = [dict(row) for row in (getattr(self.host, "_heatmap_cache", []) or []) if isinstance(row, dict)]
            # MarketObject projects the canonical persisted regime observation;
            # it no longer invents a second +/-0.15% mean-index heuristic.
            try:
                regime_latest = MarketRegimeChangeService(self.host.store).latest()
            except Exception:
                regime_latest = {"confirmed_regime": "UNKNOWN", "state": "NO_REGIME_OBSERVATIONS"}
            confirmed = str(regime_latest.get("confirmed_regime") or "UNKNOWN").upper()
            trend_state = "supportive" if confirmed == "BULL" else "hostile" if confirmed == "BEAR" else "neutral" if confirmed in {"RANGE", "SECTOR_ROTATION", "VOLATILE"} else "pending"
            governed_rows = [row for row in heat if row.get("direction_authority_ready") is True and row.get("change_pct") is not None]
            mean = (sum(float(row.get("change_pct") or 0.0) for row in governed_rows) / len(governed_rows)) if governed_rows else None
            regime_projection = {
                "state": trend_state,
                "mean_index_change_pct": round(mean, 2) if mean is not None else None,
                "index_count": len(governed_rows),
                "confirmed_regime": confirmed,
                "authority": regime_latest.get("authority") or "MarketRegimeAuthority",
                "authority_version": regime_latest.get("authority_version") or "1.1.0",
            }
            market_object = {"ok": True, "market_object": build_market_object(regime=regime_projection, breadth=breadth, institutional_flow=flow), "regime_projection": regime_projection, "projection_state": "ready" if confirmed != "UNKNOWN" else "warming", "time": now_iso()}

            try:
                events = self.host.earnings_calendar.event_risk_map(3) or {}
            except Exception:
                events = {}
            event_payload = {"ok": True, "events": events, "projection_state": "ready", "time": now_iso()}

            # Warm expensive Performance/Accuracy evidence asynchronously.
            # The operator projection owns only the request; background repair
            # owns database work and foreground HTTP remains cache-only.
            try:
                from core.materialized_performance_snapshot_service import MaterializedPerformanceSnapshotService
                performance = MaterializedPerformanceSnapshotService(self.host)
                performance.prime_from_persistence(mode="all")
                performance.request_refresh(mode="all")
            except Exception:
                pass

            try:
                outcome_rows = self.host.store.outcome_learning_rows(limit=5000) or []
                winrate = WinRateDiagnosticsService().analyze(outcome_rows)
                winrate = {**winrate, "projection_state": "ready", "time": now_iso()}
            except Exception as exc:
                winrate = {"ok": False, "state": "unavailable", "error": str(exc)[:240], "overall": {"samples": 0}, "root_causes": [], "projection_state": "degraded", "time": now_iso()}

            self._publish(
                market_breadth={universe: breadth},
                market_object=market_object,
                event_calendar_3d=event_payload,
                winrate_diagnostics=winrate,
            )
            return self.snapshot()
        except Exception as exc:
            self._error(exc)
            return self.snapshot()

    @staticmethod
    def _business_progress_token(snapshot: Dict[str, Any]) -> str:
        """Hash operator projection content while excluding refresh clocks.

        A projection refresh is liveness; only changed market/research content is
        useful progress. Timestamp-only tokens previously kept this worker green
        even if every projected business value was frozen.
        """
        market_object = dict(snapshot.get("market_object") or {})
        market_object.pop("time", None)
        winrate = dict(snapshot.get("winrate_diagnostics") or {})
        winrate.pop("time", None)
        events = dict(snapshot.get("event_calendar_3d") or {})
        events.pop("time", None)
        payload = {
            "state": snapshot.get("state"),
            "market_breadth": snapshot.get("market_breadth") or {},
            "market_object": market_object,
            "winrate_diagnostics": winrate,
            "event_calendar_3d": events,
        }
        raw = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
        return "operator:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]

    def run(self, sup=None, running_fn=lambda: True) -> None:
        time.sleep(2.0)
        while running_fn() and (sup is None or sup.running):
            if sup:
                sup.beat("operator_read_models")
            snapshot = self.refresh()
            if sup:
                sup.progress(
                    "operator_read_models",
                    token=self._business_progress_token(snapshot or {}),
                    stage="read_model_projection",
                    completed_units=1,
                    total_units=1,
                    expected_idle=False,
                )
            time.sleep(30.0)
