"""
MarketDataService — Phase 2 (v37.2), Cluster A of docs/SERVICE_CONTRACTS_v37_2.md.

Owns everything that talks to Upstox or the candle/quote store: historical
candles (cache-first, single-flight background refresh), live quote-delta,
and multi-timeframe trend. Nothing outside this file should touch
_hist_lock / _hist_inflight / _hist_executor / _quote_executor /
_quote_delta_cache directly -- go through the methods below instead.

Input/output contract (see docs/SERVICE_CONTRACTS_v37_2.md):
  - get_historical(instrument_key, interval, days, force, max_wait_sec) -> list[candle dict]
  - schedule_historical_refresh(instrument_key, interval, days, reason) -> Future
  - stored_candles(instrument_key, interval, limit) -> list[candle dict]
  - live_quotes(symbols_csv, allow_cached) -> dict (unchanged shape from old LadduRuntime.live_quotes)
  - mtf_trend(instrument) -> list[dict] (unchanged shape from old LadduRuntime.mtf_trend)

Depends only on: Store, UpstoxClient, RateController, and two callbacks
(event, record_error) borrowed from the runtime for consistent logging.
Does NOT depend on engines.py, discovery/opportunity logic, or dashboard code.
"""
from __future__ import annotations

import hashlib
import threading
import time
from datetime import datetime, time as dtime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from concurrent.futures import TimeoutError as FutTimeoutError
from typing import Any, Callable, Dict, Optional

from core.rate_controller import RateController, SlotBusy
from core.canonical_candle_projection_service import CanonicalCandleProjectionService
from core.market_level_service import compute_levels_from_candles
from core.historical_data_service import PREFERRED_RESEARCH_YEARS
from core.local_history_window_service import read_local_history_window
from core.historical_backfill_queue_service import (
    HistoricalBackfillQueueService, summarize_queue,
)
from core.instrument_identity_contract import identity_contract
from core.timeframe import (
    Timeframe, interval_minutes as timeframe_minutes, is_daily as timeframe_is_daily,
    is_intraday as timeframe_is_intraday, parse_timeframe, provider_interval,
    storage_interval,
)
from core.market_clock import (
    IST, candle_staleness, india_now, is_india_market_open,
    parse_timestamp, symbol_key,
)
from core.incremental_market_cache_authority import (
    IncrementalMarketCacheAuthority, PROVIDER_BASE_PLAN, canonical_provider_source, derived_intraday_target_minutes,
)
from core.incremental_data_pipeline import (
    CoverageRecord, JobKey, QualityState, RequestCoalescer, TimeRange,
    exact_missing_ranges, merge_ranges, plan_fetch, validate_candles,
)
from models import now_iso



