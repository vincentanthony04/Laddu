"""
ServiceLogger — v37.4, decentralized logging.

Previously every module funneled through LadduRuntime.event()/record_error(),
which meant (a) every extracted service still needed a live LadduRuntime
instance just to log, re-coupling things we're trying to decouple, and
(b) all log lines landed in one shared backend.log file regardless of which
subsystem produced them, making it hard to isolate e.g. instrument-resolution
noise from engine-dispatch noise.

ServiceLogger removes both problems:
  - Each service constructs its own `ServiceLogger("instrument_resolver")` (etc)
    with only a `store` dependency (for the events table), not the runtime.
  - Log lines are written to a per-service daily file (logs/<name>.log) via
    main.log_line(name=...), which already supported a `name` param that
    nothing was using -- this just plugs it in.
  - `store.event(...)` writes still go to the shared events table so existing
    dashboards/status endpoints that read self.store.events(...) keep working
    unchanged.

This is intentionally tiny: it borrows main.log_line at call time (lazy
import) to avoid a circular import (main.py imports the services that use
this at module load time).
"""
from __future__ import annotations

import json
import time
from typing import Any, Dict, Optional


class ServiceLogger:
    def __init__(self, name: str, store=None):
        self.name = name
        self.store = store
        self._recent_errors: list[Dict[str, Any]] = []

    # ------------------------------------------------------------- event
    def event(self, level: str, message: str, detail: Optional[Dict[str, Any]] = None) -> None:
        detail = detail or {}
        try:
            from core.runtime_logging import log_line
            log_line(f"{level} [{self.name}] {message} {json.dumps(detail)}", name=self.name)
        except Exception:
            pass
        if self.store is not None:
            try:
                self.store.event(level, self.name, message, detail)
            except Exception:
                pass

    def info(self, message: str, detail: Optional[Dict[str, Any]] = None) -> None:
        self.event("INFO", message, detail)

    def warn(self, message: str, detail: Optional[Dict[str, Any]] = None) -> None:
        self.event("WARN", message, detail)

    # --------------------------------------------------------- error record
    def record_error(self, error: str, endpoint: Optional[str] = None) -> None:
        item = {"time": time.time(), "module": self.name, "error": str(error)}
        if endpoint:
            item["endpoint"] = endpoint
        self._recent_errors = (self._recent_errors[-7:]) + [item]
        self.warn("error recorded", {"error": str(error)[:220], "endpoint": endpoint})

    def recent_errors(self, limit: int = 7) -> list[Dict[str, Any]]:
        return self._recent_errors[-limit:]
