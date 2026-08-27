"""Bounded session-scoped persistence for quotes, canonical bars, and live risk.

The runtime database is deliberately separate from the operational decision
ledger and the historical Parquet lake. Accepted market observations are
written here so browser charts, strategy features, risk checks, and restart
recovery read one canonical intraday bar plane without contending with
operational SQLite.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import threading
from typing import Any, Dict, Iterable, List, Optional, Tuple


IST = timezone(timedelta(hours=5, minutes=30))
RUNTIME_SCHEMA_VERSION = "runtime-market-state-3.0.0-binding-mtf"
CANONICAL_BAR_INTERVALS: Tuple[int, ...] = (1, 3, 5, 15, 30, 60, 240)
_INTERVAL_ALIASES = {
    "1": 1, "1m": 1, "1min": 1, "1minute": 1, "minute": 1,
    "3": 3, "3m": 3, "3min": 3, "3minute": 3,
    "5": 5, "5m": 5, "5min": 5, "5minute": 5,
    "15": 15, "15m": 15, "15min": 15, "15minute": 15,
    "30": 30, "30m": 30, "30min": 30, "30minute": 30,
    "60": 60, "60m": 60, "60min": 60, "60minute": 60, "1h": 60, "1hour": 60,
    "240": 240, "240m": 240, "240min": 240, "240minute": 240, "4h": 240, "4hour": 240,
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _float(value: Any) -> Optional[float]:
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _parse_timestamp(value: Any) -> Optional[datetime]:
    try:
        if value in (None, ""):
            return None
        if isinstance(value, (int, float)) or str(value).strip().isdigit():
            raw = float(value)
            if raw > 10**12:
                raw /= 1000.0
            return datetime.fromtimestamp(raw, tz=timezone.utc)
        text = str(value).strip().replace("Z", "+00:00")
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=IST)
        return parsed.astimezone(timezone.utc)
    except Exception:
        return None


def _interval_minutes(interval: Any) -> Optional[int]:
    from core.timeframe import Timeframe, interval_minutes, parse_timeframe
    tf = parse_timeframe(interval)
    if tf in {Timeframe.D1, Timeframe.W1, Timeframe.MN1}:
        return None
    return interval_minutes(tf)


def _session_bucket(provider_time: datetime, minutes: int) -> Optional[Tuple[datetime, datetime]]:
    meta = _session_bucket_meta(provider_time, minutes)
    if meta is None:
        return None
    return meta[0], meta[1]


def _session_bucket_meta(provider_time: datetime, minutes: int) -> Optional[Tuple[datetime, datetime, bool, int]]:
    """Return an NSE-session anchored bucket and explicit partial-bar metadata.

    The regular cash session is 375 minutes, so 30m, 60m and 240m do not all
    divide it evenly.  The final session bucket remains available for display
    and current context, but ``session_partial`` is persisted so pattern and
    promotion logic can exclude it from strict completed-bar structures.
    """
    local = provider_time.astimezone(IST)
    session_start = local.replace(hour=9, minute=15, second=0, microsecond=0)
    session_end = local.replace(hour=15, minute=30, second=0, microsecond=0)
    if local < session_start or local > session_end:
        return None
    elapsed = int((local - session_start).total_seconds() // 60)
    index = max(0, elapsed // minutes)
    start = session_start + timedelta(minutes=index * minutes)
    end = min(start + timedelta(minutes=minutes), session_end)
    span_minutes = max(0, int((end - start).total_seconds() // 60))
    return (
        start.astimezone(timezone.utc),
        end.astimezone(timezone.utc),
        span_minutes < int(minutes),
        span_minutes,
    )


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _lineage(payload: Dict[str, Any]) -> str:
    material = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class RuntimeMarketStateStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._write_lock = threading.RLock()
        bootstrap = self._connect()
        bootstrap.executescript("""
        CREATE TABLE IF NOT EXISTS runtime_session_meta (
          key TEXT PRIMARY KEY, value_json TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS runtime_latest_quotes (
          instrument_key TEXT PRIMARY KEY, symbol TEXT, exchange TEXT, ltp REAL,
          change_pct REAL, volume REAL, provider_ts TEXT, received_at TEXT NOT NULL,
          identity_verified INTEGER NOT NULL DEFAULT 0, payload_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ix_runtime_quotes_symbol
          ON runtime_latest_quotes(symbol, received_at DESC);
        CREATE TABLE IF NOT EXISTS runtime_tick_state (
          instrument_key TEXT PRIMARY KEY, trade_date TEXT NOT NULL,
          last_provider_ts TEXT NOT NULL, last_cumulative_volume REAL,
          last_price REAL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS runtime_canonical_bars (
          instrument_key TEXT NOT NULL, symbol TEXT, exchange TEXT,
          interval TEXT NOT NULL, bar_start TEXT NOT NULL, bar_end TEXT NOT NULL,
          open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL, close REAL NOT NULL,
          volume REAL NOT NULL DEFAULT 0, oi REAL, tick_count INTEGER NOT NULL DEFAULT 0,
          first_provider_ts TEXT, last_provider_ts TEXT, received_at TEXT NOT NULL,
          is_closed INTEGER NOT NULL DEFAULT 0, source TEXT NOT NULL,
          lineage_sha256 TEXT NOT NULL, payload_json TEXT NOT NULL,
          PRIMARY KEY(instrument_key, interval, bar_start)
        );
        CREATE INDEX IF NOT EXISTS ix_runtime_bars_lookup
          ON runtime_canonical_bars(instrument_key, interval, bar_start DESC);
        CREATE INDEX IF NOT EXISTS ix_runtime_bars_symbol
          ON runtime_canonical_bars(symbol, interval, bar_start DESC);
        CREATE TABLE IF NOT EXISTS runtime_risk_state (
          decision_id TEXT PRIMARY KEY, symbol TEXT NOT NULL, mode TEXT NOT NULL,
          state TEXT NOT NULL, last_price REAL, stop_price REAL, target_price REAL,
          updated_at TEXT NOT NULL, payload_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS runtime_events (
          event_id INTEGER PRIMARY KEY AUTOINCREMENT, priority INTEGER NOT NULL,
          event_type TEXT NOT NULL, decision_id TEXT, symbol TEXT,
          occurred_at TEXT NOT NULL, payload_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ix_runtime_events_priority_time
          ON runtime_events(priority, occurred_at DESC);
        """)
        # Existing installations are upgraded in place.  Runtime state is
        # disposable/rebuildable, but the migration remains additive so a
        # restart never requires deleting a healthy current-session database.
        existing_columns = {row[1] for row in bootstrap.execute("PRAGMA table_info(runtime_canonical_bars)").fetchall()}
        for column, ddl in (
            ("session_partial", "ALTER TABLE runtime_canonical_bars ADD COLUMN session_partial INTEGER NOT NULL DEFAULT 0"),
            ("expected_minutes", "ALTER TABLE runtime_canonical_bars ADD COLUMN expected_minutes INTEGER"),
            ("actual_span_minutes", "ALTER TABLE runtime_canonical_bars ADD COLUMN actual_span_minutes INTEGER"),
        ):
            if column not in existing_columns:
                bootstrap.execute(ddl)
        bootstrap.execute(
            "INSERT OR REPLACE INTO runtime_session_meta(key,value_json,updated_at) VALUES(?,?,?)",
            ("schema_version", json.dumps(RUNTIME_SCHEMA_VERSION), _now()),
        )
        bootstrap.commit()
        bootstrap.close()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), check_same_thread=False, timeout=2.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=2000")
        return conn

    @property
    def conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._connect()
            self._local.conn = conn
        return conn

    def save_latest_quotes(self, quotes: Iterable[Dict[str, Any]]) -> int:
        rows = []
        received_at = _now()
        for raw in quotes or ():
            row = dict(raw or {})
            key = str(row.get("instrument_key") or "").strip()
            if not key:
                continue
            rows.append((
                key, row.get("symbol"), row.get("exchange"), row.get("ltp"),
                row.get("change_pct"), row.get("volume") or row.get("volume_traded_today"),
                row.get("provider_timestamp") or row.get("source_time") or row.get("timestamp"),
                received_at, 1 if row.get("identity_verified") else 0,
                json.dumps(row, sort_keys=True, default=str),
            ))
        if not rows:
            return 0
        with self._write_lock:
            self.conn.executemany("""INSERT INTO runtime_latest_quotes
              (instrument_key,symbol,exchange,ltp,change_pct,volume,provider_ts,received_at,
               identity_verified,payload_json) VALUES(?,?,?,?,?,?,?,?,?,?)
              ON CONFLICT(instrument_key) DO UPDATE SET
                symbol=excluded.symbol,exchange=excluded.exchange,ltp=excluded.ltp,
                change_pct=excluded.change_pct,volume=excluded.volume,
                provider_ts=excluded.provider_ts,received_at=excluded.received_at,
                identity_verified=excluded.identity_verified,payload_json=excluded.payload_json""", rows)
            self.conn.commit()
        return len(rows)

    def latest_quotes(self, symbols: Iterable[str] = ()) -> List[Dict[str, Any]]:
        clean = [str(symbol).upper().strip() for symbol in symbols or () if str(symbol).strip()]
        if clean:
            marks = ",".join("?" for _ in clean)
            rows = self.conn.execute(
                f"SELECT payload_json FROM runtime_latest_quotes WHERE UPPER(symbol) IN ({marks}) ORDER BY received_at DESC",
                tuple(clean),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT payload_json FROM runtime_latest_quotes ORDER BY received_at DESC LIMIT 500"
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def ingest_market_observation(self, observation: Dict[str, Any]) -> Dict[str, Any]:
        """Persist one accepted quote and update all canonical intraday bars.

        Buckets are anchored to the NSE 09:15 IST session. The same persisted
        rows are later overlaid onto historical/lake candles for every consumer.
        """
        row = dict(observation or {})
        key = str(row.get("instrument_key") or "").strip()
        price = _float(row.get("ltp") if row.get("ltp") is not None else row.get("close"))
        provider_time = _parse_timestamp(
            row.get("provider_ts_ms") or row.get("provider_timestamp") or row.get("timestamp") or row.get("source_time")
        )
        if not key or price is None or price <= 0 or provider_time is None:
            return {"ok": False, "state": "REJECTED", "reason": "identity_price_or_timestamp_missing"}
        if row.get("identity_verified") is False:
            return {"ok": False, "state": "REJECTED", "reason": "instrument_identity_unverified"}
        local_date = provider_time.astimezone(IST).date().isoformat()
        cumulative_volume = _float(row.get("volume_traded_today") or row.get("volume") or row.get("vtt"))
        prior = self.conn.execute(
            "SELECT trade_date,last_provider_ts,last_cumulative_volume FROM runtime_tick_state WHERE instrument_key=?",
            (key,),
        ).fetchone()
        prior_ts = _parse_timestamp(prior["last_provider_ts"] if prior else None)
        if prior_ts is not None and provider_time < prior_ts:
            return {"ok": False, "state": "REJECTED", "reason": "out_of_order_runtime_tick"}
        # Only an ordered, identity-checked observation may replace restart/LKG
        # quote truth. This prevents a delayed feed packet from rolling back
        # both the visible price and canonical bar state.
        self.save_latest_quotes([row])
        prior_volume = _float(prior["last_cumulative_volume"] if prior and prior["trade_date"] == local_date else None)
        volume_delta = 0.0
        if cumulative_volume is not None:
            volume_delta = max(0.0, cumulative_volume - prior_volume) if prior_volume is not None else 0.0
        provider_iso = _iso(provider_time)
        received_at = _now()
        updated = []
        with self._write_lock:
            for minutes in CANONICAL_BAR_INTERVALS:
                bucket = _session_bucket_meta(provider_time, minutes)
                if bucket is None:
                    continue
                start, end, session_partial, actual_span_minutes = bucket
                interval = f"{minutes}m"
                start_iso, end_iso = _iso(start), _iso(end)
                existing = self.conn.execute(
                    """SELECT open,high,low,close,volume,tick_count,first_provider_ts,source
                         FROM runtime_canonical_bars
                        WHERE instrument_key=? AND interval=? AND bar_start=?""",
                    (key, interval, start_iso),
                ).fetchone()
                open_price = _float(existing["open"] if existing else price) or price
                high = max(_float(existing["high"] if existing else price) or price, price)
                low = min(_float(existing["low"] if existing else price) or price, price)
                volume = (_float(existing["volume"] if existing else 0.0) or 0.0) + volume_delta
                tick_count = int(existing["tick_count"] if existing else 0) + 1
                first_provider = str(existing["first_provider_ts"] if existing else provider_iso)
                source = "upstox_v3_canonical_tick_bar"
                payload = {
                    "instrument_key": key,
                    "symbol": str(row.get("symbol") or key).upper(),
                    "exchange": row.get("exchange") or "NSE",
                    "interval": interval,
                    "timestamp": start_iso,
                    "bar_end": end_iso,
                    "open": open_price, "high": high, "low": low, "close": price,
                    "volume": round(volume, 6), "oi": _float(row.get("oi")),
                    "tick_count": tick_count,
                    "first_provider_timestamp": first_provider,
                    "last_provider_timestamp": provider_iso,
                    "received_at": received_at,
                    "forming": provider_time < end,
                    "is_closed": provider_time >= end,
                    "session_partial": bool(session_partial),
                    "expected_minutes": minutes,
                    "actual_span_minutes": actual_span_minutes,
                    "pattern_eligible": not bool(session_partial),
                    "source": source,
                    "canonical_bar_version": RUNTIME_SCHEMA_VERSION,
                }
                digest = _lineage(payload)
                self.conn.execute("""INSERT INTO runtime_canonical_bars(
                    instrument_key,symbol,exchange,interval,bar_start,bar_end,open,high,low,close,
                    volume,oi,tick_count,first_provider_ts,last_provider_ts,received_at,is_closed,
                    session_partial,expected_minutes,actual_span_minutes,source,lineage_sha256,payload_json
                  ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                  ON CONFLICT(instrument_key,interval,bar_start) DO UPDATE SET
                    symbol=excluded.symbol,exchange=excluded.exchange,bar_end=excluded.bar_end,
                    high=excluded.high,low=excluded.low,close=excluded.close,volume=excluded.volume,
                    oi=excluded.oi,tick_count=excluded.tick_count,last_provider_ts=excluded.last_provider_ts,
                    received_at=excluded.received_at,is_closed=excluded.is_closed,
                    session_partial=excluded.session_partial,expected_minutes=excluded.expected_minutes,
                    actual_span_minutes=excluded.actual_span_minutes,source=excluded.source,
                    lineage_sha256=excluded.lineage_sha256,payload_json=excluded.payload_json""", (
                    key, payload["symbol"], payload["exchange"], interval, start_iso, end_iso,
                    open_price, high, low, price, volume, payload["oi"], tick_count,
                    first_provider, provider_iso, received_at, 1 if payload["is_closed"] else 0,
                    1 if session_partial else 0, minutes, actual_span_minutes,
                    source, digest, json.dumps(payload, sort_keys=True, default=str),
                ))
                # Any earlier bar for this instrument/interval is immutable and closed.
                self.conn.execute(
                    "UPDATE runtime_canonical_bars SET is_closed=1 WHERE instrument_key=? AND interval=? AND bar_end<=?",
                    (key, interval, start_iso),
                )
                updated.append(interval)
            self.conn.execute("""INSERT INTO runtime_tick_state(
                instrument_key,trade_date,last_provider_ts,last_cumulative_volume,last_price,updated_at
              ) VALUES(?,?,?,?,?,?) ON CONFLICT(instrument_key) DO UPDATE SET
                trade_date=excluded.trade_date,last_provider_ts=excluded.last_provider_ts,
                last_cumulative_volume=excluded.last_cumulative_volume,last_price=excluded.last_price,
                updated_at=excluded.updated_at""", (
                key, local_date, provider_iso, cumulative_volume, price, received_at,
            ))
            self.conn.commit()
        return {"ok": True, "state": "UPDATED", "instrument_key": key, "intervals": updated, "provider_timestamp": provider_iso}

    def save_canonical_candles(
        self, instrument_key: str, interval: Any, candles: Iterable[Dict[str, Any]], *, source: str = "upstox_intraday_seed"
    ) -> int:
        """Seed/reconcile runtime bars from official provider candle history."""
        minutes = _interval_minutes(interval)
        key = str(instrument_key or "").strip()
        if not key or minutes not in CANONICAL_BAR_INTERVALS:
            return 0
        now_dt = datetime.now(timezone.utc)
        rows = []
        for raw in candles or ():
            row = dict(raw or {})
            start = _parse_timestamp(row.get("timestamp") or row.get("time") or row.get("date"))
            if start is None:
                continue
            bucket = _session_bucket_meta(start, minutes)
            if bucket is None:
                continue
            bucket_start, bucket_end, session_partial, actual_span_minutes = bucket
            values = {name: _float(row.get(name)) for name in ("open", "high", "low", "close")}
            if any(values[name] is None for name in values):
                continue
            payload = {
                **row,
                "instrument_key": key,
                "symbol": str(row.get("symbol") or "").upper() or None,
                "interval": f"{minutes}m",
                "timestamp": _iso(bucket_start),
                "bar_end": _iso(bucket_end),
                "open": values["open"], "high": values["high"], "low": values["low"], "close": values["close"],
                "volume": _float(row.get("volume")) or 0.0,
                "oi": _float(row.get("oi")),
                "tick_count": int(row.get("tick_count") or row.get("bar_count") or 0),
                "forming": now_dt < bucket_end,
                "is_closed": now_dt >= bucket_end,
                "session_partial": bool(session_partial),
                "expected_minutes": minutes,
                "actual_span_minutes": actual_span_minutes,
                "pattern_eligible": not bool(session_partial),
                "source": source,
                "canonical_bar_version": RUNTIME_SCHEMA_VERSION,
            }
            rows.append((
                key, payload.get("symbol"), row.get("exchange") or "NSE", payload["interval"],
                payload["timestamp"], payload["bar_end"], payload["open"], payload["high"], payload["low"], payload["close"],
                payload["volume"], payload["oi"], payload["tick_count"],
                str(row.get("provider_timestamp") or row.get("timestamp") or payload["timestamp"]),
                str(row.get("provider_timestamp") or row.get("timestamp") or payload["timestamp"]),
                _now(), 1 if payload["is_closed"] else 0, 1 if session_partial else 0,
                minutes, actual_span_minutes, source, _lineage(payload),
                json.dumps(payload, sort_keys=True, default=str),
            ))
        if not rows:
            return 0
        with self._write_lock:
            self.conn.executemany("""INSERT INTO runtime_canonical_bars(
                instrument_key,symbol,exchange,interval,bar_start,bar_end,open,high,low,close,
                volume,oi,tick_count,first_provider_ts,last_provider_ts,received_at,is_closed,
                session_partial,expected_minutes,actual_span_minutes,source,lineage_sha256,payload_json
              ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
              ON CONFLICT(instrument_key,interval,bar_start) DO UPDATE SET
                symbol=COALESCE(excluded.symbol,runtime_canonical_bars.symbol),
                exchange=excluded.exchange,bar_end=excluded.bar_end,open=excluded.open,
                high=excluded.high,low=excluded.low,close=excluded.close,volume=excluded.volume,
                oi=excluded.oi,tick_count=MAX(excluded.tick_count,runtime_canonical_bars.tick_count),
                last_provider_ts=excluded.last_provider_ts,received_at=excluded.received_at,
                is_closed=excluded.is_closed,session_partial=excluded.session_partial,
                expected_minutes=excluded.expected_minutes,actual_span_minutes=excluded.actual_span_minutes,
                source=excluded.source,
                lineage_sha256=excluded.lineage_sha256,payload_json=excluded.payload_json""", rows)
            self.conn.commit()
        return len(rows)

    def canonical_bars(
        self, instrument_key: str, interval: Any, *, limit: int = 2000, include_forming: bool = True
    ) -> List[Dict[str, Any]]:
        minutes = _interval_minutes(interval)
        key = str(instrument_key or "").strip()
        if not key or minutes not in CANONICAL_BAR_INTERVALS:
            return []
        now_iso = _iso(datetime.now(timezone.utc))
        with self._write_lock:
            self.conn.execute(
                "UPDATE runtime_canonical_bars SET is_closed=1 WHERE instrument_key=? AND interval=? AND bar_end<=?",
                (key, f"{minutes}m", now_iso),
            )
            self.conn.commit()
        clause = "" if include_forming else "AND (is_closed=1 OR bar_end<=?)"
        params: Tuple[Any, ...] = (key, f"{minutes}m") if include_forming else (key, f"{minutes}m", now_iso)
        rows = self.conn.execute(
            f"""SELECT payload_json,bar_end,is_closed,session_partial,expected_minutes,actual_span_minutes,lineage_sha256,source FROM runtime_canonical_bars
                 WHERE instrument_key=? AND interval=? {clause}
                 ORDER BY bar_start DESC LIMIT ?""",
            params + (max(1, int(limit)),),
        ).fetchall()
        out = []
        current = datetime.now(timezone.utc)
        for raw in reversed(rows):
            payload = json.loads(raw["payload_json"])
            end = _parse_timestamp(raw["bar_end"])
            closed = bool(raw["is_closed"] or (end is not None and current >= end))
            payload.update({
                "is_closed": closed,
                "forming": not closed,
                "lineage_sha256": raw["lineage_sha256"],
                "source": raw["source"],
                "session_partial": bool(raw["session_partial"]),
                "expected_minutes": raw["expected_minutes"],
                "actual_span_minutes": raw["actual_span_minutes"],
                "pattern_eligible": not bool(raw["session_partial"]),
                "canonical": True,
            })
            out.append(payload)
        return out

    def canonical_bar_health(self, instrument_key: str = "") -> Dict[str, Any]:
        key = str(instrument_key or "").strip()
        where = "WHERE instrument_key=?" if key else ""
        params = (key,) if key else ()
        rows = self.conn.execute(f"""SELECT interval,COUNT(*) AS rows,
               SUM(CASE WHEN is_closed=1 THEN 1 ELSE 0 END) AS closed_rows,
               SUM(CASE WHEN is_closed=0 THEN 1 ELSE 0 END) AS forming_rows,
               SUM(CASE WHEN session_partial=1 THEN 1 ELSE 0 END) AS session_partial_rows,
               COUNT(DISTINCT instrument_key) AS instruments,
               MIN(bar_start) AS first_bar,MAX(bar_start) AS last_bar,
               MAX(received_at) AS last_received_at
          FROM runtime_canonical_bars {where} GROUP BY interval ORDER BY interval""", params).fetchall()
        return {
            "ok": True,
            "schema_version": RUNTIME_SCHEMA_VERSION,
            "database": str(self.path),
            "intervals": [dict(row) for row in rows],
            "canonical_intervals": [f"{value}m" for value in CANONICAL_BAR_INTERVALS],
            "configured_intervals": [f"{value}m" for value in CANONICAL_BAR_INTERVALS],
            "ownership": "accepted Upstox V3 observations + official intraday seed; NSE-session anchored 1m/3m/5m/15m/30m/1H/4H bars for chart, strategy, risk and restart recovery",
            "partial_bar_policy": "final 30m/1H/4H session bucket is labelled session_partial and excluded from strict pattern confirmation",
        }

    def reconcile_derived_bars_from_1m(self) -> Dict[str, Any]:
        """Rebuild every derived intraday timeframe from canonical 1m rows.

        This is intentionally deterministic and network-free.  It repairs old
        installations that had seeded 1m/5m/15m rows but never materialised
        3m/30m/1H/4H.  The same 1m authority used by live ingestion therefore
        owns restart recovery for all higher intraday frames.
        """
        source_rows = self.conn.execute("""SELECT instrument_key,symbol,exchange,bar_start,bar_end,
                   open,high,low,close,volume,oi,tick_count,first_provider_ts,last_provider_ts
              FROM runtime_canonical_bars
             WHERE interval='1m'
             ORDER BY instrument_key,bar_start""").fetchall()
        if not source_rows:
            return {"ok": True, "state": "NO_1M_SOURCE_ROWS", "source_rows": 0, "written_rows": 0, "intervals": {}}

        now_dt = datetime.now(timezone.utc)
        aggregates: Dict[Tuple[str, int, str], Dict[str, Any]] = {}
        for raw in source_rows:
            row = dict(raw)
            start = _parse_timestamp(row.get("bar_start"))
            if start is None:
                continue
            for minutes in CANONICAL_BAR_INTERVALS:
                if minutes == 1:
                    continue
                bucket = _session_bucket_meta(start, minutes)
                if bucket is None:
                    continue
                bucket_start, bucket_end, session_partial, actual_span_minutes = bucket
                bucket_start_iso = _iso(bucket_start)
                key = (str(row.get("instrument_key") or ""), minutes, bucket_start_iso)
                agg = aggregates.get(key)
                volume = _float(row.get("volume")) or 0.0
                oi = _float(row.get("oi"))
                if agg is None:
                    agg = {
                        "instrument_key": key[0],
                        "symbol": row.get("symbol"),
                        "exchange": row.get("exchange") or "NSE",
                        "interval": f"{minutes}m",
                        "bar_start": bucket_start_iso,
                        "bar_end": _iso(bucket_end),
                        "open": _float(row.get("open")),
                        "high": _float(row.get("high")),
                        "low": _float(row.get("low")),
                        "close": _float(row.get("close")),
                        "volume": volume,
                        "oi": oi,
                        "tick_count": int(row.get("tick_count") or 0),
                        "first_provider_ts": row.get("first_provider_ts") or row.get("bar_start"),
                        "last_provider_ts": row.get("last_provider_ts") or row.get("bar_end"),
                        "session_partial": bool(session_partial),
                        "expected_minutes": minutes,
                        "actual_span_minutes": actual_span_minutes,
                        "minute_rows": 1,
                    }
                    aggregates[key] = agg
                else:
                    agg["high"] = max(_float(agg.get("high")) or float("-inf"), _float(row.get("high")) or float("-inf"))
                    agg["low"] = min(_float(agg.get("low")) or float("inf"), _float(row.get("low")) or float("inf"))
                    agg["close"] = _float(row.get("close"))
                    agg["volume"] = (_float(agg.get("volume")) or 0.0) + volume
                    agg["oi"] = oi if oi is not None else agg.get("oi")
                    agg["tick_count"] = int(agg.get("tick_count") or 0) + int(row.get("tick_count") or 0)
                    agg["last_provider_ts"] = row.get("last_provider_ts") or row.get("bar_end")
                    agg["minute_rows"] = int(agg.get("minute_rows") or 0) + 1

        received_at = _now()
        inserts = []
        counts: Dict[str, int] = {}
        for agg in aggregates.values():
            if any(agg.get(name) is None for name in ("open", "high", "low", "close")):
                continue
            end = _parse_timestamp(agg["bar_end"])
            closed = bool(end is not None and now_dt >= end)
            payload = {
                "instrument_key": agg["instrument_key"],
                "symbol": str(agg.get("symbol") or "").upper() or None,
                "exchange": agg.get("exchange") or "NSE",
                "interval": agg["interval"],
                "timestamp": agg["bar_start"],
                "bar_end": agg["bar_end"],
                "open": agg["open"], "high": agg["high"], "low": agg["low"], "close": agg["close"],
                "volume": round(_float(agg.get("volume")) or 0.0, 6),
                "oi": agg.get("oi"),
                "tick_count": int(agg.get("tick_count") or 0),
                "minute_rows": int(agg.get("minute_rows") or 0),
                "first_provider_timestamp": agg.get("first_provider_ts"),
                "last_provider_timestamp": agg.get("last_provider_ts"),
                "received_at": received_at,
                "forming": not closed,
                "is_closed": closed,
                "session_partial": bool(agg.get("session_partial")),
                "expected_minutes": int(agg.get("expected_minutes") or 0),
                "actual_span_minutes": int(agg.get("actual_span_minutes") or 0),
                "pattern_eligible": not bool(agg.get("session_partial")),
                "source": "canonical_1m_reconciliation",
                "canonical_bar_version": RUNTIME_SCHEMA_VERSION,
            }
            digest = _lineage(payload)
            inserts.append((
                payload["instrument_key"], payload.get("symbol"), payload["exchange"], payload["interval"],
                payload["timestamp"], payload["bar_end"], payload["open"], payload["high"], payload["low"], payload["close"],
                payload["volume"], payload["oi"], payload["tick_count"],
                str(payload.get("first_provider_timestamp") or payload["timestamp"]),
                str(payload.get("last_provider_timestamp") or payload["timestamp"]),
                received_at, 1 if closed else 0, 1 if payload["session_partial"] else 0,
                payload["expected_minutes"], payload["actual_span_minutes"], payload["source"], digest,
                json.dumps(payload, sort_keys=True, default=str),
            ))
            counts[payload["interval"]] = counts.get(payload["interval"], 0) + 1

        if inserts:
            with self._write_lock:
                self.conn.executemany("""INSERT INTO runtime_canonical_bars(
                    instrument_key,symbol,exchange,interval,bar_start,bar_end,open,high,low,close,
                    volume,oi,tick_count,first_provider_ts,last_provider_ts,received_at,is_closed,
                    session_partial,expected_minutes,actual_span_minutes,source,lineage_sha256,payload_json
                  ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                  ON CONFLICT(instrument_key,interval,bar_start) DO UPDATE SET
                    symbol=COALESCE(excluded.symbol,runtime_canonical_bars.symbol),
                    exchange=excluded.exchange,bar_end=excluded.bar_end,open=excluded.open,
                    high=excluded.high,low=excluded.low,close=excluded.close,volume=excluded.volume,
                    oi=excluded.oi,tick_count=excluded.tick_count,
                    first_provider_ts=excluded.first_provider_ts,last_provider_ts=excluded.last_provider_ts,
                    received_at=excluded.received_at,is_closed=excluded.is_closed,
                    session_partial=excluded.session_partial,expected_minutes=excluded.expected_minutes,
                    actual_span_minutes=excluded.actual_span_minutes,source=excluded.source,
                    lineage_sha256=excluded.lineage_sha256,payload_json=excluded.payload_json""", inserts)
                self.conn.commit()
        return {
            "ok": True,
            "state": "DERIVED_BARS_RECONCILED",
            "source_rows": len(source_rows),
            "written_rows": len(inserts),
            "intervals": counts,
        }

    def record_risk_state(self, payload: Dict[str, Any]) -> None:
        decision_id = str(payload.get("decision_id") or payload.get("signal_id") or "").strip()
        if not decision_id:
            raise ValueError("decision_id is required")
        with self._write_lock:
            self.conn.execute("""INSERT INTO runtime_risk_state
              (decision_id,symbol,mode,state,last_price,stop_price,target_price,updated_at,payload_json)
              VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(decision_id) DO UPDATE SET
              symbol=excluded.symbol,mode=excluded.mode,state=excluded.state,
              last_price=excluded.last_price,stop_price=excluded.stop_price,
              target_price=excluded.target_price,updated_at=excluded.updated_at,
              payload_json=excluded.payload_json""", (
                decision_id, str(payload.get("symbol") or "").upper(),
                str(payload.get("mode") or "").lower(), str(payload.get("state") or payload.get("status") or "WATCHING"),
                payload.get("last_price") or payload.get("ltp"), payload.get("stop_price") or payload.get("sl"),
                payload.get("target_price") or payload.get("t1"), _now(),
                json.dumps(payload, sort_keys=True, default=str),
            ))
            self.conn.commit()

    def latest_risk_state(self, symbol: str, *, mode: str = "", state: str = "") -> Dict[str, Any] | None:
        clauses, params = ["symbol=?"], [str(symbol or "").upper().strip()]
        if mode:
            clauses.append("mode=?")
            params.append(str(mode).lower())
        if state:
            clauses.append("state=?")
            params.append(str(state).upper())
        row = self.conn.execute(
            "SELECT payload_json FROM runtime_risk_state WHERE " + " AND ".join(clauses) + " ORDER BY updated_at DESC LIMIT 1",
            tuple(params),
        ).fetchone()
        return json.loads(row[0]) if row else None

    def risk_states(self, *, mode: str = "", state: str = "") -> List[Dict[str, Any]]:
        clauses, params = [], []
        if mode:
            clauses.append("mode=?")
            params.append(str(mode).lower())
        if state:
            clauses.append("state=?")
            params.append(str(state).upper())
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        rows = self.conn.execute(
            f"SELECT payload_json FROM runtime_risk_state {where} ORDER BY updated_at DESC LIMIT 1000",
            tuple(params),
        ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def prune(self, *, keep_days: int = 5) -> Dict[str, int]:
        """Bound runtime storage without deleting the structural context needed by higher frames.

        The caller's ``keep_days`` remains the 1m floor.  Wider intervals keep
        progressively longer restart context; durable history still belongs to
        Parquet/DuckDB and operational corrections, not this session store.
        """
        base = max(1, int(keep_days))
        retention = {
            "1m": base, "3m": max(base * 2, 10), "5m": max(base * 3, 15),
            "15m": max(base * 9, 45), "30m": max(base * 18, 90),
            "60m": max(base * 36, 180), "240m": max(base * 73, 365),
        }
        removed: Dict[str, int] = {}
        with self._write_lock:
            for interval, days in retention.items():
                cutoff = _iso(datetime.now(timezone.utc) - timedelta(days=days))
                count = self.conn.execute(
                    "DELETE FROM runtime_canonical_bars WHERE interval=? AND bar_start<?",
                    (interval, cutoff),
                ).rowcount
                removed[f"bars_{interval}"] = max(0, int(count or 0))
            event_cutoff = _iso(datetime.now(timezone.utc) - timedelta(days=max(base, 5)))
            events = self.conn.execute("DELETE FROM runtime_events WHERE occurred_at<?", (event_cutoff,)).rowcount
            self.conn.commit()
        removed["events_removed"] = max(0, int(events or 0))
        removed["bars_removed"] = sum(value for key, value in removed.items() if key.startswith("bars_") and key != "bars_removed")
        return removed
