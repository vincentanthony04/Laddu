"""Bounded liveness, progress and recovery supervisor for Project Laddu.

The supervisor does not decide trading outcomes.  It observes long-running
workers, restarts crashed loops, records useful progress separately from a
heartbeat, and exposes allow-listed recovery hooks to the autonomic controller.
A live thread is never force-killed: Python cannot safely terminate arbitrary
threads.  Stuck work must be recovered through an explicit component playbook
or an isolated executor generation.
"""
from __future__ import annotations

import threading
import time
import traceback
from datetime import datetime, timezone
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional


RecoveryFn = Callable[[Dict[str, Any]], Dict[str, Any] | None]


@dataclass
class LoopRecord:
    name: str
    fn: Callable[["Supervisor"], None]
    restart_backoff_sec: float = 5.0
    max_backoff_sec: float = 60.0
    thread: Optional[threading.Thread] = None
    last_heartbeat: float = 0.0
    started_at: float = field(default_factory=time.time)
    restart_count: int = 0
    last_error: Optional[str] = None
    last_error_at: Optional[float] = None
    entered_at: Optional[float] = None
    execution_count: int = 0
    heartbeat_count: int = 0
    stale_after_sec: float = 120.0
    progress_stale_after_sec: float = 180.0
    last_progress_at: float = 0.0
    progress_count: int = 0
    progress_token: str = ""
    stage: str = "starting"
    current_item: Optional[str] = None
    completed_units: Optional[int] = None
    total_units: Optional[int] = None
    expected_idle: bool = False
    waiting_on: Optional[str] = None
    recover_fn: Optional[RecoveryFn] = None
    safety_class: str = "SAFE_COMPONENT"
    recovery_count: int = 0
    recovery_failures: int = 0
    last_recovery_at: Optional[float] = None
    last_recovery_action: Optional[str] = None
    last_recovery_result: Optional[Dict[str, Any]] = None
    circuit_open: bool = False


