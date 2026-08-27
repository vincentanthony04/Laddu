"""Layer 4 — Factor Store.

New tables layered onto the existing SQLite `Store` conventions (WAL
mode, one connection per thread, no new infra). This module is
self-contained (its own connection helper) so it can be exercised and
tested independently; wiring it into the actual `Store` class means
having `Store.__init__` call `ensure_factor_tables(self._conn)` once at
startup instead of opening a second database file. Do not point this at
a second .db file in production -- pass the same connection/path the
rest of Store already uses.

Tables:
    factor_values(symbol, date, factor_name, value)
        Point-in-time. One row per symbol/date/factor. NEVER overwritten
        retroactively -- a re-run for the same (symbol, date, factor_name)
        is an upsert of that exact cell only, not a delete-and-replace of
        history, so a factor definition change doesn't silently rewrite
        old point-in-time values under the same name (bump factor_name/
        add a version suffix instead if the formula changes).
    factor_registry(factor_name, family, ic_score, ir_score, status,
                     last_validated)
        The alive/reversed/dead status per factor for OUR universe, as
        produced by ic_ir_runner.evaluate_factor. One row per factor
        (latest validation wins -- history of past validations is not
        kept here; if that's wanted later, log ICResult objects to a
        separate factor_validation_history table instead of overloading
        this one).
"""
from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from typing import Iterable, Optional

_CREATE_FACTOR_VALUES = """
CREATE TABLE IF NOT EXISTS factor_values (
    symbol      TEXT    NOT NULL,
    date        TEXT    NOT NULL,
    factor_name TEXT    NOT NULL,
    value       REAL,
    PRIMARY KEY (symbol, date, factor_name)
)
"""

_CREATE_FACTOR_VALUES_IDX = """
CREATE INDEX IF NOT EXISTS idx_factor_values_lookup
ON factor_values (factor_name, date, symbol)
"""

_CREATE_FACTOR_REGISTRY = """
CREATE TABLE IF NOT EXISTS factor_registry (
    factor_name            TEXT PRIMARY KEY,
    family                 TEXT NOT NULL,
    ic_score               REAL,
    ir_score               REAL,
    status                 TEXT NOT NULL,
    last_validated         TEXT NOT NULL,
    redundancy_status      TEXT NOT NULL DEFAULT 'UNMEASURED',
    canonical_factor_name  TEXT,
    redundancy_correlation REAL,
    dedup_version          TEXT,
    dedup_measured_at      TEXT,
    formula_class          TEXT NOT NULL DEFAULT 'UNVERIFIED',
    formula_verification_hash TEXT,
    empirical_qualification_hash TEXT,
    production_influence   INTEGER NOT NULL DEFAULT 0 CHECK(production_influence IN (0,1))
)
"""

_CREATE_FACTOR_DECAY_HISTORY = """
CREATE TABLE IF NOT EXISTS factor_decay_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    factor_name TEXT NOT NULL,
    measured_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    status TEXT NOT NULL,
    baseline_dates INTEGER NOT NULL,
    recent_dates INTEGER NOT NULL,
    baseline_ic REAL,
    recent_ic REAL,
    ic_change REAL,
    recent_hit_rate REAL,
    reason TEXT NOT NULL
)
"""


def _ensure_registry_columns(conn: sqlite3.Connection) -> None:
    existing = {row[1] for row in conn.execute("PRAGMA table_info(factor_registry)").fetchall()}
    additions = {
        "redundancy_status": "TEXT NOT NULL DEFAULT 'UNMEASURED'",
        "canonical_factor_name": "TEXT",
        "redundancy_correlation": "REAL",
        "dedup_version": "TEXT",
        "dedup_measured_at": "TEXT",
        "formula_class": "TEXT NOT NULL DEFAULT 'UNVERIFIED'",
        "formula_verification_hash": "TEXT",
        "empirical_qualification_hash": "TEXT",
        "production_influence": "INTEGER NOT NULL DEFAULT 0",
    }
    for name, ddl in additions.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE factor_registry ADD COLUMN {name} {ddl}")


def ensure_factor_tables(conn: sqlite3.Connection) -> None:
    """Idempotent DDL and additive migration for factor-governance metadata."""
    with closing(conn.cursor()) as cur:
        cur.execute(_CREATE_FACTOR_VALUES)
        cur.execute(_CREATE_FACTOR_VALUES_IDX)
        cur.execute(_CREATE_FACTOR_REGISTRY)
        cur.execute(_CREATE_FACTOR_DECAY_HISTORY)
    _ensure_registry_columns(conn)
    conn.commit()