def _expected_requested_end(interval: str, *, now: datetime | None = None) -> datetime:
    """Exclusive UTC boundary of the latest bar that is legitimately complete.

    Planning to a future midnight made every intraday series permanently look
    incomplete and allowed partial provider responses to be promoted as a
    complete requested range.
    """
    from core.candle_freshness_service import CandleFreshnessService

    clock = (now or india_now()).astimezone(IST)
    tf = parse_timeframe(interval)
    if tf == Timeframe.D1:
        day = datetime.fromisoformat(CandleFreshnessService.expected_daily_date(clock)).date()
        return datetime.combine(day + timedelta(days=1), dtime.min, tzinfo=timezone.utc)
    if timeframe_is_intraday(tf):
        from core.trading_session_authority import DEFAULT_TRADING_SESSION_AUTHORITY
        sessions = DEFAULT_TRADING_SESSION_AUTHORITY
        session_day = datetime.fromisoformat(CandleFreshnessService.expected_intraday_date(clock)).date()
        window = sessions.session_window(session_day)
        if window is None:
            raise RuntimeError("intraday requested-end unavailable: session window is unverified")
        session_start = window.open_at()
        session_end = window.close_at()
        effective = session_end if session_day < clock.date() or clock >= session_end else clock
        minutes = max(1, timeframe_minutes(tf))
        elapsed = max(0, int((effective - session_start).total_seconds() // 60))
        completed = elapsed // minutes
        if completed <= 0:
            # No bar from the current session is complete yet; use the exact
            # previous governed session rather than guessing weekdays.
            previous_day = sessions.previous_trading_day(session_day)
            previous_window = sessions.session_window(previous_day)
            if previous_window is None:
                raise RuntimeError("previous intraday session window is unavailable")
            return previous_window.close_at().astimezone(timezone.utc)
        return (session_start + timedelta(minutes=completed * minutes)).astimezone(timezone.utc)
    # Weekly/monthly are locally derived and should not normally reach the
    # provider planner.  A daily-compatible boundary keeps any legacy caller
    # safe while the canonical derivation path is used.
    day = datetime.fromisoformat(CandleFreshnessService.expected_daily_date(clock)).date()
    return datetime.combine(day + timedelta(days=1), dtime.min, tzinfo=timezone.utc)


def _bounded_local_history_limit(interval: str, days: int) -> int:
    """Bound the foreground local tail to the requested calendar window.

    Deep retained history remains available through chart pagination and the
    Parquet authority.  A freshness/readiness request must not materialise a
    5,000-row tail merely because that was the historical default cap.
    """
    requested_days = max(1, int(days or 1))
    tf = parse_timeframe(interval)
    if tf == Timeframe.D1:
        return min(1500, max(120, int(requested_days * 1.20) + 32))
    if timeframe_is_intraday(tf):
        minutes = max(1, timeframe_minutes(tf))
        estimated = int(requested_days * (375.0 / minutes) * 0.85) + 64
        return min(5000, max(240, estimated))
    return min(1500, max(180, int(requested_days * 1.25) + 32))


def _gap_contains_expected_weekday(start: datetime, end: datetime) -> bool:
    """Compatibility name: true if a gap can contain a governed cash session.

    Inside the release calendar horizon we use TradingSessionAuthority, which
    preserves holidays and special weekend sessions. Outside that proven
    horizon we fail closed (return True) so a historical gap is never bridged
    merely because it falls on a weekend guessed from the Gregorian calendar.
    A provenance-backed historical session index can relax this conservatively.
    """
    from core.trading_session_authority import DEFAULT_TRADING_SESSION_AUTHORITY
    sessions = DEFAULT_TRADING_SESSION_AUTHORITY
    cursor = start.date()
    stop = end.date()
    while cursor < stop:
        if not sessions.calendar_covered(cursor):
            return True
        if sessions.is_trading_day(cursor):
            return True
        cursor += timedelta(days=1)
    return False


def _verified_ranges_from_rows(rows, interval: str, requested: TimeRange) -> tuple[TimeRange, ...]:
    """Return only physically represented, contiguous provider coverage.

    A response with one row cannot certify a multi-day requested range. Missing
    bars inside a response remain visible. Weekend-only daily gaps may be
    bridged because no cash-market candle is expected there.
    """
    stamps = []
    for row in rows or []:
        parsed = _parse_ts_datetime(row.get("timestamp") or row.get("bar_start_ts") or row.get("time") or row.get("date"))
        if parsed is not None:
            stamps.append(parsed.astimezone(timezone.utc))
    if not stamps:
        return ()
    tf = parse_timeframe(interval)
    ranges: list[TimeRange] = []
    if tf == Timeframe.D1:
        for day in sorted({stamp.astimezone(IST).date() for stamp in stamps}):
            physical = TimeRange(
                datetime.combine(day, dtime.min, tzinfo=timezone.utc),
                datetime.combine(day + timedelta(days=1), dtime.min, tzinfo=timezone.utc),
            )
            start, end = max(requested.start, physical.start), min(requested.end, physical.end)
            if end > start:
                ranges.append(TimeRange(start, end))
        bridged: list[TimeRange] = []
        for item in ranges:
            if bridged and not _gap_contains_expected_weekday(bridged[-1].end, item.start):
                bridged[-1] = TimeRange(bridged[-1].start, item.end)
            else:
                bridged.append(item)
        return tuple(bridged)
    duration = timedelta(minutes=max(1, timeframe_minutes(tf)))
    for stamp in sorted(set(stamps)):
        start, end = max(requested.start, stamp), min(requested.end, stamp + duration)
        if end > start:
            ranges.append(TimeRange(start, end))
    return merge_ranges(ranges)


def _verified_range_from_rows(rows, interval: str, requested: TimeRange) -> TimeRange | None:
    """Compatibility helper; only returns a range when coverage is contiguous."""
    ranges = _verified_ranges_from_rows(rows, interval, requested)
    return ranges[0] if len(ranges) == 1 else None

def _daily_history_readiness(coverage: Dict[str, Any], *, target_years: int = 10) -> Dict[str, Any]:
    """Measure readiness from physically persisted daily candles.

    Count alone is not sufficient: a dense recent slice must not masquerade as
    10-year point-in-time research history.  This mirrors the governed
    HistoricalDataReadinessService thresholds while working with the merged
    candle catalogue returned by Store.candle_coverage.
    """
    count = int((coverage or {}).get("count") or 0)
    first = parse_timestamp((coverage or {}).get("first"))
    last = parse_timestamp((coverage or {}).get("last"))
    span_years = 0.0
    if first and last and last >= first:
        span_years = (last - first).total_seconds() / (365.2425 * 86400.0)
    target = max(1, int(target_years or 10))
    minimum_rows = int(target * 252 * 0.90)
    minimum_span_years = round(target * 0.95, 2)
    ready = bool(count >= minimum_rows and span_years >= minimum_span_years)
    blockers = []
    if count < minimum_rows:
        blockers.append(f"{count} candles below required {minimum_rows}")
    if span_years < minimum_span_years:
        blockers.append(f"{span_years:.2f}y span below required {minimum_span_years:.2f}y")
    return {
        "ready": ready, "count": count, "span_years": round(span_years, 3),
        "minimum_rows": minimum_rows, "minimum_span_years": minimum_span_years,
        "target_years": target, "blockers": blockers,
    }


def deep_history_backfill_policy(market_open: bool) -> Dict[str, int | str]:
    """Return a throttled, never-disabled policy for durable history work.

    Market-session state may reduce provider pressure, but it must not stop
    cache reconciliation, analytical storage or backtest preparation.  New
    exchange ticks remain session-bound; historical processing does not.
    """
    if market_open:
        return {
            "state": "running_bounded_market_open",
            "batch_size": 3,
            "scan_window": 24,
            "cycle_sleep_seconds": 30,
            "workers": 1,
        }
    return {
        "state": "running",
        "batch_size": 8,
        "scan_window": 64,
        "cycle_sleep_seconds": 10,
        "workers": 1,
    }


class MarketDataService:
    # C19: three provider bases; all other customer frames are local derivatives.
    PRIORITY_INTERVAL_PLAN = PROVIDER_BASE_PLAN
    PRIORITY_COVERAGE_PLAN = (
        ("1m", "1minute", 40), ("3m", "3minute", 40),
        ("5m", "5minute", 40), ("15m", "15minute", 40),
        ("30m", "30minute", 40), ("1H", "60minute", 40),
        ("4H", "240minute", 20), ("1D", "day", 200),
        ("1W", "week", 40), ("1M", "month", 20),
    )

    def __init__(self, store, client, rate: RateController,
                 event: Callable[..., None], record_error: Callable[..., None],
                 host: Any = None, running_fn: Optional[Callable[[], bool]] = None):
        self.store = store
        self.client = client
        self.rate = rate
        self.event = event
        self.record_error = record_error
        # v51 (Cluster 6): host is the owning LadduRuntime, used only for the
        # handful of things that are genuinely someone else's concern --
        # instrument resolution (_first_instrument / _index_instrument_for_chart),
        # the shared _level_cache, and .status -- same "host reference for
        # entangled bits" pattern used by ScanOrchestrationService. running_fn
        # is a closure over the process-level running flag so later stop
        # transitions remain visible without importing the runtime owner.
        self.host = host
        self.running_fn = running_fn or (lambda: True)

        # v37.2: moved verbatim from LadduRuntime -- same locks, same
        # executors, same semantics. Only owner changed.
        self.hist_blocked_until = 0.0
        # v37.3: was max_workers=3. mtf_trend() alone dispatches 6 parallel
        # historical fetches for one symbol; with chart/quote-delta/dashboard
        # requests overlapping, a 3-worker pool queued jobs behind itself with
        # NO timeout and NO visibility -- the actual Upstox connection cap is
        # already enforced correctly downstream by RateController.net_slot
        # (which has priority + SlotBusy timeout). This pool should only be
        # wide enough that dispatch itself never becomes the bottleneck;
        # net_slot remains the single real gate on concurrent Upstox sockets.
        self._hist_executor = ThreadPoolExecutor(max_workers=12, thread_name_prefix="LadduHist")
        self._hist_lock = threading.RLock()
        self._hist_inflight: Dict[tuple, Any] = {}
        self._exact_gap_coalescer = RequestCoalescer(max_workers=6)

        # v37.3: was max_workers=2 -- same reasoning as _hist_executor above.
        self._quote_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="quote-watchdog")
        self._quote_delta_cache: Dict[str, Dict[str, Any]] = {}
        self._quote_delta_cache_ts = 0.0
        # Visible quote fetches are single-flight.  The previous 1.5-second
        # watchdog abandoned still-running HTTP calls and launched replacements
        # every 3 seconds, producing a queue of orphaned requests and permanent
        # stale-cache delivery.
        self._quote_refresh_lock = threading.RLock()
        self._quote_refresh_future = None
        self._quote_refresh_keys: tuple[str, ...] = ()
        # Stock Intelligence may render the same symbol several times while
        # independent components hydrate. Cache the pure MTF result so those
        # renders do not repeat four SQLite history reads against a multi-GB DB.
        self._mtf_result_cache: Dict[str, tuple[float, list, float]] = {}
        self._mtf_result_lock = threading.RLock()
        self._mtf_result_ttl_sec = 120.0
        self._mtf_closed_ttl_sec = 900.0
        self._mtf_pending_ttl_sec = 15.0
        self._mtf_compute_locks: Dict[str, threading.Lock] = {}
        # Background-only base->derived watermark/materialisation authority.
        self.incremental_cache = IncrementalMarketCacheAuthority(self.store)

    # ---------------------------------------------------------------- candles

    def stored_candles(self, instrument_key: str, interval: str, limit: int = 5000):
        try:
            return self.store.get_candles(instrument_key, interval, limit=limit)
        except Exception as exc:
            self.event("WARN", "candle_store", "Stored candle read failed",
                       {"instrument_key": instrument_key, "interval": interval, "error": str(exc)[:160]})
            return []

    def stored_candles_many(self, instrument_key: str, intervals, limit: int = 5000):
        try:
            reader = getattr(self.store, "get_candles_many", None)
            if callable(reader):
                return reader(instrument_key, list(intervals or []), limit=limit)
            return {
                str(interval): self.stored_candles(instrument_key, str(interval), limit=limit)
                for interval in intervals or []
            }
        except Exception as exc:
            self.event("WARN", "candle_store", "Batch candle read failed", {
                "instrument_key": instrument_key, "error": str(exc)[:160]
            })
            return {}

    def _invalidate_mtf_cache(self, instrument_key: str) -> None:
        with self._mtf_result_lock:
            self._mtf_result_cache.pop(str(instrument_key or ""), None)

    def _mtf_compute_lock(self, instrument_key: str) -> threading.Lock:
        key = str(instrument_key or "")
        with self._mtf_result_lock:
            lock = self._mtf_compute_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._mtf_compute_locks[key] = lock
            return lock

    def _seed_runtime_bars(self, instrument_key: str, interval: str, candles) -> int:
        runtime = getattr(self.store, "runtime_market_state", None) or getattr(self.host, "runtime_market_state", None)
        writer = getattr(runtime, "save_canonical_candles", None)
        if not callable(writer):
            return 0
        try:
            return int(writer(instrument_key, interval, candles or [], source="upstox_intraday_reconciliation") or 0)
        except Exception as exc:
            self.event("WARN", "canonical_bar_plane", "Provider candle reconciliation failed", {
                "instrument_key": instrument_key, "interval": interval, "error": str(exc)[:180]
            })
            return 0

    def _cached_read_instrument(self, symbol: str):
        """Resolve an exact persisted identity for cache reads without network work.

        A temporary resolver/ISIN refresh failure must not hide candles already
        stored under an exact symbol + instrument_key.  This identity is valid
        for local reads only; provider refreshes still use the live resolver.
        """
        requested = str(symbol or "").strip().upper()
        if not requested:
            return None
        candidates = []
        try:
            cache_key = "EQUITY::" + requested
            cached = self.store.get_cached_instrument(cache_key)
            if cached:
                candidates.append(cached)
        except Exception:
            pass
        try:
            candidates.extend(self.store.find_instruments(requested, limit=5) or [])
        except Exception:
            pass
        for row in candidates:
            contract = identity_contract(requested, row)
            if contract.get("ok"):
                return dict(row)
        return None

    def schedule_historical_refresh(self, instrument_key: str, interval: str,
                                     days: int = 20, reason: str = "background"):
        """Single-flight background refresh. Returns a Future (existing
        in-flight one if a matching request is already running), or None if
        blocked by the circuit breaker / missing token."""
        if not instrument_key:
            return None
        # Route every caller to one canonical provider base before gap planning.
        interval = canonical_provider_source(interval)
        if time.time() < self.hist_blocked_until:
            return None
        token = self.client.token_status()
        if not token.get("ok"):
            return None
        interval = provider_interval(interval)
        requested_end = _expected_requested_end(interval)
        requested_start = requested_end - timedelta(days=max(1, int(days or 20)))
        requested = TimeRange(requested_start, requested_end)
        data_version = "upstox-bars-v69.8.0"
        repository = getattr(self.host, "universe_authority_repository", None)
        coverage_repository = repository
        provider_key = str(instrument_key or "")
        is_market_context = provider_key.upper().startswith(("NSE_INDEX|", "BSE_INDEX|"))
        security_id = provider_key
        manifest = None
        if repository is not None:
            security_id = repository.security_id_for_provider_key(provider_key) or ""
            if not security_id and is_market_context:
                # Indices are read-only market context, not ordinary-equity
                # securities.  They deliberately do not enter core.securities
                # or desk snapshots, but their bars must still hydrate and be
                # chartable.  Use the durable candle lake as their coverage
                # authority instead of quarantining a valid provider key.
                security_id = "context:" + hashlib.sha256(provider_key.encode("utf-8")).hexdigest()
                coverage_repository = None
                self.event("INFO", "historical", "Market-context identity accepted outside equity authority", {
                    "instrument_key": provider_key, "interval": interval,
                    "identity_scope": "READ_ONLY_MARKET_CONTEXT",
                })
            elif not security_id:
                self.event("ERROR", "historical", "Canonical security identity missing for provider key", {
                    "instrument_key": instrument_key, "interval": interval,
                    "quality_state": QualityState.QUARANTINED_IDENTITY.value,
                })
                return None
            if coverage_repository is not None:
                manifest = coverage_repository.load_coverage(security_id, str(interval or "day"), data_version)
        coverage = self.store.candle_coverage(instrument_key, interval)
        verified = ()
        first = _parse_ts_datetime(coverage.get("first"))
        last = _parse_ts_datetime(coverage.get("last"))
        if manifest is not None:
            verified = manifest.verified_ranges
            first = manifest.earliest_stored
            last = manifest.latest_stored
        elif first is not None and last is not None and last >= first:
            # One-time retained-data bootstrap.  Once persisted, only the
            # PostgreSQL verified-range manifest is allowed to drive planning.
            verified = (TimeRange(first.astimezone(timezone.utc), last.astimezone(timezone.utc) + timedelta(seconds=1)),)
        coverage_record = CoverageRecord(
            security_id=security_id, interval=str(interval or "day"),
            earliest_stored=first, latest_stored=last,
            expected_latest_completed=requested_end,
            verified_ranges=verified, adjustment_version="provider-raw-v1",
            data_version=data_version, last_verified_at=datetime.now(timezone.utc),
            quality_state=QualityState.ACCEPTED.value,
        )
        fetch_plan = plan_fetch(coverage_record, requested)
        if coverage_repository is not None:
            coverage_repository.audit_fetch_plan(fetch_plan)
        if not fetch_plan.missing:
            self.event("INFO", "historical", "Historical request served from verified local coverage", {
                "instrument_key": instrument_key, "interval": interval,
                "cache_outcome": fetch_plan.outcome,
            })
            return None
        if coverage_repository is not None:
            coverage_repository.enqueue_hydration_jobs(
                fetch_plan, priority=1 if reason not in {"background", "coverage_backfill"} else 3,
                reason_code=reason.upper()[:80],
            )
        # Every missing range is an independent, versioned job identity.
        # Consumers requesting the same gap share the same Future, while one
        # aggregate Future preserves the historical public method contract.
        jobs = [
            (item, self._submit_exact_historical_gap(
                instrument_key, interval, item, coverage_record.data_version,
                reason=reason, requested_days=days, cache_outcome=fetch_plan.outcome,
                incremental=bool(verified),
            ))
            for item in fetch_plan.missing
        ]

        def collect_exact_ranges():
            merged_rows: dict[str, dict[str, Any]] = {}
            accepted_ranges = list(verified)
            for item, future in jobs:
                rows = future.result() if future is not None else []
                for row in rows or []:
                    stamp = str(row.get("timestamp") or row.get("bar_start_ts") or "")
                    if stamp:
                        merged_rows[stamp] = row
                physical_ranges = _verified_ranges_from_rows(rows, interval, item)
                accepted_ranges.extend(physical_ranges)
                if coverage_repository is not None:
                    unresolved_item = exact_missing_ranges(item, physical_ranges)
                    complete = not unresolved_item
                    physical_range = TimeRange(physical_ranges[0].start, physical_ranges[-1].end) if physical_ranges else None
                    coverage_repository.complete_hydration_job(
                        security_id=security_id, interval=str(interval or "day"), item=item,
                        data_version=coverage_record.data_version,
                        state="COMPLETE" if complete else ("PARTIAL" if physical_range is not None else "FAILED"),
                        error=None if complete else (
                            f"provider covered {physical_range.start.isoformat()} to {physical_range.end.isoformat()}"
                            if physical_range is not None else "provider returned no accepted bars"
                        ),
                    )
            if coverage_repository is not None:
                accepted = merge_ranges(accepted_ranges)
                unresolved = exact_missing_ranges(requested, accepted)
                coverage_repository.persist_coverage(CoverageRecord(
                    security_id=security_id, interval=str(interval or "day"),
                    earliest_stored=min((item.start for item in accepted), default=first),
                    latest_stored=max((item.end for item in accepted), default=last),
                    expected_latest_completed=requested_end,
                    verified_ranges=accepted, missing_ranges=unresolved,
                    adjustment_version=coverage_record.adjustment_version,
                    data_version=coverage_record.data_version,
                    last_verified_at=datetime.now(timezone.utc),
                    quality_state=(QualityState.ACCEPTED.value if not unresolved else QualityState.UNSCORABLE_DATA_GAP.value),
                ))
            return [merged_rows[key] for key in sorted(merged_rows)]

        return self._hist_executor.submit(collect_exact_ranges)

    def _submit_exact_historical_gap(
        self,
        instrument_key: str,
        interval: str,
        missing_range: TimeRange,
        data_version: str,
        *,
        reason: str,
        requested_days: int,
        cache_outcome: str,
        incremental: bool,
    ):
        key = JobKey.from_range(str(instrument_key), str(interval or "day"), missing_range, data_version)
        throttled_reasons = (
            "stale_while_revalidate", "coverage_backfill", "chart_cache_revalidate",
            "stock_intelligence_revalidate", "background",
        )
        if reason in throttled_reasons and not self.rate.should_revalidate(instrument_key, interval):
            with self._hist_lock:
                current = self._hist_inflight.get(key)
                if current is not None:
                    return current
        with self._hist_lock:
            current = self._hist_inflight.get(key)
            if current is not None and not current.done():
                return current

            def job():
                live_bars = []
                try:
                    fetch_days = max(1, (missing_range.end.date() - missing_range.start.date()).days)
                    slot_priority = "background" if reason in throttled_reasons else "interactive"
                    slot_timeout = 2.5 if slot_priority == "background" else 5.0
                    unit, _ = self.client._historical_unit_interval(interval)
                    if unit == "minutes":
                        try:
                            with self.rate.net_slot(priority=slot_priority, timeout=slot_timeout):
                                live_bars = self.client.intraday_candles(instrument_key, interval) or []
                        except Exception as exc:
                            self.event("WARN", "intraday", "Current-session candle fetch deferred", {
                                "instrument_key": instrument_key, "interval": interval,
                                "reason": reason, "error": str(exc)[:180],
                            })
                    end_local = missing_range.end.astimezone(IST)
                    # An exclusive midnight boundary belongs to the prior
                    # provider date; an intraday boundary belongs to its own
                    # trading date.  Large missing ranges are split before the
                    # provider call; Upstox rejects e.g. a 420-day 30-minute
                    # request even though each <=85-day chunk is valid.
                    to_day = end_local.date() - timedelta(days=1) if end_local.time() == dtime.min else end_local.date()
                    from_day = missing_range.start.astimezone(IST).date()
                    max_reader = getattr(self.client, "historical_max_window_days", None)
                    # Provider date ranges are inclusive local calendar dates.
                    # Do not derive the fallback window from a floored UTC
                    # timedelta: a partial-boundary gap can span two provider
                    # dates while timedelta.days is only one, causing needless
                    # duplicate network chunks.  When the provider exposes no
                    # explicit cap, one exact missing range is one provider
                    # request.
                    local_span_days = max(1, (to_day - from_day).days + 1)
                    maximum = max(1, int(max_reader(interval) if callable(max_reader) else local_span_days))
                    provider_rows = []
                    provider_chunks = 0
                    cursor = from_day
                    while cursor <= to_day:
                        chunk_end = min(to_day, cursor + timedelta(days=maximum - 1))
                        with self.rate.net_slot(priority=slot_priority, timeout=slot_timeout):
                            batch = self.client.historical_candles_exact_range(
                                instrument_key, interval,
                                from_date=cursor.isoformat(), to_date=chunk_end.isoformat(),
                            ) or []
                        provider_rows.extend(batch)
                        provider_chunks += 1
                        cursor = chunk_end + timedelta(days=1)
                    fresh = provider_rows
                    combined = {str(row.get("timestamp") or ""): row for row in (fresh or []) if row.get("timestamp")}
                    combined.update({str(row.get("timestamp") or ""): row for row in live_bars if row.get("timestamp")})
                    candidates = [combined[key] for key in sorted(combined)]
                    rows, quarantined = validate_candles(candidates)
                    if quarantined:
                        self.event("WARN", "historical_quality", "Provider candles quarantined before persistence", {
                            "instrument_key": instrument_key, "interval": interval,
                            "accepted": len(rows), "quarantined": len(quarantined),
                            "reasons": sorted({str(row.get("quality_reason") or "UNKNOWN") for row in quarantined}),
                            "missing_from": missing_range.start.isoformat(),
                            "missing_to": missing_range.end.isoformat(),
                        })
                    if rows:
                        self._seed_runtime_bars(instrument_key, interval, rows)
                        written = int(self.store.save_candles(instrument_key, interval, rows) or 0)
                        # Background delta materialisation; unchanged watermark is a no-op.
                        derived_materialization = self.incremental_cache.materialize_changed(
                            instrument_key, interval
                        )
                        physical = dict(self.store.candle_coverage(instrument_key, interval) or {})
                        latest_physical = str(physical.get("last") or "")
                        persisted = bool(int(physical.get("count") or 0) > 0 and latest_physical)
                        if not persisted:
                            raise RuntimeError("HISTORICAL_PERSISTENCE_VERIFICATION_FAILED")
                        self._invalidate_mtf_cache(instrument_key)
                        controller = getattr(self.host, "autonomic_controller", None)
                        notify = getattr(controller, "on_data_stored", None)
                        if callable(notify):
                            try:
                                notify(
                                    instrument_key=instrument_key, interval=interval, rows=len(rows),
                                    reason=reason, missing_from=missing_range.start.isoformat(),
                                    missing_to=missing_range.end.isoformat(),
                                )
                            except Exception as exc:
                                self.event("WARN", "autonomic_controller", "Data-ready event publication failed", {"error": str(exc)[:180]})
                        self.rate.mark_revalidated(instrument_key, interval)
                        self.event("INFO", "historical", "Exact historical gap stored and physically verified", {
                            "instrument_key": instrument_key, "interval": interval,
                            "requested_days": requested_days, "provider_days": fetch_days,
                            "provider_chunks": provider_chunks, "provider_max_window_days": maximum,
                            "incremental": incremental, "accepted_count": len(rows),
                            "new_rows_written": written,
                            "derived_rows_written": int(derived_materialization.get("written") or 0),
                            "physical_count": physical.get("count"),
                            "physical_last": latest_physical, "reason": reason,
                            "cache_outcome": cache_outcome,
                            "missing_from": missing_range.start.isoformat(),
                            "missing_to": missing_range.end.isoformat(),
                        })
                    else:
                        self.rate.mark_revalidate_failure(instrument_key, interval)
                    return rows
                except SlotBusy:
                    self.rate.mark_revalidate_failure(instrument_key, interval, retry_after_sec=5.0)
                    self.event("WARN", "historical", "Exact historical gap deferred: no network slot", {
                        "instrument_key": instrument_key, "interval": interval, "reason": reason,
                    })
                    return []
                except Exception as exc:
                    self.rate.mark_revalidate_failure(instrument_key, interval)
                    self.record_error("historical", str(exc), "/v3/historical-candle")
                    self.event("WARN", "historical", "Exact historical gap failed", {
                        "instrument_key": instrument_key, "interval": interval,
                        "error": str(exc)[:220], "reason": reason,
                    })
                    return []
                finally:
                    with self._hist_lock:
                        self._hist_inflight.pop(key, None)

            future = self._exact_gap_coalescer.submit(key, job)
            self._hist_inflight[key] = future
            return future

    def schedule_historical_backfill_before(self, instrument_key: str, interval: str, before_date: str,
                                             days: int | None = None, reason: str = "chart_left_pan"):
        """Schedule one older provider-valid chunk without blocking HTTP.

        The single-flight key includes the requested boundary so repeated
        visible-range callbacks cannot flood Upstox while one chunk is active.
        """
        if not instrument_key or not before_date or time.time() < self.hist_blocked_until:
            return None
        if not self.client.token_status().get("ok"):
            return None
        boundary = str(before_date)[:10]
        key = (str(instrument_key), str(interval or "day"), "before", boundary)
        with self._hist_lock:
            fut = self._hist_inflight.get(key)
            if fut and not fut.done():
                return fut

            def job():
                try:
                    with self.rate.net_slot(priority="interactive", timeout=5.0):
                        rows = self.client.historical_candles_range(
                            instrument_key, interval, before_date=boundary, days=days,
                        )
                    if rows:
                        self.store.save_candles(instrument_key, interval, rows, source="upstox_chart_backfill")
                        self._invalidate_mtf_cache(instrument_key)
                        self.event("INFO", "historical", "Older chart window stored", {
                            "instrument_key": instrument_key, "interval": interval,
                            "before": boundary, "count": len(rows), "reason": reason,
                        })
                    return rows or []
                except SlotBusy:
                    self.event("WARN", "historical", "Older chart fetch skipped: no interactive network slot", {
                        "instrument_key": instrument_key, "interval": interval, "before": boundary,
                    })
                    return []
                except Exception as exc:
                    self.record_error("historical_backfill_before", str(exc), "/v3/historical-candle")
                    self.event("WARN", "historical", "Older chart fetch failed", {
                        "instrument_key": instrument_key, "interval": interval,
                        "before": boundary, "error": str(exc)[:220],
                    })
                    return []
                finally:
                    with self._hist_lock:
                        self._hist_inflight.pop(key, None)

            fut = self._hist_executor.submit(job)
            self._hist_inflight[key] = fut
            return fut

    def get_historical(self, instrument_key: str, interval: str, days: int = 20, *,
                        force: bool = False, max_wait_sec: float = 2.8):
        """Cache-first: return stored candles immediately, kick a background
        refresh, and block briefly for fresh data only if nothing is cached
        yet or a manual refresh was requested."""
        stored = self.stored_candles(instrument_key, interval, limit=5000)
        if stored and not force:
            self.schedule_historical_refresh(instrument_key, interval, days, reason="stale_while_revalidate")
            return stored
        fut = self.schedule_historical_refresh(instrument_key, interval, days,
                                                reason="manual_refresh" if force else "cache_miss")
        if fut:
            try:
                fresh = fut.result(timeout=max(0.5, float(max_wait_sec or 0)))
                if fresh:
                    merged = {c.get("timestamp"): c for c in (stored or []) if c.get("timestamp")}
                    for c in fresh:
                        merged[c.get("timestamp")] = c
                    return sorted(merged.values(), key=lambda c: str(c.get("timestamp") or ""))
            except Exception:
                pass
        stored2 = self.stored_candles(instrument_key, interval, limit=5000)
        return stored2 or stored or []

    # ---------------------------------------------------------------- MTF trend

    @staticmethod
    def _aggregate_minutes(candles, group: int = 4):
        """Backward-compatible wrapper around timestamp-aligned resampling.

        The old implementation grouped by list position and could combine bars
        across NSE sessions. Keep the public helper but align every bucket to
        the actual 09:15 IST session clock.
        """
        source_minutes = 60 if group == 4 else 1
        target_minutes = source_minutes * max(1, int(group or 1))
        return MarketDataService._resample_intraday(candles, target_minutes, source_minutes=source_minutes)

    @staticmethod
    def _resample_intraday(candles, target_minutes: int, *, source_minutes: int = 1):
        return CanonicalCandleProjectionService.resample_intraday(
            candles, target_minutes, source_minutes=source_minutes
        )

    @staticmethod
    def _resample_weekly(candles):
        return CanonicalCandleProjectionService.resample_weekly(candles)

    @staticmethod
    def _resample_monthly(candles):
        return CanonicalCandleProjectionService.resample_monthly(candles)

    @staticmethod
    def _completed_daily_rows(candles):
        return CanonicalCandleProjectionService.completed_daily(candles)

    @staticmethod
    def _derive_completed_session_daily(intraday_rows, *, now=None):
        return CanonicalCandleProjectionService.derive_completed_session_daily(
            intraday_rows, now=now
        )

    def _append_operational_daily(self, instrument_key: str, daily_rows):
        rows = list(daily_rows or [])
        from core.candle_freshness_service import CandleFreshnessService
        expected = CandleFreshnessService.expected_daily_date(india_now())
        latest = _parse_ts_datetime((rows[-1] if rows else {}).get("timestamp")) if rows else None
        if latest is not None and latest.astimezone(IST).date().isoformat() == expected:
            return rows
        intraday = self.stored_candles(instrument_key, "1minute", limit=2500)
        intraday = self._completed_chart_rows(intraday, "1minute")
        derived = self._derive_completed_session_daily(intraday)
        if not derived:
            return rows
        by_date = {}
        for row in rows + [derived]:
            dt = _parse_ts_datetime(row.get("timestamp") or row.get("date"))
            key = dt.astimezone(IST).date().isoformat() if dt else str(row.get("timestamp") or "")[:10]
            if key:
                # A physical provider bar is preferred over an operational
                # derivation for the same date.
                prior = by_date.get(key)
                if prior is None or prior.get("operational_derived") is True:
                    by_date[key] = row
        return [by_date[key] for key in sorted(by_date)]

    def completed_session_quote(self, instrument: Dict[str, Any], daily_rows=None) -> Dict[str, Any]:
        """Return one exact, display-safe completed-session price snapshot."""
        key = str((instrument or {}).get("instrument_key") or "").strip()
        symbol = str((instrument or {}).get("trading_symbol") or (instrument or {}).get("symbol") or "").upper().strip()
        if not key:
            return {}
        rows = self._append_operational_daily(key, daily_rows if daily_rows is not None else self._completed_daily_rows(self.stored_candles(key, "day", limit=5000)))
        if not rows or rows[-1].get("close") is None:
            return {}
        latest = rows[-1]
        previous = rows[-2] if len(rows) >= 2 else {}
        dt = _parse_ts_datetime(latest.get("timestamp") or latest.get("date"))
        source_time = latest.get("period_end") or latest.get("timestamp")
        if dt:
            from core.trading_session_authority import DEFAULT_TRADING_SESSION_AUTHORITY
            session_day = dt.astimezone(IST).date()
            window = DEFAULT_TRADING_SESSION_AUTHORITY.session_window(session_day)
            if window is not None:
                source_time = window.close_at().isoformat(timespec="seconds")
            elif source_time in (None, ""):
                # Historical daily rows outside the active calendar retain their
                # own timestamp; do not fabricate a governed close time.
                source_time = dt.astimezone(IST).isoformat(timespec="seconds")
        return {
            "symbol": symbol, "instrument_key": key,
            "ltp": latest.get("close"), "previous_close": previous.get("close"),
            "provider_timestamp": source_time, "source_time": source_time, "timestamp": source_time,
            "identity_verified": True,
            "source": "derived_completed_session_close" if latest.get("operational_derived") else "verified_completed_daily_close",
            "operational_derived": bool(latest.get("operational_derived")),
            "research_authority": not bool(latest.get("operational_derived")),
        }

    @classmethod
    def _completed_chart_rows(cls, candles, interval):
        return CanonicalCandleProjectionService.completed_chart(candles, interval)

    @staticmethod
    def _prefer_fresh_frame(label: str, direct, derived):
        """Prefer a complete fresh direct series; otherwise use fresh resampling.

        A stale retained 240-minute series must not suppress a current 4H
        aggregate derived from complete 60-minute bars.
        """
        candidates = []
        for priority, rows in ((1, list(direct or [])), (0, list(derived or []))):
            if not rows:
                continue
            freshness = candle_staleness(label, rows[-1])
            usable = not freshness.get("stale_candles") and freshness.get("usable_for_live_confirmation") is not False
            timestamp = parse_timestamp(rows[-1].get("period_end") or rows[-1].get("bar_end") or rows[-1].get("timestamp"))
            candidates.append((1 if usable else 0, timestamp.timestamp() if timestamp else 0.0, priority, rows))
        if not candidates:
            return []
        return max(candidates, key=lambda item: (item[0], item[1], item[2]))[3]

    def _mtf_source_rows(self, instrument_key: str, *, refresh: bool = False, max_wait_sec: float = 7.0):
        """Load four canonical source histories and derive all display frames.

        Normal requests are cache-only. Explicit refresh is bounded and waits
        for at most four single-flight resources. Direct source frames are
        filtered to completed bars before any indicator is calculated.
        """
        from session_candles import closed_candles
        read_specs = {
            "1m": "1minute", "3m": "3minute", "5m": "5minute",
            "15m": "15minute", "30m": "30minute", "60m": "60minute",
            "240m": "240minute", "1D": "day",
        }
        refresh_specs = {
            "1m": ("1minute", 5), "5m": ("5minute", 28),
            "30m": ("30minute", 85), "60m": ("60minute", 365),
            "1D": ("day", 1825),
        }
        key = str(instrument_key or "")
        raw_by_interval = self.stored_candles_many(key, read_specs.values(), limit=1200)
        raw = {
            name: raw_by_interval.get({
                "1minute": "1m", "3minute": "3m", "5minute": "5m", "15minute": "15m",
                "30minute": "30m", "60minute": "60m", "240minute": "240m", "day": "1d",
            }.get(interval, interval), raw_by_interval.get(interval, []))
            for name, interval in read_specs.items()
        }
        rows = {
            "1m": self._completed_chart_rows(raw.get("1m") or [], "1minute"),
            "3m": self._completed_chart_rows(raw.get("3m") or [], "3minute"),
            "5m": self._completed_chart_rows(raw.get("5m") or [], "5minute"),
            "15m": self._completed_chart_rows(raw.get("15m") or [], "15minute"),
            "30m": self._completed_chart_rows(raw.get("30m") or [], "30minute"),
            "60m": self._completed_chart_rows(raw.get("60m") or [], "60minute"),
            "240m": self._completed_chart_rows(raw.get("240m") or [], "240minute"),
            "1D": self._append_operational_daily(
                key, self._completed_daily_rows(raw.get("1D") or [])
            ),
        }
        # Cache reads are pure. Explicit refreshes fetch only canonical provider
        # source intervals; 3m/15m are generated by the shared runtime bar plane.
        if refresh:
            for _name, (interval, days) in refresh_specs.items():
                self.schedule_historical_refresh(key, interval, days, reason="manual_refresh")
        return rows

    def mtf_trend(self, instrument: Dict[str, Any], *, refresh: bool = False) -> list:
        if not instrument or not instrument.get("instrument_key"):
            return []
        cache_key = str(instrument.get("instrument_key"))
        if not refresh:
            with self._mtf_result_lock:
                cached = self._mtf_result_cache.get(cache_key)
            if cached:
                ttl = cached[2] if len(cached) > 2 else self._mtf_result_ttl_sec
                if time.time() - cached[0] <= ttl:
                    return [dict(row, cache="memory") for row in cached[1]]

        compute_lock = self._mtf_compute_lock(cache_key)
        # Interactive Stock Report reads must never queue behind another MTF
        # calculation.  Return the last verified result immediately when a
        # single-flight owner exists; when no prior result exists, return an
        # explicit ten-frame pending contract instead of spending the HTTP
        # timeout waiting on a compute lock.
        acquired = compute_lock.acquire(timeout=0.05)
        if not acquired:
            with self._mtf_result_lock:
                cached = self._mtf_result_cache.get(cache_key)
            if cached:
                return [dict(row, cache="stale_while_revalidate") for row in cached[1]]
            return [
                {
                    "tf": label, "state": "pending", "direction": 0,
                    "strength": 0, "coverage": 0.0, "composite_score": None,
                    "freshness_state": "PENDING_LOCAL_COMPUTE",
                    "reason": "Another local MTF computation owns the single-flight lock",
                    "cache": "compute_inflight",
                }
                for label in ("1m", "3m", "5m", "15m", "30m", "1H", "4H", "1D", "1W", "1M")
            ]
        try:
            # Another request may have completed while this request waited.
            if not refresh:
                with self._mtf_result_lock:
                    cached = self._mtf_result_cache.get(cache_key)
                if cached:
                    ttl = cached[2] if len(cached) > 2 else self._mtf_result_ttl_sec
                    if time.time() - cached[0] <= ttl:
                        return [dict(row, cache="single_flight") for row in cached[1]]

            from indicators import closes, adx, support_resistance
            from core.mtf_semantic_service import MtfSemanticService
            source = self._mtf_source_rows(cache_key, refresh=refresh)
            weekly_rows = self._resample_weekly(source.get("1D") or [])
            monthly_rows = self._resample_monthly(source.get("1D") or [])
            derived_3m = self._resample_intraday(source.get("1m") or [], 3, source_minutes=1)
            derived_5m = self._resample_intraday(source.get("1m") or [], 5, source_minutes=1)
            base_5m = self._prefer_fresh_frame("5m", source.get("5m"), derived_5m)
            derived_15m = self._resample_intraday(base_5m, 15, source_minutes=5)
            derived_30m = self._resample_intraday(base_5m, 30, source_minutes=5)
            derived_1h = self._resample_intraday(source.get("15m") or derived_15m, 60, source_minutes=15)
            derived_4h = self._resample_intraday(source.get("60m") or derived_1h, 240, source_minutes=60)
            frame_rows = [
                ("1m", source.get("1m") or [], "canonical_1m"),
                ("3m", self._prefer_fresh_frame("3m", source.get("3m"), derived_3m), "freshest_canonical_3m_or_1m_resample"),
                ("5m", base_5m, "freshest_canonical_5m_or_1m_resample"),
                ("15m", self._prefer_fresh_frame("15m", source.get("15m"), derived_15m), "freshest_canonical_15m_or_5m_resample"),
                ("30m", self._prefer_fresh_frame("30m", source.get("30m"), derived_30m), "freshest_canonical_30m_or_5m_resample"),
                ("1H", self._prefer_fresh_frame("1H", source.get("60m"), derived_1h), "freshest_canonical_60m_or_15m_resample"),
                ("4H", self._prefer_fresh_frame("4H", source.get("240m"), derived_4h), "freshest_canonical_240m_or_60m_resample"),
                ("1D", source.get("1D") or [], "completed_day"),
                ("1W", weekly_rows, "completed_day_to_week"),
                ("1M", monthly_rows, "completed_day_to_month"),
            ]
            results = []
            semantic_service = MtfSemanticService()
            for label, candles, source_name in frame_rows:
                try:
                    values = closes(candles)
                    freshness = candle_staleness(label, candles[-1] if candles else None)
                    semantic = semantic_service.evaluate_frame(label, candles)
                    state = str(semantic.get("state") or "PENDING").lower()
                    if values and (freshness.get("stale_candles") or freshness.get("usable_for_live_confirmation") is False):
                        state = "stale"
                        semantic = {
                            **semantic,
                            "state": "STALE",
                            "direction": 0,
                            "score": 0,
                            "composite_score": 0,
                            "trend_score": 0,
                            "momentum_score": 0,
                            "participation_score": 0,
                            "structure_score": 0,
                            "quality_score": 0,
                            "confidence": 0,
                            "strength": 0,
                            "coverage": 0.0,
                            "reason": freshness.get("stale_message") or "completed candle is stale",
                        }
                    sr = support_resistance(candles, min(220, len(candles)))
                    metrics = semantic.get("metrics") or {}
                    last = values[-1] if values else None
                    directional_score = (
                        float(semantic.get("desk_directional_score"))
                        if semantic.get("desk_directional_score") is not None and state not in {"pending", "stale"}
                        else None
                    )
                    master_candle = None
                    if label in {"1W", "1M"}:
                        from core.master_candle_service import evaluate_master_candle
                        master_candle = evaluate_master_candle(
                            candles, instrument_key=cache_key, timeframe=label
                        )
                    results.append({
                        "tf": label, "state": state, "close": round(float(last), 2) if last is not None else None,
                        "direction": semantic.get("direction", 0),
                        "strength": semantic.get("strength", 0),
                        "coverage": semantic.get("coverage", 0.0),
                        "rsi": metrics.get("rsi14"),
                        "adx": metrics.get("adx14"),
                        "ema9": metrics.get("ema9"),
                        "ema21": metrics.get("ema21"),
                        "ema50": metrics.get("ema50"),
                        "ema_state": metrics.get("ema_state"),
                        "macd": metrics.get("macd"),
                        "macd_signal": metrics.get("macd_signal"),
                        "macd_hist": metrics.get("macd_hist"),
                        "supertrend_direction": metrics.get("supertrend_direction"),
                        "supertrend_value": metrics.get("supertrend_value"),
                        "rvol20": metrics.get("rvol20"),
                        "roc5_pct": metrics.get("roc5_pct"),
                        "trend_score": semantic.get("trend_score"),
                        "momentum_score": semantic.get("momentum_score"),
                        "participation_score": semantic.get("participation_score"),
                        "structure_score": semantic.get("structure_score"),
                        "quality_score": semantic.get("quality_score"),
                        "composite_score": semantic.get("composite_score"),
                        "confidence": semantic.get("confidence"),
                        "directional_score": directional_score,
                        "component_directional_score": directional_score,
                        "indicator_coverage": semantic.get("coverage", 0.0),
                        "components": semantic.get("components"),
                        "support": sr.get("support"), "resistance": sr.get("resistance"),
                        "last_candle": semantic.get("last_completed_at") or (candles[-1].get("timestamp") if candles else None),
                        "last_completed_at": semantic.get("last_completed_at"),
                        "source": source_name, "count": len(values), "refresh_requested": bool(refresh),
                        "completed_only": True, "ema_periods": [9, 21, 50],
                        "semantic_model": semantic.get("semantic_model"),
                        "semantic_reason": semantic.get("reason"),
                        "master_candle": master_candle,
                        "session_partial_policy": "excluded from strict MTF and master-candle confirmation",
                        **freshness,
                    })
                except Exception as exc:
                    self.record_error("mtf_trend", str(exc), "cache_resample")
                    results.append({"tf": label, "state": "pending", "reason": str(exc)[:160], "source": source_name})
            resolved_count = sum(
                1 for row in results
                if str(row.get("state") or "pending").lower() not in {"pending", "stale"}
            )
            cache_ttl = (self._mtf_result_ttl_sec if is_india_market_open() else self._mtf_closed_ttl_sec) if resolved_count >= 4 else self._mtf_pending_ttl_sec
            with self._mtf_result_lock:
                self._mtf_result_cache[cache_key] = (time.time(), [dict(row) for row in results], cache_ttl)
                if len(self._mtf_result_cache) > 256:
                    oldest = sorted(self._mtf_result_cache.items(), key=lambda item: item[1][0])[:64]
                    for old_key, _ in oldest:
                        self._mtf_result_cache.pop(old_key, None)
            return results
        finally:
            compute_lock.release()

    # ------------------------------------------------------- symbol history

    def schedule_historical_for_symbol(self, symbol: str, interval: str = "day", days: int | None = None,
                                       *, reason: str = "manual_refresh",
                                       force_resolve: bool = False,
                                       mode: str = "delivery") -> Dict[str, Any]:
        """Resolve and schedule history without serialising candle rows.

        This endpoint contract exists for installers, verifiers and browser
        refresh actions. Returning thousands of cached candles merely to start
        a background request caused PowerShell/JSON timeouts even though the
        acquisition itself was healthy.
        """
        symbol = str(symbol or "").strip().upper()
        interval = str(interval or "day").strip()
        if not symbol:
            return {"ok": False, "state": "bad_request", "scheduled": False, "reason": "symbol required"}
        inst = self.host._index_instrument_for_chart(symbol)
        if not inst:
            inst = self.host._first_instrument(symbol, force_refresh=force_resolve)
        if not inst or not inst.get("instrument_key"):
            return {"ok": False, "state": "instrument_not_resolved", "scheduled": False, "symbol": symbol, "interval": interval, "reason": "Instrument token not resolved"}
        requested_tf = parse_timeframe(interval)
        source_interval = canonical_provider_source(requested_tf)
        requested_days = int(days or (260 if requested_tf in {Timeframe.D1, Timeframe.W1, Timeframe.MN1} else 60))
        if not self.client.token_status().get("ok"):
            return {"ok": False, "state": "token_missing", "scheduled": False, "symbol": symbol, "interval": interval, "instrument_key": inst.get("instrument_key"), "reason": "Upstox token missing or expired"}
        started = time.perf_counter()
        self.rate.prioritize_interactive(12.0)
        future = self.schedule_historical_refresh(inst.get("instrument_key"), source_interval, requested_days, reason=reason)
        # A priority job is durable and progresses even when the browser closes.
        # The worker callback reconciles the complete mandatory coverage matrix
        # into PostgreSQL/KV pipeline authority after every exact-gap completion.
        self._observe_priority_coverage(symbol, mode, str(inst.get("instrument_key") or ""),
                                        requested_interval=interval,
                                        running=future is not None,
                                        detail=("Exact-gap worker scheduled" if future is not None else "Local coverage/current in-flight work inspected"))
        if future is not None:
            def _priority_done(done_future):
                try:
                    done_future.result()
                    detail = "Exact-gap worker completed; canonical coverage reconciled"
                except Exception as exc:
                    detail = f"Exact-gap worker failed: {str(exc)[:180]}"
                try:
                    self._observe_priority_coverage(symbol, mode, str(inst.get("instrument_key") or ""),
                                                    requested_interval=interval, running=False, detail=detail)
                except Exception as exc:
                    self.event("WARN", "priority_pipeline", "Coverage reconciliation failed", {
                        "symbol": symbol, "mode": mode, "interval": interval, "error": str(exc)[:180],
                    })
            future.add_done_callback(_priority_done)
        elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
        coverage = self.store.candle_coverage(str(inst.get("instrument_key") or ""), source_interval) or {}
        has_local = int(coverage.get("count") or 0) > 0
        state = "scheduled" if future is not None else "current_or_coalesced" if has_local else "cooldown_or_blocked"
        return {
            "ok": bool(future is not None or has_local), "state": state, "scheduled": future is not None,
            "symbol": symbol, "interval": interval, "source_interval": source_interval,
            "requested_days": requested_days, "instrument_key": inst.get("instrument_key"),
            "elapsed_ms": elapsed_ms, "retry_after_sec": 3,
            "local_count": int(coverage.get("count") or 0),
            "reason": "Historical refresh accepted; durable pipeline progress will update after completion." if future is not None else "Verified local coverage is current or an equivalent exact-gap job is already coalesced." if has_local else "Historical refresh was not scheduled because the provider/token/circuit is unavailable.",
        }

    def _priority_coverage_rows(self, instrument_key: str) -> list[Dict[str, Any]]:
        rows: list[Dict[str, Any]] = []
        for label, interval, minimum in self.PRIORITY_COVERAGE_PLAN:
            coverage = dict(self.store.candle_coverage(instrument_key, interval) or {})
            count = int(coverage.get("count") or 0)
            ready = bool(count >= minimum and coverage.get("first") and coverage.get("last"))
            rows.append({
                "label": label, "interval": interval, "count": count,
                "required_count": minimum, "first": coverage.get("first"),
                "last": coverage.get("last"), "source": coverage.get("source"),
                "state": "CURRENT" if ready else "PARTIAL" if count else "MISSING",
            })
        return rows

    def _observe_priority_coverage(self, symbol: str, mode: str, instrument_key: str, *,
                                   requested_interval: str, running: bool, detail: str) -> Dict[str, Any] | None:
        try:
            from core.priority_pipeline_service import PriorityPipelineService
            service = PriorityPipelineService(self.host)
            snapshot = service.snapshot(symbol=symbol, mode=mode)
            if snapshot.get("state") == "NOT_STARTED":
                return None
            rows = self._priority_coverage_rows(instrument_key)
            ready = sum(1 for row in rows if row.get("state") == "CURRENT")
            state = "READY" if ready == len(rows) else "RUNNING" if running or ready else "WAITING"
            return service.update_stage(
                symbol=symbol, mode=mode, stage_key="coverage", state=state,
                detail=f"{ready}/{len(rows)} mandatory timeframe sources ready · {detail}",
                completed_units=ready, total_units=len(rows),
                evidence={"requested_interval": requested_interval, "timeframes": rows},
            )
        except Exception as exc:
            self.event("WARN", "priority_pipeline", "Priority coverage observation unavailable", {
                "symbol": symbol, "mode": mode, "interval": requested_interval, "error": str(exc)[:180],
            })
            return None

    def schedule_priority_stock_pipeline(self, symbol: str, *, mode: str = "delivery",
                                         selected_interval: str = "day", action: str = "priority_sync") -> Dict[str, Any]:
        """Schedule three exact-gap provider bases; all other frames derive locally."""
        governor = getattr(self.host, "workload_governor", None)
        if governor is not None:
            governor.activate_selected(symbol, mode, ttl_seconds=75.0)
        selected_tf = parse_timeframe(selected_interval or "day")
        selected_source = canonical_provider_source(selected_tf)
        plan = list(self.PRIORITY_INTERVAL_PLAN)
        plan.sort(key=lambda row: 0 if row[1] == selected_source else 1)
        results = []
        for label, interval, days in plan:
            results.append(self.schedule_historical_for_symbol(
                symbol, interval, days, reason=("operator_gap_repair" if action == "repair_gaps" else "operator_priority_sync"),
                force_resolve=False, mode=mode,
            ))
        accepted = sum(1 for row in results if row.get("scheduled"))
        current = sum(1 for row in results if row.get("ok") and not row.get("scheduled"))
        return {
            "ok": bool(accepted or current), "state": "RUNNING" if accepted else "CURRENT_OR_COALESCED" if current else "BLOCKED",
            "symbol": str(symbol or "").upper(), "mode": mode, "action": action,
            "selected_interval": selected_interval, "base_intervals": len(results),
            "scheduled_intervals": accepted, "current_or_coalesced_intervals": current,
            "intervals": results,
        }

    def schedule_historical_before_for_symbol(self, symbol: str, interval: str, before_date: str,
                                               days: int | None = None) -> Dict[str, Any]:
        symbol = str(symbol or "").strip().upper()
        interval = str(interval or "day").strip()
        before_date = str(before_date or "")[:10]
        if not symbol or not before_date:
            return {"ok": False, "state": "bad_request", "scheduled": False, "reason": "symbol and before date are required"}
        inst = self.host._index_instrument_for_chart(symbol)
        if not inst:
            inst = self.host._first_instrument(symbol, force_refresh=False)
        if not inst or not inst.get("instrument_key"):
            return {"ok": False, "state": "instrument_not_resolved", "scheduled": False, "symbol": symbol, "interval": interval}
        requested_tf = parse_timeframe(interval)
        source_interval = canonical_provider_source(requested_tf)
        chunk_days = int(days or self.client.historical_max_window_days(source_interval))
        started = time.perf_counter()
        self.rate.prioritize_interactive(12.0)
        future = self.schedule_historical_backfill_before(inst.get("instrument_key"), source_interval, before_date, chunk_days)
        return {
            "ok": future is not None, "state": "scheduled" if future is not None else "cooldown_or_blocked",
            "scheduled": future is not None, "symbol": symbol, "interval": interval,
            "source_interval": source_interval, "before": before_date, "requested_days": chunk_days,
            "instrument_key": inst.get("instrument_key"),
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
            "reason": "Older chart window accepted; the chart will merge it from local storage." if future is not None else "Older chart window is already running or unavailable.",
        }

    def cached_historical_for_symbol(self, symbol: str, interval: str = "day", *, schedule_refresh: bool = False) -> Dict[str, Any]:
        """Strictly bounded history lookup for composite intelligence routes.

        Reads the local candle cache only. A missing/short cache may schedule a
        single-flight refresh, but this call never waits on network I/O.
        """
        symbol = str(symbol or "").strip().upper()
        interval = str(interval or "day").strip()
        inst = self.host._index_instrument_for_chart(symbol)
        if not inst:
            inst = self._cached_read_instrument(symbol)
        if not inst:
            inst = self.host._first_instrument(symbol)
        if not inst:
            return {"ok": False, "symbol": symbol, "interval": interval, "instrument": None, "candles": [], "count": 0, "data_status": "instrument_not_resolved", "message": "Instrument not resolved"}
        identity = identity_contract(symbol, inst)
        if not identity.get("ok"):
            self.event("ERROR", "cached_history_identity", "Cached historical request rejected by exact identity contract", identity)
            return {"ok": False, "symbol": symbol, "interval": interval, "instrument": inst, "identity_contract": identity, "candles": [], "count": 0, "data_status": "instrument_identity_mismatch", "message": identity.get("reason")}
        requested_tf = parse_timeframe(interval)
        derive_intraday_minutes = derived_intraday_target_minutes(requested_tf)
        derive_weekly = requested_tf == Timeframe.W1
        derive_monthly = requested_tf == Timeframe.MN1
        source_interval = canonical_provider_source(requested_tf)
        key = inst.get("instrument_key")
        rows = self.stored_candles(key, source_interval, limit=5000) if key else []
        # The composite intelligence route is completed-candle only just like
        # the chart route. Filtering here keeps levels, coverage, freshness,
        # technical evidence and Delivery daily analysis on the same rows.
        rows = self._completed_chart_rows(rows, source_interval)
        if timeframe_is_daily(source_interval) and key:
            rows = self._append_operational_daily(key, rows)
        if derive_intraday_minutes:
            source_minutes = 1 if source_interval == "1minute" else 15
            rows = self._resample_intraday(rows, derive_intraday_minutes, source_minutes=source_minutes)
        elif derive_weekly:
            rows = self._resample_weekly(rows)
        elif derive_monthly:
            rows = self._resample_monthly(rows)
        required = 80 if requested_tf in {Timeframe.D1, Timeframe.W1, Timeframe.MN1} else 50
        status = "full" if len(rows) >= required else "partial" if rows else "pending"
        freshness_interval = interval if (derive_intraday_minutes or derive_weekly or derive_monthly) else source_interval
        stale = candle_staleness(freshness_interval, rows[-1] if rows else None)
        refresh_future = None
        refresh_needed = status != "full" or bool(stale.get("stale_candles"))
        if schedule_refresh and refresh_needed and key and self.client.token_status().get("ok"):
            source_tf = parse_timeframe(source_interval)
            days = 900 if source_tf == Timeframe.D1 else 180 if source_tf == Timeframe.H1 else 35
            refresh_future = self.schedule_historical_refresh(key, source_interval, days, reason="stock_intelligence_revalidate")
        levels = compute_levels_from_candles(rows, interval=source_interval) if rows else {}
        return {
            "ok": bool(rows), "symbol": symbol, "interval": interval, "instrument": inst, "identity_contract": identity,
            "candles": rows, "count": len(rows), "last_candle": rows[-1] if rows else None,
            "data_status": status, "coverage_status": status, "required_candles": required,
            "source": "local_candle_cache", "refreshing": bool(refresh_future),
            "levels": levels, "support": levels.get("support"), "resistance": levels.get("resistance"),
            "time": now_iso(), **stale,
            "refresh_needed": refresh_needed,
            "message": (f"Cache {status}: {len(rows)}/{required} completed candles; refresh scheduled." if refresh_future else f"Cache {status}: {len(rows)}/{required} completed candles.") if status != "full" else f"Cache ready: {len(rows)} completed candles; no provider refresh needed.",
        }

    def scanner_analysis_snapshot(self, instrument: Dict[str, Any], mode: str, *, schedule_refresh: bool = True) -> Dict[str, Any]:
        """Return the scanner's immutable local-only analysis input.

        Scanner compute workers are deliberately forbidden from owning provider
        calls.  This method resolves their entire candle dependency from the
        canonical local candle authority before a worker is admitted.  Missing
        or stale history is an explicit DATA_PENDING outcome and may schedule a
        single-flight exact-gap refresh outside the analysis executor.
        """
        desk = str(mode or "").strip().lower()
        if desk not in {"intraday", "delivery"}:
            raise ValueError("scanner analysis snapshot supports Intraday and Delivery only")
        inst = dict(instrument or {})
        instrument_key = str(inst.get("instrument_key") or "").strip()
        symbol = str(inst.get("trading_symbol") or inst.get("symbol") or "").upper().strip()
        if not instrument_key or not symbol:
            return {
                "ok": False, "ready": False, "state": "IDENTITY_UNAVAILABLE",
                "symbol": symbol, "instrument_key": instrument_key, "candles": [],
                "reason": "verified instrument identity is required before scanner analysis",
            }
        interval = "5minute" if desk == "intraday" else "day"
        required = 50 if desk == "intraday" else 120
        limit = 600 if desk == "intraday" else 800
        if desk == "intraday":
            # The live scanner needs only the recent completed-session tail.
            # Opening the retained Parquet lake once per shortlisted symbol put
            # cold file discovery and concurrent-write contention inside the
            # 30-second analysis lane (the installed R8 run took >20 minutes).
            # QuestDB/runtime recent bars are the canonical live input; missing
            # depth schedules the existing exact-gap background refresh.
            recent_reader = getattr(self.store, "get_recent_candles", None)
            rows = list(recent_reader(instrument_key, interval, limit=limit) or []) if callable(recent_reader) else []
            if len(rows) < required and callable(recent_reader):
                base_rows = list(recent_reader(instrument_key, "1minute", limit=500) or [])
                if base_rows:
                    rows = self._resample_intraday(base_rows, 5, source_minutes=1)
        else:
            rows = self.stored_candles(instrument_key, interval, limit=limit)
        rows = self._completed_chart_rows(rows, interval)
        if desk == "delivery":
            rows = self._append_operational_daily(instrument_key, rows)
        freshness = candle_staleness(interval, rows[-1] if rows else None)
        fresh_enough = freshness.get("usable_for_live_confirmation") is not False
        ready = bool(len(rows) >= required and fresh_enough)
        refresh_scheduled = False
        if schedule_refresh and not ready and self.client.token_status().get("ok"):
            try:
                days = 12 if desk == "intraday" else 540
                refresh_scheduled = self.schedule_historical_refresh(
                    instrument_key, interval, days, reason=f"{desk}_scanner_local_snapshot_gap"
                ) is not None
            except Exception as exc:
                self.record_error(f"{desk}_scanner_snapshot_refresh", str(exc)[:240])
        reason = None
        if len(rows) < required:
            reason = f"LOCAL_HISTORY_PENDING:{len(rows)}/{required}:{interval}"
        elif not fresh_enough:
            reason = f"LOCAL_HISTORY_STALE:{freshness.get('stale_message') or interval}"
        return {
            "ok": True,
            "ready": ready,
            "state": "READY" if ready else "DATA_PENDING",
            "symbol": symbol,
            "instrument_key": instrument_key,
            "mode": desk,
            "interval": interval,
            "required_candles": required,
            "count": len(rows),
            "candles": rows,
            "last_candle": rows[-1] if rows else None,
            "refresh_scheduled": refresh_scheduled,
            "reason": reason,
            **freshness,
        }

    def historical_for_symbol(self, symbol: str, interval: str = "day", days: int | None = None,
                               refresh: bool = False, recent_only: bool = False) -> Dict[str, Any]:
        symbol = (symbol or "").strip().upper()
        interval = (interval or "day").strip()
        if not symbol:
            return {"ok": False, "error_type": "bad_request", "message": "symbol required", "error": "symbol required", "endpoint": "historical", "candles": []}
        if refresh:
            self.rate.prioritize_interactive(15.0)
        inst = self.host._index_instrument_for_chart(symbol)
        if not inst:
            inst = self.host._first_instrument(symbol, force_refresh=refresh)
        if not inst:
            return {"ok": False, "error_type": "instrument_not_resolved", "message": "Instrument token not resolved", "error": "instrument not found", "endpoint": "historical", "symbol": symbol, "candles": [], "instrument_count": self.store.instrument_count(), "last_success_at": self.host.status.get("last_historical_fetch")}
        identity = identity_contract(symbol, inst)
        if not identity.get("ok"):
            self.event("ERROR", "historical_identity", "Historical request rejected by exact identity contract", identity)
            return {"ok": False, "error_type": "instrument_identity_mismatch", "message": identity.get("reason"), "error": "instrument identity mismatch", "endpoint": "historical", "symbol": symbol, "instrument": inst, "identity_contract": identity, "candles": []}
        requested_tf = parse_timeframe(interval)
        derive_intraday_minutes = derived_intraday_target_minutes(requested_tf)
        derive_weekly = requested_tf == Timeframe.W1
        derive_monthly = requested_tf == Timeframe.MN1
        source_interval = canonical_provider_source(requested_tf)
        level_lookup_names = [symbol, inst.get("trading_symbol"), inst.get("name")]
        if symbol in ("NIFTY", "NIFTY50"):
            level_lookup_names.append("NIFTY 50")
        if symbol in ("BANKNIFTY", "BANK", "NIFTY BANK"):
            level_lookup_names.append("NIFTY BANK")
        if symbol in ("BSE SENSEX",):
            level_lookup_names.append("SENSEX")
        cached_levels = None
        for nm in level_lookup_names:
            cached_levels = self.host._level_cache.get(f"{symbol_key(nm)}|{source_interval}") if nm else None
            if cached_levels:
                break
        if days is None:
            days = 260 if requested_tf in {Timeframe.D1, Timeframe.W1, Timeframe.MN1} else 60

        def _coverage_payload(count: int) -> Dict[str, Any]:
            # v38.2: requested history is not the same as usable proof. If Upstox/cache
            # returns only a handful of daily candles, Stock Intelligence/Chart Desk
            # must mark S/R as reference-only instead of pretending delivery levels are valid.
            min_required = 80 if requested_tf in {Timeframe.D1, Timeframe.W1, Timeframe.MN1} else 20
            try:
                c = int(count or 0)
            except Exception:
                c = 0
            status = "full" if c >= min_required else ("partial" if c > 0 else "pending")
            if status == "full":
                msg = f"History full: {c}/{min_required} candles available."
            elif status == "partial":
                msg = f"History partial: {c}/{min_required} candles available; S/R is reference-only until coverage improves."
            else:
                msg = f"History pending: 0/{min_required} candles available."
            return {"requested_days": days, "required_candles": min_required, "coverage_status": status, "coverage_message": msg}

        key = inst.get("instrument_key")
        local_read_limit = _bounded_local_history_limit(source_interval, int(days))
        recent_authority_only = bool(recent_only and timeframe_is_intraday(source_interval))
        stored = read_local_history_window(
            self.store, key, source_interval, days=int(days), limit=local_read_limit,
            recent_only=recent_authority_only, fallback=self.stored_candles,
        )
        stored = self._completed_chart_rows(stored, source_interval)
        if timeframe_is_daily(source_interval) and key:
            stored = self._append_operational_daily(key, stored)
        if derive_intraday_minutes and stored:
            source_minutes = 1 if source_interval == "1minute" else 15
            stored = self._resample_intraday(stored, derive_intraday_minutes, source_minutes=source_minutes)
        elif derive_weekly and stored:
            stored = self._resample_weekly(stored)
        elif derive_monthly and stored:
            stored = self._resample_monthly(stored)
        coverage = self.store.candle_coverage(key, source_interval) if key else {"count": 0, "first": None, "last": None}
        token_ok = bool(self.client.token_status().get("ok"))
        if stored:
            # A chart refresh is schedule-and-return. Waiting for Upstox here
            # serialised the chart, selected-stock card and MTF hydration behind
            # one socket and made a healthy background fetch look like a browser
            # timeout. Local completed candles remain authoritative for display;
            # the single-flight refresh updates storage independently.
            display_interval = interval if (derive_intraday_minutes or derive_weekly or derive_monthly) else source_interval
            stale_meta = candle_staleness(display_interval, stored[-1] if stored else None)
            proof = _coverage_payload(len(stored))
            refresh_needed = bool(refresh or stale_meta.get("stale_candles") or proof.get("coverage_status") != "full")
            refresh_reason = "manual_refresh" if refresh else "stale_while_revalidate" if stale_meta.get("stale_candles") else "coverage_backfill"
            fut = self.schedule_historical_refresh(
                key, source_interval, int(days), reason=refresh_reason,
            ) if token_ok and refresh_needed else None
            older_backfill = None
            older_backfill_before = None
            session_dates = set()
            # A valid intraday chart must include multiple completed sessions,
            # not merely the current morning. When local storage contains only
            # a shallow window, schedule one older provider-valid chunk and let
            # the browser merge it on the next poll. Single-flight keys prevent
            # duplicate requests for the same boundary.
            if timeframe_is_intraday(source_interval) and stored and token_ok:
                for candle in stored:
                    dt = _parse_ts_datetime(candle.get("timestamp") or candle.get("time") or candle.get("date"))
                    if dt is not None:
                        session_dates.add(dt.astimezone(IST).date().isoformat())
                if len(stored) < 120 or len(session_dates) < 3:
                    first_dt = _parse_ts_datetime(stored[0].get("timestamp") or stored[0].get("time") or stored[0].get("date"))
                    if first_dt is not None:
                        older_backfill_before = first_dt.astimezone(IST).date().isoformat()
                        older_backfill = self.schedule_historical_backfill_before(
                            key, source_interval, older_backfill_before,
                            self.client.historical_max_window_days(source_interval),
                            reason="automatic_multi_session_chart_depth",
                        )
            levels = dict(cached_levels or compute_levels_from_candles(stored, interval=display_interval))
            levels.setdefault("interval", display_interval)
            levels.setdefault("cached_at", now_iso())
            status = "partial_history" if proof.get("coverage_status") == "partial" else ("stale_cache_refreshing" if stale_meta.get("stale_candles") else ("cache_refreshing" if fut else "cache_only"))
            return {
                "ok": True, "symbol": symbol, "interval": interval, "source_interval": source_interval,
                "derived_interval": interval if derive_intraday_minutes else "week" if derive_weekly else "month" if derive_monthly else None,
                "provider_base_interval": source_interval,
                "days": days, "instrument": inst, "identity_contract": identity,
                "local_read_limit": local_read_limit,
                "count": len(stored), "candles": stored, "last_candle": stored[-1] if stored else None,
                "levels": levels, "support": levels.get("support"), "resistance": levels.get("resistance"),
                "data_status": status,
                "source": "recent_session_authority" if recent_authority_only else "local_candle_cache",
                "recent_authority_only": recent_authority_only,
                "fresh": bool(not stale_meta.get("stale_candles")), "refreshing": bool(fut), "refresh_needed": refresh_needed,
                "older_backfill_scheduled": bool(older_backfill), "older_backfill_before": older_backfill_before,
                "session_count": len(session_dates) if timeframe_is_intraday(source_interval) and stored else None,
                "coverage": coverage, "last_success_at": self.host.status.get("last_historical_fetch"),
                **proof, **stale_meta,
                "message": proof.get("coverage_message") if proof.get("coverage_status") == "partial" else (stale_meta.get("stale_message") or ("Using stored candles immediately; incremental refresh runs in background." if fut else "Using stored completed candles; no provider refresh needed."))
            }
        if not token_ok:
            return {"ok": False, "error_type": "token_missing", "message": "Upstox token missing or expired and no stored candles available", "error": "UPSTOX_TOKEN_MISSING", "endpoint": "historical", "symbol": symbol, "instrument": inst, "identity_contract": identity, "candles": [], "count": 0, "coverage": coverage, "last_success_at": self.host.status.get("last_historical_fetch")}
        # Cold cache: schedule one provider-valid single-flight fetch and return
        # immediately. The frontend progressively polls this endpoint, so a
        # request thread must never sit on a network future just to produce a
        # pending chart shell.
        started = time.time()
        self.rate.prioritize_interactive(12.0)
        fut = self.schedule_historical_refresh(key, source_interval, int(days), reason="manual_refresh" if refresh else "cache_miss")
        elapsed = int((time.time() - started) * 1000)
        proof = _coverage_payload(0)
        return {"ok": False, "error_type": "historical_refresh_pending", "message": "No local completed candles yet. A provider-valid historical refresh is running independently; the chart will retry automatically.", "error": "historical_refresh_pending", "endpoint": "historical", "symbol": symbol, "interval": interval, "instrument": inst, "identity_contract": identity, "candles": [], "count": 0, "data_status": "refresh_pending", "refreshing": bool(fut), "elapsed_ms": elapsed, "local_read_limit": local_read_limit, "recent_authority_only": recent_authority_only, "source": "recent_session_authority" if recent_authority_only else "local_candle_cache", "coverage": coverage, "last_success_at": self.host.status.get("last_historical_fetch"), "retry_after_sec": 3, **proof}

    # -------------------------------------------------- deep history backfill

    def _history_provider_states(self) -> Dict[str, str]:
        try:
            rows = self.store.conn.execute(
                """SELECT instrument_key,state FROM historical_backfill_state
                   WHERE interval IN ('1d','day')"""
            ).fetchall()
            return {str(row[0]): str(row[1] or "") for row in rows}
        except Exception:
            return {}

    def _history_priority_maps(self, universe) -> tuple[Dict[str, int], Dict[str, str]]:
        """Build exact-key priorities without allowing UI activity to bypass identity."""
        by_symbol = {
            str(inst.get("trading_symbol") or inst.get("symbol") or "").upper().strip(): str(inst.get("instrument_key") or "")
            for inst in universe if inst.get("instrument_key")
        }
        priority: Dict[str, int] = {}
        reason: Dict[str, str] = {}

        def add_key(key: str, value: int, why: str) -> None:
            key = str(key or "").strip()
            if not key:
                return
            if key not in priority or value < priority[key]:
                priority[key] = value
                reason[key] = why

        def add_symbol(symbol: str, value: int, why: str) -> None:
            add_key(by_symbol.get(str(symbol or "").upper().strip(), ""), value, why)

        # Open governed Model Paper positions are the highest-priority
        # consumers. Read the production position authority, never a legacy
        # compatibility table.
        try:
            rows = list(self.host.model_portfolio.open_positions() or [])
            for row in rows:
                add_key(row.get("instrument_key"), 0, "open_model_paper_position")
                add_symbol(row.get("symbol"), 0, "open_model_paper_position")
        except Exception:
            pass

        # Interactive selected-stock authority is memory-first; backfill must
        # react immediately and must not wait for a compatibility KV write.
        try:
            governor = getattr(self.host, "workload_governor", None)
            selected = dict(governor.snapshot() or {}) if governor is not None else {}
            add_key(selected.get("selected_instrument_key"), 1, "selected_stock")
            add_symbol(selected.get("selected_stock"), 1, "selected_stock")
        except Exception:
            pass

        # Explicit scanner/Sync-All queue.
        try:
            for symbol in self.store.get_kv("scan_priority_queue", []) or []:
                add_symbol(symbol, 10, "scanner_priority")
        except Exception:
            pass

        # Latest immutable research population.
        try:
            rows = self.store.conn.execute(
                """SELECT instrument_key,symbol FROM candidate_population_observations
                   ORDER BY observed_at DESC LIMIT 600"""
            ).fetchall()
            for row in rows:
                add_key(row[0], 15, "research_population")
                if not row[0]:
                    add_symbol(row[1], 15, "research_population")
        except Exception:
            pass

        # Current scanner shortlist and explicit blockers remain above broad-universe work.
        try:
            scanners = dict(getattr(self.host, "status", {}).get("scanners") or {})
            for desk in ("delivery", "intraday"):
                stage_members = dict((scanners.get(desk) or {}).get("stage_members") or {})
                for stage in ("shortlisted", "analysed", "data_blocked", "blocked"):
                    for row in list(stage_members.get(stage) or [])[:300]:
                        add_key(row.get("instrument_key"), 20 if stage == "shortlisted" else 25, f"scanner_{stage}")
                        add_symbol(row.get("symbol"), 20 if stage == "shortlisted" else 25, f"scanner_{stage}")
        except Exception:
            pass
        return priority, reason

    def deep_history_backfill_loop(self, sup=None):
        """Persistent priority queue for cache-first daily history convergence.

        v97 removes cursor sweeps. Only instruments actually attempted are
        advanced, younger listings can complete against their listing age, and
        provider-depth completion is terminal instead of being retried forever.
        """
        time.sleep(8.0)
        queue = HistoricalBackfillQueueService(self.store)
        while self.running_fn() and (sup is None or sup.running):
            if sup:
                sup.beat("deep_history_backfill")
            policy = deep_history_backfill_policy(bool(is_india_market_open()))
            governor = getattr(self.host, "workload_governor", None)
            if governor is not None:
                policy = governor.history_policy(policy)
            try:
                market_open = bool(is_india_market_open())
                universe = self.store.liquid_wide_universe(limit=2000) or []
                if not universe:
                    time.sleep(60)
                    continue
                priority, priority_reason = self._history_priority_maps(universe)
                rows = queue.reconcile(
                    universe,
                    priority_by_key=priority,
                    priority_reason_by_key=priority_reason,
                    provider_state_by_key=self._history_provider_states(),
                )
                due = queue.due(rows, limit=int(policy.get("batch_size") or 0), market_open=market_open)
                yield_reason = str(policy.get("yield_reason") or "")

                def _backfill_one(item):
                    key = str(item.get("instrument_key") or "")
                    before = self.store.candle_coverage(key, "day") or {}
                    before_count = int(before.get("count") or 0)
                    saved = 0
                    error = ""
                    try:
                        selected = str(item.get("priority_reason") or "") == "selected_stock"
                        open_position = str(item.get("priority_reason") or "") == "open_model_paper_position"
                        network_priority = "interactive" if selected or open_position else "background"
                        saved = int(self.client.deep_backfill_daily_candles(
                            key,
                            years=PREFERRED_RESEARCH_YEARS,
                            request_guard=lambda: self.rate.net_slot(priority=network_priority, timeout=3.5),
                        ) or 0)
                    except Exception as exc:
                        error = str(exc)[:240]
                    after = self.store.candle_coverage(key, "day") or {}
                    after_count = int(after.get("count") or 0)
                    actual_saved = max(saved, after_count - before_count, 0)
                    provider_state = self._history_provider_states().get(key, "PAUSED_ERROR" if error else "RUNNING")
                    if actual_saved <= 0 and not error and provider_state not in {"COMPLETE", "COMPLETE_PROVIDER_DEPTH"}:
                        error = "provider request stored zero new daily candles"
                    return key, actual_saved, provider_state, error, before_count, after_count

                results = []
                workers = max(0, int(policy.get("workers", 1)))
                if due and workers > 0:
                    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="laddu-history-queue") as pool:
                        futures = [pool.submit(_backfill_one, item) for item in due]
                        for future in as_completed(futures):
                            results.append(future.result())
                advanced = no_progress = failed = 0
                for key, saved, provider_state, error, before_count, after_count in results:
                    updated = queue.apply_result(
                        rows,
                        instrument_key=key,
                        rows_saved=saved,
                        provider_state=provider_state,
                        error=error,
                    ) or {}
                    if saved > 0:
                        advanced += 1
                    else:
                        no_progress += 1
                    if error and provider_state == "PAUSED_ERROR":
                        failed += 1
                        self.record_error("deep_history_backfill", f"{key}: {error}; stored {before_count}->{after_count}")
                # Reconcile once more so terminal provider states and physical rows
                # are reflected in the published counts from the same cycle.
                rows = queue.reconcile(
                    universe,
                    priority_by_key=priority,
                    priority_reason_by_key=priority_reason,
                    provider_state_by_key=self._history_provider_states(),
                )
                summary = summarize_queue(rows)
                if summary.get("remaining_unaccounted", summary["remaining_operational"]) == 0:
                    state = "accounted_with_terminal_failures" if summary.get("terminal_failures") else "complete"
                else:
                    state = "yielding_to_higher_priority" if yield_reason else "running" if due else "waiting_retry"
                payload = {
                    "state": state,
                    "version": "persistent-priority-queue-v120-terminal-accounting",
                    "done": summary["operational_ready"],
                    "total": summary["total"],
                    "pct": round(100.0 * summary["operational_ready"] / max(1, summary["total"]), 1),
                    "accounted": summary.get("accounted"),
                    "accounted_pct": round(100.0 * int(summary.get("accounted") or 0) / max(1, summary["total"]), 1),
                    "operational_ready": summary["operational_ready"],
                    "research_ready": summary["research_ready"],
                    "deep_enriched": summary["deep_enriched"],
                    "backfilling": summary["backfilling"],
                    "retry_scheduled": summary["retry_scheduled"],
                    "provider_depth_complete": summary["provider_depth_complete"],
                    "listing_history_complete": summary["listing_history_complete"],
                    "failed": summary["failed"],
                    "terminal_failures": summary.get("terminal_failures", 0),
                    "remaining_unaccounted": summary.get("remaining_unaccounted", summary["remaining_operational"]),
                    "remaining_operational": summary["remaining_operational"],
                    "remaining_research": summary["remaining_research"],
                    "remaining_deep": summary["remaining_deep"],
                    "market_open": market_open,
                    "current_item": (due[0].get("symbol") or due[0].get("instrument_key")) if due else None,
                    "yield_reason": yield_reason or None,
                    "governor_policy": {k: policy.get(k) for k in ("state", "batch_size", "workers", "cycle_sleep_seconds", "yield_reason")},
                    "attempted_this_cycle": len(results),
                    "advanced_this_cycle": advanced,
                    "no_progress_this_cycle": no_progress,
                    "failed_this_cycle": failed,
                    "rows_saved_this_cycle": sum(int(result[1] or 0) for result in results),
                    "preferred_target_years": PREFERRED_RESEARCH_YEARS,
                    "readiness_rule": "listing-age-aware persisted candles; provider/listing depth or exhausted governed retry budget is terminal-accounted without implying operational readiness",
                    "priority_order": [
                        "open_model_paper_position", "selected_stock", "scanner_priority",
                        "research_population", "scanner_shortlisted", "governed_universe",
                    ],
                    "members": rows[:300],
                    "last_run": now_iso(),
                }
                with self.host.lock:
                    self.host.status["deep_history_backfill"] = payload
                if sup:
                    sup.progress(
                        "deep_history_backfill",
                        token=f"{summary['operational_ready']}:{summary['research_ready']}:{summary['deep_enriched']}:{summary.get('terminal_failures', 0)}:{payload['rows_saved_this_cycle']}",
                        stage=state,
                        current_item=(due[0].get("symbol") or due[0].get("instrument_key")) if due else None,
                        completed_units=int(summary["operational_ready"]),
                        total_units=int(summary["total"]),
                        waiting_on=yield_reason or ("retry schedule" if not due and summary["remaining_operational"] else None),
                        expected_idle=not bool(due) and not bool(yield_reason),
                    )
            except Exception as exc:
                self.record_error("deep_history_backfill", str(exc)[:240])
                with self.host.lock:
                    self.host.status["deep_history_backfill"] = {
                        "state": "failed", "error": str(exc)[:240], "last_run": now_iso(),
                        "version": "persistent-priority-queue-v97",
                    }
            time.sleep(int(policy.get("cycle_sleep_seconds", 15)))


def _parse_ts_datetime(ts):
    """Backward-compatible local name for the standalone timestamp parser."""
    return parse_timestamp(ts)
