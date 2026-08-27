from __future__ import annotations

import sqlite3

from core.canonical_decision_repository import ensure_canonical_decision_schema
from core.historical_data_service import ensure_historical_data_schema


DECISION_PIPELINE_MIGRATIONS = (
    (8, "canonical decision record and lifecycle event store"),
    (9, "historical coverage and feature-cache manifests"),
)


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    except Exception:
        return set()


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _repair_legacy_decision_identity(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, "decisions"):
        return
    columns = _columns(conn, "decisions")
    for column in ("decision_id", "thesis_id", "signal_id"):
        if column not in columns:
            conn.execute(f"ALTER TABLE decisions ADD COLUMN {column} TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_decisions_canonical_id "
        "ON decisions(decision_id, created_at)"
    )


def apply_decision_pipeline_migration(conn: sqlite3.Connection, version: int) -> None:
    if version == 8:
        ensure_canonical_decision_schema(conn)
        _repair_legacy_decision_identity(conn)
    elif version == 9:
        ensure_historical_data_schema(conn)


def repair_decision_pipeline_schema(conn: sqlite3.Connection) -> None:
    """Repair physical invariants even after a partial/old migration ledger."""
    ensure_canonical_decision_schema(conn)
    ensure_historical_data_schema(conn)
    _repair_legacy_decision_identity(conn)
