"""Canonical decision admission guard.

Scanner/research rows are evidence, not decisions.  This module is the single
persistence boundary that decides whether a row may enter the canonical
Decision/Model-Paper lifecycle.  It deliberately contains no ranking logic.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from core.production_mode_policy import is_production_mode
from core.numeric_semantics import finite_number


CANONICAL_ACTIVE_STATUSES = {
    "PROMOTED", "SIGNAL_OPEN", "TRIGGERED", "CONFIRMED", "WEAKENING",
}
CANONICAL_TERMINAL_STATUSES = {
    "COMPLETED", "CLOSED", "SUCCESS", "FAIL", "TARGET_HIT", "STOP_HIT",
    "INVALIDATED", "EXPIRED", "CANCELLED",
}
RESEARCH_ONLY_STATUSES = {
    "", "WAIT", "WATCH", "WATCHING", "RESEARCH", "PREPARING",
    "WAITING_FOR_CONFIRMATION", "UNDER_REVIEW", "REJECTED", "BLOCKED",
    "NOT_PUBLISHABLE", "BEST_AVAILABLE", "SETUP",
}
APPROVED_RISK_STATES = {"APPROVED_RESEARCH_ONLY", "APPROVED_CAPITAL"}


@dataclass(frozen=True)
class AdmissionResult:
    allowed: bool
    reason: str
    normalized_side: str | None = None
    terminal_update: bool = False


def _upper(value: Any) -> str:
    return str(value or "").strip().upper()


def _number(value: Any) -> float | None:
    return finite_number(value)


def normalize_side(value: Any) -> str | None:
    side = _upper(value)
    if side in {"LONG", "BUY", "BULLISH"}:
        return "LONG"
    if side in {"SHORT", "SELL", "BEARISH"}:
        return "SHORT"
    return None


def _first(row: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        value = row.get(key)
        if value not in (None, "", [], {}):
            return value
    return None


def _trade_map(row: Mapping[str, Any]) -> tuple[float | None, float | None, float | None]:
    return (
        _number(_first(row, "entry", "entry_price", "planned_entry", "trigger_level")),
        _number(_first(row, "target", "target_price", "t1", "planned_target", "planned_t1")),
        _number(_first(row, "stop", "stop_price", "sl", "planned_stop", "planned_sl", "managed_stop")),
    )

def _has_complete_trade_map(row: Mapping[str, Any]) -> bool:
    entry, target, stop = _trade_map(row)
    return entry is not None and target is not None and stop is not None and entry > 0 and target > 0 and stop > 0

def _trade_map_directionally_valid(row: Mapping[str, Any], side: str) -> bool:
    entry, target, stop = _trade_map(row)
    if None in (entry, target, stop):
        return False
    if side == "LONG":
        return stop < entry < target
    if side == "SHORT":
        return target < entry < stop
    return False


def evaluate_canonical_admission(decision: Mapping[str, Any]) -> AdmissionResult:
    row = dict(decision or {})
    if not is_production_mode(row.get("mode")):
        return AdmissionResult(False, "UNSUPPORTED_MODE")

    side = normalize_side(row.get("side"))
    status = _upper(row.get("status") or row.get("canonical_state") or row.get("lifecycle_state"))
    action = _upper(row.get("decision") or row.get("decision_action") or row.get("action"))
    explicit_id = str(row.get("decision_id") or row.get("signal_id") or "").strip()

    # Existing canonical records must be allowed to settle even when a closure
    # payload no longer carries the original level map.
    terminal = status in CANONICAL_TERMINAL_STATUSES or action in CANONICAL_TERMINAL_STATUSES
    if explicit_id and terminal:
        if side is None:
            return AdmissionResult(False, "TERMINAL_UPDATE_SIDE_MISSING")
        return AdmissionResult(True, "TERMINAL_UPDATE", side, terminal_update=True)

    if status in RESEARCH_ONLY_STATUSES or action in {"", "WAIT", "WATCH", "RESEARCH", "REJECT", "AVOID", "AVOID_LONG"}:
        return AdmissionResult(False, "RESEARCH_OR_REJECTION_ROW", side)
    if status not in CANONICAL_ACTIVE_STATUSES:
        return AdmissionResult(False, "CANONICAL_STATUS_NOT_ADMITTED", side)
    if side is None:
        return AdmissionResult(False, "CANONICAL_SIDE_INVALID")

    risk_state = _upper(row.get("risk_admission_state"))
    publication = _upper(row.get("publication_authority"))
    if risk_state not in APPROVED_RISK_STATES and publication not in {"MODEL_PAPER", "CAPITAL"}:
        return AdmissionResult(False, "RISK_ADMISSION_NOT_APPROVED", side)

    if row.get("identity_verified") is False:
        return AdmissionResult(False, "IDENTITY_UNVERIFIED", side)
    if _upper(row.get("mode")) == "DELIVERY" and row.get("fundamental_required") is True:
        fundamental_state = _upper(row.get("fundamental_state") or row.get("fundamentals_state"))
        if fundamental_state not in {"ACCEPTED", "VERIFIED", "READY"}:
            return AdmissionResult(False, "MANDATORY_FUNDAMENTALS_UNAVAILABLE", side)
    freshness = _upper(row.get("freshness_state") or row.get("price_freshness") or row.get("data_state"))
    if freshness in {"STALE", "UNSAFE", "UNVERIFIED", "MISSING"}:
        return AdmissionResult(False, "DATA_NOT_CURRENT_OR_VERIFIED", side)
    if not _has_complete_trade_map(row):
        return AdmissionResult(False, "INCOMPLETE_ENTRY_TARGET_STOP", side)
    if not _trade_map_directionally_valid(row, side):
        return AdmissionResult(False, "INVALID_ENTRY_TARGET_STOP_GEOMETRY", side)

    return AdmissionResult(True, "ADMITTED", side)
