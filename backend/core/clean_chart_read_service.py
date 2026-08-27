from __future__ import annotations

"""Local-first chart read model for Clean Core.

Interactive HTTP never opens Parquet/DuckDB/QuestDB directly.  A chart request
reads hot runtime bars plus the last completed local page.  Missing/stale local
pages are materialized on the dedicated local-projection lane; provider/exact-gap
repair is a different low-concurrency lane and is never awaited by HTTP.
"""

from datetime import datetime, timedelta, timezone
import threading
import time
from typing import Any, Dict, List

from core.canonical_presentation_service import CanonicalPresentationService
from core.db_utils import canonical_interval, canonical_timestamp
from core.canonical_candle_projection_service import CanonicalCandleProjectionService
from core.runtime_primitives import candle_staleness
from core.background_repair_dispatcher import for_app as repair_dispatcher_for_app
from core.local_projection_dispatcher import for_app as local_projection_dispatcher_for_app, PRIORITY_CHART
from core.materialized_chart_overlay_service import MaterializedChartOverlayService
from core.india_time import INDIA_TZ
from core.trading_session_authority import DEFAULT_TRADING_SESSION_AUTHORITY


class CleanChartReadService:
    VERSION = "clean-core-chart-read-6.0.0-strict-continuity-fail-closed"
    LIMIT = 5000
    PAGE_KEY_PREFIX = "clean_core:chart_page:v2:"

    def __init__(self, app: Any):
        self.app = app
        self.store = app.store
        self.presentation = CanonicalPresentationService(self.store)
        self.projector = CanonicalCandleProjectionService()
        self.overlays = MaterializedChartOverlayService(app)
        if not hasattr(app, "_chart_page_cache"):
            setattr(app, "_chart_page_cache", {})
        if not hasattr(app, "_chart_page_cache_lock"):
            setattr(app, "_chart_page_cache_lock", threading.RLock())
        self._page_cache = app._chart_page_cache
        self._page_lock = app._chart_page_cache_lock


    @staticmethod
    def _resolution_identity(rows: List[Dict[str, Any]], norm: str) -> Dict[str, Any]:
        """Prove served timeframe identity from every relevant timestamp.

        Intraday proof is strict: duplicate timestamps, non-grid timestamps,
        missing in-session slots and missing covered trading days all fail.
        Overnight/weekend/holiday gaps are calendar structure, not bar gaps.
        """
        def parsed(value: Any) -> datetime | None:
            if value in (None, ""):
                return None
            try:
                if isinstance(value, (int, float)):
                    raw = float(value)
                    if raw > 10_000_000_000:
                        raw /= 1000.0
                    return datetime.fromtimestamp(raw, tz=timezone.utc).astimezone(INDIA_TZ)
                dt = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=INDIA_TZ)
                return dt.astimezone(INDIA_TZ)
            except Exception:
                return None

        parsed_rows = [parsed(row.get("timestamp") or row.get("ts") or row.get("time") or row.get("date")) for row in rows or []]
        stamps = [dt for dt in parsed_rows if dt is not None]
        raw_count = len(stamps)
        unique = sorted(set(stamps))
        duplicate_count = raw_count - len(unique)
        expected = {"1m":1, "3m":3, "5m":5, "15m":15, "30m":30, "60m":60, "240m":240}.get(norm)
        missing_slots: list[str] = []
        off_grid: list[str] = []
        missing_days: list[str] = []
        observed_deltas: list[float] = []

        if expected:
            by_day: Dict[Any, List[datetime]] = {}
            for dt in unique:
                by_day.setdefault(dt.date(), []).append(dt)
            for day, day_rows in sorted(by_day.items()):
                day_rows = sorted(day_rows)
                for dt in day_rows:
                    minute_of_day = dt.hour * 60 + dt.minute
                    anchor = 9 * 60 + 15
                    if (minute_of_day - anchor) % expected != 0 or dt.second != 0:
                        off_grid.append(dt.isoformat())
                for left, right in zip(day_rows, day_rows[1:]):
                    delta = (right - left).total_seconds()
                    observed_deltas.append(delta)
                    if abs(delta - expected * 60) > 1.0:
                        cursor = left + timedelta(minutes=expected)
                        while cursor < right and len(missing_slots) < 50:
                            missing_slots.append(cursor.isoformat())
                            cursor += timedelta(minutes=expected)
            if unique:
                cursor = unique[0].date()
                last_day = unique[-1].date()
                while cursor <= last_day:
                    if DEFAULT_TRADING_SESSION_AUTHORITY.calendar_covered(cursor) and DEFAULT_TRADING_SESSION_AUTHORITY.is_trading_day(cursor) and cursor not in by_day:
                        missing_days.append(cursor.isoformat())
                    cursor += timedelta(days=1)
            passed = bool(unique) and duplicate_count == 0 and not off_grid and not missing_slots and not missing_days
            reason = "MATCH" if passed else "INTRADAY_CONTINUITY_FAILED"
            return {
                "authority": "SERVED_TIMESTAMP_CONTINUITY", "canonical_interval": norm,
                "passed": passed, "reason": reason, "sample_count": len(unique),
                "expected_seconds": expected * 60, "duplicate_count": duplicate_count,
                "off_grid": off_grid[:20], "missing_slots": missing_slots[:20],
                "missing_trading_days": missing_days[:20],
                "observed_delta_seconds": observed_deltas[:20],
            }

        # Higher timeframes are validated from ordered unique dates. These are
        # presentation-only unless the canonical candle projection has already
        # certified source completeness.
        deltas = [(unique[i] - unique[i-1]).total_seconds() for i in range(1, len(unique))]
        if norm == "1d":
            evidence = [d for d in deltas if 18*3600 <= d <= 5*86400]
            passed = bool(unique) and duplicate_count == 0 and (len(unique) == 1 or len(evidence) == len(deltas))
            reason = "MATCH" if passed else "DAILY_SPACING_MISMATCH"
        elif norm == "1w":
            evidence = deltas
            passed = bool(unique) and duplicate_count == 0 and all(4*86400 <= d <= 11*86400 for d in evidence)
            reason = "MATCH" if passed else "WEEKLY_SPACING_MISMATCH"
        elif norm == "1mo":
            evidence = deltas
            passed = bool(unique) and duplicate_count == 0 and all(24*86400 <= d <= 38*86400 for d in evidence)
            reason = "MATCH" if passed else "MONTHLY_SPACING_MISMATCH"
        else:
            evidence = deltas
            passed, reason = False, "UNSUPPORTED_TIMEFRAME"
        return {
            "authority": "SERVED_TIMESTAMP_CONTINUITY", "canonical_interval": norm,
            "passed": bool(passed), "reason": reason, "sample_count": len(unique),
            "expected_seconds": None, "duplicate_count": duplicate_count,
            "observed_delta_seconds": evidence[:20],
        }

    @staticmethod
    def _public_interval(value: str) -> str:
        norm = canonical_interval(value)
        mapping = {
            "1m": "1minute", "3m": "3minute", "5m": "5minute", "15m": "15minute",
            "30m": "30minute", "60m": "60minute", "240m": "240minute",
            "1d": "day", "1w": "week", "1mo": "month",
        }
        return mapping.get(norm, value)

    def _runtime_rows(self, key: str, interval: str, *, limit: int, before: Any = None) -> List[Dict[str, Any]]:
        """Read only the hot in-memory bar authority on the request thread."""
        norm = canonical_interval(interval)
        cap = max(1, int(limit))
        try:
            rows = list(self.app.runtime_market_state.canonical_bars(
                key, norm, limit=min(cap, 5000), include_forming=before in (None, "")
            ) or [])
        except Exception:
            rows = []
        if before not in (None, ""):
            before_ts = canonical_timestamp(before, norm)
            if before_ts:
                rows = [
                    row for row in rows
                    if (canonical_timestamp(row.get("timestamp") or row.get("ts") or row.get("time") or row.get("date"), norm) or "") < before_ts
                ]
        return rows[-cap:]

    def _append_operational_daily(self, key: str, rows: List[Dict[str, Any]], *, producer: bool = False) -> List[Dict[str, Any]]:
        """Append today's completed session from the canonical 1m base.

        Foreground uses only RuntimeMarketState. The background producer may use
        one bounded local 1m read. The derived row is operational continuity only
        (``research_authority=False``); point-in-time Research keeps provider daily
        history as its authority.
        """
        base = []
        if producer:
            try:
                base = self._direct_store_rows(key, "1m", limit=600)
            except Exception:
                base = []
        else:
            base = self._runtime_rows(key, "1m", limit=600)
        try:
            derived = self.projector.derive_completed_session_daily(base)
            return self.projector.append_preferred_daily(rows, derived)
        except Exception:
            return list(rows or [])

    def _direct_store_rows(self, key: str, interval: str, *, limit: int, before: Any = None) -> List[Dict[str, Any]]:
        """Canonical cold/local read for local-projection workers only."""
        norm = canonical_interval(interval)
        cap = max(1, int(limit))
        before_ts = canonical_timestamp(before, norm) if before not in (None, "") else None
        if before_ts:
            reader = getattr(self.store, "get_candles_before", None)
            if callable(reader):
                return list(reader(key, norm, before_ts, cap) or [])[-cap:]
            rows = list(self.store.get_candles(key, norm, limit=min(20000, max(cap * 4, cap + 256))) or [])
            rows = [
                row for row in rows
                if (canonical_timestamp(row.get("timestamp") or row.get("ts") or row.get("time") or row.get("date"), norm) or "") < before_ts
            ]
            return rows[-cap:]
        window_reader = getattr(self.store, "get_candles_window", None)
        if callable(window_reader):
            return list(window_reader(key, norm, limit=cap) or [])[-cap:]
        return list(self.store.get_candles(key, norm, limit=cap) or [])[-cap:]

    def materialize_rows(self, key: str, interval: str, *, limit: int, before: Any = None) -> List[Dict[str, Any]]:
        """Producer-only entrypoint; callers must already be off the HTTP thread."""
        return self._direct_store_rows(key, interval, limit=limit, before=before)

    @staticmethod
    def _cache_key(key: str, norm: str, before_ts: str | None, cap: int) -> str:
        return f"{key}|{norm}|{before_ts or 'tail'}|{cap}"

    def _materialized_page(self, key: str, norm: str, *, limit: int, before: Any = None) -> tuple[List[Dict[str, Any]], bool]:
        """Return a retained chart read model without cold-storage foreground I/O.

        Memory is authoritative for the hot path. On a process-cold miss C22 may
        perform exactly one hard-bounded indexed KV lookup through the isolated
        PostgreSQL interactive-read pool. It may *not* touch candle catalogues,
        Parquet/DuckDB, QuestDB or providers. Missing/insufficient retained pages
        are rebuilt asynchronously by the chart producer lane.
        """
        cap = max(1, int(limit))
        before_ts = canonical_timestamp(before, norm) if before not in (None, "") else None
        token = self._cache_key(key, norm, before_ts, cap)
        persist_key = self.PAGE_KEY_PREFIX + token
        ttl = 10.0 if norm.endswith("m") else 45.0
        now = time.monotonic()
        with self._page_lock:
            entry = dict(self._page_cache.get(token) or {})

        # Process restart continuity: restore only this exact already-materialized
        # page through one bounded indexed read.  This is intentionally not a
        # fallback to the candle lake and therefore cannot trigger 47k-file I/O.
        if not entry:
            bounded = getattr(self.store, "get_kv_bounded", None)
            retained = {}
            if callable(bounded):
                try:
                    retained = dict(bounded(
                        persist_key, {}, statement_timeout_ms=180, pool_timeout_seconds=0.18
                    ) or {})
                except TypeError:
                    try:
                        retained = dict(bounded(persist_key, {}) or {})
                    except Exception:
                        retained = {}
                except Exception:
                    retained = {}
            retained_rows = [dict(row) for row in (retained.get("rows") or []) if isinstance(row, dict)]
            if retained_rows:
                entry = {
                    "rows": retained_rows[-cap:],
                    "completed_at": now,
                    "storage_high_water": retained.get("storage_high_water"),
                    "retained_materialized_at": retained.get("materialized_at"),
                    "restored_from_indexed_read_model": True,
                }
                with self._page_lock:
                    self._page_cache[token] = dict(entry)

        age = None if not entry.get("completed_at") else max(0.0, now - float(entry.get("completed_at") or 0.0))
        retained_depth = len(entry.get("rows") or [])
        # A previous one-row continuity page is useful to render immediately but
        # must not suppress a proper daily-tail rebuild for liquid stocks.
        insufficient_depth = bool(norm == "1d" and before_ts is None and cap >= 30 and retained_depth < min(30, cap))
        stale = not entry or age is None or age > ttl or insufficient_depth

        def publish(rows: List[Dict[str, Any]], *, storage_high_water: Any = None, persist: bool = False) -> None:
            payload = {
                "rows": [dict(row) for row in rows[-cap:]],
                "completed_at": time.monotonic(),
                "storage_high_water": storage_high_water,
            }
            with self._page_lock:
                self._page_cache[token] = payload
            if persist and rows:
                durable = {
                    "version": self.VERSION,
                    "instrument_key": key,
                    "canonical_interval": norm,
                    "before": before_ts,
                    "limit": cap,
                    "storage_high_water": storage_high_water,
                    "rows": [dict(row) for row in rows[-cap:]],
                    "materialized_at": datetime.now(timezone.utc).isoformat(),
                }
                try:
                    self.store.set_kv(persist_key, durable)
                except Exception:
                    pass

        def produce() -> None:
            try:
                coverage = dict(self.store.candle_coverage(key, norm) or {})
            except Exception:
                coverage = {}
            storage_high_water = coverage.get("last")
            try:
                retained = dict(self.store.get_kv(persist_key, {}) or {})
            except Exception:
                retained = {}
            retained_rows = [dict(row) for row in (retained.get("rows") or []) if isinstance(row, dict)]
            retained_high = retained.get("storage_high_water")
            # Daily continuity is special: a provider daily row can legitimately
            # arrive after close, while the already-persisted 1m base is complete.
            # Rebuild only this bounded page in the producer so the browser does
            # not stay one session stale.
            if norm == "1d" and before_ts is None:
                rows = self._direct_store_rows(key, norm, limit=cap, before=before_ts)
                rows = self._append_operational_daily(key, rows, producer=True)[-cap:]
                high = (rows[-1].get("timestamp") if rows else None) or storage_high_water
                publish(rows, storage_high_water=high, persist=True)
                return
            retained_matches = bool(
                retained_rows
                and int(retained.get("limit") or cap) >= cap
                and (not storage_high_water or not retained_high or str(retained_high) == str(storage_high_water))
            )
            if retained_matches:
                publish(retained_rows, storage_high_water=storage_high_water or retained_high, persist=False)
                return
            rows = self._direct_store_rows(key, norm, limit=cap, before=before_ts)
            publish(rows, storage_high_water=storage_high_water or (rows[-1].get("timestamp") if rows else None), persist=True)

        refreshing = False
        if stale:
            result = local_projection_dispatcher_for_app(self.app).submit(
                f"chart-page:{token}", produce, priority=PRIORITY_CHART
            )
            refreshing = bool(result.accepted or result.state == "COALESCED")
        return [dict(row) for row in (entry.get("rows") or [])][-cap:], refreshing

    def _store_rows(self, key: str, interval: str, *, limit: int, before: Any = None) -> tuple[List[Dict[str, Any]], bool]:
        """Return hot runtime + last completed local page without cold HTTP I/O."""
        norm = canonical_interval(interval)
        cap = max(1, int(limit))
        before_ts = canonical_timestamp(before, norm) if before not in (None, "") else None
        if getattr(self.store, "production_candle_repository", None) is None:
            return self._direct_store_rows(key, norm, limit=cap, before=before_ts), False
        persisted, refreshing = self._materialized_page(key, norm, limit=cap, before=before_ts)
        runtime = self._runtime_rows(key, norm, limit=cap, before=before_ts)
        if norm == "1d" and before_ts is None:
            persisted = self._append_operational_daily(key, persisted, producer=False)[-cap:]
        if not runtime:
            return persisted[-cap:], refreshing
        merged: Dict[str, Dict[str, Any]] = {}
        for row in persisted + runtime:
            ts = canonical_timestamp(row.get("timestamp") or row.get("ts") or row.get("time") or row.get("date"), norm)
            if ts:
                out = dict(row)
                out["timestamp"] = ts
                merged[ts] = out
        return [merged[k] for k in sorted(merged)][-cap:], refreshing

    def _derived_rows(self, key: str, requested: str, *, limit: int, before: Any = None) -> tuple[List[Dict[str, Any]], bool]:
        """Derive customer frames only from Candidate-19 canonical bases.

        1m -> 3m/5m, 15m -> 30m/1H/4H, daily -> week/month. The
        provider/data-lake boundary therefore never needs separate 3m/5m/30m/
        hourly/4H histories.
        """
        norm = canonical_interval(requested)
        cap = max(1, int(limit))
        refreshing = False
        try:
            if norm == "1w":
                daily, refreshing = self._store_rows(key, "1d", limit=min(self.LIMIT * 3, cap * 7 + 14), before=before)
                return self.projector.resample_weekly(daily)[-cap:], refreshing
            if norm == "1mo":
                daily, refreshing = self._store_rows(key, "1d", limit=min(self.LIMIT * 6, cap * 23 + 46), before=before)
                return self.projector.resample_monthly(daily)[-cap:], refreshing
            if norm in {"3m", "5m"}:
                target = int(norm[:-1])
                minute, refreshing = self._store_rows(key, "1m", limit=min(self.LIMIT * target, cap * target + target * 8), before=before)
                return self.projector.resample_intraday(minute, target, source_minutes=1)[-cap:], refreshing
            if norm in {"30m", "60m", "240m"}:
                target = int(norm[:-1])
                factor = max(1, target // 15)
                base, refreshing = self._store_rows(key, "15m", limit=min(self.LIMIT * factor, cap * factor + factor * 12), before=before)
                return self.projector.resample_intraday(base, target, source_minutes=15)[-cap:], refreshing
        except Exception:
            return [], refreshing
        return [], refreshing

    def _paging_coverage(self, key: str, norm: str, direct: Dict[str, Any]) -> tuple[Dict[str, Any], str]:
        """Foreground-safe coverage projection.

        Storage catalogue reconciliation belongs to the page producer. HTTP does
        not call candle_coverage or acquire the 47k-file catalogue lock.
        """
        source = {
            "3m": "1m", "5m": "1m",
            "30m": "15m", "60m": "15m", "240m": "15m",
            "1w": "1d", "1mo": "1d",
        }.get(norm, norm)
        return dict(direct or {}), source

    def _completed(self, rows: List[Dict[str, Any]], interval: str) -> List[Dict[str, Any]]:
        try:
            return self.projector.completed_chart(rows, interval)
        except Exception:
            return list(rows or [])

    def _schedule_repair(self, symbol: str, interval: str) -> bool:
        scheduler = getattr(self.app, "schedule_historical_for_symbol", None)
        if not callable(scheduler):
            return False
        public_interval = self._public_interval(interval)

        def run() -> None:
            try:
                scheduler(symbol, public_interval, None)
            except TypeError:
                scheduler(symbol, public_interval)

        result = repair_dispatcher_for_app(self.app).submit(
            f"historical:{symbol.upper()}:{canonical_interval(interval)}", run
        )
        return bool(result.accepted or result.state == "COALESCED")

    def read(self, symbol_or_key: str, interval: str, *, limit: int | None = None, before: Any = None, schedule_repair: bool = True) -> Dict[str, Any]:
        identity = self.presentation.resolve(symbol_or_key)
        if not identity.ok or not identity.instrument_key:
            return {
                "ok": False,
                "service_version": self.VERSION,
                "state": "IDENTITY_UNAVAILABLE",
                "symbol": identity.symbol,
                "instrument": identity.as_dict(),
                "interval": interval,
                "candles": [],
                "count": 0,
                "message": identity.reason,
            }
        cap = max(50, min(self.LIMIT, int(limit or self.LIMIT)))
        norm = canonical_interval(interval)
        before_ts = canonical_timestamp(before, norm) if before not in (None, "") else None
        rows, materializing = self._store_rows(identity.instrument_key, norm, limit=cap, before=before_ts)
        if not rows:
            rows, derived_materializing = self._derived_rows(identity.instrument_key, norm, limit=cap, before=before_ts)
            materializing = materializing or derived_materializing
        rows = self._completed(rows, norm)[-cap:]
        # Foreground coverage is derived from the page already in memory. The
        # producer owns physical catalogue/high-water reconciliation so HTTP can
        # never contend on the 47k-file candle index.
        first = rows[0].get("timestamp") if rows else None
        last = rows[-1].get("timestamp") if rows else None
        coverage = {"count": len(rows), "first": first, "last": last, "scope": "served_page"}
        paging_coverage, paging_source_interval = self._paging_coverage(identity.instrument_key, norm, coverage)
        freshness = candle_staleness(self._public_interval(norm), rows[-1] if rows else None)
        repair_needed = (not rows) if before_ts else (not rows or bool(freshness.get("stale_candles")))
        repair_scheduled = False
        if repair_needed and schedule_repair:
            repair_scheduled = self._schedule_repair(identity.symbol, norm)
        coverage_first = canonical_timestamp(paging_coverage.get("first"), paging_source_interval) if paging_coverage.get("first") else None
        page_first = canonical_timestamp(first, norm) if first else None
        has_more_older = bool(rows and ((coverage_first and page_first and coverage_first < page_first) or (not coverage_first and len(rows) >= cap)))
        overlay_projection = self.overlays.read(identity.instrument_key, norm, rows) if not before_ts else {
            "authority": "CanonicalChartOverlayProjectionAuthority",
            "read_model_version": self.overlays.VERSION,
            "state": "PAGINATED_PRICE_ONLY",
            "series": {}, "metrics": {}, "events": {},
            "policy": "Older chart pages extend price/volume only; retained current overlay projection is not replaced by a partial-history page.",
        }
        timeframe_identity = self._resolution_identity(rows, norm)
        live_usable = bool(rows) and bool(timeframe_identity.get("passed")) and bool(before_ts or not freshness.get("stale_candles"))
        data_status = "ready" if live_usable else "stale_disabled" if rows and freshness.get("stale_candles") else "continuity_failed" if rows and not timeframe_identity.get("passed") else "warming" if materializing else "missing_local"
        return {
            "ok": live_usable,
            "chart_enabled": live_usable,
            "decision_usable": False,
            "service_version": self.VERSION,
            "authority": "LOCAL_CANDLE_REPOSITORY",
            "symbol": identity.symbol,
            "instrument": identity.as_dict(),
            "interval": self._public_interval(norm),
            "canonical_interval": norm,
            "timeframe_identity": timeframe_identity,
            "count": len(rows),
            "candles": rows,
            "first_candle": first,
            "last_candle": last,
            "storage_high_water": coverage.get("last") or last,
            "coverage": coverage,
            "paging": {
                "before": before_ts,
                "page_size": cap,
                "has_more_older": has_more_older,
                "oldest_loaded": first,
                "newest_loaded": last,
                "authority": "LOCAL_ONLY",
                "coverage_source_interval": paging_source_interval,
            },
            "data_status": data_status,
            "refresh_needed": repair_needed,
            "refreshing": bool(materializing or repair_scheduled),
            "freshness": freshness,
            "chart_projection": overlay_projection,
            "request_completed_at": datetime.now(timezone.utc).isoformat(),
            "serving_policy": "HOT_RUNTIME_PLUS_LAST_COMPLETED_LOCAL_PAGE",
            "message": "Older local candles returned without moving the chart authority." if before_ts and rows else "No older local candles are available; local materialization/repair may continue independently." if before_ts else "Verified local candles returned; chart remains non-authoritative decision context." if live_usable else "Local candles exist but chart is disabled because freshness/continuity is not proven." if rows else "Chart history is materializing locally; the request did not wait on cold storage or provider I/O.",
        }
