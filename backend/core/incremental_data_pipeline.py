"""Exact cache-first coverage planning and request coalescing for v69.8.0."""
from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import threading
from typing import Any, Callable, Iterable, Mapping, Sequence


DATA_PIPELINE_VERSION = "pl-data-69.8.0"


class CacheOutcome(str, Enum):
    CACHE_HIT = "CACHE_HIT"
    CACHE_PARTIAL_DELTA = "CACHE_PARTIAL_DELTA"
    PROVIDER_GAP_FETCH = "PROVIDER_GAP_FETCH"
    DEFERRED_RATE_LIMIT = "DEFERRED_RATE_LIMIT"
    GOVERNED_FULL_REBUILD = "GOVERNED_FULL_REBUILD"
    UNSCORABLE_DATA_GAP = "UNSCORABLE_DATA_GAP"


class QualityState(str, Enum):
    ACCEPTED = "ACCEPTED"
    REPAIRED = "REPAIRED"
    QUARANTINED_IDENTITY = "QUARANTINED_IDENTITY"
    QUARANTINED_CANDLE = "QUARANTINED_CANDLE"
    DEFERRED_PROVIDER = "DEFERRED_PROVIDER"
    EXCLUDED_INSTRUMENT = "EXCLUDED_INSTRUMENT"
    UNSCORABLE_DATA_GAP = "UNSCORABLE_DATA_GAP"


def _utc(value: datetime | str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True, order=True)
class TimeRange:
    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        start, end = _utc(self.start), _utc(self.end)
        if end <= start:
            raise ValueError("time range must be non-empty and half-open [start,end)")
        object.__setattr__(self, "start", start)
        object.__setattr__(self, "end", end)

    def as_dict(self) -> dict[str, str]:
        return {"from": self.start.isoformat(), "to": self.end.isoformat()}


def merge_ranges(ranges: Iterable[TimeRange]) -> tuple[TimeRange, ...]:
    ordered = sorted(ranges)
    if not ordered:
        return ()
    merged: list[TimeRange] = [ordered[0]]
    for item in ordered[1:]:
        prior = merged[-1]
        if item.start <= prior.end:
            merged[-1] = TimeRange(prior.start, max(prior.end, item.end))
        else:
            merged.append(item)
    return tuple(merged)


def exact_missing_ranges(requested: TimeRange, verified: Iterable[TimeRange]) -> tuple[TimeRange, ...]:
    """Subtract verified coverage from one requested half-open range."""
    clipped: list[TimeRange] = []
    for item in verified:
        start, end = max(requested.start, item.start), min(requested.end, item.end)
        if end > start:
            clipped.append(TimeRange(start, end))
    cursor = requested.start
    missing: list[TimeRange] = []
    for item in merge_ranges(clipped):
        if item.start > cursor:
            missing.append(TimeRange(cursor, item.start))
        cursor = max(cursor, item.end)
    if cursor < requested.end:
        missing.append(TimeRange(cursor, requested.end))
    return tuple(missing)


@dataclass(frozen=True)
class CoverageRecord:
    security_id: str
    interval: str
    earliest_stored: datetime | None
    latest_stored: datetime | None
    expected_latest_completed: datetime
    verified_ranges: tuple[TimeRange, ...] = ()
    missing_ranges: tuple[TimeRange, ...] = ()
    adjustment_version: str = "raw-v1"
    data_version: str = DATA_PIPELINE_VERSION
    last_verified_at: datetime | None = None
    quality_state: str = QualityState.ACCEPTED.value


@dataclass(frozen=True)
class FetchPlan:
    security_id: str
    interval: str
    requested: TimeRange
    missing: tuple[TimeRange, ...]
    outcome: str
    data_version: str
    governed_reason: str | None = None


_FULL_REBUILD_REASONS = {
    "SCHEMA_CHANGE", "FEATURE_DEFINITION_CHANGE", "LABEL_DEFINITION_CHANGE",
    "CORPORATE_ACTION_ADJUSTMENT", "HISTORICAL_CORRECTION", "DATA_INTEGRITY_REPAIR",
    "MODEL_VERSION_CHANGE", "BACKTEST_EXPANSION", "OPERATOR_OVERRIDE",
}


