from __future__ import annotations

"""One-time retained SQLite compatibility-state migration.

Production runtime never reads these SQLite tables after cutover. Source files
are opened read-only, later/focused operational sources win for mutable keys,
and append-only rows use deterministic hashes so reruns are idempotent.

v75 hardening:
- validates and normalises every retained scalar before PostgreSQL receives it;
- migrates table-by-table in bounded chunks instead of one opaque transaction;
- falls back to row savepoints only for PostgreSQL data/constraint errors;
- writes every rejected or forbidden legacy row only to the external JSON evidence report;
- prints the real failed table/error in the console summary;
- never recreates forbidden derivatives relations or an active database quarantine.
"""

import argparse
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import sys
from typing import Any, Callable, Iterable, Mapping, Sequence

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

ALLOWED_MODES = {"intraday", "delivery"}
TABLES = (
    "kv", "manual_watch", "opportunity_memory", "priority_symbols",
    "trade_journal", "daily_learning", "outcome_learning",
    "bulk_block_deals", "market_breadth_daily",
    "reference_data_runs", "fundamentals_cache", "earnings_calendar",
)
FORBIDDEN_TABLES = ("fno_ban_list", "option_chain_snapshot")
ALL_SOURCE_TABLES = TABLES + FORBIDDEN_TABLES
CHUNK_SIZE = 750
RESERVED_AUTHORITY_KV_KEYS = {"instruments_meta"}


class LegacyValueError(ValueError):
    def __init__(self, field: str, code: str, value: Any = None):
        self.field = field
        self.code = code
        self.value = value
        super().__init__(f"{code}:{field}:{value!r}")


class MigrationApplyError(RuntimeError):
    def __init__(self, table: str, row_index: int | None, cause: BaseException):
        self.table = table
        self.row_index = row_index
        self.cause = cause
        sqlstate = getattr(cause, "sqlstate", None)
        detail = f"{type(cause).__name__}: {cause}"
        if sqlstate:
            detail = f"SQLSTATE {sqlstate}: {detail}"
        super().__init__(f"{table}[{row_index if row_index is not None else 'batch'}]: {detail}")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _find_dbs(install_dir: Path) -> list[Path]:
    candidates = (
        install_dir / "data" / "project_laddu.sqlite3",
        install_dir / "data" / "operational" / "project_laddu_ops.sqlite3",
    )
    return [path.resolve() for path in candidates if path.exists()]


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _rows(conn: sqlite3.Connection, table: str, source: Path) -> list[dict[str, Any]]:
    if not _table_exists(conn, table):
        return []
    result: list[dict[str, Any]] = []
    for raw in conn.execute(f'SELECT rowid AS __rowid__, * FROM "{table}"').fetchall():
        row = dict(raw)
        row["__source_database"] = str(source)
        row["__source_table"] = table
        result.append(row)
    return result


def _public(row: Mapping[str, Any]) -> dict[str, Any]:
    return {str(k): v for k, v in row.items() if not str(k).startswith("__")}