@dataclass(frozen=True)
class FactorValueRow:
    symbol: str
    date: str  # ISO date string, e.g. "2026-07-11"
    factor_name: str
    value: Optional[float]


def upsert_factor_values(conn: sqlite3.Connection, rows: Iterable[FactorValueRow]) -> int:
    """Insert an immutable batch of point-in-time factor cells.

    An exact replay is idempotent. A different value for an existing key is
    rejected; callers must version the factor name instead of rewriting
    history under changed mathematics.
    """
    rows = list(rows)
    if not rows:
        return 0
    with closing(conn.cursor()) as cur:
        for row in rows:
            existing = cur.execute(
                """SELECT value FROM factor_values
                   WHERE symbol=? AND date=? AND factor_name=?""",
                (row.symbol, row.date, row.factor_name),
            ).fetchone()
            if existing is not None and existing[0] != row.value:
                raise ValueError(
                    "factor value is immutable; version factor_name for revised mathematics"
                )
        cur.executemany(
            """
            INSERT INTO factor_values (symbol, date, factor_name, value)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(symbol, date, factor_name) DO NOTHING
            """,
            [(r.symbol, r.date, r.factor_name, r.value) for r in rows],
        )
    conn.commit()
    return len(rows)


def get_factor_values(
    conn: sqlite3.Connection,
    factor_name: str,
    symbol: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> list[FactorValueRow]:
    """Query factor_values, optionally filtered by symbol and/or an
    inclusive date range. Ordered by date ascending, then symbol."""
    query = "SELECT symbol, date, factor_name, value FROM factor_values WHERE factor_name = ?"
    params: list[object] = [factor_name]
    if symbol is not None:
        query += " AND symbol = ?"
        params.append(symbol)
    if start_date is not None:
        query += " AND date >= ?"
        params.append(start_date)
    if end_date is not None:
        query += " AND date <= ?"
        params.append(end_date)
    query += " ORDER BY date ASC, symbol ASC"

    with closing(conn.cursor()) as cur:
        cur.execute(query, params)
        return [FactorValueRow(*row) for row in cur.fetchall()]


@dataclass(frozen=True)
class FactorRegistryRow:
    factor_name: str
    family: str
    ic_score: Optional[float]
    ir_score: Optional[float]
    status: str
    last_validated: str  # ISO datetime string
    redundancy_status: str = "UNMEASURED"
    canonical_factor_name: Optional[str] = None
    redundancy_correlation: Optional[float] = None
    dedup_version: Optional[str] = None
    dedup_measured_at: Optional[str] = None
    formula_class: str = "UNVERIFIED"
    formula_verification_hash: Optional[str] = None
    empirical_qualification_hash: Optional[str] = None
    production_influence: int = 0

    @property
    def effective_status(self) -> str:
        return "REDUNDANT" if str(self.redundancy_status).upper() == "REDUNDANT" else self.status


def upsert_factor_registry(conn: sqlite3.Connection, row: FactorRegistryRow) -> None:
    """Insert-or-replace the single latest registry row for this factor."""
    with closing(conn.cursor()) as cur:
        cur.execute(
            """
            INSERT INTO factor_registry
                (factor_name, family, ic_score, ir_score, status, last_validated,
                 redundancy_status, canonical_factor_name, redundancy_correlation,
                 dedup_version, dedup_measured_at, formula_class, formula_verification_hash,
                 empirical_qualification_hash, production_influence)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(factor_name) DO UPDATE SET
                family = excluded.family,
                ic_score = excluded.ic_score,
                ir_score = excluded.ir_score,
                status = excluded.status,
                last_validated = excluded.last_validated,
                redundancy_status = CASE
                    WHEN factor_registry.redundancy_status='REDUNDANT' AND excluded.redundancy_status='UNMEASURED'
                    THEN factor_registry.redundancy_status ELSE excluded.redundancy_status END,
                canonical_factor_name = COALESCE(excluded.canonical_factor_name, factor_registry.canonical_factor_name),
                redundancy_correlation = COALESCE(excluded.redundancy_correlation, factor_registry.redundancy_correlation),
                dedup_version = COALESCE(excluded.dedup_version, factor_registry.dedup_version),
                dedup_measured_at = COALESCE(excluded.dedup_measured_at, factor_registry.dedup_measured_at),
                formula_class = excluded.formula_class,
                formula_verification_hash = excluded.formula_verification_hash,
                empirical_qualification_hash = excluded.empirical_qualification_hash,
                production_influence = excluded.production_influence
            """,
            (row.factor_name, row.family, row.ic_score, row.ir_score, row.status, row.last_validated,
             row.redundancy_status, row.canonical_factor_name, row.redundancy_correlation,
             row.dedup_version, row.dedup_measured_at, row.formula_class, row.formula_verification_hash,
             row.empirical_qualification_hash, int(row.production_influence or 0)),
        )
    conn.commit()


def get_factor_registry(
    conn: sqlite3.Connection, status: Optional[str] = None
) -> list[FactorRegistryRow]:
    """List registry rows, optionally filtered to a single status
    ("alive" / "reversed" / "dead" / "insufficient_data")."""
    query = """SELECT factor_name, family, ic_score, ir_score, status, last_validated,
                      redundancy_status, canonical_factor_name, redundancy_correlation,
                      dedup_version, dedup_measured_at, formula_class, formula_verification_hash,
                      empirical_qualification_hash, production_influence FROM factor_registry"""
    params: list[object] = []
    if status is not None:
        query += " WHERE status = ?"
        params.append(status)
    query += " ORDER BY factor_name ASC"

    with closing(conn.cursor()) as cur:
        cur.execute(query, params)
        return [FactorRegistryRow(*row) for row in cur.fetchall()]


def alive_factor_names(conn: sqlite3.Connection) -> list[str]:
    """Convenience for Layer 7 (composite scoring): the set of factor
    names currently safe to use live -- status == 'alive' only.
    'reversed' factors are excluded here on purpose; a caller that wants
    to use a reversed factor must explicitly negate it and re-register
    under a distinct name, not silently flip sign at read time."""
    # Static two-panel audit protects production immediately, before an
    # installation has accumulated enough local factor_values for a store audit.
    try:
        from core.factor_dedup_service import load_static_manifest
        static_redundant = set((load_static_manifest().get("redundant_factors") or {}).keys())
    except Exception:
        static_redundant = set()
    return [
        r.factor_name for r in get_factor_registry(conn, status="alive")
        if str(r.redundancy_status or "UNMEASURED").upper() != "REDUNDANT"
        and r.factor_name not in static_redundant
    ]


def record_decay_report(conn: sqlite3.Connection, report) -> int:
    """Persist an immutable predictive-decay measurement; never overwrite history."""
    with closing(conn.cursor()) as cur:
        cur.execute("""INSERT INTO factor_decay_history
            (factor_name,status,baseline_dates,recent_dates,baseline_ic,recent_ic,ic_change,recent_hit_rate,reason)
            VALUES(?,?,?,?,?,?,?,?,?)""", (report.factor_id, report.status, report.baseline_dates,
            report.recent_dates, report.baseline_ic, report.recent_ic, report.ic_change,
            report.recent_hit_rate, report.reason))
        row_id = cur.lastrowid
    conn.commit()
    return int(row_id)


def latest_decay_reports(conn: sqlite3.Connection) -> list[dict]:
    """Latest report per factor, suitable for model-authority and UI gates."""
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""SELECT h.* FROM factor_decay_history h JOIN
        (SELECT factor_name, MAX(id) id FROM factor_decay_history GROUP BY factor_name) latest
        ON latest.id=h.id ORDER BY h.factor_name""").fetchall()
    return [dict(row) for row in rows]


def production_factor_names(conn: sqlite3.Connection) -> list[str]:
    """Factors allowed to influence actionable production ranking.

    Runtime/IC status alone is insufficient.  Published formula identity must
    be EXACT and hash-verified, empirical qualification must be frozen, and the
    explicit production-influence bit must be one.
    """
    return [
        r.factor_name for r in get_factor_registry(conn, status="alive")
        if str(r.redundancy_status or "UNMEASURED").upper() != "REDUNDANT"
        and str(r.formula_class or "UNVERIFIED").upper() == "EXACT"
        and bool(r.formula_verification_hash)
        and bool(r.empirical_qualification_hash)
        and int(r.production_influence or 0) == 1
    ]