class Supervisor:
    """Registers, runs, restarts and reports named background loops."""

    def __init__(self, event_fn: Optional[Callable[[str, str, str, dict], None]] = None):
        self._event = event_fn or (lambda *a, **k: None)
        self._loops: Dict[str, LoopRecord] = {}
        self._running = True
        self._lock = threading.RLock()

    def register(
        self,
        name: str,
        fn: Callable[["Supervisor"], None],
        restart_backoff_sec: float = 5.0,
        max_backoff_sec: float = 60.0,
        stale_after_sec: float = 120.0,
        progress_stale_after_sec: float | None = None,
        recover_fn: RecoveryFn | None = None,
        safety_class: str = "SAFE_COMPONENT",
    ) -> None:
        with self._lock:
            self._loops[name] = LoopRecord(
                name=name,
                fn=fn,
                restart_backoff_sec=restart_backoff_sec,
                max_backoff_sec=max_backoff_sec,
                stale_after_sec=stale_after_sec,
                progress_stale_after_sec=max(
                    stale_after_sec,
                    float(progress_stale_after_sec or stale_after_sec * 1.5),
                ),
                recover_fn=recover_fn,
                safety_class=str(safety_class or "SAFE_COMPONENT").upper(),
            )

    @property
    def running(self) -> bool:
        return self._running

    @property
    def registered_names(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._loops))

    def heartbeat_interval(self, name: str, default: float = 10.0) -> float:
        with self._lock:
            rec = self._loops.get(name)
            if rec is None:
                return max(1.0, float(default))
            return max(1.0, min(float(default), float(rec.stale_after_sec) / 3.0))

    def stop(self) -> None:
        self._running = False

    def beat(self, name: str) -> None:
        with self._lock:
            rec = self._loops.get(name)
            if rec:
                rec.last_heartbeat = time.time()
                rec.heartbeat_count += 1

    @contextmanager
    def heartbeat_guard(self, name: str, *, interval_sec: float | None = None):
        """Keep liveness current while one synchronous bounded call is in flight.

        This guard deliberately calls :meth:`beat` only.  It never touches the
        useful-progress token, counters, stage or ``last_progress_at``.  A slow
        but alive database/provider/research call therefore remains distinguishable
        from a business pipeline that actually advanced.
        """
        interval = max(0.01, float(interval_sec or self.heartbeat_interval(name, default=10.0)))
        stop = threading.Event()
        self.beat(name)

        def _heartbeat() -> None:
            while not stop.wait(interval):
                self.beat(name)

        thread = threading.Thread(target=_heartbeat, name=f"HeartbeatGuard-{name}", daemon=True)
        thread.start()
        try:
            yield
        finally:
            stop.set()
            thread.join(timeout=min(0.25, interval + 0.05))
            self.beat(name)

    def progress(
        self,
        name: str,
        *,
        token: Any | None = None,
        stage: str | None = None,
        current_item: Any | None = None,
        completed_units: int | None = None,
        total_units: int | None = None,
        waiting_on: str | None = None,
        expected_idle: bool | None = None,
    ) -> None:
        """Record useful progress independently of liveness.

        Repeating the same token is a heartbeat only.  A changed token, counter,
        stage or item advances ``last_progress_at`` and proves useful work.
        """
        with self._lock:
            rec = self._loops.get(name)
            if rec is None:
                return
            now = time.time()
            rec.last_heartbeat = now
            rec.heartbeat_count += 1
            new_token = str(token) if token is not None else rec.progress_token
            changed = any((
                token is not None and new_token != rec.progress_token,
                stage is not None and str(stage) != rec.stage,
                current_item is not None and str(current_item) != str(rec.current_item),
                completed_units is not None and completed_units != rec.completed_units,
                total_units is not None and total_units != rec.total_units,
            ))
            if changed or rec.last_progress_at <= 0:
                rec.last_progress_at = now
                rec.progress_count += 1
            if token is not None:
                rec.progress_token = new_token
            if stage is not None:
                rec.stage = str(stage)
            if current_item is not None:
                rec.current_item = str(current_item)
            if completed_units is not None:
                rec.completed_units = int(completed_units)
            if total_units is not None:
                rec.total_units = int(total_units)
            if waiting_on is not None:
                rec.waiting_on = str(waiting_on) or None
            elif expected_idle is False:
                # A worker that resumed useful work must not keep an old
                # "market closed" / dependency wait reason indefinitely.
                rec.waiting_on = None
            if expected_idle is not None:
                rec.expected_idle = bool(expected_idle)

    def set_expected_idle(self, name: str, idle: bool, *, waiting_on: str | None = None) -> None:
        """Mark a cadence/dependency wait without manufacturing progress.

        Expected-idle is a scheduling state, not evidence that useful work
        advanced.  The previous implementation refreshed ``last_progress_at``
        every time a worker entered its sleep cadence, allowing a permanently
        stalled worker to remain green forever.
        """
        with self._lock:
            rec = self._loops.get(name)
            if rec:
                rec.expected_idle = bool(idle)
                rec.waiting_on = waiting_on
                if idle:
                    rec.stage = "expected_idle"

    def start(self, names: Optional[tuple[str, ...] | list[str]] = None) -> tuple[str, ...]:
        started: list[str] = []
        with self._lock:
            selected = list(names) if names is not None else list(self._loops.keys())
            unknown = sorted(set(selected) - set(self._loops))
            if unknown:
                raise KeyError("unknown supervised loops: " + ",".join(unknown))
            for name in selected:
                if self._start_one(name):
                    started.append(name)
        return tuple(started)

    def start_all(self) -> None:
        self.start()

    def _start_one(self, name: str) -> bool:
        rec = self._loops[name]
        if rec.thread is not None and rec.thread.is_alive():
            return False
        t = threading.Thread(
            target=self._run_with_restart,
            args=(name,),
            name=f"Supervised-{name}",
            daemon=True,
        )
        rec.thread = t
        rec.started_at = time.time()
        rec.last_heartbeat = rec.started_at
        rec.last_progress_at = rec.started_at
        rec.stage = "starting"
        t.start()
        return True

    def _run_with_restart(self, name: str) -> None:
        rec = self._loops[name]
        backoff = rec.restart_backoff_sec
        while self._running:
            try:
                rec.entered_at = time.time()
                rec.execution_count += 1
                rec.stage = "running"
                rec.last_progress_at = rec.entered_at
                rec.fn(self)
                self._event("INFO", "supervisor", f"loop '{name}' exited cleanly", {})
                rec.stage = "complete"
                return
            except Exception as exc:
                rec.last_error = f"{exc}"
                rec.last_error_at = time.time()
                rec.restart_count += 1
                rec.stage = "failed"
                self._event(
                    "ERROR",
                    "supervisor",
                    f"loop '{name}' crashed, restarting in {backoff:.0f}s",
                    {"error": str(exc)[:300], "traceback": traceback.format_exc()[-1500:]},
                )
                time.sleep(backoff)
                backoff = min(backoff * 2, rec.max_backoff_sec)
        return

    def recover(self, name: str, *, reason: str, action: str = "SAFE_RECOVERY") -> Dict[str, Any]:
        """Execute an allow-listed component recovery hook.

        Critical authorities have no automatic restart hook.  The caller gets a
        fail-closed result rather than an unsafe process-wide restart.
        """
        with self._lock:
            rec = self._loops.get(name)
            if rec is None:
                return {"ok": False, "state": "UNKNOWN_COMPONENT", "component": name}
            if rec.circuit_open:
                return {"ok": False, "state": "CIRCUIT_OPEN", "component": name}
            if rec.safety_class in {"RISK_AUTHORITY", "LEDGER_AUTHORITY", "DATABASE_AUTHORITY"}:
                return {
                    "ok": False,
                    "state": "APPROVAL_REQUIRED",
                    "component": name,
                    "safety_class": rec.safety_class,
                }
            handler = rec.recover_fn
            if handler is None:
                # A dead worker can be started safely; a live stale Python thread
                # cannot be force-killed and therefore requires a playbook.
                alive = bool(rec.thread and rec.thread.is_alive())
                if not alive:
                    started = self._start_one(name)
                    return {"ok": started, "state": "RESTARTED" if started else "NOT_RESTARTED", "component": name}
                return {"ok": False, "state": "NO_SAFE_RECOVERY_PLAYBOOK", "component": name}
            context = {
                "component": name,
                "reason": reason,
                "action": action,
                "snapshot": self._row_snapshot(rec, time.time()),
            }
        try:
            result = dict(handler(context) or {})
            ok = bool(result.get("ok", result.get("recovered", False)))
        except Exception as exc:
            result = {"ok": False, "state": "RECOVERY_EXCEPTION", "error": str(exc)[:300]}
            ok = False
        with self._lock:
            rec = self._loops[name]
            rec.recovery_count += 1
            rec.last_recovery_at = time.time()
            rec.last_recovery_action = action
            rec.last_recovery_result = result
            if ok:
                rec.recovery_failures = 0
                # Recovery acceptance is not useful business progress. Keep the
                # prior progress clock/token intact until the recovered worker
                # advances its governed counters or immutable cursor.
                rec.stage = "recovering"
            else:
                rec.recovery_failures += 1
                if rec.recovery_failures >= 3:
                    rec.circuit_open = True
            self._event(
                "INFO" if ok else "WARN",
                "autonomic_controller",
                f"Recovery {'accepted' if ok else 'failed'} for {name}",
                {"reason": reason, "action": action, "result": result},
            )
        return {"component": name, **result, "ok": ok}

    def close_circuit(self, name: str) -> bool:
        with self._lock:
            rec = self._loops.get(name)
            if rec is None:
                return False
            rec.circuit_open = False
            rec.recovery_failures = 0
            return True

    @staticmethod
    def _classify(rec: LoopRecord, *, alive: bool, heartbeat_age: float | None, progress_age: float | None) -> str:
        if rec.circuit_open:
            return "CIRCUIT_OPEN"
        if rec.expected_idle:
            return "EXPECTED_IDLE"
        if not alive:
            return "DEAD" if rec.thread is not None else "NOT_STARTED"
        if heartbeat_age is not None and heartbeat_age > rec.stale_after_sec:
            return "STUCK"
        if progress_age is not None and progress_age > rec.progress_stale_after_sec:
            if rec.progress_count <= 0 or not rec.progress_token:
                return "UNINSTRUMENTED"
            return "NO_PROGRESS"
        if rec.last_error:
            return "RECOVERED_WITH_ERROR" if rec.restart_count else "FAILED"
        if rec.stage == "recovering":
            return "RECOVERING"
        return "RUNNING"

    @staticmethod
    def _iso_epoch(value: float | None) -> str | None:
        if not value:
            return None
        return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat().replace("+00:00", "Z")

    def _row_snapshot(self, rec: LoopRecord, now: float) -> Dict[str, Any]:
        heartbeat_age = now - rec.last_heartbeat if rec.last_heartbeat else None
        progress_age = now - rec.last_progress_at if rec.last_progress_at else None
        alive = bool(rec.thread and rec.thread.is_alive())
        state = self._classify(rec, alive=alive, heartbeat_age=heartbeat_age, progress_age=progress_age)
        return {
            "started": rec.thread is not None,
            "alive": alive,
            "stale": state in {"STUCK", "NO_PROGRESS", "DEAD"},
            "state": state,
            "heartbeat_age_sec": round(heartbeat_age, 1) if heartbeat_age is not None else None,
            "progress_age_sec": round(progress_age, 1) if progress_age is not None else None,
            "last_heartbeat_at": self._iso_epoch(rec.last_heartbeat),
            "last_progress_at": self._iso_epoch(rec.last_progress_at),
            "restart_count": rec.restart_count,
            "last_error": rec.last_error,
            "last_error_at": rec.last_error_at,
            "entered": rec.execution_count > 0,
            "entered_at": rec.entered_at,
            "execution_count": rec.execution_count,
            "heartbeat_count": rec.heartbeat_count,
            "progress_count": rec.progress_count,
            "progress_token": rec.progress_token or None,
            "stage": rec.stage,
            "current_item": rec.current_item,
            "completed_units": rec.completed_units,
            "total_units": rec.total_units,
            "expected_idle": rec.expected_idle,
            "waiting_on": rec.waiting_on,
            "recovery_available": rec.recover_fn is not None,
            "safety_class": rec.safety_class,
            "recovery_count": rec.recovery_count,
            "recovery_failures": rec.recovery_failures,
            "last_recovery_at": rec.last_recovery_at,
            "last_recovery_action": rec.last_recovery_action,
            "last_recovery_result": rec.last_recovery_result,
            "circuit_open": rec.circuit_open,
        }

    def snapshot(self) -> Dict[str, Any]:
        now = time.time()
        with self._lock:
            return {name: self._row_snapshot(rec, now) for name, rec in self._loops.items()}