def _hash(row: Mapping[str, Any]) -> str:
    blob = json.dumps(_public(row), sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _source_key(row: Mapping[str, Any]) -> str:
    source = str(row.get("__source_database") or "unknown")
    table = str(row.get("__source_table") or "unknown")
    rowid = str(row.get("__rowid__") or "")
    return hashlib.sha256(f"{source}|{table}|{rowid}|{_hash(row)}".encode("utf-8")).hexdigest()


def _text(value: Any, default: str = "") -> str:
    value = default if value is None else value
    return str(value).strip()


def _required_text(value: Any, field: str) -> str:
    text = _text(value)
    if not text:
        raise LegacyValueError(field, "MISSING_REQUIRED_FIELD", value)
    return text


def _json_value(value: Any, default: Any) -> Any:
    if value in (None, ""):
        return default
    if isinstance(value, (dict, list, int, float, bool)):
        return value
    try:
        return json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        # Preserve non-JSON legacy text as a JSON string. It remains evidence,
        # never executable configuration.
        return str(value)


def _json_text(value: Any, default: Any) -> str:
    return json.dumps(_json_value(value, default), sort_keys=True, default=str)


def _mode(value: Any, *, allow_all: bool = False) -> str | None:
    mode = _text(value, "all" if allow_all else "").lower()
    aliases = {"fast": "intraday", "day": "intraday", "slow": "delivery"}
    mode = aliases.get(mode, mode)
    if allow_all and mode in {"", "all", "both"}:
        return "all"
    return mode if mode in ALLOWED_MODES else None


def _exchange(value: Any) -> str:
    raw = _text(value, "NSE").upper()
    return "BSE" if raw.startswith("BSE") else "NSE"


def _side(value: Any, *, allow_wait: bool = False) -> str | None:
    raw = _text(value).upper()
    aliases = {"BUY": "LONG", "BULLISH": "LONG", "SELL": "SHORT", "BEARISH": "SHORT"}
    raw = aliases.get(raw, raw)
    if allow_wait and raw in {"", "WAIT", "WATCH", "NONE"}:
        return "WAIT"
    return raw if raw in {"LONG", "SHORT"} else None


def _boolean(value: Any, field: str, *, default: bool = False) -> bool:
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    raw = _text(value).lower()
    if raw in {"1", "true", "yes", "y", "on", "ok", "ready"}:
        return True
    if raw in {"0", "false", "no", "n", "off", "failed", "error"}:
        return False
    raise LegacyValueError(field, "INVALID_BOOLEAN", value)


def _integer(value: Any, field: str, *, default: int = 0) -> int:
    if value in (None, ""):
        return default
    try:
        number = Decimal(str(value).strip())
        if not number.is_finite():
            raise InvalidOperation
        return int(number)
    except (InvalidOperation, ValueError, TypeError, OverflowError):
        raise LegacyValueError(field, "INVALID_INTEGER", value)


def _number(value: Any, field: str) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        number = Decimal(str(value).strip())
        if not number.is_finite():
            raise InvalidOperation
        return number
    except (InvalidOperation, ValueError, TypeError, OverflowError):
        raise LegacyValueError(field, "INVALID_NUMBER", value)


def _timestamp(value: Any, field: str, *, required: bool = False) -> datetime | None:
    if value in (None, ""):
        if required:
            raise LegacyValueError(field, "MISSING_REQUIRED_TIMESTAMP", value)
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime(value.year, value.month, value.day)
    elif isinstance(value, (int, float)) and math.isfinite(float(value)):
        raw = float(value)
        if abs(raw) > 10_000_000_000:
            raw /= 1000.0
        try:
            parsed = datetime.fromtimestamp(raw, tz=timezone.utc)
        except (ValueError, OSError, OverflowError):
            raise LegacyValueError(field, "INVALID_TIMESTAMP", value)
    else:
        raw = _text(value)
        candidates = [raw, raw.replace("Z", "+00:00")]
        parsed = None
        for candidate in candidates:
            try:
                parsed = datetime.fromisoformat(candidate)
                break
            except ValueError:
                pass
        if parsed is None:
            for fmt in (
                "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S",
                "%Y/%m/%d %H:%M:%S", "%d-%m-%Y %H:%M:%S",
                "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y",
            ):
                try:
                    parsed = datetime.strptime(raw, fmt)
                    break
                except ValueError:
                    pass
        if parsed is None:
            raise LegacyValueError(field, "INVALID_TIMESTAMP", value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _date_value(value: Any, field: str, *, required: bool = False) -> date | None:
    if value in (None, ""):
        if required:
            raise LegacyValueError(field, "MISSING_REQUIRED_DATE", value)
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        parsed = _timestamp(value, field, required=required)
        return parsed.date() if parsed is not None else None
    raw = _text(value)
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        pass
    for fmt in ("%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d", "%Y%m%d"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            pass
    raise LegacyValueError(field, "INVALID_DATE", value)


def _inventory(sources: list[Path]) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
    inventory = {table: [] for table in ALL_SOURCE_TABLES}
    errors: list[dict[str, Any]] = []
    for source in sources:
        conn = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        try:
            for table in ALL_SOURCE_TABLES:
                try:
                    inventory[table].extend(_rows(conn, table, source))
                except Exception as exc:
                    errors.append({"source": str(source), "table": table, "error": f"{type(exc).__name__}: {exc}"})
        finally:
            conn.close()
    return inventory, errors


def _latest(rows: Iterable[dict[str, Any]], key_fn: Callable[[dict[str, Any]], Any]) -> list[dict[str, Any]]:
    # Sources are inventoried oldest/legacy first, focused operational last.
    merged: dict[Any, dict[str, Any]] = {}
    for row in rows:
        key = key_fn(row)
        if key not in (None, "", ("", "")):
            merged[key] = row
    return list(merged.values())


def _reject(table: str, reason: str, row: Mapping[str, Any], *, detail: Any = None) -> dict[str, Any]:
    return {
        "table": table,
        "reason": reason,
        "detail": detail,
        "source_database": str(row.get("__source_database") or "unknown"),
        "source_table": str(row.get("__source_table") or table),
        "source_rowid": str(row.get("__rowid__") or "unknown"),
        "row": _public(row),
    }


def _prepare(table: str, row: dict[str, Any]) -> dict[str, Any]:
    prepared = dict(row)
    if table == "kv":
        prepared["k"] = _required_text(row.get("k"), "k")
        prepared["updated_at"] = _timestamp(row.get("updated_at"), "updated_at")
    elif table == "manual_watch":
        prepared["symbol"] = _required_text(row.get("symbol"), "symbol").upper()
        mode = _mode(row.get("mode"))
        if mode is None:
            raise LegacyValueError("mode", "NON_PRODUCTION_MODE", row.get("mode"))
        prepared["mode"] = mode
        side = _side(row.get("side"), allow_wait=True)
        if side is None:
            raise LegacyValueError("side", "INVALID_SIDE", row.get("side"))
        prepared["side"] = side
        prepared["pinned"] = _boolean(row.get("pinned"), "pinned")
        prepared["created_at"] = _timestamp(row.get("created_at"), "created_at")
        prepared["updated_at"] = _timestamp(row.get("updated_at"), "updated_at")
    elif table == "opportunity_memory":
        prepared["symbol"] = _required_text(row.get("symbol"), "symbol").upper()
        mode = _mode(row.get("mode"))
        if mode is None:
            raise LegacyValueError("mode", "NON_PRODUCTION_MODE", row.get("mode"))
        prepared["mode"] = mode
        prepared["priority_score"] = _integer(row.get("priority_score"), "priority_score")
        prepared["next_scan_at"] = _timestamp(row.get("next_scan_at"), "next_scan_at")
        prepared["last_seen_at"] = _timestamp(row.get("last_seen_at"), "last_seen_at")
        prepared["updated_at"] = _timestamp(row.get("updated_at"), "updated_at")
    elif table == "priority_symbols":
        prepared["symbol"] = _required_text(row.get("symbol"), "symbol").upper()
        mode = _mode(row.get("mode"), allow_all=True)
        if mode is None:
            raise LegacyValueError("mode", "NON_PRODUCTION_MODE", row.get("mode"))
        prepared["mode"] = mode
        prepared["created_at"] = _timestamp(row.get("created_at"), "created_at")
    elif table == "trade_journal":
        prepared["symbol"] = _required_text(row.get("symbol"), "symbol").upper()
        mode = _mode(row.get("mode"))
        side = _side(row.get("side"))
        if mode is None:
            raise LegacyValueError("mode", "NON_PRODUCTION_MODE", row.get("mode"))
        if side is None:
            raise LegacyValueError("side", "INVALID_SIDE", row.get("side"))
        prepared["mode"], prepared["side"] = mode, side
        for field in ("entry", "exit", "qty", "pnl", "holding_minutes"):
            prepared[field] = _number(row.get(field), field)
        for field in ("opened_at", "closed_at", "created_at"):
            prepared[field] = _timestamp(row.get(field), field)
    elif table == "daily_learning":
        prepared["learning_date"] = _date_value(row.get("learning_date"), "learning_date")
        prepared["created_at"] = _timestamp(row.get("created_at"), "created_at")
    elif table == "outcome_learning":
        prepared["signal_id"] = _required_text(row.get("signal_id"), "signal_id")
        prepared["symbol"] = _required_text(row.get("symbol"), "symbol").upper()
        mode = _mode(row.get("mode"))
        side = _side(row.get("side"))
        if mode is None:
            raise LegacyValueError("mode", "NON_PRODUCTION_MODE", row.get("mode"))
        if side is None:
            raise LegacyValueError("side", "INVALID_SIDE", row.get("side"))
        prepared["mode"], prepared["side"] = mode, side
        prepared["pnl_points"] = _number(row.get("pnl_points"), "pnl_points")
        prepared["holding_minutes"] = _number(row.get("holding_minutes"), "holding_minutes")
        prepared["closed_at"] = _timestamp(row.get("closed_at"), "closed_at")
        prepared["created_at"] = _timestamp(row.get("created_at"), "created_at")
    elif table == "bulk_block_deals":
        prepared["trade_date"] = _date_value(row.get("trade_date"), "trade_date", required=True)
        prepared["symbol"] = _required_text(row.get("symbol"), "symbol").upper()
        prepared["qty"] = _number(row.get("qty"), "qty")
        prepared["price"] = _number(row.get("price"), "price")
        prepared["fetched_at"] = _timestamp(row.get("fetched_at"), "fetched_at")
    elif table == "market_breadth_daily":
        prepared["ts"] = _timestamp(row.get("ts"), "ts", required=True)
        prepared["universe"] = _required_text(row.get("universe"), "universe")
        for field in ("advances", "declines", "unchanged"):
            prepared[field] = _integer(row.get(field), field)
    elif table == "reference_data_runs":
        prepared["job_name"] = _required_text(row.get("job_name"), "job_name")
        prepared["run_date"] = _date_value(row.get("run_date"), "run_date", required=True)
        prepared["rows_written"] = _integer(row.get("rows_written"), "rows_written")
        prepared["finished_at"] = _timestamp(row.get("finished_at"), "finished_at")
    elif table == "fundamentals_cache":
        prepared["isin"] = _required_text(row.get("isin"), "isin").upper()
        prepared["ok"] = _boolean(row.get("ok"), "ok")
        prepared["fetched_at"] = _timestamp(row.get("fetched_at"), "fetched_at")
    elif table == "earnings_calendar":
        prepared["symbol"] = _required_text(row.get("symbol"), "symbol").upper()
        prepared["event_date"] = _date_value(row.get("event_date"), "event_date", required=True)
        prepared["event_type"] = _text(row.get("event_type"), "board_meeting") or "board_meeting"
        prepared["fetched_at"] = _timestamp(row.get("fetched_at"), "fetched_at")
    return prepared


def _plan(inventory: dict[str, list[dict[str, Any]]]) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
    candidates: dict[str, list[dict[str, Any]]] = {}
    candidates["kv"] = [
        row for row in _latest(inventory["kv"], lambda r: _text(r.get("k")))
        if _text(row.get("k")) not in RESERVED_AUTHORITY_KV_KEYS
    ]
    candidates["manual_watch"] = _latest(
        inventory["manual_watch"], lambda r: (_text(r.get("symbol")).upper(), _text(r.get("mode")).lower())
    )
    candidates["opportunity_memory"] = _latest(
        inventory["opportunity_memory"], lambda r: (_text(r.get("symbol")).upper(), _text(r.get("mode")).lower())
    )
    candidates["priority_symbols"] = _latest(
        inventory["priority_symbols"],
        lambda r: (_text(r.get("symbol")).upper(), _exchange(r.get("exchange")), _text(r.get("mode"), "all").lower()),
    )
    candidates["fundamentals_cache"] = _latest(inventory["fundamentals_cache"], lambda r: _text(r.get("isin")).upper())
    candidates["market_breadth_daily"] = _latest(
        inventory["market_breadth_daily"], lambda r: (_text(r.get("ts")), _text(r.get("universe")))
    )
    candidates["reference_data_runs"] = _latest(
        inventory["reference_data_runs"], lambda r: (_text(r.get("job_name")), _text(r.get("run_date")))
    )
    candidates["earnings_calendar"] = _latest(
        inventory["earnings_calendar"],
        lambda r: (_text(r.get("symbol")).upper(), _text(r.get("event_date")), _text(r.get("event_type"), "board_meeting")),
    )
    for table in ("trade_journal", "daily_learning", "outcome_learning", "bulk_block_deals"):
        unique: dict[str, dict[str, Any]] = {}
        for row in inventory[table]:
            unique[_hash(row)] = row
        candidates[table] = list(unique.values())

    plan = {table: [] for table in TABLES}
    rejected: list[dict[str, Any]] = []
    for table in TABLES:
        for row in candidates.get(table, []):
            try:
                plan[table].append(_prepare(table, row))
            except LegacyValueError as exc:
                rejected.append(_reject(table, exc.code, row, detail={"field": exc.field, "value": exc.value}))
            except Exception as exc:
                rejected.append(_reject(table, "LEGACY_NORMALISATION_ERROR", row, detail=f"{type(exc).__name__}: {exc}"))
    for table in FORBIDDEN_TABLES:
        for row in inventory.get(table, []):
            rejected.append(_reject(table, "FORBIDDEN_DERIVATIVES_STATE_REMOVED", row, detail="External audit evidence only; never migrated into active authority"))
    return plan, rejected


SQL: dict[str, str] = {
    "kv": """INSERT INTO runtime_control.kv(k,v,updated_at) VALUES(%s,%s::jsonb,COALESCE(%s,now()))
             ON CONFLICT(k) DO UPDATE SET v=EXCLUDED.v,updated_at=EXCLUDED.updated_at""",
    "manual_watch": """INSERT INTO trading.manual_watch(symbol,exchange,mode,side,state,waiting_for,trigger,invalidation,reason,pinned,source,payload_json,created_at,updated_at)
             VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,COALESCE(%s,now()),COALESCE(%s,now()))
             ON CONFLICT(symbol,mode) DO UPDATE SET exchange=EXCLUDED.exchange,side=EXCLUDED.side,state=EXCLUDED.state,
             waiting_for=EXCLUDED.waiting_for,trigger=EXCLUDED.trigger,invalidation=EXCLUDED.invalidation,reason=EXCLUDED.reason,
             pinned=EXCLUDED.pinned,source=EXCLUDED.source,payload_json=EXCLUDED.payload_json,updated_at=EXCLUDED.updated_at""",
    "opportunity_memory": """INSERT INTO trading.opportunity_memory(symbol,exchange,mode,stage,priority_score,sector,themes_json,priority_reason,trigger,invalidation,target_window,next_scan_at,last_seen_at,payload_json,updated_at)
             VALUES(%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s,COALESCE(%s,now()),%s::jsonb,COALESCE(%s,now()))
             ON CONFLICT(symbol,mode) DO UPDATE SET exchange=EXCLUDED.exchange,stage=EXCLUDED.stage,priority_score=EXCLUDED.priority_score,
             sector=EXCLUDED.sector,themes_json=EXCLUDED.themes_json,priority_reason=EXCLUDED.priority_reason,trigger=EXCLUDED.trigger,
             invalidation=EXCLUDED.invalidation,target_window=EXCLUDED.target_window,next_scan_at=EXCLUDED.next_scan_at,
             last_seen_at=EXCLUDED.last_seen_at,payload_json=EXCLUDED.payload_json,updated_at=EXCLUDED.updated_at""",
    "priority_symbols": """INSERT INTO trading.priority_symbols(symbol,exchange,mode,source,created_at)
             VALUES(%s,%s,%s,%s,COALESCE(%s,now()))
             ON CONFLICT(symbol,exchange,mode) DO UPDATE SET source=EXCLUDED.source,created_at=EXCLUDED.created_at""",
    "trade_journal": """INSERT INTO trading.manual_trade_journal(symbol,exchange,mode,side,entry,exit,quantity,status,pnl,holding_minutes,notes,opened_at,closed_at,created_at,updated_at,legacy_source_key)
             VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,COALESCE(%s,now()),COALESCE(%s,now()),%s)
             ON CONFLICT DO NOTHING""",
    "daily_learning": """INSERT INTO runtime_control.daily_learning(learning_date,payload,created_at,legacy_source_key)
             VALUES(COALESCE(%s,CURRENT_DATE),%s::jsonb,COALESCE(%s,now()),%s) ON CONFLICT DO NOTHING""",
    "outcome_learning": """INSERT INTO trading.outcome_learning(signal_id,symbol,mode,side,result,pnl_points,holding_minutes,attribution,features,proof,model_version,closed_at,created_at)
             VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s,%s,COALESCE(%s,now()))
             ON CONFLICT(signal_id) DO UPDATE SET result=EXCLUDED.result,pnl_points=EXCLUDED.pnl_points,holding_minutes=EXCLUDED.holding_minutes,
             attribution=EXCLUDED.attribution,features=EXCLUDED.features,proof=EXCLUDED.proof,model_version=EXCLUDED.model_version,closed_at=EXCLUDED.closed_at""",
    "bulk_block_deals": """INSERT INTO reference.bulk_block_deals(trade_date,symbol,deal_type,client_name,buy_sell,qty,price,source_hash,created_at)
             VALUES(%s,%s,%s,%s,%s,%s,%s,%s,COALESCE(%s,now())) ON CONFLICT DO NOTHING""",
    "market_breadth_daily": """INSERT INTO reference.market_breadth_daily(ts,universe,advances,declines,unchanged)
             VALUES(%s,%s,%s,%s,%s) ON CONFLICT(ts,universe) DO UPDATE SET advances=EXCLUDED.advances,declines=EXCLUDED.declines,unchanged=EXCLUDED.unchanged""",
    "reference_data_runs": """INSERT INTO reference.reference_data_runs(job_name,run_date,status,rows_written,error,finished_at)
             VALUES(%s,%s,%s,%s,%s,COALESCE(%s,now())) ON CONFLICT(job_name,run_date) DO UPDATE SET status=EXCLUDED.status,rows_written=EXCLUDED.rows_written,error=EXCLUDED.error,finished_at=EXCLUDED.finished_at""",
    "fundamentals_cache": """INSERT INTO reference.fundamentals_cache(isin,ok,payload_json,fetched_at) VALUES(%s,%s,%s::jsonb,COALESCE(%s,now()))
             ON CONFLICT(isin) DO UPDATE SET ok=EXCLUDED.ok,payload_json=EXCLUDED.payload_json,fetched_at=EXCLUDED.fetched_at""",
    "earnings_calendar": """INSERT INTO reference.earnings_calendar(symbol,event_date,event_type,purpose,created_at)
             VALUES(%s,%s,%s,%s,COALESCE(%s,now())) ON CONFLICT(symbol,event_date,event_type) DO UPDATE SET purpose=EXCLUDED.purpose,created_at=EXCLUDED.created_at""",
}


def _params(table: str, row: Mapping[str, Any]) -> tuple[Any, ...]:
    if table == "kv":
        return (row["k"], _json_text(row.get("v"), ""), row.get("updated_at"))
    if table == "manual_watch":
        return (row["symbol"], _exchange(row.get("exchange")), row["mode"], row["side"], _text(row.get("state"), "WATCH"),
                row.get("waiting_for"), row.get("trigger"), row.get("invalidation"), row.get("reason"), row["pinned"],
                _text(row.get("source"), "manual_search"), _json_text(row.get("payload_json"), {}), row.get("created_at"), row.get("updated_at"))
    if table == "opportunity_memory":
        return (row["symbol"], _exchange(row.get("exchange")), row["mode"], _text(row.get("stage"), "Potential"), row["priority_score"],
                row.get("sector"), _json_text(row.get("themes_json"), []), row.get("priority_reason"), row.get("trigger"), row.get("invalidation"),
                row.get("target_window"), row.get("next_scan_at"), row.get("last_seen_at"), _json_text(row.get("payload_json"), {}), row.get("updated_at"))
    if table == "priority_symbols":
        return (row["symbol"], _exchange(row.get("exchange")), row["mode"], _text(row.get("source"), "search"), row.get("created_at"))
    if table == "trade_journal":
        return (row["symbol"], _exchange(row.get("exchange")), row["mode"], row["side"], row.get("entry"), row.get("exit"), row.get("qty"),
                _text(row.get("status"), "CLOSED"), row.get("pnl"), row.get("holding_minutes"), row.get("notes"), row.get("opened_at"),
                row.get("closed_at"), row.get("created_at"), row.get("closed_at") or row.get("created_at"), _source_key(row))
    if table == "daily_learning":
        return (row.get("learning_date"), _json_text(row.get("payload_json"), {}), row.get("created_at"), _source_key(row))
    if table == "outcome_learning":
        return (row["signal_id"], row["symbol"], row["mode"], row["side"], _text(row.get("result"), "UNSCORABLE"), row.get("pnl_points"),
                row.get("holding_minutes"), row.get("attribution"), _json_text(row.get("feature_json"), {}), _json_text(row.get("proof_json"), {}),
                row.get("model_version"), row.get("closed_at"), row.get("created_at"))
    if table == "bulk_block_deals":
        return (row["trade_date"], row["symbol"], _text(row.get("deal_type"), "UNKNOWN"), row.get("client_name"), row.get("buy_sell"),
                row.get("qty"), row.get("price"), _hash(row), row.get("fetched_at"))
    if table == "market_breadth_daily":
        return (row["ts"], row["universe"], row["advances"], row["declines"], row["unchanged"])
    if table == "reference_data_runs":
        return (row["job_name"], row["run_date"], _text(row.get("status"), "UNKNOWN"), row["rows_written"], row.get("error"), row.get("finished_at"))
    if table == "fundamentals_cache":
        return (row["isin"], row["ok"], _json_text(row.get("payload_json"), {}), row.get("fetched_at"))
    if table == "earnings_calendar":
        return (row["symbol"], row["event_date"], row["event_type"], row.get("purpose"), row.get("fetched_at"))
    raise KeyError(table)


def _is_row_data_error(exc: BaseException) -> bool:
    sqlstate = str(getattr(exc, "sqlstate", "") or "")
    return sqlstate.startswith("22") or sqlstate.startswith("23")


def _chunks(rows: Sequence[dict[str, Any]], size: int = CHUNK_SIZE) -> Iterable[tuple[int, Sequence[dict[str, Any]]]]:
    for start in range(0, len(rows), size):
        yield start, rows[start:start + size]


def _apply_table(table: str, rows: list[dict[str, Any]], authority: Any) -> tuple[int, list[dict[str, Any]]]:
    if not rows:
        return 0, []
    accepted = 0
    rejected: list[dict[str, Any]] = []
    with authority.transaction(lock_timeout_ms=5_000, statement_timeout_ms=120_000, idle_timeout_ms=120_000) as conn:
        for start, chunk in _chunks(rows):
            params = [_params(table, row) for row in chunk]
            try:
                # Nested transaction is a PostgreSQL savepoint. A bad legacy
                # row cannot poison the outer table transaction.
                with conn.transaction():
                    with conn.cursor() as cur:
                        cur.executemany(SQL[table], params)
                accepted += len(chunk)
                continue
            except Exception as batch_exc:
                if not _is_row_data_error(batch_exc):
                    raise MigrationApplyError(table, None, batch_exc) from batch_exc
            for offset, (row, bound) in enumerate(zip(chunk, params)):
                try:
                    with conn.transaction():
                        with conn.cursor() as cur:
                            cur.execute(SQL[table], bound)
                    accepted += 1
                except Exception as row_exc:
                    if not _is_row_data_error(row_exc):
                        raise MigrationApplyError(table, start + offset, row_exc) from row_exc
                    rejected.append(_reject(
                        table, "POSTGRES_DATA_REJECTION", row,
                        detail={
                            "row_index": start + offset,
                            "sqlstate": getattr(row_exc, "sqlstate", None),
                            "error": f"{type(row_exc).__name__}: {row_exc}",
                        },
                    ))
    return accepted, rejected


def _publish_authoritative_instrument_meta(authority: Any) -> dict[str, Any]:
    """Publish the accepted PostgreSQL catalogue to the runtime KV contract."""
    row = authority.execute(
        """
        SELECT COALESCE(max(universe_revision),'') AS revision,
               count(*)::bigint AS active_total,
               count(*) FILTER (WHERE exchange='NSE' AND asset_class='CASH_EQUITY')::bigint AS nse_equities,
               count(*) FILTER (WHERE exchange='BSE' AND asset_class='CASH_EQUITY')::bigint AS bse_only_equities,
               count(*) FILTER (WHERE asset_class='INDEX')::bigint AS indices,
               0::bigint AS derivatives,
               count(*) FILTER (
                   WHERE asset_class NOT IN ('CASH_EQUITY','INDEX')
                      OR (isin IS NOT NULL AND left(upper(isin), 3)='INF')
               )::bigint AS out_of_policy_rows
          FROM core.instruments
         WHERE active_to IS NULL AND validation_status='ACCEPTED'
        """,
        fetch="one", statement_timeout_ms=10_000,
    ) or {}
    revision = _text(row.get("revision"))
    count = int(row.get("active_total") or 0)
    stats = {
        "revision": revision, "universe_revision": revision, "active_total": count,
        "nse_equities": int(row.get("nse_equities") or 0),
        "bse_only_equities": int(row.get("bse_only_equities") or 0),
        "indices": int(row.get("indices") or 0),
        "derivatives": int(row.get("derivatives") or 0),
        "out_of_policy_rows": int(row.get("out_of_policy_rows") or 0),
    }
    if not revision or count <= 0:
        raise RuntimeError(f"POSTGRES_INSTRUMENT_AUTHORITY_EMPTY_OR_UNVERSIONED:{stats}")
    if stats["nse_equities"] <= 0 or stats["bse_only_equities"] <= 0 or stats["indices"] <= 0:
        raise RuntimeError(f"POSTGRES_INSTRUMENT_AUTHORITY_INCOMPLETE:{stats}")
    if stats["derivatives"] or stats["out_of_policy_rows"]:
        raise RuntimeError(f"POSTGRES_INSTRUMENT_AUTHORITY_OUT_OF_POLICY:{stats}")
    meta = {
        "loaded": True, "count": count, "source": "postgresql-instrument-authority",
        "cache_usable": True, "message": "Using focused NSE/BSE cash-equity catalogue",
        "refresh_state": "authority_reconciled", "universe_revision": revision,
        "target_universe_revision": revision, "universe_stats": stats,
        "authority_engine": "postgresql", "last_refresh": _now(),
    }
    authority.execute(
        """INSERT INTO runtime_control.kv(k,v,updated_at)
             VALUES('instruments_meta',%s::jsonb,now())
             ON CONFLICT(k) DO UPDATE SET v=excluded.v, updated_at=excluded.updated_at""",
        (json.dumps(meta, sort_keys=True),), statement_timeout_ms=10_000,
    )
    return meta


def _target_counts(authority: Any) -> dict[str, int]:
    mapping = {
        "kv": "runtime_control.kv", "manual_watch": "trading.manual_watch",
        "opportunity_memory": "trading.opportunity_memory", "priority_symbols": "trading.priority_symbols",
        "trade_journal": "trading.manual_trade_journal", "daily_learning": "runtime_control.daily_learning",
        "outcome_learning": "trading.outcome_learning", "bulk_block_deals": "reference.bulk_block_deals",
        "market_breadth_daily": "reference.market_breadth_daily",
        "reference_data_runs": "reference.reference_data_runs", "fundamentals_cache": "reference.fundamentals_cache",
        "earnings_calendar": "reference.earnings_calendar",
    }
    result: dict[str, int] = {}
    for key, table in mapping.items():
        row = authority.execute(f"SELECT count(*) AS n FROM {table}", fetch="one", statement_timeout_ms=10_000)
        result[key] = int((row or {}).get("n") or 0)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--install-dir", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    install_dir = args.install_dir.resolve()
    report_path = args.report or install_dir / "logs" / (
        "postgres-compatibility-state-dry-run.json" if args.dry_run else "postgres-compatibility-state-migration.json"
    )
    sources = _find_dbs(install_dir)
    inventory, inventory_errors = _inventory(sources)
    plan, normalisation_rejected = _plan(inventory)
    source_counts = {table: len(inventory.get(table) or []) for table in ALL_SOURCE_TABLES}
    planned_counts = {table: len(plan.get(table) or []) for table in ALL_SOURCE_TABLES}
    rejected_by_table = {
        table: sum(1 for item in normalisation_rejected if item.get("table") == table)
        for table in ALL_SOURCE_TABLES
    }
    superseded_counts = {
        table: max(0, source_counts[table] - planned_counts[table] - rejected_by_table[table])
        for table in ALL_SOURCE_TABLES
    }
    source_total = sum(source_counts.values())
    planned_total = sum(planned_counts.values())
    superseded_total = sum(superseded_counts.values())
    report: dict[str, Any] = {
        "ok": not inventory_errors,
        "state": "DRY_RUN_RECONCILED" if args.dry_run else "PLANNED",
        "service_version": "compatibility-state-postgres-cutover-1.1.0",
        "source_sqlites": [str(path) for path in sources],
        "source_counts": source_counts,
        "planned_counts": planned_counts,
        "superseded_counts": superseded_counts,
        "superseded_count": superseded_total,
        "external_rejection_count": len(normalisation_rejected),
        "external_rejection_reason": "INVALID_NON_PRODUCTION_OR_FORBIDDEN_LEGACY_ROW",
        "external_rejection_samples": normalisation_rejected[:20],
        "inventory_errors": inventory_errors,
        "reconciliation": {
            "source": source_total,
            "planned": planned_total,
            "externally_rejected": len(normalisation_rejected),
            "superseded": superseded_total,
            "ok": source_total == planned_total + len(normalisation_rejected) + superseded_total,
        },
        "verified_at": _now(),
    }
    authority = None
    failed_table = None
    failed_row_index = None
    try:
        if not args.dry_run and report["ok"]:
            dsn = os.environ.get("PROJECT_LADDU_OPERATIONAL_DSN", "").strip()
            if not dsn:
                raise RuntimeError("PROJECT_LADDU_OPERATIONAL_DSN is required")
            from core.data_plane.postgres import PostgresAuthority
            authority = PostgresAuthority(dsn, role="compatibility-state-migration", min_size=1, max_size=2)
            authority.open()
            accepted_counts = {table: 0 for table in TABLES}
            db_rejected: list[dict[str, Any]] = []
            for table in TABLES:
                accepted, rejected = _apply_table(table, plan[table], authority)
                accepted_counts[table] = accepted
                db_rejected.extend(rejected)
            instrument_meta = _publish_authoritative_instrument_meta(authority)
            all_rejected = normalisation_rejected + db_rejected
            run_id = f"compatibility-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{hashlib.sha256('|'.join(map(str, sources)).encode()).hexdigest()[:12]}"
            target = _target_counts(authority)
            verification = {key: target[key] >= accepted_counts[key] for key in TABLES}
            accepted_total = sum(accepted_counts.values())
            final_reconciliation = {
                "source": source_total,
                "accepted": accepted_total,
                "externally_rejected": len(all_rejected),
                "superseded": superseded_total,
                "ok": source_total == accepted_total + len(all_rejected) + superseded_total,
            }
            report.update({
                "ok": all(verification.values()) and final_reconciliation["ok"],
                "state": "POSTGRES_COMPATIBILITY_AUTHORITY_MIGRATED",
                "accepted_counts": accepted_counts,
                "authoritative_instrument_meta": instrument_meta,
                "database_rejection_count": len(db_rejected),
                "external_rejection_count": len(all_rejected),
                "external_rejection_samples": all_rejected[:20],
                "target_counts": target,
                "verification": verification,
                "verification_ok": all(verification.values()),
                "reconciliation": final_reconciliation,
                "reconciliation_ok": final_reconciliation["ok"],
                "migration_run_id": run_id,
            })
    except MigrationApplyError as exc:
        failed_table = exc.table
        failed_row_index = exc.row_index
        report.update({
            "ok": False, "state": "FAILED", "failed_table": exc.table,
            "failed_row_index": exc.row_index, "error": str(exc),
        })
    except Exception as exc:
        report.update({"ok": False, "state": "FAILED", "error": f"{type(exc).__name__}: {exc}"})
    finally:
        if authority is not None:
            authority.close()
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    summary = {
        "ok": report.get("ok"), "state": report.get("state"),
        "source_counts": source_counts, "planned_counts": planned_counts,
        "accepted_counts": report.get("accepted_counts"),
        "external_rejection_count": report.get("external_rejection_count"),
        "superseded_count": report.get("superseded_count"),
        "reconciliation_ok": (report.get("reconciliation") or {}).get("ok"),
        "verification_ok": report.get("verification_ok"),
        "failed_table": report.get("failed_table", failed_table),
        "failed_row_index": report.get("failed_row_index", failed_row_index),
        "error": report.get("error"),
        "report": str(report_path),
    }
    print(json.dumps(summary, indent=2, default=str))
    return 0 if report.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
