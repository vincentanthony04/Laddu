"""Canonical one-row decision/position projection for the primary UI.

State and Action are separate.  R:R and internal evidence-readiness labels are
not part of this projection.  Net P&L uses the correct Intraday/Delivery India
cash cost profile and a default reference quantity of 100 shares.
"""
from __future__ import annotations

import math
from typing import Any, Dict, Mapping, Optional

from core.india_cost_model import IndiaCashCostModel
from core.production_mode_policy import require_production_mode

DECISION_ROW_VERSION = "decision-row-1.0.0"
REFERENCE_QUANTITY = 100


def _num(value: Any) -> Optional[float]:
    try:
        if value in (None, "", "—"):
            return None
        out = float(value)
        return out if math.isfinite(out) else None
    except (TypeError, ValueError):
        return None


def _upper(value: Any) -> str:
    return str(value or "").strip().upper().replace("_", " ")


class DecisionRowProjectionService:
    @staticmethod
    def _direction(row: Mapping[str, Any]) -> str:
        side = _upper(row.get("side"))
        return "SHORT" if side in {"SHORT", "SELL"} else "LONG"

    @classmethod
    def _is_open(cls, row: Mapping[str, Any]) -> bool:
        status = _upper(row.get("signal_status") or row.get("status") or row.get("position_status"))
        state = _upper(row.get("state") or row.get("lifecycle_state"))
        return bool(
            row.get("signal_id") or row.get("position_id") or row.get("entry_confirmed") is True
            or status in {"OPEN", "TRIGGERED", "SELECTED", "ACTIVE", "PROMOTED", "SIGNAL OPEN"}
            or state in {"OPEN", "BREAKEVEN PROTECTED", "TRAILING PROFIT", "PARTIAL PROFIT SECURED"}
        )

    @classmethod
    def _state_action(cls, row: Mapping[str, Any], *, current: Optional[float], entry: Optional[float],
                      target: Optional[float], stop: Optional[float], t2: Optional[float]) -> tuple[str, str]:
        direction = cls._direction(row)
        status = _upper(row.get("signal_status") or row.get("status") or row.get("position_status"))
        result = _upper(row.get("result") or row.get("outcome_status"))
        lifecycle = _upper(row.get("lifecycle_state") or row.get("state"))

        if status in {"FAIL", "FAILED"} or result.startswith("FAIL") or "STOP" in result or lifecycle in {"CLOSED FAILED", "STOP HIT"}:
            return "CLOSED FAILED", "CLOSED"
        if status in {"SUCCESS", "CLOSED"} or result.startswith("SUCCESS") or lifecycle in {"CLOSED PROFITABLE", "CLOSED T2"}:
            return "CLOSED PROFITABLE", "CLOSED"
        if status == "EXPIRED" or result == "EXPIRED":
            return "EXPIRED", "CLOSED"
        if status == "INVALIDATED" or result == "INVALIDATED":
            return "INVALIDATED", "CLOSED"

        if current is not None:
            stop_hit = stop is not None and (current >= stop if direction == "SHORT" else current <= stop)
            final_target_hit = t2 is not None and (current <= t2 if direction == "SHORT" else current >= t2)
            target_hit = target is not None and (current <= target if direction == "SHORT" else current >= target)
            if stop_hit:
                return "STOP HIT", "EXIT"
            if final_target_hit:
                return "TARGET MET", "EXIT"
            if target_hit:
                return "TARGET MET", "NO NEW ADD"

        if cls._is_open(row):
            if lifecycle in {"TRAILING PROFIT", "BREAKEVEN PROTECTED", "PARTIAL PROFIT SECURED"}:
                return "OPEN", "CONTINUE"
            return "OPEN", "HOLD"

        complete_plan = all(value is not None for value in (entry, target, stop))
        readiness = _upper(row.get("rank_readiness") or row.get("readiness"))
        actionable = row.get("actionability_verified") is True or readiness == "READY"
        blocked = status in {"BLOCKED", "REJECTED", "AVOID"} or readiness == "AVOID"
        if blocked:
            return "INVALIDATED", "WATCH"
        if complete_plan and actionable:
            return "READY TO ENTER", "ENTER"
        if complete_plan:
            return "WAITING TRIGGER", "WATCH"
        return "RESEARCH", "WATCH"

    @classmethod
    def project(cls, row: Mapping[str, Any], *, rank: Optional[int] = None,
                quantity: int = REFERENCE_QUANTITY) -> Dict[str, Any]:
        mode = require_production_mode(row.get("mode"))
        direction = cls._direction(row)
        current = _num(row.get("current_price") if row.get("current_price") is not None else row.get("ltp"))
        change_pct = _num(row.get("change_pct"))
        change_value = _num(row.get("change") if row.get("change") is not None else row.get("change_value"))
        previous_close = _num(row.get("previous_close") or row.get("prev_close"))
        if change_value is None and current is not None and previous_close is not None:
            change_value = current - previous_close
        if change_pct is None and current is not None and previous_close not in (None, 0):
            change_pct = (current / previous_close - 1.0) * 100.0
        entry = _num(row.get("entry") if row.get("entry") is not None else row.get("planned_entry"))
        target = _num(row.get("target") if row.get("target") is not None else row.get("t1") if row.get("t1") is not None else row.get("planned_target"))
        stop = _num(row.get("stop") if row.get("stop") is not None else row.get("sl") if row.get("sl") is not None else row.get("planned_stop"))
        t2 = _num(row.get("t2") or row.get("final_target"))
        state, action = cls._state_action(row, current=current, entry=entry, target=target, stop=stop, t2=t2)

        q = max(1, int(quantity or REFERENCE_QUANTITY))
        net_pnl = None
        gross_pnl = None
        estimated_cost = None
        cost_state = "NOT_REQUIRED"
        cost_blocker = None
        if cls._is_open(row) and current is not None and entry is not None:
            try:
                model = IndiaCashCostModel.for_evidence(mode, dict(row))
                estimate = model.round_trip(current, entry, q) if direction == "SHORT" else model.round_trip(entry, current, q)
                net_pnl = _num(estimate.get("net_pnl"))
                gross_pnl = _num(estimate.get("gross_pnl"))
                estimated_cost = _num((estimate.get("costs") or {}).get("total"))
                cost_state = "READY"
            except ValueError as exc:
                # This is a read-model projection, not promotion authority.  A
                # legacy/open BSE row that lacks governed scrip-group evidence
                # must remain visible while its cost fields fail closed.  Do not
                # crash the entire Market Radar/OCC projection and hide every
                # other symbol because one row is incomplete.
                cost_state = "BLOCKED"
                cost_blocker = str(exc)[:180]

        final_confidence = _num(
            row.get("final_confidence") if row.get("final_confidence") is not None
            else row.get("rank_score") if row.get("rank_score") is not None
            else row.get("score")
        )
        return {
            "contract_version": DECISION_ROW_VERSION,
            "rank": int(rank if rank is not None else row.get("rank") or 0) or None,
            "symbol": str(row.get("symbol") or "").upper(),
            "instrument_key": row.get("instrument_key"),
            "identity_verified": row.get("identity_verified") is True,
            "mode": mode,
            "direction": direction,
            "current_price": None if current is None else round(current, 2),
            "change_value": None if change_value is None else round(change_value, 2),
            "change_pct": None if change_pct is None else round(change_pct, 2),
            "entry": None if entry is None else round(entry, 2),
            "target": None if target is None else round(target, 2),
            "stop": None if stop is None else round(stop, 2),
            "reference_quantity": q,
            "gross_pnl": None if gross_pnl is None else round(gross_pnl, 2),
            "estimated_cost": None if estimated_cost is None else round(estimated_cost, 2),
            "net_pnl": None if net_pnl is None else round(net_pnl, 2),
            "cost_state": cost_state,
            "cost_blocker": cost_blocker,
            "state": state,
            "action": action,
            "final_confidence": None if final_confidence is None else round(max(0.0, min(100.0, final_confidence)), 1),
            "confidence_semantics": "UNVALIDATED_HEURISTIC_OR_SHADOW_RANK; NOT PROFIT PROBABILITY",
        }
