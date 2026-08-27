from __future__ import annotations

"""Bounded independent background repair dispatcher for Clean Core.

Interactive reads may request enrichment, but they never own provider work.
The dispatcher coalesces duplicate repair keys and uses a small bounded daemon
queue, so a stuck provider call can degrade repair capacity without consuming
unbounded threads or blocking quote/chart/Stock Report requests.
"""

from dataclasses import dataclass
import queue
import threading
from typing import Callable, Any


@dataclass(frozen=True)
class RepairSubmitResult:
    accepted: bool
    state: str
    key: str


class BackgroundRepairDispatcher:
    VERSION = "clean-core-background-repair-1.0.0"

    def __init__(self, *, workers: int = 2, queue_size: int = 32):
        self._queue: queue.Queue[tuple[str, Callable[[], Any]]] = queue.Queue(maxsize=max(1, int(queue_size)))
        self._keys: set[str] = set()
        self._lock = threading.Lock()
        self._workers = []
        for index in range(max(1, int(workers))):
            thread = threading.Thread(target=self._run, name=f"clean-core-repair-{index + 1}", daemon=True)
            thread.start()
            self._workers.append(thread)

    def submit(self, key: str, fn: Callable[[], Any]) -> RepairSubmitResult:
        token = str(key or "").strip()
        if not token or not callable(fn):
            return RepairSubmitResult(False, "INVALID", token)
        with self._lock:
            if token in self._keys:
                return RepairSubmitResult(False, "COALESCED", token)
            self._keys.add(token)
        try:
            self._queue.put_nowait((token, fn))
            return RepairSubmitResult(True, "QUEUED", token)
        except queue.Full:
            with self._lock:
                self._keys.discard(token)
            return RepairSubmitResult(False, "CAPACITY_DEFERRED", token)

    def _run(self) -> None:
        while True:
            token, fn = self._queue.get()
            try:
                fn()
            except Exception:
                # Repair failure is intentionally contained. The owning data
                # service records provider/storage details; foreground reads do
                # not inherit background exceptions.
                pass
            finally:
                with self._lock:
                    self._keys.discard(token)
                self._queue.task_done()

    def status(self) -> dict[str, Any]:
        with self._lock:
            keys = len(self._keys)
        return {
            "version": self.VERSION,
            "state": "READY",
            "workers": len(self._workers),
            "pending_or_running": keys,
            "queue_depth": self._queue.qsize(),
            "queue_capacity": self._queue.maxsize,
        }


_creation_lock = threading.Lock()


def for_app(app: Any) -> BackgroundRepairDispatcher:
    current = getattr(app, "_clean_core_repair_dispatcher", None)
    if isinstance(current, BackgroundRepairDispatcher):
        return current
    with _creation_lock:
        current = getattr(app, "_clean_core_repair_dispatcher", None)
        if not isinstance(current, BackgroundRepairDispatcher):
            current = BackgroundRepairDispatcher()
            setattr(app, "_clean_core_repair_dispatcher", current)
        return current
