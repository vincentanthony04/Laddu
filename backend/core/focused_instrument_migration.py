"""Deterministic migration from a legacy provider-wide instrument table.

The installer runs this while the Project Laddu service is stopped.  It uses
only the already-persisted instrument rows, so the binding NSE-first/BSE-only
catalogue does not depend on network downloads or a background worker during
release acceptance.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

from core.instrument_universe_policy import (ACTIVE_UNIVERSE_REVISION, allowed_equity_series, build_active_universe, is_cash_equity, is_cash_index)
from models import now_iso

_INSTRUMENT_COLUMNS: Tuple[str, ...] = (
    "instrument_key", "exchange", "segment", "trading_symbol", "name",
    "instrument_type", "isin", "expiry", "strike", "option_type", "lot_size",
)


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _rows(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    if not _table_exists(conn, "instruments"):
        raise RuntimeError("operational database does not contain an instruments table")
    query = "SELECT " + ",".join(_INSTRUMENT_COLUMNS) + " FROM instruments"
    return [dict(row) for row in conn.execute(query).fetchall()]


def _stats_from_rows(rows: Iterable[Dict[str, Any]]) -> Dict[str, int]:
    out = {
        "nse_equities": 0,
        "bse_only_equities": 0,
        "indices": 0,
        "derivatives": 0,
        "out_of_policy_rows": 0,
        "active_total": 0,
    }
    for row in rows:
        segment = str(row.get("segment") or "").upper()
        instrument_type = str(row.get("instrument_type") or "").upper()
        option_type = str(row.get("option_type") or "").upper()
        out["active_total"] += 1
        if segment == "NSE_EQ":
            out["nse_equities"] += 1
        elif segment == "BSE_EQ":
            out["bse_only_equities"] += 1
        elif segment in {"NSE_INDEX", "BSE_INDEX"}:
            out["indices"] += 1
        if (
            option_type in {"CE", "PE"}
            or instrument_type in {"CE", "PE", "FUT", "FUTIDX", "FUTSTK", "OPTIDX", "OPTSTK"}
            or "FO" in segment
        ):
            out["derivatives"] += 1
        if not (is_cash_equity(row) or is_cash_index(row)):
            out["out_of_policy_rows"] += 1
    return out


def prepare_focused_rows(conn: sqlite3.Connection) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    provider_rows = _rows(conn)
    if not provider_rows:
        raise RuntimeError("legacy instrument catalogue is empty; cannot perform an offline focused migration")
    active_rows, policy_stats = build_active_universe(provider_rows)
    derived_stats = _stats_from_rows(active_rows)
    if not active_rows:
        raise RuntimeError("focused universe policy produced an empty catalogue")
    if derived_stats["nse_equities"] <= 0:
        raise RuntimeError("focused universe policy produced no NSE cash equities")
    if derived_stats["bse_only_equities"] <= 0:
        raise RuntimeError("focused universe policy produced no BSE-only cash equities")
    if derived_stats["indices"] <= 0:
        raise RuntimeError("focused universe policy produced no NSE/BSE indices")
    if derived_stats["derivatives"] != 0:
        raise RuntimeError("focused universe policy admitted derivative instruments")
    if derived_stats["out_of_policy_rows"] != 0:
        raise RuntimeError("focused universe policy admitted non-stock cash rows")
    return active_rows, {
        "provider_rows": len(provider_rows),
        "policy_stats": policy_stats,
        "universe_stats": derived_stats,
    }


def migrate_connection(conn: sqlite3.Connection, *, source: str = "installer_offline_legacy_catalogue") -> Dict[str, Any]:
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    active_rows, proof = prepare_focused_rows(conn)
    stats = dict(proof["universe_stats"])
    meta = {
        "loaded": True,
        "count": stats["active_total"],
        "loaded_this_run": stats["active_total"],
        "last_refresh": now_iso(),
        "last_attempt": now_iso(),
        "format": "focused-offline-migration",
        "refresh_state": "ok",
        "cache_usable": True,
        "universe_revision": ACTIVE_UNIVERSE_REVISION,
        "universe_stats": stats,
        "policy_stats": proof["policy_stats"],
        "source_files": [],
        "migration_source": source,
        "derivatives_active": False,
        "allowed_equity_series": allowed_equity_series(),
        "errors": [],
    }
    sql = """INSERT INTO instruments(
        instrument_key,exchange,segment,trading_symbol,name,instrument_type,isin,
        expiry,strike,option_type,lot_size
      ) VALUES(
        :instrument_key,:exchange,:segment,:trading_symbol,:name,:instrument_type,:isin,
        :expiry,:strike,:option_type,:lot_size
      )"""
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT NOT NULL, updated_at TEXT DEFAULT CURRENT_TIMESTAMP)")
        conn.execute("DROP TABLE IF EXISTS instruments_next")
        conn.execute("CREATE TABLE instruments_next AS SELECT * FROM instruments WHERE 0")
        conn.executemany(sql.replace("INSERT INTO instruments(", "INSERT INTO instruments_next("), active_rows)
        staged = conn.execute("SELECT COUNT(*) FROM instruments_next").fetchone()[0]
        if int(staged or 0) != stats["active_total"]:
            raise RuntimeError(f"staged focused catalogue row mismatch: expected={stats['active_total']} actual={staged}")
        conn.execute("DELETE FROM instruments")
        conn.execute("INSERT INTO instruments SELECT * FROM instruments_next")
        conn.execute("DROP TABLE instruments_next")
        conn.execute("DELETE FROM kv WHERE k LIKE 'instkey:%'")
        conn.execute(
            """INSERT INTO kv(k,v,updated_at) VALUES('instruments_meta',?,CURRENT_TIMESTAMP)
               ON CONFLICT(k) DO UPDATE SET v=excluded.v,updated_at=CURRENT_TIMESTAMP""",
            (json.dumps(meta, sort_keys=True, default=str),),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    post_rows = _rows(conn)
    post_stats = _stats_from_rows(post_rows)
    if post_stats != stats:
        raise RuntimeError(f"post-migration focused catalogue mismatch: expected={stats} actual={post_stats}")
    return {
        "ok": True,
        "state": "FOCUSED_CATALOGUE_READY",
        "universe_revision": ACTIVE_UNIVERSE_REVISION,
        "source": source,
        "before_rows": proof["provider_rows"],
        "after_rows": stats["active_total"],
        "policy_stats": proof["policy_stats"],
        "universe_stats": stats,
    }


def migrate_database(path: Path, *, source: str = "installer_offline_legacy_catalogue") -> Dict[str, Any]:
    db_path = Path(path)
    if not db_path.exists():
        raise FileNotFoundError(f"operational database not found: {db_path}")
    conn = sqlite3.connect(str(db_path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    try:
        result = migrate_connection(conn, source=source)
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception:
            pass
        return result
    finally:
        conn.close()
