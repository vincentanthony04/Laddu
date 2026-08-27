from __future__ import annotations

"""Incremental canonical market-cache authority.

Candidate 19 contract:

* provider acquisition is limited to three canonical base series;
* already validated local history is never re-requested merely because a
  derived timeframe is opened;
* derived timeframes are materialised from local base series only when the
  source watermark changes;
* the durable watermark manifest is routing evidence, never a replacement for
  canonical candle storage or PostgreSQL coverage authority.

This service is deliberately background-only. Foreground Workspace/Stock Report
reads continue to consume memory/materialised projections and never invoke it.
"""

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import threading
from typing import Any, Dict, Iterable, Mapping

from core.canonical_candle_projection_service import CanonicalCandleProjectionService
from core.db_utils import canonical_timestamp
from core.storage_layout import StorageLayout, atomic_write_json
from core.timeframe import Timeframe, parse_timeframe


SERVICE_VERSION = "incremental-market-cache-authority-1.0.0"
MANIFEST_VERSION = "incremental-market-cache-manifest-1.0.0"

# Three provider families cover the ten customer timeframes without deleting
# any mathematical evidence. 1m supplies 1m/3m/5m; 15m supplies
# 15m/30m/1H/4H; daily supplies 1D/1W/1M.
PROVIDER_BASE_PLAN = (
    ("1m", "1minute", 28),
    ("15m", "15minute", 85),
    ("1D", "day", 1825),
)


def canonical_provider_source(value: Any) -> str:
    tf = parse_timeframe(value)
    if tf in {Timeframe.M1, Timeframe.M3, Timeframe.M5, Timeframe.M10}:
        return "1minute"
    if tf in {Timeframe.M15, Timeframe.M30, Timeframe.H1, Timeframe.H4}:
        return "15minute"
    return "day"


def derived_intraday_target_minutes(value: Any) -> int | None:
    return {Timeframe.M3: 3, Timeframe.M5: 5, Timeframe.M30: 30, Timeframe.H1: 60, Timeframe.H4: 240}.get(parse_timeframe(value))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _coverage_fingerprint(coverage: Mapping[str, Any] | None) -> Dict[str, Any]:
    row = dict(coverage or {})
    return {
        "count": int(row.get("count") or 0),
        "first": row.get("first"),
        "last": row.get("last"),
        "last_received_at": row.get("last_received_at"),
    }


def _row_stamp(row: Mapping[str, Any], interval: str) -> str | None:
    return canonical_timestamp(
        row.get("timestamp") or row.get("ts") or row.get("bar_start_ts") or row.get("date"),
        interval,
    )


