from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sqlite3
import sys
from typing import Any

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from core.instrument_universe_policy import (
    ACTIVE_UNIVERSE_REVISION,
    is_cash_equity,
    is_cash_index,
)


def _projection_meta(path: Path) -> dict[str, Any]:
    if not path.exists() or not path.is_file():
        return {"exists": False, "path": str(path)}
    try:
        conn = sqlite3.connect(str(path), timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='instruments'"
            ).fetchone()
            kv = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='kv'"
            ).fetchone()
            if not table or not kv:
                return {
                    "exists": True,
                    "path": str(path),
                    "revision": "",
                    "count": 0,
                    "reason": "required instruments/kv tables are missing",
                }
            meta_row = conn.execute(
                "SELECT v FROM kv WHERE k='instruments_meta'"
            ).fetchone()
            meta = json.loads(meta_row["v"]) if meta_row else {}
            count = int(conn.execute("SELECT COUNT(*) FROM instruments").fetchone()[0])
            return {
                "exists": True,
                "path": str(path),
                "revision": str(meta.get("universe_revision") or ""),
                "count": count,
                "meta": meta,
            }
        finally:
            conn.close()
    except Exception as exc:
        return {
            "exists": True,
            "path": str(path),
            "revision": "",
            "count": 0,
            "reason": str(exc),
        }


def _candidate_paths(install_dir: Path) -> list[Path]:
    return [
        install_dir / "data" / "runtime" / "compatibility" / "compatibility_projection.sqlite3",
        install_dir / "data" / "operational" / "project_laddu_ops.sqlite3",
        install_dir / "data" / "project_laddu.sqlite3",
    ]


def _find_db(install_dir: Path) -> Path:
    """Find a current focused projection, never the first merely existing DB.

    v68 production can retain an older operational/legacy SQLite file while the
    installer prepares the v3 focused catalogue in the bounded compatibility
    projection. Selecting by path order alone can therefore bind PostgreSQL to
    stale v2 metadata. Only a non-empty projection carrying the active revision
    is eligible.
    """

    inspected = [_projection_meta(path) for path in _candidate_paths(install_dir)]
    current = [
        row for row in inspected
        if row.get("revision") == ACTIVE_UNIVERSE_REVISION and int(row.get("count") or 0) > 0
    ]
    if not current:
        summary = [
            {
                "path": row.get("path"),
                "exists": bool(row.get("exists")),
                "revision": row.get("revision") or "",
                "count": int(row.get("count") or 0),
                "reason": row.get("reason") or "",
            }
            for row in inspected
        ]
        raise RuntimeError(
            "focused SQLite projection with the active revision was not found: "
            + json.dumps(summary, sort_keys=True)
        )
    # Candidate order is intentional: the v68 compatibility projection is the
    # installer-owned staging projection; operational/legacy SQLite are
    # retained migration evidence and are fallback sources only when current.
    return Path(str(current[0]["path"])).resolve()


def _resolve_db(install_dir: Path, explicit: Path | None) -> Path:
    if explicit is None:
        return _find_db(install_dir)
    path = explicit.expanduser().resolve()
    if not path.exists() or not path.is_file():
        raise FileNotFoundError(f"focused SQLite projection does not exist: {path}")
    return path


def _load_projection(db_path: Path) -> tuple[list[dict[str, Any]], str, dict[str, Any]]:
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        meta_row = conn.execute("SELECT v FROM kv WHERE k='instruments_meta'").fetchone()
        if not meta_row:
            raise RuntimeError("focused instruments_meta is missing")
        meta = json.loads(meta_row["v"])
        revision = str(meta.get("universe_revision") or "")
        if revision != ACTIVE_UNIVERSE_REVISION:
            raise RuntimeError(
                f"focused revision mismatch: expected={ACTIVE_UNIVERSE_REVISION} actual={revision} source={db_path}"
            )
        rows = [
            dict(row)
            for row in conn.execute(
                "SELECT instrument_key,exchange,segment,trading_symbol,name,"
                "instrument_type,isin,expiry,strike,option_type,lot_size FROM instruments"
            ).fetchall()
        ]
    finally:
        conn.close()

    if not rows:
        raise RuntimeError(f"focused SQLite projection is empty: {db_path}")
    invalid = [row for row in rows if not (is_cash_equity(row) or is_cash_index(row))]
    if invalid:
        raise RuntimeError(
            f"focused SQLite projection contains {len(invalid)} out-of-policy rows: {db_path}"
        )
    expected_count = int(meta.get("count") or (meta.get("universe_stats") or {}).get("active_total") or 0)
    if expected_count and expected_count != len(rows):
        raise RuntimeError(
            f"focused projection row-count mismatch: metadata={expected_count} actual={len(rows)} source={db_path}"
        )
    return rows, revision, meta


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--install-dir", type=Path, required=True)
    parser.add_argument("--sqlite-source", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    install_dir = args.install_dir.resolve()
    dsn = os.environ.get("PROJECT_LADDU_OPERATIONAL_DSN", "").strip()
    if not dsn:
        raise RuntimeError("PROJECT_LADDU_OPERATIONAL_DSN is required")

    db_path = _resolve_db(install_dir, args.sqlite_source)
    rows, revision, meta = _load_projection(db_path)

    # Delay PostgreSQL imports until the complete SQLite authority proof has
    # passed. This also keeps source-selection tests independent of psycopg.
    from core.data_plane.instrument_repository import ProductionInstrumentRepository
    from core.data_plane.postgres import PostgresAuthority

    authority = PostgresAuthority(dsn, role="operational-reference-migration", min_size=1, max_size=2)
    try:
        authority.open()
        proof = ProductionInstrumentRepository(authority).replace_active(rows, revision=revision)
    finally:
        authority.close()
    report = {
        "ok": True,
        "state": "POSTGRES_REFERENCE_AUTHORITY_READY",
        "source_sqlite": str(db_path),
        "source_count": len(rows),
        "source_format": str(meta.get("format") or ""),
        "universe_revision": revision,
        "proof": proof.as_dict(),
    }
    report_path = args.report or (install_dir / "logs" / "postgres-reference-migration.json")
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