def plan_fetch(
    coverage: CoverageRecord,
    requested: TimeRange,
    *,
    governed_rebuild_reason: str | None = None,
) -> FetchPlan:
    if governed_rebuild_reason:
        reason = governed_rebuild_reason.strip().upper()
        if reason not in _FULL_REBUILD_REASONS:
            raise ValueError("ungoverned full rebuild reason")
        missing = (requested,)
        outcome = CacheOutcome.GOVERNED_FULL_REBUILD.value
    else:
        missing = exact_missing_ranges(requested, coverage.verified_ranges)
        outcome = (
            CacheOutcome.CACHE_HIT.value if not missing else
            CacheOutcome.PROVIDER_GAP_FETCH.value if len(missing) == 1 and missing[0] == requested else
            CacheOutcome.CACHE_PARTIAL_DELTA.value
        )
        reason = None
    return FetchPlan(
        security_id=coverage.security_id,
        interval=coverage.interval,
        requested=requested,
        missing=missing,
        outcome=outcome,
        data_version=coverage.data_version,
        governed_reason=reason,
    )


@dataclass(frozen=True)
class JobKey:
    security_id: str
    interval: str
    start: datetime
    end: datetime
    data_version: str

    @classmethod
    def from_range(cls, security_id: str, interval: str, value: TimeRange, data_version: str) -> "JobKey":
        return cls(security_id, interval, value.start, value.end, data_version)


class RequestCoalescer:
    """One in-flight Future per exact security/interval/range/version."""
    def __init__(self, max_workers: int = 6):
        self._executor = ThreadPoolExecutor(max_workers=max(1, max_workers), thread_name_prefix="LadduGap")
        self._lock = threading.RLock()
        self._inflight: dict[JobKey, Future[Any]] = {}

    def submit(self, key: JobKey, fetch: Callable[[], Any]) -> Future[Any]:
        with self._lock:
            current = self._inflight.get(key)
            if current is not None and not current.done():
                return current

            def run() -> Any:
                try:
                    return fetch()
                finally:
                    with self._lock:
                        self._inflight.pop(key, None)

            future = self._executor.submit(run)
            self._inflight[key] = future
            return future

    def inflight(self) -> tuple[JobKey, ...]:
        with self._lock:
            return tuple(key for key, future in self._inflight.items() if not future.done())

    def close(self) -> None:
        self._executor.shutdown(wait=True, cancel_futures=False)


def validate_candles(rows: Sequence[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    accepted: list[dict[str, Any]] = []
    quarantined: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in rows:
        row = dict(raw)
        stamp = str(row.get("timestamp") or row.get("bar_start_ts") or "")
        reason = ""
        try:
            open_, high, low, close = (float(row[name]) for name in ("open", "high", "low", "close"))
            volume = float(row.get("volume") or 0)
            if high < max(open_, close, low) or low > min(open_, close, high):
                reason = "INVALID_OHLC_ORDER"
            elif volume < 0:
                reason = "NEGATIVE_VOLUME"
            elif not stamp:
                reason = "TIMESTAMP_MISSING"
            elif stamp in seen:
                reason = "DUPLICATE_TIMESTAMP"
        except (KeyError, TypeError, ValueError):
            reason = "NON_NUMERIC_OHLC"
        if reason:
            row["quality_state"] = QualityState.QUARANTINED_CANDLE.value
            row["quality_reason"] = reason
            quarantined.append(row)
            continue
        seen.add(stamp)
        row["quality_state"] = QualityState.ACCEPTED.value
        accepted.append(row)
    accepted.sort(key=lambda row: str(row.get("timestamp") or row.get("bar_start_ts") or ""))
    return accepted, quarantined


def derive_calendar_bars(rows: Sequence[Mapping[str, Any]], period: str) -> list[dict[str, Any]]:
    """Derive weekly/monthly OHLCV only from accepted daily bars."""
    key = period.strip().lower()
    if key not in {"week", "month"}:
        raise ValueError("period must be week or month")
    accepted, rejected = validate_candles(rows)
    if rejected:
        raise ValueError("cannot derive from quarantined daily candles")
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in accepted:
        stamp = _utc(str(row.get("timestamp") or row.get("bar_start_ts")))
        bucket = f"{stamp.isocalendar().year}-W{stamp.isocalendar().week:02d}" if key == "week" else stamp.strftime("%Y-%m")
        groups.setdefault(bucket, []).append(row)
    output: list[dict[str, Any]] = []
    for bucket in sorted(groups):
        bars = groups[bucket]
        output.append({
            "timestamp": bars[0].get("timestamp") or bars[0].get("bar_start_ts"),
            "open": float(bars[0]["open"]),
            "high": max(float(row["high"]) for row in bars),
            "low": min(float(row["low"]) for row in bars),
            "close": float(bars[-1]["close"]),
            "volume": sum(float(row.get("volume") or 0) for row in bars),
            "source": f"derived_daily_to_{key}",
            "quality_state": QualityState.ACCEPTED.value,
            "bucket": bucket,
        })
    return output