def _rows_fingerprint(rows: Iterable[Mapping[str, Any]], interval: str) -> str:
    digest = hashlib.sha256()
    for row in rows or []:
        stamp = _row_stamp(row, interval) or ""
        material = (
            stamp, row.get("open"), row.get("high"), row.get("low"),
            row.get("close"), row.get("volume"), row.get("oi"),
        )
        digest.update(json.dumps(material, separators=(",", ":"), default=str).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


class IncrementalMarketCacheAuthority:
    VERSION = SERVICE_VERSION
    SOURCE_LIMITS = {"1minute": 12_000, "15minute": 8_000, "day": 2_500}

    def __init__(self, store: Any):
        self.store = store
        layout = getattr(store, "layout", None)
        if not isinstance(layout, StorageLayout):
            data_dir = getattr(store, "data_dir", None)
            layout = StorageLayout.from_data_dir(Path(data_dir)) if data_dir else None
        self.layout = layout
        self.manifest_path = (
            layout.manifests_dir / "incremental-market-cache.json" if isinstance(layout, StorageLayout) else None
        )
        self.projector = CanonicalCandleProjectionService()
        self._manifest_lock = threading.RLock()
        self._series_lock_guard = threading.Lock()
        self._series_locks: dict[str, threading.Lock] = {}
        self._manifest = self._load_manifest()

    def _load_manifest(self) -> Dict[str, Any]:
        path = self.manifest_path
        if path is None or not path.exists():
            return {"manifest_version": MANIFEST_VERSION, "updated_at": None, "series": {}}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if payload.get("manifest_version") != MANIFEST_VERSION or not isinstance(payload.get("series"), dict):
                return {"manifest_version": MANIFEST_VERSION, "updated_at": None, "series": {}}
            return payload
        except Exception:
            return {"manifest_version": MANIFEST_VERSION, "updated_at": None, "series": {}}

    def _lock_for(self, instrument_key: str) -> threading.Lock:
        key = str(instrument_key or "")
        with self._series_lock_guard:
            lock = self._series_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._series_locks[key] = lock
            return lock

    def _persist_manifest(self) -> None:
        if self.manifest_path is None:
            return
        with self._manifest_lock:
            self._manifest["updated_at"] = _now()
            payload = json.loads(json.dumps(self._manifest, default=str))
        atomic_write_json(self.manifest_path, payload)

    @staticmethod
    def _targets(source_interval: str) -> tuple[tuple[str, int | str], ...]:
        source = canonical_provider_source(source_interval)
        if source == "1minute":
            return (("3m", 3), ("5m", 5))
        if source == "15minute":
            return (("30m", 30), ("60m", 60), ("240m", 240))
        return (("1w", "week"), ("1mo", "month"))

    def _derive(self, source_interval: str, rows: Iterable[Mapping[str, Any]], target: str, rule: int | str):
        material = [dict(row) for row in rows or []]
        if not material:
            return []
        source = canonical_provider_source(source_interval)
        if source == "1minute":
            return self.projector.resample_intraday(material, int(rule), source_minutes=1)
        if source == "15minute":
            return self.projector.resample_intraday(material, int(rule), source_minutes=15)
        if rule == "week":
            return self.projector.resample_weekly(self.projector.completed_daily(material))
        return self.projector.resample_monthly(self.projector.completed_daily(material))

    def materialize_changed(self, instrument_key: str, source_interval: Any) -> Dict[str, Any]:
        """Materialise only downstream bars affected by a changed base watermark.

        Repeated invocation with the same count/last/received watermark is a
        storage-free no-op. A provider correction is detected through
        ``last_received_at`` even when the terminal timestamp did not advance.
        """
        key = str(instrument_key or "").strip()
        source = canonical_provider_source(source_interval)
        if not key:
            return {"ok": False, "state": "BAD_REQUEST", "source_interval": source, "written": 0}
        lock = self._lock_for(key)
        with lock:
            coverage = _coverage_fingerprint(self.store.candle_coverage(key, source) or {})
            series_id = f"{key}\u001f{source}"
            with self._manifest_lock:
                previous = dict((self._manifest.get("series") or {}).get(series_id) or {})
            if coverage["count"] <= 0 or not coverage.get("last"):
                return {
                    "ok": True, "state": "SOURCE_EMPTY", "source_interval": source,
                    "source_watermark": coverage, "written": 0, "targets": [],
                }
            previous_watermark = dict(previous.get("source_watermark") or {})
            structural_now = {name: coverage.get(name) for name in ("count", "first", "last")}
            structural_before = {name: previous_watermark.get(name) for name in ("count", "first", "last")}
            if previous and structural_before == structural_now and previous_watermark.get("last_received_at") == coverage.get("last_received_at"):
                return {
                    "ok": True, "state": "CURRENT", "source_interval": source,
                    "source_watermark": coverage, "written": 0, "targets": list(previous.get("targets") or []),
                }

            limit = int(self.SOURCE_LIMITS[source])
            rows = list(self.store.get_candles(key, source, limit=limit) or [])
            source_tail_hash = _rows_fingerprint(rows, source)
            # A repeated provider response may update received_at without
            # changing any OHLCV row. Treat that as CURRENT, not a correction.
            if previous and structural_before == structural_now and previous.get("source_tail_hash") == source_tail_hash:
                with self._manifest_lock:
                    self._manifest.setdefault("series", {})[series_id] = {
                        **previous, "source_watermark": coverage, "last_checked_at": _now()
                    }
                self._persist_manifest()
                return {
                    "ok": True, "state": "CURRENT_DUPLICATE_PROVIDER_RESULT",
                    "source_interval": source, "source_watermark": coverage,
                    "written": 0, "targets": list(previous.get("targets") or []),
                }

            targets: list[Dict[str, Any]] = []
            total_written = 0
            backfill = bool(previous and (
                (coverage.get("first") and previous_watermark.get("first") and str(coverage.get("first")) < str(previous_watermark.get("first")))
                or (int(coverage.get("count") or 0) > int(previous_watermark.get("count") or 0) and coverage.get("last") == previous_watermark.get("last"))
            ))
            correction = bool(previous and structural_before == structural_now and previous.get("source_tail_hash") != source_tail_hash)
            rebuild_tail = bool(backfill or correction)
            for target, rule in self._targets(source):
                derived = list(self._derive(source, rows, target, rule) or [])
                target_coverage = _coverage_fingerprint(self.store.candle_coverage(key, target) or {})
                last_target = canonical_timestamp(target_coverage.get("last"), target) if target_coverage.get("last") else None
                if rebuild_tail:
                    # Backfill/provider correction: rewrite only the bounded
                    # derivation window. Read authority dedupes by timestamp and
                    # latest received_at; immutable old parts need not be edited.
                    delta = derived
                    state = "CORRECTION_TAIL_REBUILT" if correction else "BACKFILL_TAIL_REBUILT"
                else:
                    delta = [row for row in derived if (stamp := _row_stamp(row, target)) and (not last_target or stamp > str(last_target))]
                    state = "APPENDED" if delta else "CURRENT"
                written = int(self.store.save_candles(key, target, delta, source=f"{SERVICE_VERSION}:{source}") or 0) if delta else 0
                total_written += written
                after = _coverage_fingerprint(self.store.candle_coverage(key, target) or {})
                targets.append({
                    "interval": target,
                    "state": state,
                    "candidate_rows": len(delta),
                    "written": written,
                    "watermark": after,
                })

            manifest_row = {
                "instrument_key": key,
                "source_interval": source,
                "source_watermark": coverage,
                "source_tail_hash": source_tail_hash,
                "targets": targets,
                "materialized_at": _now(),
                "policy": "FETCH_ONCE_VALIDATE_ONCE_PERSIST_ONCE_INDEX_ONCE_DELTA_ONLY",
            }
            with self._manifest_lock:
                self._manifest.setdefault("series", {})[series_id] = manifest_row
            self._persist_manifest()
            return {
                "ok": True,
                "state": "UPDATED" if total_written else "CURRENT",
                "source_interval": source,
                "source_watermark": coverage,
                "written": total_written,
                "targets": targets,
                "policy": manifest_row["policy"],
            }

    def status(self) -> Dict[str, Any]:
        with self._manifest_lock:
            rows = list((self._manifest.get("series") or {}).values())
        return {
            "ok": True,
            "service_version": SERVICE_VERSION,
            "base_provider_series": [row[1] for row in PROVIDER_BASE_PLAN],
            "series_watermarks": len(rows),
            "updated_at": self._manifest.get("updated_at"),
            "policy": "FETCH_ONCE_VALIDATE_ONCE_PERSIST_ONCE_INDEX_ONCE_DELTA_ONLY",
        }
