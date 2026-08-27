"""Materialized chart-overlay read model.

Foreground chart GETs only read a retained projection and may enqueue a local
background refresh.  Indicator/overlay mathematics never executes on the HTTP
thread.
"""
from __future__ import annotations

import threading
from typing import Any, Dict, Iterable, Mapping

from core.local_projection_dispatcher import for_app as local_projection_dispatcher_for_app, PRIORITY_CHART
from core.canonical_chart_overlay_projection_authority import DEFAULT_CANONICAL_CHART_OVERLAY_PROJECTION_AUTHORITY
from core.db_utils import canonical_interval


class MaterializedChartOverlayService:
    VERSION = "materialized-chart-overlay-1.1.0-memory-reader"
    KEY_PREFIX = "clean_core:chart_overlay:v1:"

    def __init__(self, app: Any):
        self.app = app
        self.store = app.store
        if not hasattr(app, "_clean_core_chart_overlay_cache"):
            setattr(app, "_clean_core_chart_overlay_cache", {})
        if not hasattr(app, "_clean_core_chart_overlay_lock"):
            setattr(app, "_clean_core_chart_overlay_lock", threading.RLock())
        self.cache = app._clean_core_chart_overlay_cache
        self.lock = app._clean_core_chart_overlay_lock

    @classmethod
    def _key(cls, instrument_key: str, interval: str) -> str:
        return f"{cls.KEY_PREFIX}{instrument_key}:{canonical_interval(interval)}"

    def _load(self, instrument_key: str, interval: str) -> Dict[str, Any]:
        """Foreground memory-only lookup."""
        key = self._key(instrument_key, interval)
        with self.lock:
            return dict(self.cache.get(key) or {})

    @staticmethod
    def _last_source_time(rows: Iterable[Mapping[str, Any]]) -> str:
        material = list(rows or ())
        if not material:
            return ""
        row = material[-1]
        return str(row.get("timestamp") or row.get("time") or row.get("datetime") or row.get("ts") or row.get("date") or "")

    def request_projection(self, instrument_key: str, interval: str, rows: Iterable[Mapping[str, Any]]) -> bool:
        material = [dict(row) for row in (rows or ())]
        if not instrument_key or not material:
            return False
        norm = canonical_interval(interval)
        expected_last = self._last_source_time(material)
        key = self._key(instrument_key, norm)

        def produce() -> None:
            try:
                retained = dict(self.store.get_kv(key, {}) or {})
            except Exception:
                retained = {}
            if retained:
                with self.lock:
                    self.cache[key] = retained
                if str(retained.get("source_last_candle_raw") or "") == expected_last:
                    return
            projection = DEFAULT_CANONICAL_CHART_OVERLAY_PROJECTION_AUTHORITY.project(material)
            if projection.get("state") != "READY":
                return
            payload = {
                **projection,
                "read_model_version": self.VERSION,
                "instrument_key": instrument_key,
                "canonical_interval": norm,
                "source_last_candle_raw": expected_last,
            }
            self.store.set_kv(key, payload)
            with self.lock:
                self.cache[key] = dict(payload)

        result = local_projection_dispatcher_for_app(self.app).submit(
            f"chart-overlay:{instrument_key}:{norm}:{expected_last}", produce, priority=PRIORITY_CHART + 2
        )
        return bool(result.accepted or result.state == "COALESCED")

    def read(self, instrument_key: str, interval: str, rows: Iterable[Mapping[str, Any]]) -> Dict[str, Any]:
        material = list(rows or ())
        retained = self._load(instrument_key, interval)
        expected_last = self._last_source_time(material)
        retained_last = str(retained.get("source_last_candle_raw") or "")
        current = bool(retained and expected_last and retained_last == expected_last)
        refreshing = False
        if material and not current:
            refreshing = self.request_projection(instrument_key, interval, material)
        if retained:
            return {
                **retained,
                "read_model_version": self.VERSION,
                "freshness": "CURRENT" if current else "STALE",
                "refreshing": refreshing,
            }
        return {
            "authority": "CanonicalChartOverlayProjectionAuthority",
            "authority_version": "1.0.0",
            "read_model_version": self.VERSION,
            "state": "WARMING" if material else "UNAVAILABLE",
            "instrument_key": instrument_key,
            "canonical_interval": canonical_interval(interval),
            "series": {},
            "metrics": {},
            "events": {},
            "freshness": "MISSING",
            "refreshing": refreshing,
            "policy": "Foreground read is projection-only; local background materializer owns chart overlay calculation.",
        }
