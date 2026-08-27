from __future__ import annotations

"""Bounded local read-model materialization with reserved interactive capacity.

Project Laddu still has only two asynchronous ownership classes:

* local projection over already-authoritative local state,
* provider/repair work (owned by background_repair_dispatcher).

Within the local-projection class, chart/overlay work owns a tiny reserved worker
pool.  PriorityQueue ordering alone cannot protect an interactive request after
all workers have already started expensive technical projections, which was the
remaining Candidate 17 starvation mode on the full retained Windows dataset.
The separation is scheduling capacity only; it creates no second data authority.
"""

from dataclasses import dataclass
import itertools
import queue
import threading
from typing import Any, Callable, Dict

VERSION = "local-projection-dispatcher-2.2.0-bounded-six-projection-workers"

PRIORITY_CHART = 0
PRIORITY_TECHNICAL = 10
PRIORITY_PERSISTENCE = 30
PRIORITY_ENRICHMENT = 60
PRIORITY_BACKGROUND = 90
INTERACTIVE_PRIORITY_MAX = PRIORITY_CHART + 2


@dataclass(frozen=True)
class ProjectionSubmitResult:
    accepted: bool
    state: str
    key: str


class LocalProjectionDispatcher:
    """Coalesced bounded local materializer with two reserved scheduling pools."""

    def __init__(
        self, *, workers: int = 8, queue_limit: int = 512, interactive_workers: int = 2
    ):
        total = max(3, min(10, int(workers)))
        reserved = max(1, min(int(interactive_workers), total - 1))
        self._workers = total
        self._interactive_workers = reserved
        self._projection_workers = total - reserved
        self._interactive_queue: queue.PriorityQueue[tuple[int, int, str, Callable[[], Any]]] = queue.PriorityQueue(
            maxsize=max(32, min(128, int(queue_limit) // 4))
        )
        self._projection_queue: queue.PriorityQueue[tuple[int, int, str, Callable[[], Any]]] = queue.PriorityQueue(
            maxsize=max(64, int(queue_limit))
        )
        self._seq = itertools.count()
        self._lock = threading.RLock()
        self._inflight: set[str] = set()
        self._active: set[str] = set()
        self._active_interactive: set[str] = set()
        self._submitted = 0
        self._completed = 0
        self._errors = 0
        for index in range(self._interactive_workers):
            threading.Thread(
                target=self._worker,
                args=(self._interactive_queue, True),
                name=f"laddu-local-interactive-{index + 1}",
                daemon=True,
            ).start()
        for index in range(self._projection_workers):
            threading.Thread(
                target=self._worker,
                args=(self._projection_queue, False),
                name=f"laddu-local-projection-{index + 1}",
                daemon=True,
            ).start()

    def _worker(
        self,
        work_queue: queue.PriorityQueue[tuple[int, int, str, Callable[[], Any]]],
        interactive: bool,
    ) -> None:
        while True:
            _priority, _seq, token, fn = work_queue.get()
            try:
                with self._lock:
                    self._active.add(token)
                    if interactive:
                        self._active_interactive.add(token)
                fn()
            except Exception:
                with self._lock:
                    self._errors += 1
            finally:
                with self._lock:
                    self._active.discard(token)
                    self._active_interactive.discard(token)
                    self._inflight.discard(token)
                    self._completed += 1
                work_queue.task_done()

    @staticmethod
    def _is_interactive(priority: int) -> bool:
        return int(priority) <= INTERACTIVE_PRIORITY_MAX

    def submit(
        self, key: str, fn: Callable[[], Any], *, priority: int = PRIORITY_BACKGROUND
    ) -> ProjectionSubmitResult:
        token = str(key or "").strip()
        if not token or not callable(fn):
            return ProjectionSubmitResult(False, "INVALID", token)
        priority_i = int(priority)
        with self._lock:
            if token in self._inflight:
                return ProjectionSubmitResult(False, "COALESCED", token)
            self._inflight.add(token)
            self._submitted += 1
        target = self._interactive_queue if self._is_interactive(priority_i) else self._projection_queue
        try:
            target.put_nowait((priority_i, next(self._seq), token, fn))
            return ProjectionSubmitResult(True, "QUEUED", token)
        except queue.Full:
            with self._lock:
                self._inflight.discard(token)
            return ProjectionSubmitResult(False, "CAPACITY_DEFERRED", token)
        except Exception:
            with self._lock:
                self._inflight.discard(token)
                self._errors += 1
            return ProjectionSubmitResult(False, "CAPACITY_DEFERRED", token)

    def high_priority_pending(self) -> bool:
        """True while chart/technical work is queued or executing.

        Enrichment producers use this as admission control so first-ready stocks
        cannot occupy workers needed by the remaining selected-stock wave.
        """
        with self._lock:
            active = any(
                token.startswith(("chart-page:", "chart-overlay:", "technical-"))
                for token in self._active
            )
            queued = any(
                item[0] <= PRIORITY_TECHNICAL
                for item in list(self._projection_queue.queue) + list(self._interactive_queue.queue)
            )
            return bool(active or queued)

    def status(self) -> Dict[str, Any]:
        with self._lock:
            interactive_active = len(self._active_interactive)
            return {
                "version": VERSION,
                "state": "READY",
                "workers": self._workers,
                "interactive_workers": self._interactive_workers,
                "projection_workers": self._projection_workers,
                "queued": self._interactive_queue.qsize() + self._projection_queue.qsize(),
                "interactive_queued": self._interactive_queue.qsize(),
                "projection_queued": self._projection_queue.qsize(),
                "active": len(self._active),
                "interactive_active": interactive_active,
                "projection_active": max(0, len(self._active) - interactive_active),
                "pending_or_running": len(self._inflight),
                "submitted": self._submitted,
                "completed": self._completed,
                "errors": self._errors,
                "policy": "ONE_LOCAL_AUTHORITY_CLASS_RESERVED_INTERACTIVE_CAPACITY_NO_PROVIDER_IO",
            }


_creation_lock = threading.Lock()


def for_app(app: Any) -> LocalProjectionDispatcher:
    current = getattr(app, "_local_projection_dispatcher", None)
    if isinstance(current, LocalProjectionDispatcher):
        return current
    with _creation_lock:
        current = getattr(app, "_local_projection_dispatcher", None)
        if not isinstance(current, LocalProjectionDispatcher):
            current = LocalProjectionDispatcher(workers=8, queue_limit=512, interactive_workers=2)
            setattr(app, "_local_projection_dispatcher", current)
        return current
