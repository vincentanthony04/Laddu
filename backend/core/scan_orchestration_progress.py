"""Canonical scanner progress aggregation helpers.

Keeps the operator-visible sweep progression separate from batch-local scanner
telemetry.  Sweep counters are monotonic within one immutable universe sweep;
priority insertions do not advance the base-universe sweep.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, Mapping


def cumulative_delivery_sweep_stage_counts(
    *,
    universe_size: int,
    base_batch: Iterable[Mapping[str, Any]],
    base_count: int,
    stage_members: Mapping[str, Iterable[Mapping[str, Any]]],
    prior_counts: Mapping[str, Any],
) -> Dict[str, int]:
    base_keys = {str(item.get("instrument_key") or "") for item in base_batch if item.get("instrument_key")}

    def base_stage_count(stage_name: str) -> int:
        return sum(
            1
            for item in stage_members.get(stage_name, [])
            if str(item.get("instrument_key") or "") in base_keys
        )

    cycle_counts = {
        "attempted": int(base_count or 0),
        "quote_ready": base_stage_count("quote_ready"),
        "shortlisted": base_stage_count("shortlisted"),
        "data_pending": base_stage_count("data_pending"),
        "analysed": base_stage_count("analysed"),
        "deferred": base_stage_count("capacity_deferred"),
        "blocked": base_stage_count("data_blocked"),
        "mathematically_rejected": base_stage_count("mathematically_rejected"),
        "map": base_stage_count("map"),
        "rr": base_stage_count("rr"),
        "final": base_stage_count("final"),
    }
    result: Dict[str, int] = {"universe": max(0, int(universe_size or 0))}
    for stage_name, cycle_count in cycle_counts.items():
        result[stage_name] = min(
            result["universe"],
            max(0, int(prior_counts.get(stage_name) or 0)) + max(0, int(cycle_count or 0)),
        )
    terminal = min(
        result["universe"],
        result.get("analysed", 0) + result.get("blocked", 0) + result.get("mathematically_rejected", 0),
    )
    pending = min(
        result["universe"],
        result.get("data_pending", 0) + result.get("deferred", 0),
    )
    result["analysis_terminal"] = terminal
    result["analysis_pending"] = pending
    result["analysis_unresolved"] = max(0, result.get("attempted", 0) - terminal - pending)
    result["analysis_complete"] = int(
        result.get("attempted", 0) >= result["universe"]
        and result["analysis_pending"] == 0
        and result["analysis_unresolved"] == 0
    )
    return result
