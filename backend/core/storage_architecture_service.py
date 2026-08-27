"""Observable status for Project Laddu's separated storage authorities.

The status endpoint must report the runtime owners already wired into the
running application.  It must not instantiate a legacy SQLite runtime store
merely to produce health output, because that makes a compatibility/recovery
file look like the production canonical bar authority.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Dict

from config import DATA_DIR, DB_PATH
from core.storage_layout import LAYOUT_VERSION, StorageLayout


class StorageArchitectureService:
    def __init__(
        self,
        data_dir: Path = DATA_DIR,
        *,
        runtime_market_state: Any | None = None,
        production_data_plane: Any | None = None,
    ):
        self.layout = StorageLayout.from_data_dir(Path(data_dir))
        self.runtime_market_state = runtime_market_state
        self.production_data_plane = production_data_plane
        self._cache_lock = threading.RLock()
        self._cache: Dict[str, Any] | None = None
        self._cache_at = 0.0
        self._refresh_thread: threading.Thread | None = None

    @staticmethod
    def _file(path: Path, **metadata: Any) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "path": str(path),
            "exists": path.exists(),
            "bytes": path.stat().st_size if path.exists() and path.is_file() else 0,
        }
        result.update(metadata)
        return result

    def _runtime_health(self) -> Dict[str, Any]:
        runtime = self.runtime_market_state
        if runtime is None:
            return {
                "ok": False,
                "state": "UNAVAILABLE",
                "storage_engine": "none",
                "production_authority": False,
                "error": "running runtime market-state owner was not supplied",
                "configured_intervals": [],
            }
        health_fn = getattr(runtime, "canonical_bar_health", None)
        if not callable(health_fn):
            return {
                "ok": False,
                "state": "UNAVAILABLE",
                "storage_engine": type(runtime).__name__,
                "production_authority": False,
                "error": "runtime owner does not implement canonical_bar_health",
                "configured_intervals": [],
            }
        try:
            health = dict(health_fn() or {})
        except Exception as exc:
            return {
                "ok": False,
                "state": "ERROR",
                "storage_engine": type(runtime).__name__,
                "production_authority": False,
                "error": str(exc),
                "configured_intervals": [],
            }
        storage_engine = str(health.get("storage_engine") or type(runtime).__name__)
        health["owner_class"] = type(runtime).__name__
        health["production_authority"] = storage_engine == "in_process_memory"
        return health

    def _data_plane_status(self) -> Dict[str, Any]:
        plane = self.production_data_plane
        status_fn = getattr(plane, "status", None)
        if not callable(status_fn):
            return {"ok": False, "state": "UNAVAILABLE", "production_ready": False}
        try:
            return dict(status_fn(probe=False) or {})
        except TypeError:
            try:
                return dict(status_fn() or {})
            except Exception as exc:
                return {"ok": False, "state": "ERROR", "production_ready": False, "error": str(exc)}
        except Exception as exc:
            return {"ok": False, "state": "ERROR", "production_ready": False, "error": str(exc)}

    @staticmethod
    def _bounded_directory_stats(directory: Path, suffix: str, *, limit: int = 10000) -> Dict[str, Any]:
        """Return shallow, bounded metadata without recursive walks or file opens.

        The customer/verifier status path must stay fast even when the analytical
        lake contains millions of rows or many Parquet partitions. Authoritative
        lake totals come from the atomic manifest; these counts are only for the
        small operational scratch/outbox directories.
        """
        count = 0
        total_bytes = 0
        truncated = False
        try:
            for entry in directory.iterdir():
                if not entry.is_file() or entry.suffix.lower() != suffix.lower():
                    continue
                count += 1
                try:
                    total_bytes += int(entry.stat().st_size)
                except OSError:
                    pass
                if count >= max(1, int(limit)):
                    truncated = True
                    break
        except OSError:
            pass
        return {"count": count, "bytes": total_bytes, "truncated": truncated}

    @staticmethod
    def _manifest_lake_metrics(manifest: Dict[str, Any]) -> Dict[str, Any]:
        """Read bounded lake metrics from the already-produced atomic manifest."""
        tables = manifest.get("tables") if isinstance(manifest, dict) else {}
        tables = tables if isinstance(tables, dict) else {}
        explicit_files = manifest.get("curated_parquet_files") if isinstance(manifest, dict) else None
        explicit_bytes = manifest.get("curated_bytes") if isinstance(manifest, dict) else None
        row_total = 0
        file_count = 0
        byte_total = 0
        for table in tables.values():
            if not isinstance(table, dict):
                continue
            row_total += int(table.get("last_rows") or table.get("rows") or 0)
            file_count += int(table.get("file_count") or table.get("files") or 0)
            byte_total += int(table.get("bytes") or 0)
        if explicit_files is not None:
            file_count = int(explicit_files or 0)
        if explicit_bytes is not None:
            byte_total = int(explicit_bytes or 0)
        return {
            "curated_parquet_files": file_count,
            "curated_bytes": byte_total,
            "curated_rows": row_total,
            "source": "atomic_market_lake_manifest",
        }

    def _compute_status(self) -> Dict[str, Any]:
        self.layout.ensure()
        manifest_path = self.layout.manifests_dir / "market-lake.json"
        try:
            lake_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            lake_manifest = {"state": "NOT_SYNCED"}
        if not isinstance(lake_manifest, dict):
            lake_manifest = {"state": "INVALID_MANIFEST"}
        runtime_health = self._runtime_health()
        data_plane = self._data_plane_status()
        lake_metrics = self._manifest_lake_metrics(lake_manifest)
        curated_reader = {
            "ok": True,
            "available": self.layout.analytics_db.exists() and manifest_path.exists(),
            "reason": "manifest_and_analytics_db_present" if self.layout.analytics_db.exists() and manifest_path.exists() else "analytics_or_manifest_missing",
            "analytics_db": str(self.layout.analytics_db),
            "probe": "metadata_only_no_duckdb_open",
        }
        reconciliation = lake_manifest.get("reconciliation")
        operational_prune = lake_manifest.get("operational_prune")
        production_ready = bool(data_plane.get("production_ready")) and bool(runtime_health.get("production_authority"))
        snapshot_stats = self._bounded_directory_stats(self.layout.training_snapshots_dir, ".sqlite3")
        scratch_stats = self._bounded_directory_stats(self.layout.training_scratch_dir, ".sqlite3")
        outbox_stats = self._bounded_directory_stats(self.layout.publication_outbox_dir, ".json")
        return {
            "ok": production_ready,
            "layout_version": LAYOUT_VERSION,
            "production_authority": {
                "ready": production_ready,
                "data_plane": data_plane,
                "canonical_bar_runtime": runtime_health,
            },
            # Retained keys remain for compatibility, but their roles are explicit.
            "operational": self._file(
                Path(DB_PATH),
                role="bounded_compatibility_projection",
                production_authority=False,
            ),
            "legacy_source": self._file(
                self.layout.legacy_db,
                role="migration_and_rollback_evidence",
                active_runtime=False,
                production_authority=False,
            ),
            "runtime": {
                "authority": "in_process_memory_plus_questdb",
                "canonical_bar_plane": runtime_health,
                "compatibility_projection": self._file(
                    self.layout.runtime_db,
                    role="restart_recovery_and_bounded_compatibility_projection",
                    active_runtime_authority=False,
                ),
            },
            "analytics": {**self._file(self.layout.analytics_db), "curated_reader": curated_reader},
            "lake": {
                "path": str(self.layout.lake_dir),
                **lake_metrics,
                "manifest": lake_manifest,
                "reconciliation": reconciliation,
                "operational_prune": operational_prune,
            },
            "training": {
                "snapshot_count": snapshot_stats["count"],
                "snapshot_bytes": snapshot_stats["bytes"],
                "snapshot_scan_truncated": snapshot_stats["truncated"],
                "scratch_count": scratch_stats["count"],
                "scratch_bytes": scratch_stats["bytes"],
                "scratch_scan_truncated": scratch_stats["truncated"],
                "publication_outbox_count": outbox_stats["count"],
                "publication_outbox_scan_truncated": outbox_stats["truncated"],
                "lock_path": str(self.layout.locks_dir / "ai-training.lock"),
                "analytical_pipeline_lock_path": str(self.layout.locks_dir / "analytical-pipeline.lock"),
            },
            "policy": {
                "hot_runtime": "in-memory current-session state and in-flight market updates",
                "operational_postgresql": "orders, decisions, positions, risk and canonical lifecycle authority",
                "governance_postgresql": "model publication, promotion and immutable governance evidence",
                "questdb": "ticks and canonical market bars",
                "parquet_duckdb": "historical candles, analytical snapshots, features, labels and backtests",
                "compatibility_projection": "bounded rebuildable local search/read/restart projection only; never production authority",
                "live_risk_rule": "urgent risk evaluation must not wait for analytical or compatibility storage",
            },
        }

    def refresh_async(self) -> threading.Thread:
        """Refresh status in one bounded background worker.

        The HTTP verifier must never block behind filesystem or container
        metadata. Concurrent callers share the same worker and receive the
        last complete snapshot when one exists.
        """
        with self._cache_lock:
            if self._refresh_thread is not None and self._refresh_thread.is_alive():
                return self._refresh_thread

            def run() -> None:
                try:
                    snapshot = self._compute_status()
                except Exception as exc:  # fail closed but keep endpoint responsive
                    snapshot = {
                        "ok": False,
                        "state": "ERROR",
                        "production_authority": {"ready": False},
                        "error": str(exc),
                        "policy": {"production_authority": "POSTGRESQL_QUESTDB_IN_MEMORY"},
                    }
                with self._cache_lock:
                    self._cache = snapshot
                    self._cache_at = time.monotonic()

            self._refresh_thread = threading.Thread(
                target=run, name="storage-architecture-refresh", daemon=True
            )
            self._refresh_thread.start()
            return self._refresh_thread

    def status(self, *, max_wait_sec: float = 1.5, max_age_sec: float = 15.0) -> Dict[str, Any]:
        now = time.monotonic()
        with self._cache_lock:
            cached = dict(self._cache) if self._cache is not None else None
            fresh = cached is not None and (now - self._cache_at) <= max(0.0, max_age_sec)
        if fresh:
            cached["probe"] = "cached_bounded_metadata"
            return cached

        worker = self.refresh_async()
        worker.join(timeout=max(0.0, max_wait_sec))
        with self._cache_lock:
            cached = dict(self._cache) if self._cache is not None else None
        if cached is not None:
            cached["probe"] = "refreshed_bounded_metadata" if not worker.is_alive() else "stale_while_revalidate"
            return cached

        # First request timed out before a complete snapshot existed. Return an
        # explicit bounded state rather than timing out the verifier itself.
        runtime = self._runtime_health()
        plane = self._data_plane_status()
        ready = bool(runtime.get("production_authority")) and bool(plane.get("production_ready"))
        return {
            "ok": ready,
            "state": "WARMING",
            "probe": "bounded_fallback_refresh_in_progress",
            "production_authority": {
                "ready": ready,
                "data_plane": plane,
                "canonical_bar_runtime": runtime,
            },
            "policy": {
                "operational_postgresql": "orders, decisions, positions, risk and lifecycle authority",
                "questdb": "ticks and canonical market bars",
                "compatibility_projection": "never production authority",
            },
        }

