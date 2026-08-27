"""Remove obsolete analytical SQLite snapshots after lake verification.

Full operational snapshots are temporary transport objects, not durable data.
This tool is intentionally conservative: it only removes them when a curated
Parquet lake and DuckDB catalogue already exist and the lake manifest records a
successful sync.  Operational, legacy rollback, runtime-session and analytical
DuckDB files are never candidates.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from typing import Dict

HERE = Path(__file__).resolve()
BACKEND = HERE.parents[1]
sys.path.insert(0, str(BACKEND))

from core.storage_layout import (
    StorageLayout,
    atomic_write_json,
    cleanup_abandoned_sqlite_artifacts,
    interprocess_lock,
    remove_sqlite_family,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _manifest(path: Path) -> Dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _lake_ready(layout: StorageLayout, manifest: Dict) -> bool:
    return bool(
        layout.analytics_db.exists()
        and manifest.get("last_run")
        and next(layout.curated_lake_dir.glob("**/*.parquet"), None) is not None
    )


def _family_bytes(path: Path) -> int:
    return sum(
        candidate.stat().st_size
        for candidate in (
            path,
            Path(str(path) + "-wal"),
            Path(str(path) + "-shm"),
            Path(str(path) + "-journal"),
        )
        if candidate.exists()
    )


def run(data_dir: Path) -> Dict:
    layout = StorageLayout.from_data_dir(Path(data_dir))
    layout.ensure()
    manifest_path = layout.manifests_dir / "market-lake.json"
    manifest = _manifest(manifest_path)
    if not _lake_ready(layout, manifest):
        result = {
            "ok": True,
            "state": "SKIPPED_LAKE_NOT_VERIFIED",
            "removed_files": 0,
            "bytes_reclaimed": 0,
        }
        print(json.dumps(result, indent=2))
        return result

    removed_files = 0
    bytes_reclaimed = 0
    removed_paths = []
    with interprocess_lock(layout.locks_dir / "analytical-pipeline.lock", timeout_seconds=1.0):
        for directory in (layout.training_snapshots_dir, layout.training_scratch_dir):
            for path in list(directory.glob("*.sqlite3")):
                size = _family_bytes(path)
                count = remove_sqlite_family(path)
                if count:
                    removed_files += count
                    bytes_reclaimed += size
                    removed_paths.append(str(path))
            removed_files += cleanup_abandoned_sqlite_artifacts(directory, older_than_seconds=0)

        prior = manifest.get("source_snapshot")
        if isinstance(prior, str):
            prior_path = Path(prior)
            manifest["source_snapshot"] = {
                "snapshot_id": prior_path.stem,
                "retained": False,
                "legacy_path": str(prior_path),
                "removed_at": _now(),
            }
        elif isinstance(prior, dict):
            manifest["source_snapshot"] = dict(prior, retained=False, removed_at=_now())
        manifest["temporary_snapshot_retained"] = False
        if not isinstance(manifest.get("reconciliation"), dict):
            manifest["reconciliation"] = {}
        prune = manifest.get("operational_prune")
        if not isinstance(prune, dict) or str(prune.get("state") or "").strip() != "NOT_APPLICABLE_PRODUCTION_DATA_PLANE":
            manifest["operational_prune"] = {
                "state": "NOT_APPLICABLE_PRODUCTION_DATA_PLANE",
                "reason": "PostgreSQL, QuestDB and Parquet/DuckDB are authoritative; no operational SQLite authority is eligible for prune",
                "authority": "PRODUCTION_DATA_PLANE",
                "verified_at": _now(),
            }
        manifest["transient_cleanup"] = {
            "completed_at": _now(),
            "removed_files": removed_files,
            "bytes_reclaimed": bytes_reclaimed,
        }
        atomic_write_json(manifest_path, manifest)

    result = {
        "ok": True,
        "state": "TRANSIENT_STORAGE_CLEAN",
        "removed_files": removed_files,
        "bytes_reclaimed": bytes_reclaimed,
        "removed_paths": removed_paths,
        "manifest": str(manifest_path),
    }
    print(json.dumps(result, indent=2))
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Clean Project Laddu transient analytical SQLite files")
    parser.add_argument("--data-dir", type=Path, default=Path(r"C:\ProgramData\ProjectLaddu\data"))
    args = parser.parse_args()
    try:
        run(args.data_dir)
    except TimeoutError as exc:
        print(json.dumps({"ok": False, "state": "ANALYTICAL_PIPELINE_BUSY", "reason": str(exc)}, indent=2))
        raise SystemExit(3)
    except Exception as exc:
        print(json.dumps({"ok": False, "state": "TRANSIENT_CLEANUP_FAILED", "reason": str(exc)}, indent=2))
        raise SystemExit(2)
