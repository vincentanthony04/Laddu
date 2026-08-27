from __future__ import annotations

"""Deterministic local scanner analysis executor for Clean Core.

Clean Core deliberately removes ThreadPool generations from deep scanner
analysis.  Candidate preparation must already contain local candles and context;
the executor then evaluates the shortlist sequentially within a desk budget.
There are no retired generations, zombie futures or replacement workers.

A pathological CPU call can still make the scanner lane late, but that failure
is confined to the asynchronous scanner lane and cannot consume foreground
Stock Report/Chart capacity or create unbounded background threads.
"""

import time
from typing import Any, Callable, Dict, Iterable, List, Mapping, Tuple

from core.production_mode_policy import require_production_mode

AnalysisJob = Tuple[Dict[str, Any], Dict[str, Any] | None, Mapping[str, Any]]
AnalysisResult = Tuple[Any, str]


class DeterministicDeskAnalysisExecutor:
    VERSION = "clean-core-deterministic-analysis-1.0.0"

    def __init__(self, analyze_fn: Callable[..., Any], *, enforce_local_input: bool = True):
        self._analyze_fn = analyze_fn
        self._enforce_local_input = bool(enforce_local_input)
        self._stats = {
            "intraday": {"runs": 0, "completed": 0, "errors": 0, "budget_deferred": 0, "last_job_ms": 0.0},
            "delivery": {"runs": 0, "completed": 0, "errors": 0, "budget_deferred": 0, "last_job_ms": 0.0},
        }

    def _validate(self, kwargs: Mapping[str, Any]) -> None:
        if not self._enforce_local_input:
            return
        if "candles_override" not in kwargs or kwargs.get("candles_override") is None:
            raise ValueError("SCANNER_ANALYSIS_REQUIRES_LOCAL_CANDLE_SNAPSHOT")
        prepared = kwargs.get("prepared_analysis")
        if not isinstance(prepared, Mapping) or not isinstance(dict(prepared).get("context"), Mapping):
            raise ValueError("SCANNER_ANALYSIS_REQUIRES_PREPARED_CONTEXT")

    def run_many(
        self,
        jobs: Iterable[AnalysisJob],
        mode: str,
        timeout_sec: float,
        *,
        batch_budget_sec: float | None = None,
    ) -> List[AnalysisResult]:
        desk = require_production_mode(mode)
        prepared = list(jobs)
        if not prepared:
            return []
        per_job_target = max(0.001, float(timeout_sec))
        budget = max(per_job_target, float(batch_budget_sec) if batch_budget_sec is not None else per_job_target * len(prepared))
        deadline = time.monotonic() + budget
        stats = self._stats[desk]
        stats["runs"] += 1
        results: List[AnalysisResult] = []
        for index, (inst, quote, kwargs) in enumerate(prepared):
            if time.monotonic() >= deadline:
                remaining = len(prepared) - index
                stats["budget_deferred"] += remaining
                results.extend((None, "analysis_budget_exhausted") for _ in range(remaining))
                break
            try:
                self._validate(kwargs)
                started = time.monotonic()
                value = self._analyze_fn(inst, quote, desk, **dict(kwargs))
                elapsed_ms = (time.monotonic() - started) * 1000.0
                stats["last_job_ms"] = round(elapsed_ms, 2)
                stats["completed"] += 1
                # A completed local calculation remains usable even when slower
                # than the target.  The lane budget prevents starting unlimited
                # additional work; no zombie worker is left behind.
                results.append((value, "ok"))
            except Exception as exc:
                stats["errors"] += 1
                results.append((exc, "analysis_error"))
        return results

    def run(self, inst: Dict[str, Any], quote: Dict[str, Any] | None, mode: str, timeout_sec: float, **kwargs: Any) -> AnalysisResult:
        rows = self.run_many([(inst, quote, kwargs)], mode, timeout_sec, batch_budget_sec=timeout_sec)
        return rows[0] if rows else (None, "analysis_budget_exhausted")

    def capacity(self, mode: str) -> Dict[str, Any]:
        desk = require_production_mode(mode)
        stats = dict(self._stats[desk])
        return {
            "executor_version": self.VERSION,
            "workers": 1,
            "active": 0,
            "available": 1,
            "oldest_active_sec": 0.0,
            "active_over_60s": 0,
            "retired_active": 0,
            "oldest_retired_sec": 0.0,
            "generation": 0,
            "recovery_count": 0,
            "state": "DETERMINISTIC_LOCAL",
            **stats,
        }

    def recover_stale_generation(self, mode: str, *, stale_after_sec: float = 75.0) -> Dict[str, Any]:
        return {
            "recovered": False,
            "reason": "NO_GENERATION_ROTATION_IN_CLEAN_CORE",
            **self.capacity(mode),
        }
