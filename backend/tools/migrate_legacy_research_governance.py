from __future__ import annotations

"""Installer-owned one-time migration of retired SQLite research evidence.

This command is intentionally not imported by the application startup path.
It runs after the production governance schema is available and while the old
runtime is quiescent, then writes an immutable PostgreSQL completion checkpoint.
"""

import argparse
import json
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from config import DB_PATH
from core.data_plane.coordinator import ProductionDataPlane


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    source = Path(DB_PATH)
    if not source.exists():
        result = {
            "ok": True, "state": "NO_LEGACY_RESEARCH_SOURCE",
            "source": str(source), "count_verified": True,
            "hash_verified": True, "quarantine_verified": True,
        }
        output.write_text(json.dumps(result, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
        print(json.dumps(result, sort_keys=True))
        return 0

    plane = ProductionDataPlane()
    conn = None
    try:
        startup = plane.start()
        if startup.get("production_ready") is not True:
            raise RuntimeError(f"PRODUCTION_DATA_PLANE_NOT_READY:{startup.get('blockers')}")
        conn = sqlite3.connect(str(source), timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        result = plane.model_governance.migrate_legacy_research_store(SimpleNamespace(conn=conn))
        result = {**result, "source": str(source), "installer_owned": True}
        if not (
            result.get("count_verified") is True
            and result.get("hash_verified") is True
            and result.get("quarantine_verified") is True
        ):
            raise RuntimeError("LEGACY_RESEARCH_GOVERNANCE_MIGRATION_UNVERIFIED")
        output.write_text(json.dumps(result, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
        print(json.dumps({
            "ok": True, "state": result.get("state"), "source": str(source),
            "expected": result.get("expected"), "verified": result.get("verified"),
            "quarantine": result.get("quarantine"),
            "source_manifest_sha256": result.get("source_manifest_sha256"),
        }, sort_keys=True, default=str))
        return 0
    except Exception as exc:
        failure = {"ok": False, "state": "FAILED", "source": str(source), "error": f"{type(exc).__name__}: {exc}"}
        output.write_text(json.dumps(failure, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(json.dumps(failure, sort_keys=True), file=sys.stderr)
        return 1
    finally:
        if conn is not None:
            conn.close()
        plane.close()


if __name__ == "__main__":
    raise SystemExit(main())
