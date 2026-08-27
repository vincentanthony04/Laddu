"""In-memory HTTP latency truth for customer trust projection.

No SQL, provider, filesystem or repair work is performed here.  Request
handlers record completed wall-clock latency and trading surfaces read a bounded
rolling window.  This turns a buried slow-GET log into explicit customer trust
state without making the health path depend on the thing it is measuring.
"""
from __future__ import annotations

from collections import defaultdict, deque
import math
import threading
import time
from urllib.parse import urlsplit
from typing import Any, Deque, Dict


class HttpLatencyMonitor:
    VERSION = "http-latency-monitor-1.0.0"
    MAX_SAMPLES = 120

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._samples: Dict[str, Deque[tuple[float, float]]] = defaultdict(
            lambda: deque(maxlen=self.MAX_SAMPLES)
        )

    @staticmethod
    def _key(path: str) -> str:
        try:
            return urlsplit(str(path or "")).path or "/"
        except Exception:
            return str(path or "/").split("?", 1)[0]

    def record(self, method: str, path: str, elapsed_ms: float) -> None:
        key = f"{str(method or 'GET').upper()} {self._key(path)}"
        value = max(0.0, float(elapsed_ms or 0.0))
        with self._lock:
            self._samples[key].append((time.monotonic(), value))

    @staticmethod
    def _percentile(values: list[float], percentile: float) -> float | None:
        if not values:
            return None
        ordered = sorted(values)
        if len(ordered) == 1:
            return ordered[0]
        position = (len(ordered) - 1) * max(0.0, min(1.0, percentile))
        lo = int(math.floor(position)); hi = int(math.ceil(position))
        if lo == hi:
            return ordered[lo]
        weight = position - lo
        return ordered[lo] * (1.0 - weight) + ordered[hi] * weight

    def route(self, path: str, *, method: str = "GET", window_sec: float = 300.0) -> Dict[str, Any]:
        key = f"{str(method).upper()} {self._key(path)}"
        cutoff = time.monotonic() - max(10.0, float(window_sec or 300.0))
        with self._lock:
            raw = list(self._samples.get(key) or ())
        values = [ms for stamp, ms in raw if stamp >= cutoff]
        return {
            "route": key,
            "samples": len(values),
            "last_ms": round(values[-1], 1) if values else None,
            "p50_ms": round(self._percentile(values, 0.50), 1) if values else None,
            "p95_ms": round(self._percentile(values, 0.95), 1) if values else None,
            "max_ms": round(max(values), 1) if values else None,
            "window_sec": int(window_sec),
        }

    def trading_snapshot(self) -> Dict[str, Any]:
        routes = {
            "stock_snapshot": self.route("/api/stock-snapshot"),
            "chart": self.route("/api/chart-data"),
            "live_chart": self.route("/api/live-chart-bar"),
            "workspace": self.route("/api/trader-workspace"),
        }
        measured = [
            row.get("p95_ms") for key, row in routes.items()
            if key in {"stock_snapshot", "chart", "live_chart"} and row.get("p95_ms") is not None
        ]
        return {
            "version": self.VERSION,
            "routes": routes,
            "customer_read_p95_ms": round(max(measured), 1) if measured else None,
            "customer_read_samples": sum(int(routes[key].get("samples") or 0) for key in ("stock_snapshot", "chart", "live_chart")),
        }
