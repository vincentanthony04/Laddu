from __future__ import annotations

import threading


class RuntimeControl:
    """Single process-lifecycle authority shared by runtime modules."""

    def __init__(self) -> None:
        self._running = threading.Event()
        self._running.set()

    @property
    def running(self) -> bool:
        return self._running.is_set()

    def stop(self) -> None:
        self._running.clear()

    def reset(self) -> None:
        self._running.set()


CONTROL = RuntimeControl()
