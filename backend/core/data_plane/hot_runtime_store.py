from __future__ import annotations

"""In-process live market state for the v68 production data plane.

The live path performs no SQLite write. Accepted observations update one
thread-safe memory authority and are independently queued to QuestDB by the
caller-supplied callbacks. The store deliberately implements the bounded API
used by the existing application so production can cut over without silently
falling back to the legacy runtime database.
"""

from collections import defaultdict
from datetime import datetime, timedelta, timezone
import copy
import threading
from typing import Any, Callable, Dict, Iterable, List, Mapping

from core.runtime_market_state_store import (
    CANONICAL_BAR_INTERVALS,
    RUNTIME_SCHEMA_VERSION,
    _float,
    _interval_minutes,
    _iso,
    _now,
    _parse_timestamp,
    _session_bucket_meta,
)


BarSink = Callable[[Mapping[str, Any]], bool]
TickSink = Callable[[Mapping[str, Any]], bool]
QualitySink = Callable[[Mapping[str, Any]], bool]


class HotRuntimeMarketStateStore:
    """Bounded in-process quote/bar/risk authority.

    This object owns only current-session/live-recovery state. Durable market
    history belongs to QuestDB and Parquet. Operational trade/risk truth belongs
    to PostgreSQL. The in-memory state is safe to rebuild after restart.
    """

    SERVICE_VERSION = "hot-runtime-memory-1.0.0"

    def __init__(
        self,
        *,
        tick_sink: TickSink | None = None,
        bar_sink: BarSink | None = None,
        quality_sink: QualitySink | None = None,
        max_bars_per_series: int = 5000,
    ):
        self._tick_sink = tick_sink
        self._bar_sink = bar_sink
        self._quality_sink = quality_sink
        self._max_bars = max(100, int(max_bars_per_series))
        self._lock = threading.RLock()
        self._latest_by_key: dict[str, dict[str, Any]] = {}
        self._symbol_to_key: dict[str, str] = {}
        self._tick_state: dict[str, dict[str, Any]] = {}
        self._bars: dict[tuple[str, str], dict[str, dict[str, Any]]] = defaultdict(dict)
        self._risk: dict[str, dict[str, Any]] = {}
        self._counters = {
            "accepted_ticks": 0,
            "rejected_ticks": 0,
            "out_of_order_ticks": 0,
            "duplicate_sequence_ticks": 0,
            "provider_sequence_gaps": 0,
            "bars_updated": 0,
            "seed_rows": 0,
            "questdb_tick_enqueue_failures": 0,
            "questdb_bar_enqueue_failures": 0,
            "questdb_quality_enqueue_failures": 0,
        }
        self._started_at = _now()
        self._last_observation_at: str | None = None

    @staticmethod
    def _quote_key(row: Mapping[str, Any]) -> str:
        return str(row.get("instrument_key") or "").strip()

    def save_latest_quotes(self, quotes: Iterable[Dict[str, Any]]) -> int:
        count = 0
        with self._lock:
            for raw in quotes or ():
                row = dict(raw or {})
                key = self._quote_key(row)
                if not key:
                    continue
                symbol = str(row.get("symbol") or "").upper().strip()
                self._latest_by_key[key] = copy.deepcopy(row)
                if symbol:
                    self._symbol_to_key[symbol] = key
                count += 1
        return count

    def latest_quotes(self, symbols: Iterable[str] = ()) -> List[Dict[str, Any]]:
        clean = [str(symbol).upper().strip() for symbol in symbols or () if str(symbol).strip()]
        with self._lock:
            if not clean:
                rows = list(self._latest_by_key.values())[-500:]
            else:
                rows = []
                for symbol in clean:
                    key = self._symbol_to_key.get(symbol)
                    row = self._latest_by_key.get(key or "")
                    if row is not None:
                        rows.append(row)
        return [copy.deepcopy(row) for row in rows]

    def ingest_market_observation(self, observation: Dict[str, Any]) -> Dict[str, Any]:
        row = dict(observation or {})
        key = self._quote_key(row)
        price = _float(row.get("ltp") if row.get("ltp") is not None else row.get("close"))
        provider_time = _parse_timestamp(
            row.get("provider_ts_ms")
            or row.get("provider_timestamp")
            or row.get("timestamp")
            or row.get("source_time")
        )
        if not key or price is None or price <= 0 or provider_time is None:
            with self._lock:
                self._counters["rejected_ticks"] += 1
            return {"ok": False, "state": "REJECTED", "reason": "identity_price_or_timestamp_missing"}
        if row.get("identity_verified") is not True:
            with self._lock:
                self._counters["rejected_ticks"] += 1
            return {"ok": False, "state": "REJECTED", "reason": "instrument_identity_unverified"}

        provider_iso = _iso(provider_time)
        local_date = provider_time.astimezone(timezone(timedelta(hours=5, minutes=30))).date().isoformat()
        cumulative_volume = _float(row.get("volume_traded_today") or row.get("volume") or row.get("vtt"))
        try:
            provider_sequence = int(row.get("provider_sequence") or row.get("sequence") or 0)
        except (TypeError, ValueError):
            provider_sequence = 0
        updated: list[str] = []
        emitted: list[dict[str, Any]] = []
        quality_event: dict[str, Any] | None = None

        with self._lock:
            prior = self._tick_state.get(key)
            prior_time = _parse_timestamp((prior or {}).get("last_provider_ts"))
            prior_provider_sequence = int((prior or {}).get("last_provider_sequence") or 0)
            if prior_time is not None and provider_time < prior_time:
                self._counters["rejected_ticks"] += 1
                self._counters["out_of_order_ticks"] += 1
                return {"ok": False, "state": "REJECTED", "reason": "out_of_order_runtime_tick"}
            if provider_sequence > 0 and prior_provider_sequence > 0 and provider_sequence <= prior_provider_sequence and provider_time <= prior_time:
                self._counters["rejected_ticks"] += 1
                self._counters["duplicate_sequence_ticks"] += 1
                return {"ok": False, "state": "REJECTED", "reason": "duplicate_provider_sequence"}

            canonical_sequence = int((prior or {}).get("canonical_sequence") or 0) + 1
            row["canonical_sequence"] = canonical_sequence
            if provider_sequence > 0:
                row["provider_sequence"] = provider_sequence
            if provider_sequence > 0 and prior_provider_sequence > 0 and provider_sequence > prior_provider_sequence + 1:
                gap_size = provider_sequence - prior_provider_sequence - 1
                self._counters["provider_sequence_gaps"] += 1
                row["quality_state"] = "GAP_DETECTED"
                quality_event = {
                    "event_ts": provider_iso,
                    "instrument_key": key,
                    "event_type": "PROVIDER_SEQUENCE_GAP",
                    "source_sequence": provider_sequence,
                    "canonical_sequence": canonical_sequence,
                    "gap_size": gap_size,
                    "detail": f"expected {prior_provider_sequence + 1}; received {provider_sequence}",
                }

            prior_volume = None
            if prior and prior.get("trade_date") == local_date:
                prior_volume = _float(prior.get("last_cumulative_volume"))
            volume_delta = 0.0
            if cumulative_volume is not None and prior_volume is not None:
                volume_delta = max(0.0, cumulative_volume - prior_volume)

            symbol = str(row.get("symbol") or key).upper()
            row.setdefault("provider_timestamp", provider_iso)
            row.setdefault("received_at", _now())
            self._latest_by_key[key] = copy.deepcopy(row)
            if symbol:
                self._symbol_to_key[symbol] = key

            for minutes in CANONICAL_BAR_INTERVALS:
                meta = _session_bucket_meta(provider_time, minutes)
                if meta is None:
                    continue
                start, end, session_partial, actual_span_minutes = meta
                interval = f"{minutes}m"
                series = self._bars[(key, interval)]
                start_iso = _iso(start)
                end_iso = _iso(end)
                existing = series.get(start_iso)
                if existing is None:
                    bar = {
                        "instrument_key": key,
                        "symbol": symbol,
                        "exchange": row.get("exchange") or "NSE",
                        "interval": interval,
                        "timestamp": start_iso,
                        "bar_start": start_iso,
                        "bar_start_ts": start_iso,
                        "bar_end": end_iso,
                        "bar_end_ts": end_iso,
                        "open": price,
                        "high": price,
                        "low": price,
                        "close": price,
                        "volume": 0.0,
                        "oi": _float(row.get("oi")),
                        "tick_count": 0,
                        "first_provider_timestamp": provider_iso,
                        "source": "upstox_v3_hot_runtime",
                        "quality_state": str(row.get("quality_state") or "VERIFIED"),
                        "universe_revision": row.get("universe_revision") or "",
                    }
                else:
                    bar = dict(existing)
                bar["high"] = max(float(bar["high"]), price)
                bar["low"] = min(float(bar["low"]), price)
                bar["close"] = price
                bar["volume"] = round(float(bar.get("volume") or 0.0) + volume_delta, 6)
                bar["oi"] = _float(row.get("oi")) if row.get("oi") is not None else bar.get("oi")
                bar["tick_count"] = int(bar.get("tick_count") or 0) + 1
                bar["last_provider_timestamp"] = provider_iso
                bar["received_at"] = _now()
                bar["forming"] = provider_time < end
                bar["is_closed"] = provider_time >= end
                bar["session_partial"] = bool(session_partial)
                bar["is_partial_session_bar"] = bool(session_partial)
                bar["expected_minutes"] = minutes
                bar["actual_span_minutes"] = actual_span_minutes
                bar["pattern_eligible"] = not bool(session_partial)
                bar["canonical_bar_version"] = RUNTIME_SCHEMA_VERSION
                series[start_iso] = bar
                # Close older bars and bound memory.
                for older_start, older in list(series.items()):
                    if older_start < start_iso and not older.get("is_closed"):
                        older["is_closed"] = True
                        older["forming"] = False
                while len(series) > self._max_bars:
                    series.pop(min(series))
                updated.append(interval)
                emitted.append(copy.deepcopy(bar))

            self._tick_state[key] = {
                "trade_date": local_date,
                "last_provider_ts": provider_iso,
                "last_provider_sequence": provider_sequence,
                "canonical_sequence": canonical_sequence,
                "last_cumulative_volume": cumulative_volume,
                "last_price": price,
                "updated_at": _now(),
            }
            self._counters["accepted_ticks"] += 1
            self._counters["bars_updated"] += len(updated)
            self._last_observation_at = _now()

        if self._tick_sink is not None:
            try:
                if self._tick_sink(row) is False:
                    with self._lock:
                        self._counters["questdb_tick_enqueue_failures"] += 1
            except Exception:
                with self._lock:
                    self._counters["questdb_tick_enqueue_failures"] += 1
        if self._bar_sink is not None:
            for bar in emitted:
                try:
                    if self._bar_sink(bar) is False:
                        with self._lock:
                            self._counters["questdb_bar_enqueue_failures"] += 1
                except Exception:
                    with self._lock:
                        self._counters["questdb_bar_enqueue_failures"] += 1
        if quality_event is not None and self._quality_sink is not None:
            try:
                if self._quality_sink(quality_event) is False:
                    with self._lock:
                        self._counters["questdb_quality_enqueue_failures"] += 1
            except Exception:
                with self._lock:
                    self._counters["questdb_quality_enqueue_failures"] += 1

        return {
            "ok": True,
            "state": "UPDATED",
            "instrument_key": key,
            "intervals": updated,
            "provider_timestamp": provider_iso,
        }

    def save_canonical_candles(
        self,
        instrument_key: str,
        interval: Any,
        candles: Iterable[Dict[str, Any]],
        *,
        source: str = "upstox_intraday_seed",
    ) -> int:
        minutes = _interval_minutes(interval)
        key = str(instrument_key or "").strip()
        if not key or minutes not in CANONICAL_BAR_INTERVALS:
            return 0
        written = 0
        emitted: list[dict[str, Any]] = []
        with self._lock:
            series = self._bars[(key, f"{minutes}m")]
            for raw in candles or ():
                row = dict(raw or {})
                start = _parse_timestamp(row.get("timestamp") or row.get("time") or row.get("date"))
                if start is None:
                    continue
                meta = _session_bucket_meta(start, minutes)
                if meta is None:
                    continue
                bucket_start, bucket_end, partial, span = meta
                values = {name: _float(row.get(name)) for name in ("open", "high", "low", "close")}
                if any(value is None for value in values.values()):
                    continue
                start_iso, end_iso = _iso(bucket_start), _iso(bucket_end)
                bar = {
                    **row,
                    "instrument_key": key,
                    "symbol": str(row.get("symbol") or "").upper() or None,
                    "exchange": row.get("exchange") or "NSE",
                    "interval": f"{minutes}m",
                    "timestamp": start_iso,
                    "bar_start": start_iso,
                    "bar_start_ts": start_iso,
                    "bar_end": end_iso,
                    "bar_end_ts": end_iso,
                    **values,
                    "volume": _float(row.get("volume")) or 0.0,
                    "oi": _float(row.get("oi")),
                    "tick_count": int(row.get("tick_count") or 0),
                    "is_closed": bool(row.get("is_closed", True)),
                    "forming": not bool(row.get("is_closed", True)),
                    "session_partial": bool(partial),
                    "is_partial_session_bar": bool(partial),
                    "expected_minutes": minutes,
                    "actual_span_minutes": span,
                    "pattern_eligible": not bool(partial),
                    "source": source,
                    "quality_state": str(row.get("quality_state") or "VERIFIED"),
                    "canonical_bar_version": RUNTIME_SCHEMA_VERSION,
                    "received_at": _now(),
                }
                series[start_iso] = bar
                emitted.append(copy.deepcopy(bar))
                written += 1
            while len(series) > self._max_bars:
                series.pop(min(series))
            self._counters["seed_rows"] += written
        if self._bar_sink is not None:
            for bar in emitted:
                try:
                    self._bar_sink(bar)
                except Exception:
                    with self._lock:
                        self._counters["questdb_bar_enqueue_failures"] += 1
        return written

    def canonical_bars(
        self,
        instrument_key: str,
        interval: Any,
        *,
        limit: int = 500,
        include_forming: bool = True,
    ) -> List[Dict[str, Any]]:
        minutes = _interval_minutes(interval)
        key = str(instrument_key or "").strip()
        if not key or minutes not in CANONICAL_BAR_INTERVALS:
            return []
        with self._lock:
            rows = [copy.deepcopy(row) for _, row in sorted(self._bars.get((key, f"{minutes}m"), {}).items())]
        if not include_forming:
            rows = [row for row in rows if row.get("is_closed")]
        return rows[-max(1, min(int(limit), self._max_bars)) :]

    def canonical_bar_health(self, instrument_key: str = "") -> Dict[str, Any]:
        with self._lock:
            counts: dict[str, int] = defaultdict(int)
            latest: dict[str, str] = {}
            instrument_count = set()
            for (key, interval), series in self._bars.items():
                if instrument_key and key != instrument_key:
                    continue
                instrument_count.add(key)
                counts[interval] += len(series)
                if series:
                    latest[interval] = max(latest.get(interval, ""), max(series))
            counters = dict(self._counters)
        return {
            "ok": True,
            "state": "ready",
            "service_version": self.SERVICE_VERSION,
            "schema_version": RUNTIME_SCHEMA_VERSION,
            "storage_engine": "in_process_memory",
            "durability": "questdb_microbatch_plus_operational_postgres_risk",
            "configured_intervals": [f"{m}m" for m in CANONICAL_BAR_INTERVALS],
            "counts": dict(counts),
            "latest": latest,
            "instrument_count": len(instrument_count),
            "started_at": self._started_at,
            "last_observation_at": self._last_observation_at,
            "counters": counters,
        }

    def reconcile_derived_bars_from_1m(self) -> Dict[str, Any]:
        # Live ingestion already updates every canonical interval from the same
        # ordered observation. No second writer is allowed to regenerate those
        # rows in production.
        with self._lock:
            source_rows = sum(len(series) for (key, interval), series in self._bars.items() if interval == "1m")
        return {
            "ok": True,
            "state": "LIVE_CANONICAL_INTERVALS_ALREADY_DERIVED",
            "source_rows": source_rows,
            "written_rows": 0,
            "intervals": {},
        }

    def record_risk_state(self, payload: Dict[str, Any]) -> None:
        decision_id = str(payload.get("decision_id") or payload.get("signal_id") or "").strip()
        if not decision_id:
            raise ValueError("decision_id is required")
        row = dict(payload)
        row.setdefault("updated_at", _now())
        row.setdefault("state", row.get("status") or "WATCHING")
        with self._lock:
            self._risk[decision_id] = row

    def risk_states(self, *, mode: str = "", state: str = "") -> List[Dict[str, Any]]:
        with self._lock:
            rows = [copy.deepcopy(row) for row in self._risk.values()]
        if mode:
            rows = [row for row in rows if str(row.get("mode") or "").lower() == str(mode).lower()]
        if state:
            rows = [row for row in rows if str(row.get("state") or row.get("status") or "").upper() == str(state).upper()]
        rows.sort(key=lambda row: str(row.get("updated_at") or ""), reverse=True)
        return rows[:1000]

    def prune(self, *, keep_days: int = 5) -> Dict[str, int]:
        # Session memory is bounded by row count. Retention is automatic and
        # does not run disk maintenance on the live path.
        return {"bars_removed": 0, "events_removed": 0, "state": "memory_bounded"}
