"""Authoritative phased-startup state contract for Project Laddu.

Installation safety depends only on the phases required to serve the customer
and protect open risk.  Bulk hydration/research workers remain observable but
cannot hold an otherwise safe installation hostage.  Both the runtime writer
and the readiness reader use this module, preventing split-brain semantics.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping

CONTRACT_VERSION = "startup-phase-coordinator-1.1.0"
REQUIRED_STARTUP_PHASES = ("http", "critical", "operational")
OPTIONAL_STARTUP_PHASES = ("bulk",)
_FAILURE_STATES = {"BLOCKED", "FAILED"}
_READY_STATE = "READY"


def _state(row: Mapping[str, Any] | None) -> str:
    return str((row or {}).get("state") or "PENDING").upper()


def startup_phase_summary(startup: Mapping[str, Any] | None) -> dict[str, Any]:
    source = dict(startup or {})
    required_states = {phase: _state(source.get(phase)) for phase in REQUIRED_STARTUP_PHASES}
    optional_states = {phase: _state(source.get(phase)) for phase in OPTIONAL_STARTUP_PHASES}
    required_failures = [
        f"{phase}:{required_states[phase]}:{str((source.get(phase) or {}).get('reason') or '').strip()}".rstrip(":")
        for phase in REQUIRED_STARTUP_PHASES
        if required_states[phase] in _FAILURE_STATES
    ]
    required_pending = [
        f"{phase}:{required_states[phase]}"
        for phase in REQUIRED_STARTUP_PHASES
        if required_states[phase] not in _FAILURE_STATES | {_READY_STATE}
    ]
    optional_failures = [
        f"{phase}:{optional_states[phase]}:{str((source.get(phase) or {}).get('reason') or '').strip()}".rstrip(":")
        for phase in OPTIONAL_STARTUP_PHASES
        if optional_states[phase] in _FAILURE_STATES
    ]
    optional_pending = [
        f"{phase}:{optional_states[phase]}"
        for phase in OPTIONAL_STARTUP_PHASES
        if optional_states[phase] not in _FAILURE_STATES | {_READY_STATE}
    ]
    required_complete = not required_failures and not required_pending
    optional_complete = not optional_failures and not optional_pending
    overall = "BLOCKED" if required_failures else "COMPLETE" if required_complete else "STARTING"
    return {
        "state": overall,
        "required_complete": required_complete,
        "optional_complete": optional_complete,
        "required_states": required_states,
        "optional_states": optional_states,
        "required_failures": required_failures,
        "required_pending": required_pending,
        "optional_failures": optional_failures,
        "optional_pending": optional_pending,
    }


def apply_startup_phase_update(
    startup: Mapping[str, Any] | None,
    *,
    phase: str,
    state: str,
    updated_at: str,
    detail: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    phase = str(phase or "").strip().lower()
    if phase not in REQUIRED_STARTUP_PHASES + OPTIONAL_STARTUP_PHASES:
        raise KeyError(f"unknown startup phase: {phase}")
    result = deepcopy(dict(startup or {}))
    result["version"] = CONTRACT_VERSION
    result["required_phases"] = list(REQUIRED_STARTUP_PHASES)
    result["optional_phases"] = list(OPTIONAL_STARTUP_PHASES)
    row = dict(result.get(phase) or {})
    row.update({"state": str(state or "PENDING").upper(), "updated_at": updated_at, **dict(detail or {})})
    result[phase] = row
    summary = startup_phase_summary(result)
    result.update({
        "state": summary["state"],
        "required_complete": summary["required_complete"],
        "optional_complete": summary["optional_complete"],
        "required_failures": summary["required_failures"],
        "required_pending": summary["required_pending"],
        "optional_failures": summary["optional_failures"],
        "optional_pending": summary["optional_pending"],
        "last_transition_at": updated_at,
    })
    return result
