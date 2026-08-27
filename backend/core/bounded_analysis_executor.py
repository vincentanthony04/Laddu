"""Bounded parallel execution for desk analysis calls.

Python threads cannot be force-killed safely. The owner keeps one small active
worker generation per desk and at most one retired generation containing
previously timed-out calls. When every current worker is stale, the generation
is rotated once so useful scanner work can resume without creating an
unbounded queue or an unbounded number of threads.
"""
from __future__ import annotations

from collections import deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
import threading
import time
from typing import Any, Callable, Dict, Iterable, List, Mapping, Tuple

from core.production_mode_policy import require_production_mode

AnalysisJob = Tuple[Dict[str, Any], Dict[str, Any] | None, Mapping[str, Any]]
AnalysisResult = Tuple[Any, str]


class BoundedDeskAnalysisExecutor:
    """Run bounded analysis concurrently without an unbounded pending queue."""

    DEFAULT_WORKERS = {"intraday": 4, "delivery": 8}

    def __init__(self, analyze_fn: Callable[..., Any], *, workers: Mapping[str, int] | None = None, enforce_local_input: bool = False):
        self._analyze_fn = analyze_fn
        self._enforce_local_input = bool(enforce_local_input)
        configured = dict(self.DEFAULT_WORKERS)
        configured.update({str(k): max(1, int(v)) for k, v in dict(workers or {}).items()})
        self._workers = configured
        self._generation = {desk: 1 for desk in configured}
        self._recovery_count = {desk: 0 for desk in configured}
        self._executors = {desk: self._new_executor(desk) for desk in configured}
        self._lock = threading.Lock()
        self._active: Dict[str, Dict[Any, float]] = {desk: {} for desk in configured}
        self._retired: Dict[str, list[tuple[ThreadPoolExecutor, Dict[Any, float], int]]] = {desk: [] for desk in configured}

    def _new_executor(self, desk: str) -> ThreadPoolExecutor:
        return ThreadPoolExecutor(max_workers=self._workers[desk], thread_name_prefix=f"laddu-{desk}-analysis-g{self._generation[desk]}")

    def _reap_locked(self, desk: str) -> None:
        active = self._active[desk]
        for future in [future for future in active if future.done()]:
            active.pop(future, None)
        survivors = []
        for executor, retired_active, generation in self._retired[desk]:
            for future in [future for future in retired_active if future.done()]:
                retired_active.pop(future, None)
            if retired_active:
                survivors.append((executor, retired_active, generation))
            else:
                executor.shutdown(wait=False, cancel_futures=True)
        self._retired[desk] = survivors

    def recover_stale_generation(self, mode: str, *, stale_after_sec: float = 75.0) -> Dict[str, Any]:
        """Rotate one fully stale generation while keeping total recovery bounded.

        At most one retired generation may still contain work. A second rotation
        is refused until those calls finish, preventing runaway thread creation.
        """
        desk = require_production_mode(mode)
        now = time.monotonic()
        with self._lock:
            self._reap_locked(desk)
            starts = list(self._active[desk].values())
            workers = self._workers[desk]
            oldest = max((now - started for started in starts), default=0.0)
            if len(starts) < workers or oldest < max(1.0, float(stale_after_sec)):
                return {"recovered": False, "reason": "NOT_FULLY_STALE", **self._capacity_locked(desk, now)}
            if any(active for _executor, active, _generation in self._retired[desk]):
                return {"recovered": False, "reason": "RETIRED_GENERATION_STILL_ACTIVE", **self._capacity_locked(desk, now)}
            old_executor = self._executors[desk]
            old_active = self._active[desk]
            old_generation = self._generation[desk]
            self._retired[desk].append((old_executor, old_active, old_generation))
            self._generation[desk] += 1
            self._executors[desk] = self._new_executor(desk)
            self._active[desk] = {}
            self._recovery_count[desk] += 1
            return {"recovered": True, "reason": "STALE_GENERATION_ROTATED", **self._capacity_locked(desk, now)}

    def _capacity_locked(self, desk: str, now: float) -> Dict[str, Any]:
        current_starts = list(self._active[desk].values())
        retired_starts = [started for _executor, active, _generation in self._retired[desk] for started in active.values()]
        ages = [max(0.0, now - started) for started in current_starts]
        retired_ages = [max(0.0, now - started) for started in retired_starts]
        workers = int(self._workers[desk])
        active = len(current_starts)
        return {
            "workers": workers,
            "active": active,
            "available": max(0, workers - active),
            "oldest_active_sec": round(max(ages, default=0.0), 2),
            "active_over_60s": sum(1 for age in ages if age >= 60.0),
            "retired_active": len(retired_starts),
            "oldest_retired_sec": round(max(retired_ages, default=0.0), 2),
            "generation": self._generation[desk],
            "recovery_count": self._recovery_count[desk],
            "state": "SATURATED" if active >= workers else "RECOVERING" if retired_starts else "AVAILABLE",
        }

    def capacity(self, mode: str) -> Dict[str, Any]:
        desk = require_production_mode(mode)
        now = time.monotonic()
        with self._lock:
            self._reap_locked(desk)
            return self._capacity_locked(desk, now)

    def _submit(self, desk: str, inst: Dict[str, Any], quote: Dict[str, Any] | None, kwargs: Mapping[str, Any]) -> tuple[Any, float] | None:
        # v103 execution boundary: scanner workers are pure/local compute.
        # Provider/history I/O must be completed or scheduled by the orchestration
        # layer before admission.  Making this an executable guard prevents a
        # future caller from silently reintroducing network waits into threads
        # that Python cannot safely terminate.
        if self._enforce_local_input:
            if "candles_override" not in kwargs or kwargs.get("candles_override") is None:
                raise ValueError("SCANNER_ANALYSIS_REQUIRES_LOCAL_CANDLE_SNAPSHOT")
            if not isinstance(kwargs.get("prepared_analysis"), Mapping) or not isinstance(dict(kwargs.get("prepared_analysis") or {}).get("context"), Mapping):
                raise ValueError("SCANNER_ANALYSIS_REQUIRES_PREPARED_CONTEXT")
        with self._lock:
            self._reap_locked(desk)
            if len(self._active[desk]) >= self._workers[desk]:
                return None
            started = time.monotonic()
            future = self._executors[desk].submit(self._analyze_fn, inst, quote, desk, **dict(kwargs))
            self._active[desk][future] = started
            return future, started

    def run_many(self, jobs: Iterable[AnalysisJob], mode: str, timeout_sec: float, *, batch_budget_sec: float | None = None) -> List[AnalysisResult]:
        desk = require_production_mode(mode)
        prepared = list(jobs)
        results: List[AnalysisResult] = [(None, "analysis_budget_exhausted") for _ in prepared]
        if not prepared:
            return results
        per_job_timeout = max(0.25, float(timeout_sec))
        total_budget = max(per_job_timeout, float(batch_budget_sec) if batch_budget_sec is not None else per_job_timeout * max(1, len(prepared)))
        deadline = time.monotonic() + total_budget
        pending = deque(enumerate(prepared))
        current: Dict[Any, tuple[int, float]] = {}
        while pending or current:
            if time.monotonic() >= deadline:
                break
            while pending and time.monotonic() < deadline:
                index, (inst, quote, kwargs) = pending[0]
                submitted = self._submit(desk, inst, quote, kwargs)
                if submitted is None:
                    break
                pending.popleft()
                future, started = submitted
                current[future] = (index, started)
            if not current:
                while pending:
                    index, _job = pending.popleft()
                    results[index] = (None, "analysis_capacity")
                break
            now = time.monotonic()
            nearest_timeout = min(started + per_job_timeout for _index, started in current.values())
            wait_for = max(0.0, min(deadline, nearest_timeout) - now)
            done, _ = wait(tuple(current), timeout=wait_for, return_when=FIRST_COMPLETED)
            for future in done:
                index, _started = current.pop(future)
                try:
                    results[index] = (future.result(), "ok")
                except Exception as exc:
                    results[index] = (exc, "analysis_error")
                with self._lock:
                    self._active[desk].pop(future, None)
            now = time.monotonic()
            for future in [future for future, (_index, started) in current.items() if now - started >= per_job_timeout]:
                index, _started = current.pop(future)
                future.cancel()
                results[index] = (None, "analysis_timeout")
        for future, (index, started) in list(current.items()):
            # The batch deadline and per-job timeout are intentionally identical
            # for ``run()``. Scheduler jitter can leave elapsed a few
            # milliseconds below the nominal timeout when the deadline branch
            # runs; classify that active call as a timeout, not budget loss.
            elapsed = time.monotonic() - started
            timeout_slack = min(0.02, per_job_timeout * 0.05)
            state = "analysis_timeout" if elapsed + timeout_slack >= per_job_timeout else "analysis_budget_exhausted"
            future.cancel()
            results[index] = (None, state)
        while pending:
            index, _job = pending.popleft()
            results[index] = (None, "analysis_budget_exhausted")
        return results

    def run(self, inst: Dict[str, Any], quote: Dict[str, Any] | None, mode: str, timeout_sec: float, **kwargs: Any) -> AnalysisResult:
        return self.run_many([(inst, quote, kwargs)], mode, timeout_sec, batch_budget_sec=timeout_sec)[0]
