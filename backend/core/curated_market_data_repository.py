"""Lake-first candle reads for the live service.

QuestDB/PostgreSQL remain the production authorities for new market and operational
state. Completed research history is served from the curated Parquet lake through
DuckDB. Any SQLite surface is a rebuildable compatibility projection and never a
production fallback.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import threading
import time
from typing import Any, Dict, List, Optional

from core.db_utils import canonical_interval, canonical_timestamp
from core.storage_layout import StorageLayout


REPOSITORY_VERSION = "curated-market-data-repository-1.0.0"


class CuratedMarketDataRepository:
    def __init__(self, data_dir: Path):
        self.layout = StorageLayout.from_data_dir(Path(data_dir))
        self._lock = threading.RLock()
        self._availability: Dict[str, Any] = {"checked_at": 0.0, "available": False, "reason": "not_checked"}

    def _duckdb(self):
        import duckdb
        return duckdb

    def _available(self) -> bool:
        now = time.monotonic()
        with self._lock:
            if now - float(self._availability.get("checked_at") or 0.0) < 15.0:
                return bool(self._availability.get("available"))
            try:
                if not self.layout.analytics_db.exists():
                    raise FileNotFoundError("analytics DuckDB does not exist")
                duckdb = self._duckdb()
                db = duckdb.connect(str(self.layout.analytics_db), read_only=True)
                try:
                    row = db.execute(
                        "SELECT COUNT(*) FROM information_schema.views WHERE table_name='curated_candles'"
                    ).fetchone()
                    available = bool(row and int(row[0] or 0) > 0)
                    reason = "ready" if available else "curated_candles view missing"
                finally:
                    db.close()
            except Exception as exc:
                available = False
                reason = str(exc)[:240]
            self._availability = {"checked_at": now, "available": available, "reason": reason}
            return available

    @staticmethod
    def _rows(cursor) -> List[Dict[str, Any]]:
        columns = [item[0] for item in (cursor.description or [])]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]

    def get_candles(self, instrument_key: str, interval: Any, limit: int = 2000) -> List[Dict[str, Any]]:
        key = str(instrument_key or "").strip()
        norm = canonical_interval(interval)
        if not key or not self._available():
            return []
        duckdb = self._duckdb()
        db = duckdb.connect(str(self.layout.analytics_db), read_only=True)
        try:
            cursor = db.execute(
                """SELECT ts AS timestamp,open,high,low,close,volume,oi,source,
                          provider_ts AS provider_timestamp,received_at,
                          'parquet_duckdb_curated' AS storage_plane
                     FROM curated_candles
                    WHERE instrument_key=? AND interval=?
                    ORDER BY ts DESC LIMIT ?""",
                [key, norm, max(1, int(limit))],
            )
            rows = self._rows(cursor)
            rows.reverse()
            return rows
        except Exception:
            return []
        finally:
            db.close()

    def get_candles_before(self, instrument_key: str, interval: Any, before: Any, limit: int = 2000) -> List[Dict[str, Any]]:
        key = str(instrument_key or "").strip()
        norm = canonical_interval(interval)
        before_ts = canonical_timestamp(before, norm)
        if not key or not before_ts or not self._available():
            return []
        duckdb = self._duckdb()
        db = duckdb.connect(str(self.layout.analytics_db), read_only=True)
        try:
            cursor = db.execute(
                """SELECT ts AS timestamp,open,high,low,close,volume,oi,source,
                          provider_ts AS provider_timestamp,received_at,
                          'parquet_duckdb_curated' AS storage_plane
                     FROM curated_candles
                    WHERE instrument_key=? AND interval=? AND ts<?
                    ORDER BY ts DESC LIMIT ?""",
                [key, norm, before_ts, max(1, int(limit))],
            )
            rows = self._rows(cursor)
            rows.reverse()
            return rows
        except Exception:
            return []
        finally:
            db.close()

    def candle_coverage(self, instrument_key: str, interval: Any) -> Dict[str, Any]:
        key = str(instrument_key or "").strip()
        norm = canonical_interval(interval)
        if not key or not self._available():
            return {"count": 0, "first": None, "last": None, "source": "lake_unavailable"}
        duckdb = self._duckdb()
        db = duckdb.connect(str(self.layout.analytics_db), read_only=True)
        try:
            row = db.execute(
                """SELECT COUNT(*) AS n,MIN(ts) AS first_ts,MAX(ts) AS last_ts,
                          MAX(received_at) AS last_received_at
                     FROM curated_candles WHERE instrument_key=? AND interval=?""",
                [key, norm],
            ).fetchone()
            return {
                "count": int(row[0] or 0), "first": row[1], "last": row[2],
                "last_received_at": row[3], "source": "parquet_duckdb_curated",
            }
        except Exception:
            return {"count": 0, "first": None, "last": None, "source": "lake_query_failed"}
        finally:
            db.close()


    def price_snapshots(self, *, symbol: str = "", instrument_key: str = "", limit: int = 1000,
                        start: str = "", end: str = "") -> List[Dict[str, Any]]:
        if not self._available():
            return []
        clauses, params = [], []
        if instrument_key:
            clauses.append("instrument_key=?")
            params.append(str(instrument_key))
        if symbol:
            clauses.append("UPPER(symbol)=?")
            params.append(str(symbol).upper())
        if start:
            clauses.append("captured_at>=?")
            params.append(str(start))
        if end:
            clauses.append("captured_at<=?")
            params.append(str(end))
        if not clauses:
            return []
        duckdb = self._duckdb()
        db = duckdb.connect(str(self.layout.analytics_db), read_only=True)
        try:
            cursor = db.execute(
                f"""SELECT instrument_key,captured_at,symbol,exchange,ltp,change_pct,
                           provider_ts,received_at,source,
                           'parquet_duckdb_curated' AS storage_plane
                      FROM curated_price_snapshots
                     WHERE {' AND '.join(clauses)}
                     ORDER BY captured_at DESC LIMIT ?""",
                [*params, max(1, int(limit))],
            )
            return self._rows(cursor)
        except Exception:
            return []
        finally:
            db.close()

    def recent_daily_candles_many(self, instrument_keys: List[str], limit_per_key: int = 25) -> Dict[str, List[Dict[str, Any]]]:
        keys = list(dict.fromkeys(str(value or "").strip() for value in instrument_keys or [] if str(value or "").strip()))
        if not keys or not self._available():
            return {key: [] for key in keys}
        duckdb = self._duckdb()
        db = duckdb.connect(str(self.layout.analytics_db), read_only=True)
        try:
            marks = ",".join("?" for _ in keys)
            cap = max(2, min(60, int(limit_per_key or 25)))
            cursor = db.execute(f"""
                SELECT instrument_key,ts AS timestamp,close,volume,source,provider_ts AS provider_timestamp,received_at
                FROM (
                  SELECT *,ROW_NUMBER() OVER(PARTITION BY instrument_key ORDER BY ts DESC) AS rn
                  FROM curated_candles
                  WHERE interval='1d' AND instrument_key IN ({marks})
                ) WHERE rn<=? ORDER BY instrument_key,ts
            """, [*keys, cap])
            out: Dict[str, List[Dict[str, Any]]] = {key: [] for key in keys}
            for row in self._rows(cursor):
                row["storage_plane"] = "parquet_duckdb_curated"
                out.setdefault(str(row.get("instrument_key") or ""), []).append(row)
            return out
        except Exception:
            return {key: [] for key in keys}
        finally:
            db.close()

    def status(self) -> Dict[str, Any]:
        self._available()
        manifest_path = self.layout.manifests_dir / "market-lake.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception:
            manifest = {"state": "NOT_SYNCED"}
        return {
            "ok": True,
            "repository_version": REPOSITORY_VERSION,
            "available": bool(self._availability.get("available")),
            "reason": self._availability.get("reason"),
            "analytics_db": str(self.layout.analytics_db),
            "manifest": manifest,
            "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        }
