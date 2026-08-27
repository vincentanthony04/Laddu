"""Historical data coverage, lineage and readiness contracts.

The service does not claim history that is not physically present.  It records
what the local candle store actually contains and exposes explicit promotion
readiness for long-horizon research.  This is the backend contract used by
incremental backfill and future feature-cache invalidation.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import math
from typing import Any, Dict, Optional

from core.db_utils import canonical_interval

HISTORICAL_DATA_CONTRACT_VERSION = "historical-data-readiness-1.0.0"
DEFAULT_RESEARCH_YEARS = 10
PREFERRED_RESEARCH_YEARS = 15
EXPECTED_TRADING_DAYS_PER_YEAR = 252

_SCHEMA = """
CREATE TABLE IF NOT EXISTS historical_data_manifest (
  instrument_key TEXT NOT NULL,
  interval TEXT NOT NULL,
  first_ts TEXT,
  last_ts TEXT,
  row_count INTEGER NOT NULL DEFAULT 0,
  null_ohlc_count INTEGER NOT NULL DEFAULT 0,
  distinct_source_count INTEGER NOT NULL DEFAULT 0,
  sources_json TEXT NOT NULL DEFAULT '[]',
  last_received_at TEXT,
  span_years REAL NOT NULL DEFAULT 0,
  expected_rows_10y INTEGER NOT NULL DEFAULT 2520,
  coverage_ratio_10y REAL NOT NULL DEFAULT 0,
  ready_10y INTEGER NOT NULL DEFAULT 0,
  ready_15y INTEGER NOT NULL DEFAULT 0,
  data_version TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(instrument_key, interval)
);
CREATE INDEX IF NOT EXISTS ix_historical_manifest_ready
  ON historical_data_manifest(interval, ready_10y, span_years, row_count);

CREATE TABLE IF NOT EXISTS historical_backfill_state (
  instrument_key TEXT NOT NULL,
  interval TEXT NOT NULL,
  target_years INTEGER NOT NULL,
  target_start_date TEXT NOT NULL,
  next_to_date TEXT,
  state TEXT NOT NULL,
  windows_completed INTEGER NOT NULL DEFAULT 0,
  rows_saved INTEGER NOT NULL DEFAULT 0,
  last_error TEXT,
  last_attempt_at TEXT,
  completed_at TEXT,
  contract_version TEXT NOT NULL,
  PRIMARY KEY(instrument_key, interval)
);

CREATE TABLE IF NOT EXISTS feature_cache_manifest (
  instrument_key TEXT NOT NULL,
  interval TEXT NOT NULL,
  feature_name TEXT NOT NULL,
  feature_version TEXT NOT NULL,
  input_first_ts TEXT,
  input_last_ts TEXT,
  valid_through TEXT,
  dependency_hash TEXT NOT NULL,
  row_count INTEGER NOT NULL DEFAULT 0,
  state TEXT NOT NULL,
  computed_at TEXT NOT NULL,
  PRIMARY KEY(instrument_key, interval, feature_name, feature_version)
);
CREATE INDEX IF NOT EXISTS ix_feature_cache_validity
  ON feature_cache_manifest(interval, feature_name, state, valid_through);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse(value: Any) -> Optional[datetime]:
    try:
        text = str(value or "").replace("Z", "+00:00")
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except Exception:
        return None


def ensure_historical_data_schema(conn: Any) -> None:
    conn.executescript(_SCHEMA)
    conn.commit()


