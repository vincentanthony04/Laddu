from __future__ import annotations

from collections import deque, OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
import base64
import json
import math
import threading
import time
from typing import Any, Deque, Mapping, Iterable
from urllib import parse, request


def _esc_measurement(value: Any) -> str:
    return str(value or "unknown").replace("\\", "\\\\").replace(" ", "\\ ").replace(",", "\\,")


def _esc_tag(value: Any) -> str:
    return _esc_measurement(value).replace("=", "\\=")


def _str_field(value: Any) -> str:
    return '"' + str(value or "").replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


def _num(value: Any) -> float | None:
    try:
        out = float(value)
        return out if math.isfinite(out) else None
    except Exception:
        return None


def _timestamp_ns(value: Any) -> int | None:
    """Return an ILP timestamp in nanoseconds, or ``None`` when unverified.

    Market timestamps are evidence.  Missing, malformed, non-positive, or
    timezone-naive values must never be replaced with the local clock because
    doing so fabricates event chronology and makes replay/audit unreliable.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        dt = value
        if dt.tzinfo is None:
            return None
        return int(dt.timestamp() * 1_000_000_000)
    if isinstance(value, (int, float)):
        if not math.isfinite(float(value)):
            return None
        number = int(value)
        if number <= 0:
            return None
        if number > 10**17:
            return number
        if number > 10**14:
            return number * 1000
        if number > 10**11:
            return number * 1_000_000
        return number * 1_000_000_000
    text = str(value or "").strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (TypeError, ValueError, OverflowError):
        return None
    if dt.tzinfo is None:
        return None
    return int(dt.timestamp() * 1_000_000_000)


def _timestamp_us(value: Any) -> int | None:
    """Return a QuestDB non-designated TIMESTAMP field in microseconds."""
    timestamp_ns = _timestamp_ns(value)
    return None if timestamp_ns is None else timestamp_ns // 1000


@dataclass(frozen=True)
class QuestDBProbe:
    ok: bool
    latency_ms: float
    error: str | None

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "latency_ms": round(self.latency_ms, 3), "error": self.error}


class QuestDBMicroBatchWriter:
    """Bounded-loss asynchronous QuestDB ILP/HTTP writer.

    The accepted live observation path only enqueues. Network I/O occurs on a
    dedicated worker and never blocks quote handling, risk checks, or UI reads.
    Each HTTP request contains rows for exactly one table, preserving QuestDB's
    documented single-table transaction boundary. Failed batches are requeued
    in original order and deduplication keys make retries idempotent.
    """

    TABLES = ("market_ticks", "market_bars", "market_data_quality_events")

    def __init__(self, base_url: str, *, username: str | None = None, password: str | None = None,
                 flush_ms: int = 250, batch_size: int = 1000, capacity: int = 250_000):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.flush_seconds = max(0.05, flush_ms / 1000.0)
        self.batch_size = max(10, batch_size)
        self.capacity = max(self.batch_size, capacity)
        self._queues: dict[str, Deque[tuple[float, str]]] = {name: deque() for name in self.TABLES}
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._status = {
            "state": "stopped", "queued": 0, "accepted": 0, "written": 0,
            "dropped": 0, "failed_batches": 0, "last_flush_at": None,
            "last_error": None, "max_queue_age_ms": 0.0,
        }
        self._read_cache: OrderedDict[tuple[str, str, int], tuple[float, list[dict[str, Any]]]] = OrderedDict()
        self._read_cache_ttl_sec = 2.0
        self._read_cache_capacity = 512
        self._read_cache_hits = 0
        self._read_cache_misses = 0

    def _queued_total_locked(self) -> int:
        return sum(len(queue) for queue in self._queues.values())

    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="QuestDBMicroBatchWriter", daemon=True)
            self._status["state"] = "starting"
            self._thread.start()

    def close(self, *, flush_timeout: float = 5.0) -> bool:
        """Stop the writer only after a bounded, explicit durability drain.

        Returning ``False`` means queued observations remain.  The state stays
        degraded and exposes the exact remaining count; shutdown must not claim
        success while silently abandoning market evidence.
        """
        deadline = time.monotonic() + max(0.0, float(flush_timeout))
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread:
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
            if thread.is_alive():
                with self._lock:
                    remaining = self._queued_total_locked()
                    self._status.update({
                        "state": "degraded",
                        "last_error": f"QUESTDB_WRITER_SHUTDOWN_TIMEOUT:{remaining}",
                        "queued": remaining,
                    })
                return False

        while time.monotonic() < deadline:
            with self._lock:
                remaining = self._queued_total_locked()
            if remaining == 0:
                break
            written = self.flush(max_batches=max(1, len(self.TABLES)))
            if written <= 0:
                break

        with self._lock:
            remaining = self._queued_total_locked()
            if remaining:
                self._status.update({
                    "state": "degraded",
                    "last_error": f"QUESTDB_SHUTDOWN_DRAIN_INCOMPLETE:{remaining}",
                    "queued": remaining,
                })
                return False
            self._status.update({"state": "stopped", "queued": 0})
            return True

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "text/plain; charset=utf-8"}
        if self.username is not None:
            token = base64.b64encode(f"{self.username}:{self.password or ''}".encode()).decode()
            headers["Authorization"] = f"Basic {token}"
        return headers

    def _enqueue(self, table: str, line: str) -> bool:
        if table not in self._queues:
            raise ValueError(f"unsupported QuestDB table: {table}")
        self.start()
        with self._lock:
            total = self._queued_total_locked()
            if total >= self.capacity:
                self._status["dropped"] += 1
                self._status["state"] = "degraded"
                self._status["last_error"] = "QUESTDB_QUEUE_CAPACITY_EXCEEDED"
                return False
            self._queues[table].append((time.monotonic(), line))
            self._status["accepted"] += 1
            self._status["queued"] = total + 1
            table_size = len(self._queues[table])
        if table_size >= self.batch_size:
            self._wake.set()
        return True

    def enqueue_tick(self, row: Mapping[str, Any]) -> bool:
        key = str(row.get("instrument_key") or "").strip()
        ltp = _num(row.get("ltp") if row.get("ltp") is not None else row.get("close"))
        canonical_sequence = int(row.get("canonical_sequence") or 0)
        provider_ts = _timestamp_ns(
            row.get("provider_ts_ms") or row.get("provider_timestamp") or row.get("timestamp")
        )
        received_ts = _timestamp_us(
            row.get("received_at") or row.get("received_ts")
            or row.get("provider_ts_ms") or row.get("provider_timestamp") or row.get("timestamp")
        )
        if (not key or ltp is None or canonical_sequence <= 0
                or row.get("identity_verified") is not True
                or provider_ts is None or received_ts is None):
            return False
        tags = [f"instrument_key={_esc_tag(key)}"]
        for name in ("exchange", "symbol", "quality_state", "replay_state"):
            value = row.get(name)
            if value not in (None, ""):
                tags.append(f"{name}={_esc_tag(value)}")
        fields = [f"ltp={ltp}", f"canonical_sequence={canonical_sequence}i"]
        field_map = {
            "last_quantity": row.get("last_quantity") or row.get("ltq"),
            "cumulative_volume": row.get("volume_traded_today") or row.get("volume"),
            "bid": row.get("bid"), "ask": row.get("ask"), "open_interest": row.get("oi"),
        }
        for name, value in field_map.items():
            number = _num(value)
            if number is not None:
                fields.append(f"{name}={number}")
        provider_sequence = int(row.get("provider_sequence") or 0)
        if provider_sequence > 0:
            fields.append(f"provider_sequence={provider_sequence}i")
        fields.append("identity_verified=true")
        fields.append(f"received_ts={received_ts}t")
        fields.append(f"payload_json={_str_field(json.dumps(dict(row), separators=(',', ':'), default=str))}")
        line = f"market_ticks,{','.join(tags)} {','.join(fields)} {provider_ts}"
        return self._enqueue("market_ticks", line)

    def enqueue_bar(self, row: Mapping[str, Any]) -> bool:
        key = str(row.get("instrument_key") or "").strip()
        interval = str(row.get("interval") or "").strip()
        close = _num(row.get("close"))
        bar_start_ts = _timestamp_us(row.get("bar_start_ts") or row.get("start_ts"))
        bar_end_ts = _timestamp_ns(row.get("bar_end_ts") or row.get("ts") or row.get("end_ts"))
        if not key or not interval or close is None or bar_start_ts is None or bar_end_ts is None:
            return False
        tags = [f"instrument_key={_esc_tag(key)}", f"interval={_esc_tag(interval)}"]
        for name in ("source", "quality_state", "universe_revision"):
            value = row.get(name)
            if value not in (None, ""):
                tags.append(f"{name}={_esc_tag(value)}")
        fields: list[str] = []
        for name in ("open", "high", "low", "close", "volume", "oi"):
            number = _num(row.get(name))
            if number is not None:
                fields.append(f"{'open_interest' if name == 'oi' else name}={number}")
        fields.append(f"tick_count={int(row.get('tick_count') or 0)}i")
        fields.append(f"is_closed={'true' if row.get('is_closed') else 'false'}")
        fields.append(f"is_partial_session_bar={'true' if row.get('is_partial_session_bar') else 'false'}")
        fields.append(f"bar_start_ts={bar_start_ts}t")
        line = f"market_bars,{','.join(tags)} {','.join(fields)} {bar_end_ts}"
        return self._enqueue("market_bars", line)

    def enqueue_quality_event(self, row: Mapping[str, Any]) -> bool:
        event_type = str(row.get("event_type") or "").strip()
        event_ts = _timestamp_ns(row.get("event_ts") or row.get("timestamp"))
        if not event_type or event_ts is None:
            return False
        tags = [f"event_type={_esc_tag(event_type)}"]
        key = str(row.get("instrument_key") or "").strip()
        if key:
            tags.append(f"instrument_key={_esc_tag(key)}")
        fields: list[str] = []
        for name in ("source_sequence", "canonical_sequence", "gap_size"):
            value = int(row.get(name) or 0)
            fields.append(f"{name}={value}i")
        fields.append(f"detail={_str_field(row.get('detail') or '')}")
        line = f"market_data_quality_events,{','.join(tags)} {','.join(fields)} {event_ts}"
        return self._enqueue("market_data_quality_events", line)

    def _oldest_table_locked(self) -> str | None:
        candidates = [(queue[0][0], table) for table, queue in self._queues.items() if queue]
        return min(candidates)[1] if candidates else None

    def flush(self, *, max_batches: int = 1) -> int:
        written = 0
        for _ in range(max(1, int(max_batches))):
            with self._lock:
                table = self._oldest_table_locked()
                if table is None:
                    self._status["queued"] = 0
                    break
                queue = self._queues[table]
                batch = [queue.popleft() for _ in range(min(self.batch_size, len(queue)))]
                self._status["queued"] = self._queued_total_locked()
            body = ("\n".join(line for _, line in batch) + "\n").encode("utf-8")
            req = request.Request(self.base_url + "/write?precision=n", data=body, method="POST", headers=self._headers())
            try:
                with request.urlopen(req, timeout=3) as response:
                    if response.status not in (200, 204):
                        raise RuntimeError(f"HTTP_{response.status}")
                written += len(batch)
                max_age_ms = max(0.0, (time.monotonic() - min(stamp for stamp, _ in batch)) * 1000.0)
                with self._lock:
                    self._status.update({
                        "state": "ready",
                        "written": self._status["written"] + len(batch),
                        "last_flush_at": datetime.now(timezone.utc).isoformat(),
                        "last_error": None,
                        "max_queue_age_ms": max(float(self._status.get("max_queue_age_ms") or 0.0), max_age_ms),
                    })
                    if table == "market_bars":
                        self._read_cache.clear()
            except Exception as exc:
                with self._lock:
                    queue = self._queues[table]
                    for item in reversed(batch):
                        if self._queued_total_locked() < self.capacity:
                            queue.appendleft(item)
                        else:
                            self._status["dropped"] += 1
                    self._status["failed_batches"] += 1
                    self._status["state"] = "degraded"
                    self._status["last_error"] = f"{type(exc).__name__}: {exc}"[:240]
                    self._status["queued"] = self._queued_total_locked()
                break
        return written

    def _run(self) -> None:
        with self._lock:
            self._status["state"] = "ready"
        while not self._stop.is_set():
            self._wake.wait(self.flush_seconds)
            self._wake.clear()
            self.flush(max_batches=max(4, len(self.TABLES)))
        self.flush(max_batches=100)

    @staticmethod
    def _sql_literal(value: Any) -> str:
        return "'" + str(value or "").replace("'", "''") + "'"

    def _recent_cache_get(self, cache_key: tuple[str, str, int]) -> list[dict[str, Any]] | None:
        with self._lock:
            cached = self._read_cache.get(cache_key)
            if cached is None or time.monotonic() - cached[0] > self._read_cache_ttl_sec:
                if cached is not None:
                    self._read_cache.pop(cache_key, None)
                self._read_cache_misses += 1
                return None
            self._read_cache.move_to_end(cache_key)
            self._read_cache_hits += 1
            return [dict(row) for row in cached[1]]

    def _recent_cache_put(self, cache_key: tuple[str, str, int], rows: list[dict[str, Any]]) -> None:
        with self._lock:
            self._read_cache[cache_key] = (time.monotonic(), [dict(row) for row in rows])
            self._read_cache.move_to_end(cache_key)
            while len(self._read_cache) > self._read_cache_capacity:
                self._read_cache.popitem(last=False)

    def recent_bars(self, instrument_key: str, interval: str, limit: int = 2000) -> list[dict[str, Any]]:
        """Read durable current/recent bars with a very short restart cache."""
        key = str(instrument_key or "").strip()
        norm = str(interval or "").strip()
        cap = max(1, min(int(limit), 5000))
        if not key or not norm:
            return []
        cache_key = (key, norm, cap)
        cached = self._recent_cache_get(cache_key)
        if cached is not None:
            return cached
        sql = (
            "SELECT bar_start_ts,bar_end_ts,open,high,low,close,volume,open_interest,"
            "tick_count,is_closed,is_partial_session_bar,source,quality_state,universe_revision "
            "FROM market_bars WHERE instrument_key=" + self._sql_literal(key)
            + " AND interval=" + self._sql_literal(norm)
            + f" ORDER BY bar_end_ts DESC LIMIT {cap}"
        )
        query = parse.urlencode({"query": sql})
        req = request.Request(self.base_url + "/exec?" + query, headers=self._headers())
        try:
            with request.urlopen(req, timeout=1.5) as response:
                if response.status != 200:
                    raise RuntimeError(f"HTTP_{response.status}")
                payload = json.loads(response.read().decode("utf-8") or "{}")
            if payload.get("error"):
                raise RuntimeError(str(payload.get("error")))
            columns = payload.get("columns") or []
            names = [str(column.get("name") if isinstance(column, dict) else column) for column in columns]
            rows: list[dict[str, Any]] = []
            for raw in payload.get("dataset") or []:
                if not isinstance(raw, list):
                    continue
                item = dict(zip(names, raw))
                item.update({
                    "instrument_key": key,
                    "interval": norm,
                    "timestamp": item.get("bar_start_ts"),
                    "bar_start_ts": item.get("bar_start_ts"),
                    "bar_end_ts": item.get("bar_end_ts"),
                    "oi": item.pop("open_interest", None),
                    "session_partial": bool(item.get("is_partial_session_bar")),
                    "storage_plane": "questdb_market_authority",
                })
                rows.append(item)
            rows.reverse()
            self._recent_cache_put(cache_key, rows)
            return [dict(row) for row in rows]
        except Exception as exc:
            with self._lock:
                self._status["last_read_error"] = f"{type(exc).__name__}: {exc}"[:240]
            return []

    def recent_bars_many(
        self,
        instrument_key: str,
        intervals: Iterable[str],
        limit: int = 2000,
    ) -> dict[str, list[dict[str, Any]]]:
        """Bounded multi-timeframe read with shared short-lived cache.

        QuestDB stays the recent-session authority; this helper prevents every
        consumer from independently repeating the same HTTP read fan-out.
        """
        out: dict[str, list[dict[str, Any]]] = {}
        for interval in dict.fromkeys(str(value or "").strip() for value in intervals or []):
            if interval:
                out[interval] = self.recent_bars(instrument_key, interval, limit)
        return out

    def probe(self) -> QuestDBProbe:
        start = time.perf_counter()
        try:
            query = parse.urlencode({"query": "select table_name from tables() where table_name in ('market_ticks','market_bars','market_data_quality_events')"})
            req = request.Request(self.base_url + "/exec?" + query, headers=self._headers())
            with request.urlopen(req, timeout=3) as response:
                if response.status != 200:
                    raise RuntimeError(f"HTTP_{response.status}")
                payload = json.loads(response.read().decode("utf-8") or "{}")
            rows = payload.get("dataset") or []
            found = {str(row[0]) for row in rows if isinstance(row, list) and row}
            missing = set(self.TABLES) - found
            if missing:
                raise RuntimeError("REQUIRED_TABLE_MISSING:" + ",".join(sorted(missing)))
            return QuestDBProbe(True, (time.perf_counter() - start) * 1000, None)
        except Exception as exc:
            return QuestDBProbe(False, (time.perf_counter() - start) * 1000, f"{type(exc).__name__}: {exc}"[:240])

    def status(self) -> dict[str, Any]:
        with self._lock:
            out = dict(self._status)
            out["queued"] = self._queued_total_locked()
            out["queued_by_table"] = {table: len(queue) for table, queue in self._queues.items()}
            oldest = [queue[0][0] for queue in self._queues.values() if queue]
            out["current_oldest_queue_age_ms"] = round(
                max(0.0, (time.monotonic() - min(oldest)) * 1000.0), 3
            ) if oldest else 0.0
        out.update({
            "service_version": "questdb-microbatch-1.2.0-strict-time-and-drain",
            "flush_ms": int(self.flush_seconds * 1000),
            "batch_size": self.batch_size,
            "capacity": self.capacity,
            "url": self.base_url,
            "read_cache": {
                "entries": len(self._read_cache),
                "hits": self._read_cache_hits,
                "misses": self._read_cache_misses,
                "ttl_sec": self._read_cache_ttl_sec,
            },
        })
        return out
