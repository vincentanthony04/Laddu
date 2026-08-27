from __future__ import annotations

"""Desk-specific scanner analysis execution policy.

R40 keeps Delivery on the proven deterministic Clean-Core executor while giving
Intraday a bounded single-worker executor.  The prepared Intraday analysis
boundary is pure/local compute, so a pathological calculation may be timed out
without allowing one call to hold the entire ``intraday_analysis`` lane forever.
"""
from typing import Any, Callable, Dict, Iterable, List, Mapping, Tuple

from core.bounded_analysis_executor import BoundedDeskAnalysisExecutor
from core.deterministic_analysis_executor import DeterministicDeskAnalysisExecutor
from core.production_mode_policy import require_production_mode

AnalysisJob = Tuple[Dict[str, Any], Dict[str, Any] | None, Mapping[str, Any]]
AnalysisResult = Tuple[Any, str]


class DeskAnalysisExecutorRouter:
    VERSION = "desk-analysis-executor-router-1.0.0-intraday-bounded-delivery-deterministic"

    def __init__(self, analyze_fn: Callable[..., Any], *, enforce_local_input: bool = True):
        self._delivery = DeterministicDeskAnalysisExecutor(
            analyze_fn, enforce_local_input=enforce_local_input
        )
        # A single Intraday worker preserves sequential mathematical semantics.
        # Timeout/batch-budget enforcement releases the scanner lane even if a
        # pure local calculation continues briefly in a retired thread.
        self._intraday = BoundedDeskAnalysisExecutor(
            analyze_fn,
            workers={"intraday": 1, "delivery": 1},
            enforce_local_input=enforce_local_input,
        )

    def _executor(self, mode: str):
        desk = require_production_mode(mode)
        return self._intraday if desk == "intraday" else self._delivery

    def run_many(
        self,
        jobs: Iterable[AnalysisJob],
        mode: str,
        timeout_sec: float,
        *,
        batch_budget_sec: float | None = None,
    ) -> List[AnalysisResult]:
        return self._executor(mode).run_many(
            jobs, mode, timeout_sec, batch_budget_sec=batch_budget_sec
        )

    def run(
        self,
        inst: Dict[str, Any],
        quote: Dict[str, Any] | None,
        mode: str,
        timeout_sec: float,
        **kwargs: Any,
    ) -> AnalysisResult:
        return self._executor(mode).run(inst, quote, mode, timeout_sec, **kwargs)

    def capacity(self, mode: str) -> Dict[str, Any]:
        desk = require_production_mode(mode)
        payload = dict(self._executor(desk).capacity(desk) or {})
        payload["router_version"] = self.VERSION
        payload["desk_execution_policy"] = (
            "BOUNDED_LOCAL_SINGLE_WORKER" if desk == "intraday" else "DETERMINISTIC_LOCAL"
        )
        return payload

    def recover_stale_generation(self, mode: str, *, stale_after_sec: float = 75.0) -> Dict[str, Any]:
        desk = require_production_mode(mode)
        payload = dict(
            self._executor(desk).recover_stale_generation(
                desk, stale_after_sec=stale_after_sec
            ) or {}
        )
        payload["router_version"] = self.VERSION
        payload["desk_execution_policy"] = (
            "BOUNDED_LOCAL_SINGLE_WORKER" if desk == "intraday" else "DETERMINISTIC_LOCAL"
        )
        return payload