def refresh_historical_manifest(conn: Any, instrument_key: str, interval: str) -> Dict[str, Any]:
    key = str(instrument_key or "").strip()
    norm = canonical_interval(interval)
    if not key:
        raise ValueError("instrument_key is required")
    row = conn.execute(
        """SELECT COUNT(*) AS n, MIN(ts) AS first_ts, MAX(ts) AS last_ts,
                  SUM(CASE WHEN open IS NULL OR high IS NULL OR low IS NULL OR close IS NULL THEN 1 ELSE 0 END) AS null_ohlc,
                  COUNT(DISTINCT COALESCE(source,'')) AS source_count,
                  GROUP_CONCAT(DISTINCT COALESCE(source,'')) AS sources,
                  MAX(received_at) AS last_received_at
             FROM candles WHERE instrument_key=? AND interval=?""",
        (key, norm),
    ).fetchone()
    count = int((row["n"] if row else 0) or 0)
    first_ts = row["first_ts"] if row else None
    last_ts = row["last_ts"] if row else None
    first_dt, last_dt = _parse(first_ts), _parse(last_ts)
    span_years = 0.0
    if first_dt and last_dt and last_dt >= first_dt:
        span_years = (last_dt - first_dt).total_seconds() / (365.2425 * 86400.0)
    expected_10y = DEFAULT_RESEARCH_YEARS * EXPECTED_TRADING_DAYS_PER_YEAR
    ratio = min(1.0, count / expected_10y) if expected_10y else 0.0
    null_count = int((row["null_ohlc"] if row else 0) or 0)
    ready_10y = int(norm == "1d" and span_years >= 9.5 and count >= 2300 and null_count == 0)
    ready_15y = int(norm == "1d" and span_years >= 14.25 and count >= 3500 and null_count == 0)
    sources = [item for item in str((row["sources"] if row else "") or "").split(",") if item]
    stamp = _now()
    conn.execute(
        """INSERT INTO historical_data_manifest(
             instrument_key,interval,first_ts,last_ts,row_count,null_ohlc_count,distinct_source_count,sources_json,
             last_received_at,span_years,expected_rows_10y,coverage_ratio_10y,ready_10y,ready_15y,data_version,updated_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(instrument_key,interval) DO UPDATE SET
             first_ts=excluded.first_ts,last_ts=excluded.last_ts,row_count=excluded.row_count,
             null_ohlc_count=excluded.null_ohlc_count,distinct_source_count=excluded.distinct_source_count,
             sources_json=excluded.sources_json,last_received_at=excluded.last_received_at,span_years=excluded.span_years,
             expected_rows_10y=excluded.expected_rows_10y,coverage_ratio_10y=excluded.coverage_ratio_10y,
             ready_10y=excluded.ready_10y,ready_15y=excluded.ready_15y,data_version=excluded.data_version,
             updated_at=excluded.updated_at""",
        (
            key, norm, first_ts, last_ts, count, null_count, int((row["source_count"] if row else 0) or 0),
            json.dumps(sorted(sources)), row["last_received_at"] if row else None, round(span_years, 6), expected_10y,
            round(ratio, 6), ready_10y, ready_15y, HISTORICAL_DATA_CONTRACT_VERSION, stamp,
        ),
    )
    return {
        "instrument_key": key, "interval": norm, "first": first_ts, "last": last_ts, "count": count,
        "null_ohlc_count": null_count, "sources": sorted(sources), "last_received_at": row["last_received_at"] if row else None,
        "span_years": round(span_years, 3), "coverage_ratio_10y": round(ratio, 4),
        "ready_10y": bool(ready_10y), "ready_15y": bool(ready_15y),
        "contract_version": HISTORICAL_DATA_CONTRACT_VERSION,
    }


class HistoricalDataReadinessService:
    def __init__(self, connection: Any):
        self.conn = connection
        ensure_historical_data_schema(conn=self.conn)

    def readiness(self, instrument_key: str, interval: str = "1d", target_years: int = DEFAULT_RESEARCH_YEARS) -> Dict[str, Any]:
        key = str(instrument_key or "").strip()
        norm = canonical_interval(interval)
        target = max(1, int(target_years or DEFAULT_RESEARCH_YEARS))
        manifest = refresh_historical_manifest(self.conn, key, norm)
        expected_rows = int(target * EXPECTED_TRADING_DAYS_PER_YEAR)
        span_required = max(0.5, target * 0.95)
        row_required = int(expected_rows * 0.90)
        blockers = []
        if norm != "1d" and target >= 10:
            blockers.append("10-15 year readiness is defined for daily history; intraday history uses a separate rolling policy")
        if manifest["span_years"] < span_required:
            blockers.append(f"history span {manifest['span_years']:.2f}y is below required {span_required:.2f}y")
        if manifest["count"] < row_required:
            blockers.append(f"{manifest['count']} candles are below required {row_required}")
        if manifest["null_ohlc_count"]:
            blockers.append(f"{manifest['null_ohlc_count']} candles have incomplete OHLC values")
        ready = not blockers
        return {
            **manifest,
            "target_years": target,
            "preferred_years": PREFERRED_RESEARCH_YEARS,
            "expected_rows": expected_rows,
            "minimum_rows": row_required,
            "minimum_span_years": round(span_required, 2),
            "promotion_ready": ready,
            "blockers": blockers,
            "policy": "history readiness is measured from physically persisted point-in-time candles; no synthetic depth is assumed",
        }

    def mark_backfill(
        self,
        *,
        instrument_key: str,
        interval: str,
        target_years: int,
        target_start_date: str,
        next_to_date: Optional[str],
        state: str,
        rows_saved_delta: int = 0,
        windows_delta: int = 0,
        error: str = "",
    ) -> None:
        norm = canonical_interval(interval)
        stamp = _now()
        self.conn.execute(
            """INSERT INTO historical_backfill_state(
                 instrument_key,interval,target_years,target_start_date,next_to_date,state,windows_completed,rows_saved,
                 last_error,last_attempt_at,completed_at,contract_version
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(instrument_key,interval) DO UPDATE SET
                 target_years=excluded.target_years,target_start_date=excluded.target_start_date,
                 next_to_date=excluded.next_to_date,state=excluded.state,
                 windows_completed=historical_backfill_state.windows_completed+?,
                 rows_saved=historical_backfill_state.rows_saved+?,last_error=excluded.last_error,
                 last_attempt_at=excluded.last_attempt_at,
                 completed_at=CASE WHEN excluded.state='COMPLETE' THEN excluded.last_attempt_at ELSE historical_backfill_state.completed_at END,
                 contract_version=excluded.contract_version""",
            (
                instrument_key, norm, int(target_years), target_start_date, next_to_date, state,
                max(0, int(windows_delta)), max(0, int(rows_saved_delta)), error or None, stamp,
                stamp if state == "COMPLETE" else None, HISTORICAL_DATA_CONTRACT_VERSION,
                max(0, int(windows_delta)), max(0, int(rows_saved_delta)),
            ),
        )
        self.conn.commit()
