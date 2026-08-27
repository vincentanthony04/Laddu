"""Project Laddu storage-plane layout and safe SQLite utilities.

The live decision service, historical/analytical workloads, and model training
must not contend on one ever-growing SQLite file.  This module establishes the
v67.2 storage contract and provides migration/snapshot primitives that are safe
for existing ProgramData installations.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import time
from typing import Dict, Iterator, Optional


LAYOUT_VERSION = "storage-layout-1.1.0"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class StorageLayout:
    data_dir: Path
    legacy_db: Path
    operational_dir: Path
    operational_db: Path
    runtime_dir: Path
    runtime_db: Path
    lake_dir: Path
    raw_lake_dir: Path
    curated_lake_dir: Path
    feature_lake_dir: Path
    label_lake_dir: Path
    prediction_lake_dir: Path
    outcome_lake_dir: Path
    analytics_dir: Path
    analytics_db: Path
    manifests_dir: Path
    models_dir: Path
    snapshots_dir: Path
    training_snapshots_dir: Path
    training_scratch_dir: Path
    publication_outbox_dir: Path
    locks_dir: Path

    @classmethod
    def from_data_dir(cls, data_dir: Path) -> "StorageLayout":
        data_dir = Path(data_dir)
        operational_dir = data_dir / "operational"
        runtime_dir = data_dir / "runtime"
        lake_dir = data_dir / "lake"
        analytics_dir = data_dir / "analytics"
        manifests_dir = data_dir / "manifests"
        snapshots_dir = data_dir / "snapshots"
        return cls(
            data_dir=data_dir,
            legacy_db=data_dir / "project_laddu.sqlite3",
            operational_dir=operational_dir,
            operational_db=operational_dir / "project_laddu_ops.sqlite3",
            runtime_dir=runtime_dir,
            runtime_db=runtime_dir / "market_session.sqlite3",
            lake_dir=lake_dir,
            raw_lake_dir=lake_dir / "raw",
            curated_lake_dir=lake_dir / "curated",
            feature_lake_dir=lake_dir / "features",
            label_lake_dir=lake_dir / "labels",
            prediction_lake_dir=lake_dir / "predictions",
            outcome_lake_dir=lake_dir / "outcomes",
            analytics_dir=analytics_dir,
            analytics_db=analytics_dir / "project_laddu_quant.duckdb",
            manifests_dir=manifests_dir,
            models_dir=data_dir / "models",
            snapshots_dir=snapshots_dir,
            training_snapshots_dir=snapshots_dir / "training",
            training_scratch_dir=runtime_dir / "training_scratch",
            publication_outbox_dir=runtime_dir / "publication_outbox",
            locks_dir=runtime_dir / "locks",
        )

    def ensure(self) -> None:
        directories = (
            self.data_dir, self.operational_dir, self.runtime_dir,
            self.raw_lake_dir, self.curated_lake_dir, self.feature_lake_dir,
            self.label_lake_dir, self.prediction_lake_dir, self.outcome_lake_dir,
            self.analytics_dir, self.manifests_dir, self.models_dir,
            self.training_snapshots_dir, self.training_scratch_dir,
            self.publication_outbox_dir, self.locks_dir,
        )
        for directory in directories:
            directory.mkdir(parents=True, exist_ok=True)
        manifest = self.manifests_dir / "storage-layout.json"
        if not manifest.exists():
            atomic_write_json(manifest, {
                "layout_version": LAYOUT_VERSION,
                "created_at": _now(),
                "operational_db": str(self.operational_db),
                "runtime_db": str(self.runtime_db),
                "analytics_db": str(self.analytics_db),
                "lake_dir": str(self.lake_dir),
            })


def atomic_write_json(path: Path, payload: Dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True, indent=2, default=str)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        try:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
        except OSError:
            pass


def _sqlite_integrity(path: Path) -> bool:
    if not Path(path).exists():
        return False
    conn = sqlite3.connect(f"file:{Path(path).resolve()}?mode=ro", uri=True, timeout=10)
    try:
        row = conn.execute("PRAGMA quick_check").fetchone()
        return bool(row and str(row[0]).lower() == "ok")
    finally:
        conn.close()


def sqlite_snapshot(source: Path, destination: Path, *, timeout_seconds: float = 30.0) -> Path:
    """Create a transactionally consistent SQLite copy using the online backup API.

    The source is opened read-only. WAL writers may continue while SQLite copies
    consistent pages. The result is written to a temporary path and atomically
    renamed only after ``quick_check`` succeeds.
    """
    source, destination = Path(source), Path(destination)
    if not source.exists():
        raise FileNotFoundError(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = destination.with_name(destination.name + f".{os.getpid()}.tmp")
    if temp.exists():
        temp.unlink()
    source_conn = sqlite3.connect(
        f"file:{source.resolve()}?mode=ro", uri=True, timeout=max(5.0, timeout_seconds)
    )
    target_conn = sqlite3.connect(str(temp), timeout=max(5.0, timeout_seconds))
    try:
        try:
            source_conn.execute(f"PRAGMA busy_timeout={int(timeout_seconds * 1000)}")
            source_conn.backup(target_conn, pages=2048, sleep=0.05)
            target_conn.commit()
        finally:
            target_conn.close()
            source_conn.close()
        if not _sqlite_integrity(temp):
            raise RuntimeError("SQLite snapshot failed integrity verification")
        os.replace(temp, destination)
        return destination
    except Exception:
        remove_sqlite_family(temp)
        raise


def prepare_operational_database(layout: StorageLayout) -> Dict[str, object]:
    """Ensure the operational database exists in the separated v67.2 location.

    Existing ``data/project_laddu.sqlite3`` installations are copied with the
    SQLite online backup API.  The legacy file is deliberately retained as a
    rollback source but is never selected by the new runtime after migration.
    """
    layout.ensure()
    result: Dict[str, object] = {
        "layout_version": LAYOUT_VERSION,
        "operational_db": str(layout.operational_db),
        "legacy_db": str(layout.legacy_db),
        "migrated": False,
    }
    if layout.operational_db.exists():
        if not _sqlite_integrity(layout.operational_db):
            raise RuntimeError(f"Operational database integrity check failed: {layout.operational_db}")
        result["state"] = "READY"
        return result
    if layout.legacy_db.exists():
        sqlite_snapshot(layout.legacy_db, layout.operational_db)
        result.update({"state": "MIGRATED_FROM_LEGACY", "migrated": True, "migrated_at": _now()})
        atomic_write_json(layout.manifests_dir / "operational-db-migration.json", result)
        return result
    result["state"] = "NEW_DATABASE"
    return result


def file_sha256(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


@contextmanager
def interprocess_lock(path: Path, *, timeout_seconds: float = 1.0) -> Iterator[Path]:
    """Cross-platform exclusive file lock used to prevent duplicate trainers.

    This is a process boundary, unlike ``threading.RLock``.  The lock is held
    for the duration of the context and automatically released on process exit.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    deadline = time.monotonic() + max(0.0, float(timeout_seconds))
    acquired = False
    try:
        while not acquired:
            try:
                if os.name == "nt":
                    import msvcrt
                    handle.seek(0, os.SEEK_END)
                    if handle.tell() == 0:
                        handle.write(b"0")
                        handle.flush()
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except (BlockingIOError, OSError):
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"another process owns lock: {path}")
                time.sleep(0.1)
        handle.seek(0)
        handle.truncate()
        handle.write(json.dumps({"pid": os.getpid(), "acquired_at": _now()}).encode("utf-8"))
        handle.flush()
        yield path
    finally:
        if acquired:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        handle.close()


