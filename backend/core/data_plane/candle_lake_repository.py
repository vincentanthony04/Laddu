from __future__ import annotations

"""Direct Parquet authority for historical OHLCV candles.

v69.9.4 serves a persistent file catalogue. A selected-symbol chart must never
open every Parquet part in a timeframe partition merely to discover which
files contain that instrument. The catalogue is a rebuildable routing index;
Parquet remains the historical authority.
"""

from collections import defaultdict, OrderedDict
from datetime import datetime, timezone
import hashlib
import importlib
import copy
import re
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from typing import Any, Dict, Iterable, List, Mapping

from config import DATA_DIR
from core.db_utils import canonical_interval, canonical_timestamp
from core.storage_layout import StorageLayout, atomic_write_json


class CandleLakeRepository:
    SERVICE_VERSION = "candle-parquet-authority-1.5.0-nonblocking-catalog-persistence"
    CATALOG_VERSION = "candle-file-catalog-2.0.0-window-indexed"
    QUERY_CACHE_TTL_SEC = 120.0
    QUERY_CACHE_CAPACITY = 256

    def __init__(
        self,
        data_dir: Path | None = None,
        *,
        max_memory_rows_per_series: int = 5000,
        start_catalog_builder: bool = True,
    ):
        self.layout = StorageLayout.from_data_dir(Path(data_dir or DATA_DIR))
        self.layout.ensure()
        self.root = self.layout.curated_lake_dir / "candles"
        self.catalog_path = self.layout.manifests_dir / "candle-file-catalog.json"
        self._lock = threading.RLock()
        self._catalog_lock = threading.RLock()
        # Serialise manifest writes without holding the read-side catalogue
        # lock. The retained catalogue is large (~tens of thousands of files);
        # serialising it under _catalog_lock previously blocked chart/history
        # coverage reads for seconds during ingestion.
        self._catalog_persist_lock = threading.Lock()
        self._memory: dict[tuple[str, str], dict[str, dict[str, Any]]] = defaultdict(dict)
        self._max_memory_rows = max(100, int(max_memory_rows_per_series))
        self._parts_written = 0
        self._rows_written = 0
        self._catalog: dict[str, Any] = self._empty_catalog()
        self._catalog_state = "MISSING"
        self._catalog_error: str | None = None
        self._catalog_progress = {"processed": 0, "total": 0}
        self._catalog_thread: threading.Thread | None = None
        self._catalog_rebuild_state = "IDLE"
        self._catalog_serving_generation: str | None = None
        self._query_local = threading.local()
        self._series_locks: dict[tuple[str, str, int], threading.Lock] = {}
        self._series_locks_guard = threading.Lock()
        self._multi_query_locks: dict[tuple[str, tuple[str, ...], int], threading.Lock] = {}
        self._query_cache: OrderedDict[tuple[str, str, int], tuple[float, list[dict[str, Any]]]] = OrderedDict()
        self._query_cache_hits = 0
        self._query_cache_misses = 0
        self._query_total_ms = 0.0
        self._query_count = 0
        self._load_catalog()
        # A validated installer catalogue is immediately authoritative.  Never
        # walk or scan the whole retained lake during interactive service
        # startup merely to compare mtimes; the installer and incremental
        # writer own exact catalogue maintenance.
        if start_catalog_builder and self._catalog_state != "READY":
            self.start_catalog_rebuild()

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")

    @classmethod
    def _empty_catalog(cls) -> dict[str, Any]:
        return {
            "catalog_version": cls.CATALOG_VERSION,
            "generated_at": None,
            "root_file_count": 0,
            "root_latest_mtime_ns": 0,
            "series": {},
            "unreadable_files": [],
        }

    @staticmethod
    def _series_id(instrument_key: str, interval: str) -> str:
        return f"{instrument_key}\u001f{interval}"

    def _root_signature(self) -> tuple[int, int]:
        count = 0
        latest = 0
        if not self.root.exists():
            return count, latest
        for path in self.root.glob("timeframe=*/year=*/*.parquet"):
            try:
                stat = path.stat()
            except OSError:
                continue
            count += 1
            latest = max(latest, int(stat.st_mtime_ns))
        return count, latest

    def _load_catalog(self) -> None:
        if not self.catalog_path.exists():
            return
        try:
            payload = json.loads(self.catalog_path.read_text(encoding="utf-8"))
            if payload.get("catalog_version") != self.CATALOG_VERSION or not isinstance(payload.get("series"), dict):
                return
            with self._catalog_lock:
                self._catalog = payload
                self._catalog_state = "READY"
                self._catalog_serving_generation = str(payload.get("generated_at") or "catalog-loaded")
                self._catalog_error = None
        except Exception as exc:
            self._catalog_error = str(exc)
            self._catalog_state = "INVALID"

    def _catalog_needs_rebuild(self, *, verify_disk_signature: bool = False) -> bool:
        """Return whether an explicit maintenance rebuild is required.

        Runtime startup deliberately trusts a structurally valid catalogue.
        A full root signature is available only to installer/maintenance code
        that explicitly opts in; it is never an interactive request cost.
        """
        with self._catalog_lock:
            ready = self._catalog_state == "READY" and isinstance(self._catalog.get("series"), dict)
        if not ready:
            return True
        if not verify_disk_signature:
            return False
        file_count, latest = self._root_signature()
        with self._catalog_lock:
            return (
                int(self._catalog.get("root_file_count") or 0) != file_count
                or int(self._catalog.get("root_latest_mtime_ns") or 0) != latest
            )

    @staticmethod
    def _safe_min(*values: Any) -> Any:
        material = [str(value) for value in values if value not in (None, "")]
        return min(material) if material else None

    @staticmethod
    def _safe_max(*values: Any) -> Any:
        material = [str(value) for value in values if value not in (None, "")]
        return max(material) if material else None

    def _persist_catalog_snapshot(self) -> None:
        """Persist a coherent catalogue snapshot without monopolising readers.

        Save callers may update the in-memory catalogue concurrently, so writes
        are serialised.  Only a deepcopy/generation stamp occurs under the
        catalogue lock; JSON serialisation/fsync/replace runs outside it. A later
        writer always snapshots at least the state seen by an earlier writer.
        """
        with self._catalog_persist_lock:
            with self._catalog_lock:
                generation = self._now()
                self._catalog["generated_at"] = generation
                payload = copy.deepcopy(self._catalog)
            atomic_write_json(self.catalog_path, payload)
            with self._catalog_lock:
                self._catalog_serving_generation = str(self._catalog.get("generated_at") or generation)

    def _persist_catalog_payload(self, payload: dict[str, Any]) -> None:
        payload["generated_at"] = self._now()
        atomic_write_json(self.catalog_path, payload)

    def _merge_catalog_row(
        self,
        catalog: dict[str, Any],
        *,
        instrument_key: str,
        interval: str,
        file_path: Path,
        count: int,
        first: Any,
        last: Any,
        last_received_at: Any,
    ) -> None:
        norm = canonical_interval(interval)
        sid = self._series_id(str(instrument_key), norm)
        series = catalog.setdefault("series", {}).setdefault(sid, {
            "instrument_key": str(instrument_key),
            "interval": norm,
            "files": [],
            "count": 0,
            "first": None,
            "last": None,
            "last_received_at": None,
            "file_segments": [],
        })
        try:
            relative = file_path.resolve().relative_to(self.root.resolve()).as_posix()
        except Exception:
            relative = file_path.resolve().as_posix()
        if relative not in series["files"]:
            series["files"].append(relative)
            series["count"] = int(series.get("count") or 0) + max(0, int(count or 0))
        segments = list(series.get("file_segments") or [])
        segment = {
            "path": relative,
            "count": max(0, int(count or 0)),
            "first": str(first) if first not in (None, "") else None,
            "last": str(last) if last not in (None, "") else None,
            "last_received_at": str(last_received_at) if last_received_at not in (None, "") else None,
        }
        replaced = False
        for index, prior in enumerate(segments):
            if str((prior or {}).get("path") or "") == relative:
                segments[index] = segment
                replaced = True
                break
        if not replaced:
            segments.append(segment)
        series["file_segments"] = sorted(segments, key=lambda row: (str(row.get("last") or ""), str(row.get("path") or "")))
        series["first"] = self._safe_min(series.get("first"), first)
        series["last"] = self._safe_max(series.get("last"), last)
        series["last_received_at"] = self._safe_max(series.get("last_received_at"), last_received_at)

    def rebuild_catalog(self) -> dict[str, Any]:
        """Build an exact rebuildable routing + file-window catalogue.

        This is the only intentionally broad Parquet scan. Candidate 13 records
        per-file first/last timestamps so interactive reads can open only files
        that can satisfy the requested window. Parquet remains immutable cold
        history; the catalogue is a rebuildable metadata projection.
        """
        files = sorted(self.root.glob("timeframe=*/year=*/*.parquet")) if self.root.exists() else []
        with self._catalog_lock:
            had_usable_catalog = self._catalog_state == "READY" and bool(self._catalog.get("series"))
            self._catalog_rebuild_state = "BUILDING"
            if not had_usable_catalog:
                self._catalog_state = "BUILDING"
            self._catalog_error = None
            self._catalog_progress = {"processed": 0, "total": len(files)}
        catalog = self._empty_catalog()
        if files:
            duckdb = importlib.import_module("duckdb")
            pattern = str((self.root / "timeframe=*" / "year=*" / "*.parquet").resolve()).replace("\\", "/")
            conn = duckdb.connect(database=":memory:")
            try:
                # Per-file windows are deliberately built offline. The runtime
                # then needs no filesystem discovery and no year-wide query.
                cursor = conn.execute(
                    """SELECT instrument_key, interval, filename,
                              count(DISTINCT ts)::BIGINT AS row_count,
                              min(ts) AS first_ts, max(ts) AS last_ts,
                              max(received_at) AS last_received_at
                         FROM read_parquet(?, union_by_name=true, filename=true)
                        WHERE instrument_key IS NOT NULL
                          AND interval IS NOT NULL
                          AND ts IS NOT NULL
                        GROUP BY instrument_key, interval, filename
                        ORDER BY instrument_key, interval, last_ts""",
                    [pattern],
                )
                for instrument_key, interval, filename, row_count, first_ts, last_ts, received_at in cursor.fetchall():
                    self._merge_catalog_row(
                        catalog, instrument_key=str(instrument_key), interval=interval,
                        file_path=Path(str(filename)), count=int(row_count or 0),
                        first=first_ts, last=last_ts, last_received_at=received_at,
                    )
                self._catalog_progress = {"processed": len(files), "total": len(files)}
            except Exception as exc:
                catalog["unreadable_files"] = [{"path": pattern, "error": str(exc)[:500]}]
                raise
            finally:
                conn.close()
        count, latest = self._root_signature()
        catalog["root_file_count"] = count
        catalog["root_latest_mtime_ns"] = latest
        catalog["generated_at"] = self._now()
        self._persist_catalog_payload(catalog)
        with self._catalog_lock:
            self._catalog = catalog
            self._catalog_state = "READY"
            self._catalog_rebuild_state = "READY"
            self._catalog_serving_generation = str(catalog.get("generated_at") or "")
            self._catalog_progress = {"processed": len(files), "total": len(files)}
            self._query_cache.clear()
        return self.catalog_status()

    def _catalog_worker(self) -> None:
        try:
            self.rebuild_catalog()
        except Exception as exc:
            with self._catalog_lock:
                self._catalog_rebuild_state = "FAILED"
                if not (self._catalog_state == "READY" and bool(self._catalog.get("series"))):
                    self._catalog_state = "FAILED"
                self._catalog_error = str(exc)

    def start_catalog_rebuild(self) -> bool:
        with self._catalog_lock:
            if self._catalog_thread is not None and self._catalog_thread.is_alive():
                return False
            self._catalog_thread = threading.Thread(
                target=self._catalog_worker,
                name="laddu-candle-file-catalog",
                daemon=True,
            )
            self._catalog_thread.start()
            return True

    def catalog_status(self) -> dict[str, Any]:
        with self._catalog_lock:
            unreadable = self._catalog.get("unreadable_files") or []
            return {
                "state": self._catalog_state,
                "usable": self._catalog_state == "READY" and isinstance(self._catalog.get("series"), dict),
                "serving_state": "READY" if self._catalog_state == "READY" else self._catalog_state,
                "rebuild_state": self._catalog_rebuild_state,
                "serving_generation": self._catalog_serving_generation,
                "catalog_version": self.CATALOG_VERSION,
                "path": str(self.catalog_path),
                "series": len(self._catalog.get("series") or {}),
                "files": int(self._catalog.get("root_file_count") or 0),
                "unreadable_files": len(unreadable),
                "generated_at": self._catalog.get("generated_at"),
                "processed": int(self._catalog_progress.get("processed") or 0),
                "total": int(self._catalog_progress.get("total") or 0),
                "error": self._catalog_error,
            }

    @classmethod
    def _normalise(
        cls,
        instrument_key: str,
        interval: Any,
        raw: Mapping[str, Any],
        *,
        source: str,
        received_at: str,
    ) -> dict[str, Any] | None:
        key = str(instrument_key or "").strip()
        norm = canonical_interval(interval)
        row = dict(raw or {})
        provider_ts = row.get("timestamp") or row.get("time") or row.get("date") or row.get("ts")
        ts = canonical_timestamp(provider_ts, norm)
        if not key or not norm or ts is None:
            return None
        try:
            values = {name: float(row.get(name)) for name in ("open", "high", "low", "close")}
        except (TypeError, ValueError):
            return None
        if any(value != value or value in (float("inf"), float("-inf")) for value in values.values()):
            return None

        def finite_or_none(value: Any) -> float | None:
            try:
                number = float(value)
                return number if number == number and number not in (float("inf"), float("-inf")) else None
            except (TypeError, ValueError):
                return None

        return {
            "instrument_key": key,
            "interval": norm,
            "ts": ts,
            **values,
            "volume": finite_or_none(row.get("volume")),
            "oi": finite_or_none(row.get("oi")),
            "source": str(source or "upstox_historical"),
            "provider_ts": str(provider_ts or ""),
            "received_at": received_at,
            "lake_ingested_at": received_at,
            "schema_version": cls.SERVICE_VERSION,
        }

    @staticmethod
    def _stable_digest(rows: Iterable[Mapping[str, Any]]) -> str:
        material = []
        for raw in rows:
            row = dict(raw)
            row.pop("received_at", None)
            row.pop("lake_ingested_at", None)
            material.append(row)
        text = "\n".join(json.dumps(row, sort_keys=True, separators=(",", ":"), default=str) for row in material)
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    @staticmethod
    def _jsonl(rows: Iterable[Mapping[str, Any]]) -> str:
        return "\n".join(json.dumps(dict(row), sort_keys=True, separators=(",", ":"), default=str) for row in rows) + "\n"

    def _write_partition(self, interval: str, year: str, rows: list[dict[str, Any]]) -> Path | None:
        material = sorted(rows, key=lambda row: (row["instrument_key"], row["ts"]))
        digest = self._stable_digest(material)
        target = self.root / f"timeframe={interval}" / f"year={year}" / f"part-{digest}.parquet"
        if target.exists():
            return None
        target.parent.mkdir(parents=True, exist_ok=True)
        duckdb = importlib.import_module("duckdb")
        work_root = self.layout.runtime_dir / "candle-lake-work"
        work_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="laddu_candles_", dir=work_root) as temp_dir:
            temp = Path(temp_dir)
            source_file = temp / "rows.jsonl"
            staging = temp / "part.parquet"
            source_file.write_text(self._jsonl(material), encoding="utf-8")
            source = str(source_file.resolve()).replace("\\", "/").replace("'", "''")
            destination = str(staging.resolve()).replace("\\", "/").replace("'", "''")
            conn = duckdb.connect(database=":memory:")
            try:
                conn.execute(
                    f"COPY (SELECT * FROM read_json_auto('{source}', format='newline_delimited')) "
                    f"TO '{destination}' (FORMAT PARQUET, COMPRESSION ZSTD)"
                )
            finally:
                conn.close()
            os.replace(staging, target)
        return target

    def save_candles(
        self,
        instrument_key: str,
        interval: Any,
        candles: List[Dict[str, Any]],
        source: str = "upstox_historical",
    ) -> int:
        if not instrument_key or not candles:
            return 0
        received_at = self._now()
        normalised = [
            self._normalise(instrument_key, interval, row, source=source, received_at=received_at)
            for row in candles
        ]
        dedup: dict[tuple[str, str, str], dict[str, Any]] = {}
        for row in normalised:
            if row:
                dedup[(row["instrument_key"], row["interval"], row["ts"])] = row
        material = list(dedup.values())
        if not material:
            return 0
        partitions: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for row in material:
            year = str(row["ts"])[:4]
            if len(year) != 4 or not year.isdigit():
                year = "unknown"
            partitions[(row["interval"], year)].append(row)
        with self._lock:
            written: list[tuple[Path, list[dict[str, Any]]]] = []
            for (norm, year), rows in partitions.items():
                target = self._write_partition(norm, year, rows)
                if target is not None:
                    self._parts_written += 1
                    written.append((target, rows))
            for row in material:
                series = self._memory[(row["instrument_key"], row["interval"])]
                series[row["ts"]] = dict(row)
                while len(series) > self._max_memory_rows:
                    series.pop(min(series))
            self._rows_written += len(material)
        if written:
            with self._catalog_lock:
                for target, rows in written:
                    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
                    for row in rows:
                        grouped[(row["instrument_key"], row["interval"])].append(row)
                    for (key, norm), group in grouped.items():
                        self._merge_catalog_row(
                            self._catalog,
                            instrument_key=key,
                            interval=norm,
                            file_path=target,
                            count=len(group),
                            first=min(row["ts"] for row in group),
                            last=max(row["ts"] for row in group),
                            last_received_at=max(row["received_at"] for row in group),
                        )
                self._catalog["root_file_count"] = int(self._catalog.get("root_file_count") or 0) + len(written)
                latest = int(self._catalog.get("root_latest_mtime_ns") or 0)
                for target, _rows in written:
                    try:
                        latest = max(latest, int(target.stat().st_mtime_ns))
                    except OSError:
                        pass
                self._catalog["root_latest_mtime_ns"] = latest
                self._catalog_state = "READY"
                for _target, rows in written:
                    for row in rows:
                        self._invalidate_query_cache(row.get("instrument_key"), row.get("interval"))
            # The immutable Parquet part and in-memory catalogue are already
            # authoritative. Persist the rebuildable routing manifest outside
            # _catalog_lock so foreground coverage reads cannot be blocked by
            # multi-megabyte JSON serialisation/fsync.
            self._persist_catalog_snapshot()
        return len(material)

    def _catalog_files(self, instrument_key: str, interval: str) -> list[Path]:
        sid = self._series_id(instrument_key, interval)
        with self._catalog_lock:
            entry = dict((self._catalog.get("series") or {}).get(sid) or {})
        files = []
        for raw in entry.get("files") or []:
            path = Path(raw)
            if not path.is_absolute():
                path = self.root / path
            if path.exists():
                files.append(path)
        return files

    def _catalog_segments(self, instrument_key: str, interval: str) -> list[dict[str, Any]]:
        sid = self._series_id(str(instrument_key or "").strip(), canonical_interval(interval))
        with self._catalog_lock:
            entry = dict((self._catalog.get("series") or {}).get(sid) or {})
        out: list[dict[str, Any]] = []
        for raw in entry.get("file_segments") or []:
            row = dict(raw or {})
            path = Path(str(row.get("path") or ""))
            if not path.is_absolute():
                path = self.root / path
            if path.exists():
                row["_path"] = path
                out.append(row)
        return out

    def _window_catalog_files(
        self, instrument_key: str, interval: str, limit: int, *,
        since: Any = None, before: Any = None, extra_segments: int = 2,
    ) -> list[Path]:
        """Choose only catalogue files capable of intersecting a read window.

        The index is metadata-only and built offline/incrementally. No Parquet
        file is opened here. A small overlap cushion handles duplicate/corrected
        bars across immutable parts without making the request year-wide.
        """
        norm = canonical_interval(interval)
        segments = self._catalog_segments(instrument_key, norm)
        if not segments:
            return self._recent_catalog_files(self._catalog_files(instrument_key, norm), norm, limit)
        since_ts = canonical_timestamp(since, norm) if since not in (None, "") else None
        before_ts = canonical_timestamp(before, norm) if before not in (None, "") else None
        eligible: list[dict[str, Any]] = []
        for row in segments:
            first = canonical_timestamp(row.get("first"), norm)
            last = canonical_timestamp(row.get("last"), norm)
            if since_ts and last and last < since_ts:
                continue
            if before_ts and first and first >= before_ts:
                continue
            eligible.append(row)
        eligible.sort(key=lambda row: (str(row.get("last") or ""), str(row.get("path") or "")), reverse=True)
        selected: list[Path] = []
        rows_budget = 0
        target = max(1, int(limit))
        for row in eligible:
            path = row.get("_path")
            if isinstance(path, Path) and path not in selected:
                selected.append(path)
                rows_budget += max(1, int(row.get("count") or 0))
            if rows_budget >= target and len(selected) >= min(len(eligible), 1 + max(0, int(extra_segments))):
                break
        return selected

    @staticmethod
    def _recent_catalog_files(files: list[Path], interval: str, limit: int, *, extra_years: int = 0) -> list[Path]:
        """Choose newest retained year partitions sufficient for a bounded read.

        The catalogue is still the authority. If the initial recent window is
        sparse, callers expand deterministically until the requested rows or all
        retained files are covered.
        """
        rows_per_year = {
            "1minute": 100000, "3minute": 34000, "5minute": 20000, "15minute": 7000,
            "30minute": 3500, "60minute": 1800, "hour": 1800, "4hour": 450,
            "day": 260, "week": 52, "month": 12,
        }.get(str(interval or "day").lower(), 260)
        years: dict[int, list[Path]] = {}
        undated: list[Path] = []
        for path in files:
            match = re.search(r"(?:^|[\/])year=(\d{4})(?:[\/]|$)", str(path))
            if match:
                years.setdefault(int(match.group(1)), []).append(path)
            else:
                undated.append(path)
        if not years:
            return list(files)
        needed = max(1, (max(1, int(limit)) + rows_per_year - 1) // rows_per_year) + max(0, int(extra_years))
        selected: list[Path] = []
        for year in sorted(years, reverse=True)[:needed]:
            selected.extend(years[year])
        selected.extend(undated)
        return selected

    @staticmethod
    def _duckdb_path_list(files: list[Path]) -> str:
        escaped = ["'" + str(path.resolve()).replace("\\", "/").replace("'", "''") + "'" for path in files]
        return "[" + ",".join(escaped) + "]"

    def _duckdb_connection(self):
        conn = getattr(self._query_local, "conn", None)
        if conn is None:
            duckdb = importlib.import_module("duckdb")
            conn = duckdb.connect(database=":memory:")
            try:
                conn.execute("PRAGMA threads=2")
            except Exception:
                pass
            self._query_local.conn = conn
        return conn

    def _series_query_lock(self, cache_key: tuple[str, str, int]) -> threading.Lock:
        with self._series_locks_guard:
            lock = self._series_locks.get(cache_key)
            if lock is None:
                lock = threading.Lock()
                self._series_locks[cache_key] = lock
            return lock

    def _multi_query_lock(self, key: str, intervals: Iterable[str], limit: int) -> threading.Lock:
        lock_key = (str(key), tuple(sorted(set(intervals))), int(limit))
        with self._series_locks_guard:
            lock = self._multi_query_locks.get(lock_key)
            if lock is None:
                lock = threading.Lock()
                self._multi_query_locks[lock_key] = lock
            return lock

    def _invalidate_query_cache(self, instrument_key: Any, interval: Any = None) -> None:
        key = str(instrument_key or "").strip()
        norm = canonical_interval(interval) if interval is not None else None
        with self._catalog_lock:
            for cache_key in list(self._query_cache):
                if cache_key[0] == key and (norm is None or cache_key[1] == norm):
                    self._query_cache.pop(cache_key, None)

    def _cache_get(self, cache_key: tuple[str, str, int]) -> list[dict[str, Any]] | None:
        with self._catalog_lock:
            cached = self._query_cache.get(cache_key)
            if cached is None or time.monotonic() - cached[0] > self.QUERY_CACHE_TTL_SEC:
                if cached is not None:
                    self._query_cache.pop(cache_key, None)
                self._query_cache_misses += 1
                return None
            self._query_cache.move_to_end(cache_key)
            self._query_cache_hits += 1
            return [dict(row) for row in cached[1]]

    def _cache_put(self, cache_key: tuple[str, str, int], rows: list[dict[str, Any]]) -> None:
        with self._catalog_lock:
            self._query_cache[cache_key] = (time.monotonic(), [dict(row) for row in rows])
            self._query_cache.move_to_end(cache_key)
            while len(self._query_cache) > self.QUERY_CACHE_CAPACITY:
                self._query_cache.popitem(last=False)

    def _read_with_connection(self, conn, instrument_key: str, interval: str, limit: int, *, files: list[Path] | None = None) -> list[dict[str, Any]]:
        files = list(files) if files is not None else self._catalog_files(instrument_key, interval)
        if not instrument_key or not files:
            return []
        sources = self._duckdb_path_list(files)
        cursor = conn.execute(
            f"""SELECT ts AS timestamp,open,high,low,close,volume,oi,source,
                        provider_ts AS provider_timestamp,received_at,
                        'parquet_direct_authority' AS storage_plane
                   FROM (
                     SELECT *,row_number() OVER (
                       PARTITION BY instrument_key,interval,ts
                       ORDER BY received_at DESC,lake_ingested_at DESC
                     ) AS rn
                     FROM read_parquet({sources},hive_partitioning=true,union_by_name=true)
                     WHERE instrument_key=? AND interval=?
                   ) WHERE rn=1 ORDER BY ts DESC LIMIT ?""",
            [instrument_key, interval, max(1, int(limit))],
        )
        names = [item[0] for item in cursor.description]
        rows = [dict(zip(names, row)) for row in cursor.fetchall()]
        rows.reverse()
        return rows

    def _query(self, instrument_key: str, interval: Any, limit: int) -> list[dict[str, Any]]:
        key = str(instrument_key or "").strip()
        norm = canonical_interval(interval)
        cap = max(1, int(limit))
        files = self._catalog_files(key, norm)
        # Exact-file query delegates to _read_with_connection, whose SQL uses
        # read_parquet({sources}) over this catalogue-derived list only.
        if not key or not files:
            return []
        cache_key = (key, norm, cap)
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached
        # Collapse concurrent chart/Stock Intelligence/MTF reads for the same
        # series. The second caller reuses the result instead of opening the
        # same Parquet files again.
        with self._series_query_lock(cache_key):
            cached = self._cache_get(cache_key)
            if cached is not None:
                return cached
            started = time.perf_counter()
            recent = self._window_catalog_files(key, norm, cap)
            rows = self._read_with_connection(self._duckdb_connection(), key, norm, cap, files=recent)
            extra_years = 0
            while len(rows) < cap and len(recent) < len(files):
                extra_years += 1
                expanded = self._recent_catalog_files(files, norm, cap, extra_years=extra_years)
                if len(expanded) <= len(recent):
                    break
                recent = expanded
                rows = self._read_with_connection(self._duckdb_connection(), key, norm, cap, files=recent)
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            with self._catalog_lock:
                self._query_count += 1
                self._query_total_ms += elapsed_ms
            self._cache_put(cache_key, rows)
            return [dict(row) for row in rows]

    def get_candles_window(
        self, instrument_key: str, interval: Any, *, since: Any = None,
        before: Any = None, limit: int = 2000,
    ) -> list[dict[str, Any]]:
        """Read a bounded cold-history window using only indexed file segments."""
        key = str(instrument_key or "").strip()
        norm = canonical_interval(interval)
        cap = max(1, int(limit))
        files = self._window_catalog_files(key, norm, cap, since=since, before=before)
        if not key or not files:
            return []
        # `since` is a file-pruning hint, not a semantic truncation. This
        # preserves the established tail-depth contract (e.g. indicators may
        # need more than the requested calendar days) while avoiding unrelated
        # immutable parts. `before` remains a hard pagination boundary.
        rows = self._read_with_connection(self._duckdb_connection(), key, norm, cap, files=files)
        # The requested calendar window is only a pruning hint. If indicator or
        # chart semantics require a deeper established tail, expand by indexed
        # file-count metadata rather than falling back to a timeframe-wide glob.
        if len(rows) < cap:
            expanded = self._window_catalog_files(key, norm, cap, since=None, before=before)
            if set(expanded) != set(files):
                files = expanded
                rows = self._read_with_connection(self._duckdb_connection(), key, norm, cap, files=files)
        if before not in (None, ""):
            before_ts = canonical_timestamp(before, norm)
            rows = [row for row in rows if canonical_timestamp(row.get("timestamp") or row.get("ts"), norm) and canonical_timestamp(row.get("timestamp") or row.get("ts"), norm) < before_ts]
        return rows[-cap:]

    def get_candles_many(
        self,
        instrument_key: str,
        intervals: Iterable[Any],
        limit: int = 2000,
        *,
        expand_sparse: bool = True,
    ) -> dict[str, list[dict[str, Any]]]:
        """Read exact multi-timeframe series in one bounded DuckDB query.

        ``expand_sparse=False`` is the interactive technical-materialization
        contract. It reads only the catalogue-selected recent window and
        returns truthful partial coverage when an illiquid/new listing cannot
        satisfy the requested row cap there. Research/backfill keeps the
        default deterministic expansion and therefore retains full historical
        depth; no mathematical family or timeframe is removed.

        The catalogue supplies only files known to contain this instrument and
        the requested intervals. One query de-duplicates and caps every frame,
        avoiding the eight independent DuckDB planning/open cycles that made
        composite MTF slow even after the catalogue became warm.
        """
        key = str(instrument_key or "").strip()
        cap = max(1, int(limit))
        norms = list(dict.fromkeys(canonical_interval(value) for value in intervals or [] if value))
        out: dict[str, list[dict[str, Any]]] = {}
        if not key or not norms:
            return out

        missing: list[str] = []
        for norm in norms:
            cached = self._cache_get((key, norm, cap))
            if cached is None:
                missing.append(norm)
            else:
                out[norm] = cached
        if not missing:
            return out

        with self._multi_query_lock(key, missing, cap):
            # A concurrent MTF request may have populated all or part of the
            # cache while this caller waited.
            still_missing: list[str] = []
            for norm in missing:
                cached = self._cache_get((key, norm, cap))
                if cached is None:
                    still_missing.append(norm)
                else:
                    out[norm] = cached
            if not still_missing:
                return out

            all_files: list[Path] = []
            for norm in still_missing:
                # C13 uses per-file timestamp/count metadata for MTF too; a
                # selected-stock read must not reopen every immutable part in
                # the newest year for each timeframe.
                bounded = self._window_catalog_files(key, norm, cap)
                for path in bounded:
                    if path not in all_files:
                        all_files.append(path)
            if not all_files:
                for norm in still_missing:
                    self._cache_put((key, norm, cap), [])
                    out[norm] = []
                return out

            sources = self._duckdb_path_list(all_files)
            placeholders = ",".join("?" for _ in still_missing)
            sql = f"""WITH deduplicated AS (
                         SELECT instrument_key,interval,ts,open,high,low,close,volume,oi,source,
                                provider_ts,received_at,
                                row_number() OVER (
                                  PARTITION BY instrument_key,interval,ts
                                  ORDER BY received_at DESC,lake_ingested_at DESC
                                ) AS version_rank
                           FROM read_parquet({sources},hive_partitioning=true,union_by_name=true)
                          WHERE instrument_key=? AND interval IN ({placeholders})
                       ), capped AS (
                         SELECT *,row_number() OVER (PARTITION BY interval ORDER BY ts DESC) AS frame_rank
                           FROM deduplicated WHERE version_rank=1
                       )
                       SELECT interval,ts AS timestamp,open,high,low,close,volume,oi,source,
                              provider_ts AS provider_timestamp,received_at,
                              'parquet_direct_authority' AS storage_plane
                         FROM capped WHERE frame_rank<=?
                        ORDER BY interval,ts"""
            started = time.perf_counter()
            grouped: dict[str, list[dict[str, Any]]] = {norm: [] for norm in still_missing}
            try:
                cursor = self._duckdb_connection().execute(sql, [key, *still_missing, cap])
                names = [item[0] for item in cursor.description]
                for raw in cursor.fetchall():
                    row = dict(zip(names, raw))
                    norm = canonical_interval(row.pop("interval", ""))
                    if norm in grouped:
                        grouped[norm].append(row)
            except Exception as exc:
                with self._catalog_lock:
                    self._catalog_error = str(exc)
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            with self._catalog_lock:
                self._query_count += 1
                self._query_total_ms += elapsed_ms
            for norm in still_missing:
                rows = grouped.get(norm) or []
                window_files = self._window_catalog_files(key, norm, cap)
                all_series_files = self._catalog_files(key, norm)
                incomplete_window = len(rows) < cap and len(window_files) < len(all_series_files)
                if incomplete_window and expand_sparse:
                    # Deep/research callers preserve deterministic expansion.
                    rows = self._query(key, norm, cap)
                elif not incomplete_window:
                    # Only a complete/full-series result may populate the
                    # established cache key. A bounded partial result must not
                    # masquerade as final history for a later research caller.
                    self._cache_put((key, norm, cap), rows)
                out[norm] = [dict(row) for row in rows]
        return out

    def get_candles(self, instrument_key: str, interval: Any, limit: int = 2000) -> list[dict[str, Any]]:
        key = str(instrument_key or "").strip()
        norm = canonical_interval(interval)
        try:
            durable = self._query(key, norm, limit)
        except Exception as exc:
            with self._catalog_lock:
                self._catalog_error = str(exc)
            durable = []
        with self._lock:
            memory = [dict(row) for _, row in sorted(self._memory.get((key, norm), {}).items())]
        merged: dict[str, dict[str, Any]] = {}
        for row in durable:
            ts = canonical_timestamp(row.get("timestamp") or row.get("ts"), norm)
            if not ts:
                continue
            out = dict(row)
            out["timestamp"] = ts
            merged[ts] = out
        for row in memory:
            ts = canonical_timestamp(row.get("ts") or row.get("timestamp"), norm)
            if ts:
                out = dict(row)
                out["timestamp"] = ts
                out["provider_timestamp"] = out.pop("provider_ts", "")
                out["storage_plane"] = "parquet_pending_memory"
                merged[ts] = out
        return [merged[ts] for ts in sorted(merged)][-max(1, int(limit)):]

    def get_candles_before(self, instrument_key: str, interval: Any, before: Any, limit: int = 2000) -> list[dict[str, Any]]:
        """Return a bounded local Parquet page strictly older than ``before``.

        The query stays catalogue-scoped and de-duplicates revisions exactly as
        the normal candle read. Pending in-memory rows are merged only when they
        are also older than the requested boundary.
        """
        key = str(instrument_key or "").strip()
        norm = canonical_interval(interval)
        before_ts = canonical_timestamp(before, norm)
        cap = max(1, int(limit))
        files = self._window_catalog_files(key, norm, cap, before=before_ts) if key and before_ts else []
        durable: list[dict[str, Any]] = []
        if key and before_ts and files:
            sources = self._duckdb_path_list(files)
            try:
                cursor = self._duckdb_connection().execute(
                    f"""SELECT ts AS timestamp,open,high,low,close,volume,oi,source,
                                provider_ts AS provider_timestamp,received_at,
                                'parquet_direct_authority' AS storage_plane
                           FROM (
                             SELECT *,row_number() OVER (
                               PARTITION BY instrument_key,interval,ts
                               ORDER BY received_at DESC,lake_ingested_at DESC
                             ) AS rn
                             FROM read_parquet({sources},hive_partitioning=true,union_by_name=true)
                             WHERE instrument_key=? AND interval=? AND ts<?
                           ) WHERE rn=1 ORDER BY ts DESC LIMIT ?""",
                    [key, norm, before_ts, cap],
                )
                names = [item[0] for item in cursor.description]
                durable = [dict(zip(names, row)) for row in cursor.fetchall()]
                durable.reverse()
                with self._catalog_lock:
                    self._query_count += 1
            except Exception as exc:
                with self._catalog_lock:
                    self._catalog_error = str(exc)
                durable = []
        with self._lock:
            memory = [dict(row) for _, row in sorted(self._memory.get((key, norm), {}).items())]
        merged: dict[str, dict[str, Any]] = {}
        for row in durable:
            ts = canonical_timestamp(row.get("timestamp") or row.get("ts"), norm)
            if ts and before_ts and ts < before_ts:
                out = dict(row); out["timestamp"] = ts; merged[ts] = out
        for row in memory:
            ts = canonical_timestamp(row.get("ts") or row.get("timestamp"), norm)
            if ts and before_ts and ts < before_ts:
                out = dict(row); out["timestamp"] = ts; out["provider_timestamp"] = out.pop("provider_ts", ""); out["storage_plane"] = "parquet_pending_memory"; merged[ts] = out
        return [merged[ts] for ts in sorted(merged)][-cap:]

    def candle_coverage(self, instrument_key: str, interval: Any) -> dict[str, Any]:
        key = str(instrument_key or "").strip()
        norm = canonical_interval(interval)
        sid = self._series_id(key, norm)
        with self._catalog_lock:
            entry = dict((self._catalog.get("series") or {}).get(sid) or {})
            catalog_state = self._catalog_state
        with self._lock:
            memory = list(self._memory.get((key, norm), {}).values())
        memory_first = min((str(row.get("ts") or "") for row in memory if row.get("ts")), default=None)
        memory_last = max((str(row.get("ts") or "") for row in memory if row.get("ts")), default=None)
        first = self._safe_min(entry.get("first"), memory_first)
        last = self._safe_max(entry.get("last"), memory_last)
        count = max(int(entry.get("count") or 0), len(memory))
        return {
            "count": count,
            "first": first,
            "last": last,
            "last_received_at": self._safe_max(
                entry.get("last_received_at"),
                max((str(row.get("received_at") or "") for row in memory), default=None),
            ),
            "source": "direct_parquet_catalog+memory",
            "catalog_state": catalog_state,
            "file_count": len(entry.get("files") or []),
            "indexed": bool(entry),
        }

    def recent_daily_candles_many(self, instrument_keys: List[str], limit_per_key: int = 25) -> dict[str, list[dict[str, Any]]]:
        cap = max(2, min(60, int(limit_per_key)))
        return {
            str(key): self.get_candles(str(key), "1d", limit=cap)
            for key in instrument_keys or [] if str(key or "").strip()
        }

    def physical_summary(self) -> dict[str, Any]:
        """Return bounded physical persistence truth without opening Parquet.

        The catalog is the rebuildable index over immutable Parquet parts.  It
        is therefore the only suitable source for process/restart-safe row
        counts and latest timestamps.  In-memory tails may extend a series
        before a catalogue write completes, but they are reported separately
        and never masquerade as durable rows.
        """
        # Lock order matches save_candles (_lock -> _catalog_lock) so an
        # observability read can never deadlock an active durable write.
        with self._lock:
            memory_rows = sum(len(rows) for rows in self._memory.values())
            runtime_writes = int(self._rows_written or 0)
            parts_written = int(self._parts_written or 0)
            with self._catalog_lock:
                catalog = dict(self._catalog or {})
                series = [dict(row or {}) for row in (catalog.get("series") or {}).values()]
                state = str(self._catalog_state or "MISSING").upper()
                generated_at = catalog.get("generated_at")
                unreadable = list(catalog.get("unreadable_files") or [])
        durable_rows = sum(max(0, int(row.get("count") or 0)) for row in series)
        durable_latest = max((str(row.get("last") or "") for row in series if row.get("last")), default=None)
        durable_first = min((str(row.get("first") or "") for row in series if row.get("first")), default=None)
        received_latest = max((str(row.get("last_received_at") or "") for row in series if row.get("last_received_at")), default=None)
        return {
            "service_version": self.SERVICE_VERSION,
            "state": "READY" if state == "READY" and not unreadable else ("DEGRADED" if series else state),
            "authority": "PARQUET_CATALOG",
            "catalog_state": state,
            "catalog_generated_at": generated_at,
            "durable_rows": durable_rows,
            "durable_series": len(series),
            "durable_first": durable_first,
            "durable_latest": durable_latest,
            "last_received_at": received_latest,
            "root_file_count": int(catalog.get("root_file_count") or 0),
            "unreadable_files": len(unreadable),
            "memory_rows": memory_rows,
            "runtime_rows_written": runtime_writes,
            "runtime_parts_written": parts_written,
            "restart_safe": bool(state == "READY" and durable_rows > 0 and not unreadable),
        }

    def status(self) -> dict[str, Any]:
        physical = self.physical_summary()
        with self._lock:
            return {
                "service_version": self.SERVICE_VERSION,
                "state": "ready",
                "root": str(self.root),
                "parts_written": self._parts_written,
                "rows_written": self._rows_written,
                "memory_series": len(self._memory),
                "physical": physical,
                "query_cache": {
                    "entries": len(self._query_cache),
                    "hits": self._query_cache_hits,
                    "misses": self._query_cache_misses,
                    "queries": self._query_count,
                    "average_query_ms": round(self._query_total_ms / max(1, self._query_count), 3),
                    "ttl_sec": self.QUERY_CACHE_TTL_SEC,
                },
                "catalog": self.catalog_status(),
            }
