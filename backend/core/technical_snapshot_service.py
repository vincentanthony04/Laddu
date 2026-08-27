from __future__ import annotations

"""Materialized local technical read model for Clean Core.

The stock page reads one replaceable technical snapshot. The foreground read is
projection-only: it never calculates MTF/SR and never performs provider work.
Cold/stale materialization runs through the bounded background producer lane.
"""

from datetime import datetime, timezone
import hashlib
import json
import threading
from typing import Any, Dict

from core.local_projection_dispatcher import (
    for_app as local_projection_dispatcher_for_app,
    PRIORITY_TECHNICAL, PRIORITY_PERSISTENCE,
)
from core.clean_chart_read_service import CleanChartReadService
from core.market_level_service import compute_level_snapshot
from core.local_mtf_projection_service import LocalMtfProjectionService
from core.indicator_snapshot_authority import DEFAULT_INDICATOR_SNAPSHOT_AUTHORITY
from core.price_performance_service import PricePerformanceService


class TechnicalSnapshotService:
    VERSION = "clean-core-technical-snapshot-5.1.0-evidence-completeness-fail-closed"
    KEY_PREFIX = "clean_core:technical_snapshot:v3:"
    LEGACY_KEY_PREFIXES = ("clean_core:technical_snapshot:v2:", "clean_core:technical_snapshot:v1:")

    def __init__(self, app: Any):
        self.app = app
        self.store = app.store
        self.chart = CleanChartReadService(app)
        self.mtf_projection = LocalMtfProjectionService(self.store)
        if not hasattr(app, "_clean_core_technical_snapshot_cache"):
            setattr(app, "_clean_core_technical_snapshot_cache", {})
        if not hasattr(app, "_clean_core_technical_snapshot_lock"):
            setattr(app, "_clean_core_technical_snapshot_lock", threading.RLock())
        self.cache = app._clean_core_technical_snapshot_cache
        self.lock = app._clean_core_technical_snapshot_lock

    @classmethod
    def _key(cls, instrument_key: str) -> str:
        return cls.KEY_PREFIX + str(instrument_key or "")

    def _load_materialized(self, instrument_key: str) -> Dict[str, Any]:
        """Memory first; one hard-bounded indexed retained lookup on cold miss.

        This is the only production persistence access allowed on the technical
        foreground path.  It reads one already-materialized KV row through the
        isolated interactive PostgreSQL pool; it never opens candles, Parquet,
        DuckDB, QuestDB, provider APIs, or prefix-scans all symbols.
        """
        with self.lock:
            cached = dict(self.cache.get(instrument_key) or {})
        if cached:
            return cached
        reader = getattr(self.store, "get_kv_bounded", None)
        if not callable(reader):
            return {}
        try:
            persisted = dict(reader(
                self._key(instrument_key), {},
                statement_timeout_ms=180,
                pool_timeout_seconds=0.18,
            ) or {})
        except TypeError:
            # Compatibility stores/tests may expose a simple bounded lookup.
            try:
                persisted = dict(reader(self._key(instrument_key), {}) or {})
            except Exception:
                persisted = {}
        except Exception:
            persisted = {}
        return self._cache_payload(instrument_key, persisted) if persisted else {}

    def _cache_payload(self, instrument_key: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        row = dict(payload or {})
        if row:
            with self.lock:
                self.cache[instrument_key] = row
        return row

    def _hydrate_one(self, instrument_key: str) -> Dict[str, Any]:
        """Background-only durable continuity lookup, including legacy keys."""
        try:
            persisted = dict(self.store.get_kv(self._key(instrument_key), {}) or {})
        except Exception:
            persisted = {}
        if persisted:
            return self._cache_payload(instrument_key, persisted)
        for prefix in self.LEGACY_KEY_PREFIXES:
            try:
                legacy = dict(self.store.get_kv(prefix + instrument_key, {}) or {})
            except Exception:
                legacy = {}
            if legacy:
                return self._cache_payload(instrument_key, {
                    **legacy,
                    "source": "RETAINED_TECHNICAL_CONTINUITY",
                    "needs_v3_projection": True,
                    "retained_from_key_prefix": prefix,
                })
        return {}

    def prewarm_retained(self) -> bool:
        """C22 deliberately avoids all-symbol JSON hydration at startup.

        Retained technical state is indexed by instrument and restored lazily via
        one bounded read on first use.  This keeps startup/scanner/UI CPU and GIL
        contention bounded even with the full 4k+ universe.
        """
        setattr(self.app, "_technical_indexed_lazy_restore_ready", True)
        return True

    def project_local(self, instrument: Dict[str, Any]) -> Dict[str, Any]:
        """Build one technical snapshot from one bounded local data read.

        The prior path opened daily history once for chart/performance and then
        reopened the candle lake through LocalMtfProjectionService for every MTF
        frame.  This producer is deliberately off the HTTP thread, but duplicate
        cold reads still made 40-symbol convergence slow.  Candidate 16 reads the
        bounded source frames once and reuses the same daily tail for MTF, S/R,
        indicator and price-performance projections.
        """
        instrument_key = str(instrument.get("instrument_key") or "")
        symbol = str(instrument.get("trading_symbol") or instrument.get("symbol") or "").upper()
        intraday_limit = int(getattr(self.mtf_projection, "INTRADAY_MATERIALIZATION_LIMIT", 420) or 420)
        daily_limit = int(getattr(self.mtf_projection, "DAILY_MATERIALIZATION_LIMIT", 1500) or 1500)
        source_reader = getattr(self.mtf_projection, "source_frames", None)
        try:
            source = dict(source_reader(
                instrument_key,
                intraday_limit=intraday_limit,
                daily_limit=daily_limit,
            ) or {}) if callable(source_reader) else {}
        except Exception:
            source = {}
        candles = list(source.get("1d") or [])[-daily_limit:]
        if not candles:
            # Compatibility/fail-soft producer fallback. This is still executed
            # only on the local projection worker, never on the HTTP reader.
            try:
                chart_payload = self.chart.read(
                    instrument_key, "1D", limit=daily_limit, schedule_repair=False
                )
                candles = list(chart_payload.get("candles") or [])[-daily_limit:]
            except Exception:
                candles = []
        try:
            coverage = dict(self.store.candle_coverage(instrument_key, "1d") or {})
        except Exception:
            coverage = {}
        daily_high_water = candles[-1].get("timestamp") if candles else None
        storage_high_water = coverage.get("last") or daily_high_water
        try:
            try:
                mtf = list(self.mtf_projection.project(instrument, source=source) or [])
            except TypeError:
                mtf = list(self.mtf_projection.project(instrument) or [])
        except Exception:
            mtf = []
        try:
            level_frames = {
                label: list(frame_rows or [])
                for label, frame_rows, _source_name in self.mtf_projection.frame_rows(instrument_key, source=source)
            } if source else {}
            level_snapshot = compute_level_snapshot(level_frames) if level_frames else {}
            levels = dict(level_snapshot.get("structural") or {})
        except Exception:
            level_snapshot = {}
            levels = {}
        try:
            indicator_snapshot = DEFAULT_INDICATOR_SNAPSHOT_AUTHORITY.calculate(candles) if candles else {}
            indicator_metrics = dict(indicator_snapshot.get("metrics") or {})
        except Exception:
            indicator_snapshot = {}
            indicator_metrics = {}
        try:
            price_performance = PricePerformanceService.project(candles) if candles else {}
        except Exception:
            price_performance = {}
        now = datetime.now(timezone.utc).isoformat()
        evidence_as_of = max(
            [str(daily_high_water or "")]
            + [str(row.get("as_of") or row.get("last_candle") or "") for row in mtf]
        ) or None

        # A materialized snapshot is not decision evidence merely because some
        # numbers were produced.  The canonical daily indicator snapshot, the
        # canonical level object and every declared MTF frame must be complete
        # and current.  Partial/stale/unavailable frames remain displayable as
        # diagnostics but cannot create an actionable decision.
        required_indicator_metrics = (
            "ema9", "ema20", "ema21", "ema50", "rsi14", "atr14",
            "plus_di14", "minus_di14", "adx14", "macd",
            "macd_signal", "macd_hist", "supertrend_value",
        )
        def _finite_number(value: Any) -> bool:
            if value is None or isinstance(value, bool):
                return False
            try:
                import math
                return math.isfinite(float(value))
            except (TypeError, ValueError, OverflowError):
                return False

        indicator_complete = (
            indicator_snapshot.get("state") == "READY"
            and indicator_snapshot.get("decision_usable") is True
            and all(_finite_number(indicator_metrics.get(name)) for name in required_indicator_metrics)
            and _finite_number(indicator_metrics.get("supertrend_direction"))
            and float(indicator_metrics.get("supertrend_direction")) in (-1.0, 1.0)
        )
        level_complete = bool(level_snapshot.get("ok"))
        expected_frames = {"1m", "3m", "5m", "15m", "30m", "1H", "4H", "1D", "1W", "1M"}
        mtf_by_tf = {str(row.get("timeframe") or row.get("tf") or ""): row for row in mtf}
        mtf_incomplete = []
        for tf in sorted(expected_frames):
            row = mtf_by_tf.get(tf)
            state = str((row or {}).get("state") or "").upper()
            if row is None or state in {"", "PENDING", "STALE", "UNAVAILABLE", "INVALID", "FAILED"}:
                mtf_incomplete.append(tf)
                continue
            if not _finite_number(row.get("composite_score")) or not _finite_number(row.get("confidence")):
                mtf_incomplete.append(tf)
        mtf_complete = not mtf_incomplete
        decision_usable = bool(candles and indicator_complete and level_complete and mtf_complete)
        decision_blockers = []
        if not candles:
            decision_blockers.append("DAILY_CANDLES_UNAVAILABLE")
        if not indicator_complete:
            decision_blockers.append("CANONICAL_INDICATORS_INCOMPLETE")
        if not level_complete:
            decision_blockers.append("CANONICAL_LEVELS_INCOMPLETE")
        if not mtf_complete:
            decision_blockers.append("MTF_INCOMPLETE:" + ",".join(mtf_incomplete))
        material = {
            "instrument_key": instrument_key,
            "symbol": symbol,
            "daily_high_water": daily_high_water,
            "storage_high_water": storage_high_water,
            "mtf": [
                (
                    row.get("timeframe") or row.get("tf") or row.get("label"),
                    row.get("as_of") or row.get("last_candle"),
                    row.get("state"), row.get("composite_score"),
                )
                for row in mtf
            ],
            "levels_version": level_snapshot.get("version") or levels.get("version"),
            "indicator_authority_version": indicator_snapshot.get("authority_version"),
            "indicator_metrics": indicator_metrics,
            "price_performance_as_of": price_performance.get("as_of"),
            "price_performance_5y_anchor": ((price_performance.get("horizons") or {}).get("5y") or {}).get("anchor_at"),
        }
        snapshot_id = hashlib.sha256(
            json.dumps(material, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()[:24]
        return {
            "ok": bool(candles or mtf or levels),
            "version": self.VERSION,
            "snapshot_id": snapshot_id,
            "symbol": symbol,
            "instrument_key": instrument_key,
            "as_of": now,
            "projected_at": now,
            "evidence_as_of": evidence_as_of,
            "decision_usable": decision_usable,
            "freshness": "CURRENT" if decision_usable else "INCOMPLETE",
            "decision_blockers": decision_blockers,
            "mathematics_state": "COMPLETE" if decision_usable else "INCOMPLETE",
            "source": "LOCAL_SINGLE_PASS_PROJECTION",
            "daily_high_water": daily_high_water,
            "storage_high_water": storage_high_water,
            "chart_count": len(candles),
            "mtf": mtf,
            # ``levels`` remains the explicitly labelled 1D structural compatibility
            # projection. Customer operating S/R must select from level_snapshot.
            "levels": levels,
            "level_snapshot": level_snapshot,
            "levels_by_timeframe": dict(level_snapshot.get("by_timeframe") or {}),
            "indicator_metrics": indicator_metrics,
            "indicator_authority": indicator_snapshot.get("authority"),
            "indicator_authority_version": indicator_snapshot.get("authority_version"),
            "price_performance": price_performance,
            "component_states": {
                "chart": "READY" if candles else "UNAVAILABLE",
                "mtf": "READY" if mtf_complete else "INCOMPLETE",
                "levels": "READY" if level_complete else "UNAVAILABLE",
                "indicators": "READY" if indicator_complete else "INCOMPLETE",
            },
            "policy": "Single-pass materialized local technical read model; all 10 MTF frames and approved mathematics preserved; no provider/scanner/controller dependency.",
        }

    @staticmethod
    def _parse_time(value: Any) -> datetime | None:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)
        except Exception:
            return None

    def _refresh_needed(self, payload: Dict[str, Any]) -> bool:
        """Return whether the materialized mathematical projection is stale.

        Projection freshness is distinct from candle/session age: a completed
        daily candle from the prior exchange session is legitimate structural
        evidence, while an hours-old materialized projection during a live
        session is not. The foreground may display retained values as explicitly
        stale diagnostics, but stale projection state is never decision-usable.
        """
        if payload.get("needs_v3_projection") or payload.get("needs_v2_projection"):
            return True
        if str((payload.get("price_performance") or {}).get("state") or "").upper() != "READY":
            return True
        stamp = self._parse_time(payload.get("projected_at") or payload.get("as_of"))
        if stamp is None:
            return True
        age = (datetime.now(timezone.utc) - stamp).total_seconds()
        # Future-dated projections are causally invalid rather than "fresh".
        if age < -2.0:
            return True
        return age > 60.0

    def request_projection(self, instrument: Dict[str, Any], *, reason: str = "read_warmup") -> bool:
        """Restore/compute exactly one missing symbol on the bounded producer lane."""
        instrument_key = str(instrument.get("instrument_key") or "")
        if not instrument_key:
            return False

        def hydrate_or_project() -> None:
            with self.lock:
                current = dict(self.cache.get(instrument_key) or {})
            if current:
                return
            retained = self._hydrate_one(instrument_key)
            if retained:
                return
            fresh = self.project_local(instrument)
            if fresh.get("ok"):
                fresh["source"] = "MATERIALIZED_LOCAL_PROJECTION"
                try:
                    self.store.set_kv(self._key(instrument_key), fresh)
                finally:
                    self._cache_payload(instrument_key, fresh)

        result = local_projection_dispatcher_for_app(self.app).submit(
            f"technical-hydrate:{instrument_key}", hydrate_or_project, priority=PRIORITY_TECHNICAL
        )
        return bool(result.accepted or result.state == "COALESCED")

    def request_refresh(self, instrument: Dict[str, Any]) -> bool:
        instrument_key = str(instrument.get("instrument_key") or "")
        if not instrument_key:
            return False

        def refresh_local() -> None:
            fresh = self.project_local(instrument)
            if fresh.get("ok"):
                fresh["source"] = "MATERIALIZED_LOCAL_REFRESH"
                try:
                    self.store.set_kv(self._key(instrument_key), fresh)
                finally:
                    self._cache_payload(instrument_key, fresh)

        result = local_projection_dispatcher_for_app(self.app).submit(
            f"technical-refresh:{instrument_key}", refresh_local, priority=PRIORITY_PERSISTENCE
        )
        return bool(result.accepted or result.state == "COALESCED")

    # Compatibility alias for explicit producer/test callers. Foreground read()
    # never invokes this method.
    def _compute_local(self, instrument: Dict[str, Any]) -> Dict[str, Any]:
        return self.project_local(instrument)

    def read(self, instrument: Dict[str, Any]) -> Dict[str, Any]:
        instrument_key = str(instrument.get("instrument_key") or "")
        if not instrument_key:
            return {"ok": False, "version": self.VERSION, "state": "IDENTITY_UNAVAILABLE"}
        retained = self._load_materialized(instrument_key)
        if retained:
            refresh_needed = self._refresh_needed(retained)
            queued = self.request_refresh(instrument) if refresh_needed else False
            if refresh_needed:
                return {
                    **retained,
                    "ok": False,
                    "state": "STALE",
                    "decision_usable": False,
                    "read_model_version": self.VERSION,
                    "source": "MATERIALIZED_MEMORY_SNAPSHOT",
                    "refreshing": queued,
                    "freshness": "STALE",
                    "data_status": "STALE_MATERIALIZED_PROJECTION",
                    "reason": "Materialized technical projection exceeded the freshness contract; retained values are display-only until refreshed.",
                }
            usable = retained.get("decision_usable") is True
            return {
                **retained,
                "ok": bool(retained.get("ok")) and usable,
                "state": "READY" if usable else "INCOMPLETE",
                "decision_usable": usable,
                "read_model_version": self.VERSION,
                "source": "MATERIALIZED_MEMORY_SNAPSHOT",
                "refreshing": False,
                "freshness": "CURRENT" if usable else "INCOMPLETE",
                "data_status": "CURRENT_MATERIALIZED_PROJECTION" if usable else "INCOMPLETE_MATERIALIZED_PROJECTION",
            }

        queued = self.request_projection(instrument, reason="cold")
        return {
            "ok": False,
            "version": self.VERSION,
            "state": "WARMING",
            "instrument_key": instrument_key,
            "symbol": str(instrument.get("trading_symbol") or instrument.get("symbol") or "").upper(),
            "mtf": [],
            "levels": {},
            "daily_high_water": None,
            "storage_high_water": None,
            "component_states": {"chart": "WARMING", "mtf": "WARMING", "levels": "WARMING"},
            "refreshing": queued,
            "source": "MATERIALIZED_SNAPSHOT_PENDING",
            "policy": "Foreground is memory-first with one bounded indexed retained-snapshot lookup; cold mathematics remains background-only.",
        }
