"""Non-reentrant scan-lane execution and manual-trigger coalescing."""
from __future__ import annotations

import threading
import time
from typing import Any, Callable, Dict, Optional

COORDINATOR_VERSION = "scan-lane-coordinator-1.0.0"


class ScanLaneCoordinator:
    """Guarantee at most one in-flight task per named lane.

    Supervised cadence and manual refreshes share the same lane lock. Manual
    triggers are coalesced instead of creating parallel scanner authorities.
    """

    def __init__(self, publish: Optional[Callable[[Dict[str, Any]], None]] = None):
        self._meta_lock = threading.RLock()
        self._locks: Dict[str, threading.Lock] = {}
        self._state: Dict[str, Dict[str, Any]] = {}
        self._pending_async: set[str] = set()
        self._publish = publish or (lambda snapshot: None)

    def _lock_for(self, lane: str) -> threading.Lock:
        with self._meta_lock:
            return self._locks.setdefault(lane, threading.Lock())

    def _update(self, lane: str, **changes: Any) -> None:
        with self._meta_lock:
            row = self._state.setdefault(lane, {
                "lane": lane,
                "state": "idle",
                "runs": 0,
                "coalesced": 0,
                "last_started_at": None,
                "last_completed_at": None,
                "last_error": None,
            })
            row.update(changes)
            snapshot = self.snapshot()
        self._publish(snapshot)

    def execute(self, lane: str, fn: Callable[[], Any]) -> Any:
        lock = self._lock_for(lane)
        if not lock.acquire(blocking=False):
            with self._meta_lock:
                current = self._state.setdefault(lane, {"lane": lane, "runs": 0, "coalesced": 0})
                current["coalesced"] = int(current.get("coalesced") or 0) + 1
                current["state"] = "running_coalesced"
                snapshot = self.snapshot()
            self._publish(snapshot)
            return {"ok": True, "state": "coalesced", "lane": lane, "reason": "lane already running"}
        started = time.time()
        self._update(lane, state="running", last_started_at=started, last_error=None)
        try:
            result = fn()
            with self._meta_lock:
                runs = int(self._state.get(lane, {}).get("runs") or 0) + 1
            self._update(lane, state="idle", runs=runs, last_completed_at=time.time())
            return result
        except Exception as exc:
            self._update(lane, state="error", last_error=str(exc)[:300], last_completed_at=time.time())
            raise
        finally:
            lock.release()

    def request_async(self, lane: str, fn: Callable[[], Any]) -> Dict[str, Any]:
        """Schedule one asynchronous run or coalesce into an existing request."""
        lock = self._lock_for(lane)
        with self._meta_lock:
            if lane in self._pending_async or lock.locked():
                row = self._state.setdefault(lane, {"lane": lane, "runs": 0, "coalesced": 0})
                row["coalesced"] = int(row.get("coalesced") or 0) + 1
                row["state"] = "queued_coalesced"
                snapshot = self.snapshot()
                self._publish(snapshot)
                return {"ok": True, "state": "coalesced", "lane": lane}
            self._pending_async.add(lane)
            row = self._state.setdefault(lane, {"lane": lane, "runs": 0, "coalesced": 0})
            row["state"] = "queued"
            snapshot = self.snapshot()
        self._publish(snapshot)

        def runner() -> None:
            with self._meta_lock:
                self._pending_async.discard(lane)
            self.execute(lane, fn)

        threading.Thread(target=runner, name=f"Requested-{lane}", daemon=True).start()
        return {"ok": True, "state": "queued", "lane": lane}

    def snapshot(self) -> Dict[str, Any]:
        with self._meta_lock:
            return {
                "version": COORDINATOR_VERSION,
                "lanes": {name: dict(row) for name, row in sorted(self._state.items())},
                "pending_async": sorted(self._pending_async),
            }
