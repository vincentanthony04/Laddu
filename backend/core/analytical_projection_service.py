"""SQLite outbox and immutable Parquet projections for Project Laddu.

This module is the compatibility/test-mode projection path retained for
regression and rollback tooling. In installed v68 production mode, dedicated
PostgreSQL owns operational transactions and QuestDB owns market time series.
DuckDB remains analytical-only in both modes.
"""
from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
import hashlib
import importlib
import json
from pathlib import Path
import tempfile
import threading
import time
from typing import Any, Deque, Dict, Iterable, Mapping, Optional

from config import DATA_DIR
from core.india_time import INDIA_TZ

SERVICE_VERSION = "sqlite-parquet-duckdb-projection-1.0.0"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _sha(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _sql_literal(value: Path) -> str:
    """Quote a local path for DuckDB statements that reject bound COPY paths."""
    return "'" + str(Path(value).resolve()).replace("\\", "/").replace("'", "''") + "'"


class AnalyticalProjectionService:
    """Own the operational outbox and append-only Parquet projections."""

    def __init__(self, store: Any, *, data_dir: Optional[Path] = None, event_fn=None):
        self.store = store
        self.data_dir = Path(data_dir or DATA_DIR)
        self.root = self.data_dir / "parquet"
        self.event_fn = event_fn or (lambda *args, **kwargs: None)
        self._ticks: Deque[Dict[str, Any]] = deque(maxlen=250_000)
        self._tick_lock = threading.RLock()
        self._status_lock = threading.RLock()
        self._status: Dict[str, Any] = {
            "service_version": SERVICE_VERSION,
            "state": "starting",
            "duckdb_available": None,
            "outbox_pending": 0,
            "tick_queue": 0,
            "durable_tick_pending": 0,
            "last_projection": None,
            "last_error": None,
            "operational_parts": 0,
            "tick_parts": 0,
            "status_refreshed_at": None,
        }
        self._ensure_schema()
        # Seed restart-surviving queue counts once during service construction.
        # Later HTTP/status reads remain strictly database-free.
        self.refresh_status_counts()

    def _emit(self, level: str, message: str, detail: Optional[Dict[str, Any]] = None) -> None:
        try:
            self.event_fn(level, "analytical_projection", message, detail or {})
        except Exception:
            pass

    def _ensure_schema(self) -> None:
        with self.store.write_lock:
            self.store.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS projection_outbox (
                  event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                  aggregate_type TEXT NOT NULL,
                  aggregate_id TEXT NOT NULL,
                  event_type TEXT NOT NULL,
                  payload_json TEXT NOT NULL,
                  source_updated_at TEXT,
                  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                  projected_at TEXT,
                  partition_path TEXT,
                  content_sha256 TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_projection_outbox_pending
                  ON projection_outbox(projected_at,event_id);

                CREATE TABLE IF NOT EXISTS projection_tick_outbox (
                  tick_id INTEGER PRIMARY KEY AUTOINCREMENT,
                  content_sha256 TEXT NOT NULL UNIQUE,
                  instrument_key TEXT NOT NULL,
                  provider_ts_ms INTEGER NOT NULL,
                  trade_date TEXT NOT NULL,
                  payload_json TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  projected_at TEXT,
                  partition_path TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_projection_tick_outbox_pending
                  ON projection_tick_outbox(projected_at,tick_id);

                DROP TRIGGER IF EXISTS trg_model_position_projection_insert;
                CREATE TRIGGER trg_model_position_projection_insert
                AFTER INSERT ON model_portfolio_positions
                BEGIN
                  INSERT INTO projection_outbox(
                    aggregate_type,aggregate_id,event_type,payload_json,source_updated_at
                  ) VALUES (
                    'model_portfolio_position',NEW.position_id,'POSITION_INSERTED',
                    json_object(
                      'position_id',NEW.position_id,'source_signal_id',NEW.source_signal_id,
                      'symbol',NEW.symbol,'mode',NEW.mode,'side',NEW.side,
                      'status',NEW.status,'quantity',NEW.quantity,'notional',NEW.notional,
                      'entry_price',NEW.entry_price,'original_stop',NEW.original_stop,
                      'managed_stop',NEW.managed_stop,'original_target',NEW.original_target,
                      'last_price',NEW.last_price,'exit_price',NEW.exit_price,
                      'gross_pnl',NEW.gross_pnl,'total_cost',NEW.total_cost,
                      'net_pnl',NEW.net_pnl,'economic_outcome',NEW.economic_outcome,
                      'signal_outcome',NEW.signal_outcome,'hit_status',NEW.hit_status,
                      'action',NEW.action,'exit_reason',NEW.exit_reason,
                      'opened_at',NEW.opened_at,'closed_at',NEW.closed_at,
                      'updated_at',NEW.updated_at
                    ),NEW.updated_at
                  );
                END;

                DROP TRIGGER IF EXISTS trg_model_position_projection_update;
                CREATE TRIGGER trg_model_position_projection_update
                AFTER UPDATE ON model_portfolio_positions
                BEGIN
                  INSERT INTO projection_outbox(
                    aggregate_type,aggregate_id,event_type,payload_json,source_updated_at
                  ) VALUES (
                    'model_portfolio_position',NEW.position_id,'POSITION_UPDATED',
                    json_object(
                      'position_id',NEW.position_id,'source_signal_id',NEW.source_signal_id,
                      'symbol',NEW.symbol,'mode',NEW.mode,'side',NEW.side,
                      'status',NEW.status,'quantity',NEW.quantity,'notional',NEW.notional,
                      'entry_price',NEW.entry_price,'original_stop',NEW.original_stop,
                      'managed_stop',NEW.managed_stop,'original_target',NEW.original_target,
                      'last_price',NEW.last_price,'exit_price',NEW.exit_price,
                      'gross_pnl',NEW.gross_pnl,'total_cost',NEW.total_cost,
                      'net_pnl',NEW.net_pnl,'economic_outcome',NEW.economic_outcome,
                      'signal_outcome',NEW.signal_outcome,'hit_status',NEW.hit_status,
                      'action',NEW.action,'exit_reason',NEW.exit_reason,
                      'opened_at',NEW.opened_at,'closed_at',NEW.closed_at,
                      'updated_at',NEW.updated_at
                    ),NEW.updated_at
                  );
                END;
                """
            )
            self.store.conn.commit()
        with self._status_lock:
            self._status["state"] = "ready"

    def record_tick(self, row: Mapping[str, Any]) -> None:
        observation = dict(row or {})
        if not observation.get("instrument_key") or observation.get("ltp") in (None, ""):
            return
        observation.setdefault("archived_at", _now())
        observation.setdefault("schema_version", SERVICE_VERSION)
        with self._tick_lock:
            self._ticks.append(observation)
            queue_size = len(self._ticks)
        with self._status_lock:
            self._status["tick_queue"] = queue_size

    @staticmethod
    def _trade_date(row: Mapping[str, Any]) -> str:
        value = row.get("provider_timestamp") or row.get("created_at") or row.get("archived_at") or _now()
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(INDIA_TZ).date().isoformat()
        except Exception:
            return datetime.now(INDIA_TZ).date().isoformat()

    def _duckdb(self):
        try:
            module = importlib.import_module("duckdb")
            with self._status_lock:
                self._status["duckdb_available"] = True
            return module
        except Exception as exc:
            with self._status_lock:
                self._status.update({"duckdb_available": False, "state": "projection_paused", "last_error": "DUCKDB_UNAVAILABLE"})
            raise RuntimeError("DUCKDB_UNAVAILABLE") from exc

    def _write_parquet(self, rows: Iterable[Mapping[str, Any]], target: Path) -> int:
        material = [dict(row) for row in rows]
        if not material:
            return 0
        duckdb = self._duckdb()
        target.parent.mkdir(parents=True, exist_ok=True)
        # JSONL gives DuckDB an explicit, dependency-free interchange layer.
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".jsonl", delete=False) as handle:
            temp_path = Path(handle.name)
            for row in material:
                handle.write(_canonical(row) + "\n")
        try:
            conn = duckdb.connect(database=":memory:")
            try:
                source_sql = _sql_literal(temp_path)
                target_sql = _sql_literal(target)
                conn.execute(
                    "COPY (SELECT * FROM read_json_auto("
                    + source_sql
                    + ", format='newline_delimited')) TO "
                    + target_sql
                    + " (FORMAT PARQUET, COMPRESSION ZSTD)"
                )
            finally:
                conn.close()
        finally:
            temp_path.unlink(missing_ok=True)
        return len(material)

    def persist_tick_queue(self, limit: int = 20_000) -> Dict[str, Any]:
        """Move the volatile ingress buffer into the durable SQLite outbox."""
        material = []
        with self._tick_lock:
            for _ in range(min(max(1, int(limit)), len(self._ticks))):
                material.append(self._ticks.popleft())
        if not material:
            return {"persisted": 0, "state": "idle"}
        rows = []
        for observation in material:
            provider_ts = int(
                observation.get("provider_ts_ms") or time.time() * 1000
            )
            identity = {
                "instrument_key": str(observation.get("instrument_key") or ""),
                "provider_ts_ms": provider_ts,
                "ltp": observation.get("ltp"),
                "volume": observation.get("volume_traded_today"),
                "bid": observation.get("bid_price"),
                "ask": observation.get("ask_price"),
            }
            rows.append(
                (
                    _sha(identity),
                    identity["instrument_key"],
                    provider_ts,
                    self._trade_date(observation),
                    _canonical(observation),
                    _now(),
                )
            )
        try:
            with self.store.write_lock:
                before = self.store.conn.total_changes
                self.store.conn.executemany(
                    """INSERT OR IGNORE INTO projection_tick_outbox(
                       content_sha256,instrument_key,provider_ts_ms,trade_date,
                       payload_json,created_at
                    ) VALUES(?,?,?,?,?,?)""",
                    rows,
                )
                self.store.conn.commit()
                persisted = self.store.conn.total_changes - before
        except Exception:
            with self._tick_lock:
                for row in reversed(material):
                    self._ticks.appendleft(row)
            raise
        with self._tick_lock:
            remaining = len(self._ticks)
        with self._status_lock:
            self._status["tick_queue"] = remaining
            # INSERT OR IGNORE means persisted may be lower than received, but
            # every accepted unique row now resides in the durable outbox. Keep
            # the last published count monotonic until the worker refreshes the
            # exact SQLite count; HTTP status remains database-free.
            self._status["durable_tick_pending"] = int(self._status.get("durable_tick_pending") or 0) + int(persisted)
        return {
            "persisted": int(persisted),
            "received": len(material),
            "state": "persisted",
        }

    def flush_outbox(self, limit: int = 1000) -> Dict[str, Any]:
        rows = self.store.conn.execute(
            "SELECT * FROM projection_outbox WHERE projected_at IS NULL ORDER BY event_id LIMIT ?",
            (max(1, int(limit)),),
        ).fetchall()
        if not rows:
            return {"projected": 0, "state": "idle"}
        material = []
        for raw in rows:
            row = dict(raw)
            try:
                payload = json.loads(row.pop("payload_json") or "{}")
            except Exception:
                payload = {}
            material.append({**row, "payload": payload, "content_sha256": _sha(payload)})
        day = self._trade_date(material[-1])
        first_id, last_id = int(material[0]["event_id"]), int(material[-1]["event_id"])
        rel = Path("operational_events") / f"trade_date={day}" / f"part-{first_id:012d}-{last_id:012d}.parquet"
        target = self.root / rel
        count = self._write_parquet(material, target)
        projected_at = _now()
        with self.store.write_lock:
            self.store.conn.executemany(
                "UPDATE projection_outbox SET projected_at=?,partition_path=?,content_sha256=? WHERE event_id=? AND projected_at IS NULL",
                [(projected_at, str(rel).replace("\\", "/"), row["content_sha256"], row["event_id"]) for row in material],
            )
            self.store.conn.commit()
        with self._status_lock:
            self._status.update({"state": "ready", "last_projection": projected_at, "operational_parts": self._status.get("operational_parts", 0) + 1, "last_error": None})
        return {"projected": count, "path": str(target), "state": "projected"}

    def flush_ticks(self, limit: int = 20_000) -> Dict[str, Any]:
        self.persist_tick_queue(limit)
        pending = self.store.conn.execute(
            """SELECT * FROM projection_tick_outbox
               WHERE projected_at IS NULL ORDER BY tick_id LIMIT ?""",
            (max(1, int(limit)),),
        ).fetchall()
        if not pending:
            return {"projected": 0, "state": "idle"}
        rows = [dict(row) for row in pending]
        material = []
        for row in rows:
            try:
                payload = json.loads(row.get("payload_json") or "{}")
            except Exception:
                payload = {}
            material.append(dict(payload, tick_outbox_id=int(row["tick_id"])))
        day = str(rows[-1]["trade_date"])
        first_id = int(rows[0]["tick_id"])
        last_id = int(rows[-1]["tick_id"])
        rel = Path("market_ticks") / f"trade_date={day}" / f"part-{first_id:012d}-{last_id:012d}.parquet"
        target = self.root / rel
        count = self._write_parquet(material, target)
        projected_at = _now()
        with self.store.write_lock:
            self.store.conn.executemany(
                """UPDATE projection_tick_outbox
                   SET projected_at=?,partition_path=?
                   WHERE tick_id=? AND projected_at IS NULL""",
                [
                    (projected_at, str(rel).replace("\\", "/"), int(row["tick_id"]))
                    for row in rows
                ],
            )
            self.store.conn.commit()
        remaining = int(
            self.store.conn.execute(
                "SELECT COUNT(*) FROM projection_tick_outbox WHERE projected_at IS NULL"
            ).fetchone()[0]
        )
        with self._status_lock:
            self._status.update({"state": "ready", "last_projection": projected_at, "tick_parts": self._status.get("tick_parts", 0) + 1, "durable_tick_pending": remaining, "last_error": None})
        return {"projected": count, "path": str(target), "state": "projected"}

    def run(self, sup=None, running_fn=lambda: True) -> None:
        while running_fn() and (sup is None or sup.running):
            if sup:
                sup.beat("analytical_projection")
            try:
                self.flush_outbox(1000)
                self.flush_ticks(20_000)
                self.refresh_status_counts()
                time.sleep(1.0 if self._ticks else 5.0)
            except RuntimeError as exc:
                if str(exc) != "DUCKDB_UNAVAILABLE":
                    self._emit("WARN", "Projection paused", {"error": str(exc)[:240]})
                time.sleep(30.0)
            except Exception as exc:
                with self._status_lock:
                    self._status.update({"state": "degraded", "last_error": str(exc)[:300]})
                self._emit("WARN", "Projection failed", {"error": str(exc)[:240]})
                time.sleep(5.0)

    def refresh_status_counts(self) -> Dict[str, Any]:
        """Refresh analytical queue counts on the analytical worker only.

        HTTP/runtime health readers must never execute SQLite COUNT queries.
        Those queries can wait behind the operational writer and previously
        happened while LadduRuntime's global status lock was held, freezing
        /api/health and /api/pipeline-health.
        """
        try:
            pending = int(self.store.conn.execute(
                "SELECT COUNT(*) FROM projection_outbox WHERE projected_at IS NULL"
            ).fetchone()[0])
        except Exception:
            pending = -1
        try:
            durable_ticks = int(self.store.conn.execute(
                "SELECT COUNT(*) FROM projection_tick_outbox WHERE projected_at IS NULL"
            ).fetchone()[0])
        except Exception:
            durable_ticks = -1
        with self._tick_lock:
            tick_queue = len(self._ticks)
        with self._status_lock:
            self._status.update({
                "outbox_pending": pending,
                "tick_queue": tick_queue,
                "durable_tick_pending": durable_ticks,
                "status_refreshed_at": _now(),
            })
            return dict(self._status)

    def status(self) -> Dict[str, Any]:
        """Return the last published projection status without database I/O."""
        with self._tick_lock:
            tick_queue = len(self._ticks)
        with self._status_lock:
            return {**dict(self._status), "tick_queue": tick_queue, "root": str(self.root)}
