from __future__ import annotations

"""Bounded in-memory operator event buffer for the production runtime.

Operational trading truth is PostgreSQL and market chronology is QuestDB.
Human-readable runtime diagnostics must not synchronously write the research /
compatibility SQLite projection, because a bulk research transaction could then
block a live-market, stop-monitor, or readiness thread merely while it logs.
The durable text log remains the forensic record; this buffer serves recent UI
queries without entering any database writer path.
"""

from collections import deque
from datetime import datetime, timezone
import threading
from typing import Any, Deque, Dict, Iterable


class RuntimeEventBuffer:
    SERVICE_VERSION = "runtime-event-buffer-1.0.0"

    def __init__(self, *, capacity: int = 10_000):
        self.capacity = max(100, int(capacity))
        self._rows: Deque[Dict[str, Any]] = deque(maxlen=self.capacity)
        self._lock = threading.RLock()

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")

    def append(self, level: str, module: str, message: str, detail: Dict[str, Any] | None = None) -> None:
        row = {
            "level": str(level or "INFO").upper(),
            "module": str(module or "runtime"),
            "message": str(message or ""),
            "detail": dict(detail or {}),
            "timestamp": self._now(),
        }
        with self._lock:
            self._rows.append(row)

    def recent(self, limit: int = 80) -> list[Dict[str, Any]]:
        cap = max(1, min(int(limit), self.capacity))
        with self._lock:
            rows: Iterable[Dict[str, Any]] = list(self._rows)[-cap:]
            return [dict(row, detail=dict(row.get("detail") or {})) for row in reversed(list(rows))]

    def status(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "service_version": self.SERVICE_VERSION,
                "state": "ready",
                "rows": len(self._rows),
                "capacity": self.capacity,
                "durability": "text_log_only",
                "database_writes": 0,
            }
