from __future__ import annotations

"""Direct Parquet authority for exchange delivery/participation history.

Bulk EOD delivery files never enter the operational PostgreSQL service and
never write the legacy SQLite compatibility database. Parts are content
addressed, partitioned by trade date, and atomically published.
"""

from collections import defaultdict
from datetime import datetime, timedelta, timezone
import hashlib
import importlib
import json
from pathlib import Path
import tempfile
import threading
from typing import Any, Dict, Iterable, List, Mapping

from config import DATA_DIR
from core.reference_data_repository import ReferenceDataRepository
from core.db_utils import to_float
from models import now_iso


class DeliveryLakeRepository:
    SERVICE_VERSION = "delivery-parquet-authority-1.0.0"

    def __init__(self, data_dir: Path | None = None):
        self.root = Path(data_dir or DATA_DIR) / "parquet" / "delivery"
        self._lock = threading.RLock()
        self._memory: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self._parts_written = 0
        self._rows_written = 0

    @staticmethod
    def _normalise(raw: Mapping[str, Any], *, source: str, forced_date: str | None = None) -> dict[str, Any] | None:
        row = dict(raw or {})
        symbol = str(row.get("symbol") or row.get("SYMBOL") or row.get(" Symbol") or "").upper().strip()
        if not symbol:
            return None
        date_raw = forced_date or row.get("trade_date") or row.get("DATE1") or row.get(" Date") or row.get("TIMESTAMP") or now_iso()[:10]
        trade_date = ReferenceDataRepository._normalize_trade_date(str(date_raw).strip())
        traded = to_float(row.get("traded_qty") or row.get("TTL_TRD_QNTY") or row.get(" TTL_TRD_QNTY"))
        deliverable = to_float(row.get("deliverable_qty") or row.get("DELIV_QTY") or row.get(" DELIV_QTY"))
        pct = to_float(row.get("delivery_pct") or row.get("DELIV_PER") or row.get(" DELIV_PER"))
        if pct is None and traded and deliverable is not None and traded > 0:
            pct = round((deliverable / traded) * 100.0, 6)
        return {
            "symbol": symbol,
            "exchange": str(row.get("exchange") or "NSE").upper(),
            "trade_date": trade_date,
            "traded_qty": traded,
            "deliverable_qty": deliverable,
            "delivery_pct": pct,
            "close": to_float(row.get("close") or row.get("CLOSE_PRICE") or row.get(" CLOSE_PRICE")),
            "source": source,
            "raw_json": json.dumps(row, sort_keys=True, default=str),
            "published_at": datetime.now(timezone.utc).isoformat(),
            "schema_version": DeliveryLakeRepository.SERVICE_VERSION,
        }

    @staticmethod
    def _canonical(rows: Iterable[Mapping[str, Any]]) -> str:
        return "\n".join(json.dumps(dict(row), sort_keys=True, separators=(",", ":"), default=str) for row in rows)

    @classmethod
    def _content_digest(cls, rows: list[dict[str, Any]]) -> str:
        # Publication time is metadata, not business content. Excluding it makes
        # retries idempotent while a genuine correction still creates a new part.
        stable = [{key: value for key, value in row.items() if key != "published_at"} for row in rows]
        return hashlib.sha256(cls._canonical(stable).encode("utf-8")).hexdigest()

    def _write_partition(self, trade_date: str, rows: list[dict[str, Any]]) -> tuple[Path, bool]:
        material = sorted(rows, key=lambda row: (row["symbol"], row["exchange"]))
        digest = self._content_digest(material)
        target = self.root / f"trade_date={trade_date}" / f"part-{digest}.parquet"
        if target.exists():
            return target, False
        target.parent.mkdir(parents=True, exist_ok=True)
        duckdb = importlib.import_module("duckdb")
        with tempfile.TemporaryDirectory(prefix="laddu_delivery_") as temp_dir:
            temp = Path(temp_dir)
            jsonl = temp / "rows.jsonl"
            staging = temp / "part.parquet"
            jsonl.write_text(self._canonical(material) + "\n", encoding="utf-8")
            source = str(jsonl.resolve()).replace("\\", "/").replace("'", "''")
            destination = str(staging.resolve()).replace("\\", "/").replace("'", "''")
            conn = duckdb.connect(database=":memory:")
            try:
                conn.execute(
                    f"COPY (SELECT * FROM read_json_auto('{source}', format='newline_delimited')) "
                    f"TO '{destination}' (FORMAT PARQUET, COMPRESSION ZSTD)"
                )
            finally:
                conn.close()
            staging.replace(target)
        return target, True

    def save_delivery_rows(self, rows: List[Dict[str, Any]], source: str = "nse_delivery") -> int:
        normalised = [self._normalise(row, source=source) for row in rows or []]
        material = [row for row in normalised if row]
        if not material:
            return 0
        by_date: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in material:
            by_date[row["trade_date"]].append(row)
        with self._lock:
            for trade_date, date_rows in by_date.items():
                _, created = self._write_partition(trade_date, date_rows)
                if created:
                    self._parts_written += 1
            for row in material:
                bucket = self._memory[row["symbol"]]
                bucket[:] = [old for old in bucket if old.get("trade_date") != row["trade_date"]]
                bucket.append(row)
                bucket.sort(key=lambda item: item.get("trade_date") or "", reverse=True)
                del bucket[500:]
            self._rows_written += len(material)
        return len(material)

    def save_delivery_data(self, trade_date: str, rows: List[Dict[str, Any]]) -> int:
        enriched = [dict(row, trade_date=trade_date) for row in rows or []]
        return self.save_delivery_rows(enriched, source="nse_archive")

    def _query_lake(self, symbol: str, limit: int) -> list[dict[str, Any]]:
        paths = list(self.root.glob("trade_date=*/part-*.parquet"))
        if not paths:
            return []
        duckdb = importlib.import_module("duckdb")
        pattern = str((self.root / "trade_date=*" / "part-*.parquet").resolve()).replace("\\", "/").replace("'", "''")
        conn = duckdb.connect(database=":memory:")
        try:
            rows = conn.execute(
                f"SELECT symbol,exchange,trade_date,traded_qty,deliverable_qty,delivery_pct,close,source "
                f"FROM read_parquet('{pattern}', union_by_name=true) WHERE upper(symbol)=? "
                f"QUALIFY row_number() OVER (PARTITION BY trade_date,symbol ORDER BY published_at DESC)=1 "
                f"ORDER BY trade_date DESC LIMIT ?",
                [symbol.upper(), int(limit)],
            ).fetchall()
            names = [desc[0] for desc in conn.description]
            return [dict(zip(names, row)) for row in rows]
        finally:
            conn.close()

    def liquidity_ranked_symbols(
        self,
        *,
        limit: int = 1500,
        lookback_days: int = 60,
        min_avg_turnover: float = 50_000_000.0,
        min_observations: int = 10,
    ) -> List[str]:
        """Rank desk eligibility directly from the Parquet/DuckDB authority.

        The active scanner/universe path must not read the compatibility
        SQLite delivery table.  Duplicate/corrected parts are resolved by the
        latest published row per trade date and symbol before turnover is
        calculated.
        """
        paths = list(self.root.glob("trade_date=*/part-*.parquet"))
        if not paths:
            return []
        duckdb = importlib.import_module("duckdb")
        pattern = str((self.root / "trade_date=*" / "part-*.parquet").resolve()).replace("\\", "/").replace("'", "''")
        conn = duckdb.connect(database=":memory:")
        try:
            max_date = conn.execute(
                f"SELECT max(CAST(trade_date AS DATE)) FROM read_parquet('{pattern}', union_by_name=true)"
            ).fetchone()[0]
            if max_date is None:
                return []
            cutoff = max_date - timedelta(days=max(1, int(lookback_days)))
            rows = conn.execute(
                f"""WITH typed AS (
                       SELECT upper(CAST(symbol AS VARCHAR)) AS symbol,
                              CAST(trade_date AS DATE) AS trade_date,
                              TRY_CAST(traded_qty AS DOUBLE) AS traded_qty_num,
                              TRY_CAST(close AS DOUBLE) AS close_num,
                              TRY_CAST(published_at AS TIMESTAMPTZ) AS published_ts
                       FROM read_parquet('{pattern}', union_by_name=true)
                     ), ranked AS (
                       SELECT *, row_number() OVER (
                         PARTITION BY trade_date, symbol
                         ORDER BY published_ts DESC NULLS LAST
                       ) AS rn
                       FROM typed
                     ), eligible AS (
                       SELECT symbol, traded_qty_num * close_num AS turnover
                       FROM ranked
                       WHERE rn=1
                         AND trade_date >= CAST(? AS DATE)
                         AND traded_qty_num IS NOT NULL AND close_num IS NOT NULL
                         AND traded_qty_num > 0 AND close_num > 0
                     )
                     SELECT symbol, avg(turnover) AS avg_turnover, count(*) AS observations
                     FROM eligible
                     GROUP BY symbol
                     HAVING count(*) >= ? AND avg(turnover) >= ?
                     ORDER BY avg_turnover DESC
                     LIMIT ?""",
                [cutoff, int(min_observations), float(min_avg_turnover), int(limit)],
            ).fetchall()
            return [str(row[0] or "").upper().strip() for row in rows if row and row[0]]
        finally:
            conn.close()

    def latest_delivery(self, symbol: str, limit: int = 20) -> List[Dict[str, Any]]:
        clean = str(symbol or "").upper().strip()
        with self._lock:
            memory = [dict(row) for row in self._memory.get(clean, [])[: int(limit)]]
        if memory:
            return memory
        try:
            return self._query_lake(clean, limit)
        except Exception:
            return []

    def get_delivery_data(self, symbol: str, days: int = 10) -> List[Dict[str, Any]]:
        return self.latest_delivery(symbol, days)

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "service_version": self.SERVICE_VERSION,
                "state": "ready",
                "root": str(self.root),
                "parts_written": self._parts_written,
                "rows_written": self._rows_written,
                "memory_symbols": len(self._memory),
            }