def remove_sqlite_family(path: Path) -> int:
    """Remove one SQLite database and its journal/WAL sidecars.

    Analytical snapshots are temporary implementation details.  Removing the
    whole family avoids leaving multi-gigabyte copies or orphaned ``-wal`` /
    ``-shm`` files after a successful or failed job.
    """
    path = Path(path)
    candidates = [
        path,
        Path(str(path) + "-wal"),
        Path(str(path) + "-shm"),
        Path(str(path) + "-journal"),
    ]
    removed = 0
    for candidate in candidates:
        try:
            if candidate.exists():
                candidate.unlink()
                removed += 1
        except OSError:
            pass
    return removed


def cleanup_abandoned_sqlite_artifacts(directory: Path, *, older_than_seconds: float = 3600.0) -> int:
    """Clean stale temporary snapshot files without touching active outputs."""
    directory = Path(directory)
    if not directory.exists():
        return 0
    cutoff = time.time() - max(0.0, float(older_than_seconds))
    removed = 0
    patterns = ("*.tmp", "*.tmp-wal", "*.tmp-shm", "*.tmp-journal")
    for pattern in patterns:
        for path in directory.glob(pattern):
            try:
                if path.stat().st_mtime <= cutoff:
                    path.unlink()
                    removed += 1
            except OSError:
                pass
    return removed


@contextmanager
def temporary_sqlite_snapshot(source: Path, destination: Path, *, timeout_seconds: float = 30.0) -> Iterator[Path]:
    """Yield a verified SQLite snapshot and always delete it afterwards."""
    destination = sqlite_snapshot(source, destination, timeout_seconds=timeout_seconds)
    try:
        yield destination
    finally:
        remove_sqlite_family(destination)


def cleanup_old_snapshots(directory: Path, *, keep: int = 0) -> int:
    """Retain at most ``keep`` full snapshots, including zero."""
    files = sorted(Path(directory).glob("*.sqlite3"), key=lambda p: p.stat().st_mtime, reverse=True)
    keep = max(0, int(keep))
    removed = 0
    for path in files[keep:]:
        removed += remove_sqlite_family(path)
    return removed
