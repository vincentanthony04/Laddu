"""Non-blocking persistence boundary for controller/operator audit projections.

The autonomic controller must remain able to diagnose failures even when a
compatibility/audit store is slow.  Control decisions therefore publish to an
in-memory event authority first and enqueue last-known snapshots for this
single isolated writer.  Queue saturation is explicit and never grants product
or trading authority.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from queue import Empty, Full, Queue
import threading
import time
from typing import Any, Callable, Dict

from models import now_iso


@dataclass(frozen=True)
class AuditWrite:
    key: str
    payload: Any
    enqueued_at: str


class ControlAuditWriter:
    VERSION = "control-audit-writer-1.0.0-isolated"

    def __init__(self, store: Any, *, capacity: int = 512):
        self.store = store
        self._queue: Queue[AuditWrite] = Queue(maxsize=max(32, int(capacity)))
        self._lock = threading.RLock()
        self._submitted = 0
        self._written = 0
        self._dropped = 0
        self._failures = 0
        self._last_error = None
        self._last_write_at = None
        self._recent: deque[Dict[str, Any]] = deque(maxlen=100)

    def submit(self, key: str, payload: Any) -> Dict[str, Any]:
        row = AuditWrite(str(key), payload, now_iso())
        try:
            self._queue.put_nowait(row)
            with self._lock:
                self._submitted += 1
            return {"ok": True, "state": "QUEUED", "key": row.key, "depth": self._queue.qsize()}
        except Full:
            # Last-known projections are replaceable audit data, never trading
            # authority. Drop rather than block the controller thread.
            with self._lock:
                self._dropped += 1
            return {"ok": False, "state": "AUDIT_QUEUE_FULL", "key": row.key, "depth": self._queue.qsize()}

    def status(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "version": self.VERSION,
                "queue_depth": self._queue.qsize(),
                "capacity": self._queue.maxsize,
                "submitted": self._submitted,
                "written": self._written,
                "dropped": self._dropped,
                "failures": self._failures,
                "last_error": self._last_error,
                "last_write_at": self._last_write_at,
                "recent": list(self._recent)[-20:],
            }

    def run(self, supervisor: Any = None, *, running_fn: Callable[[], bool] = lambda: True) -> None:
        name = "control_audit_writer"
        while running_fn() and (supervisor is None or supervisor.running):
            if supervisor:
                supervisor.beat(name)
            try:
                row = self._queue.get(timeout=1.0)
            except Empty:
                if supervisor:
                    supervisor.set_expected_idle(name, True, waiting_on="audit queue empty")
                continue
            try:
                if supervisor:
                    supervisor.set_expected_idle(name, False)
                    with supervisor.heartbeat_guard(name):
                        self.store.set_kv(row.key, row.payload)
                else:
                    self.store.set_kv(row.key, row.payload)
                with self._lock:
                    self._written += 1
                    self._last_write_at = now_iso()
                    self._last_error = None
                    self._recent.append({"key": row.key, "state": "WRITTEN", "at": self._last_write_at})
                if supervisor:
                    supervisor.progress(
                        name,
                        token=f"{self._written}:{row.key}",
                        stage="persist_control_audit",
                        current_item=row.key,
                        completed_units=self._written,
                        total_units=None,
                        expected_idle=False,
                    )
            except Exception as exc:
                with self._lock:
                    self._failures += 1
                    self._last_error = f"{type(exc).__name__}: {exc}"[:300]
                    self._recent.append({"key": row.key, "state": "FAILED", "error": self._last_error, "at": now_iso()})
            finally:
                self._queue.task_done()
