from __future__ import annotations

"""Fail-closed browser projection of already-canonical trade geometry."""

from typing import Any, Dict, Mapping


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class TradeMapProjectionService:
    VERSION = "canonical-trade-map-projection-1.0.0"

    @staticmethod
    def _ordered(side: str, entry: float, stop: float, target: float) -> bool:
        return stop < entry < target if side == "LONG" else target < entry < stop if side == "SHORT" else False

    @classmethod
    def project(cls, decision: Mapping[str, Any] | None) -> Dict[str, Any]:
        row = dict(decision or {})
        existing = dict(row.get("trade_map") or {})
        if existing and str(existing.get("state") or "").upper() in {"FINAL", "RESEARCH", "UNAVAILABLE"}:
            return {"authority_version": cls.VERSION, **existing}

        side = str(row.get("side") or "").upper()
        prepared = bool(row.get("planned_map_valid"))
        final = bool(row.get("trade_map_valid")) and str(row.get("level_status") or "").lower() == "valid"
        entry = _number(row.get("entry") if final else row.get("planned_entry"))
        stop = _number(row.get("sl") if final else row.get("planned_sl"))
        target_1 = _number(row.get("t1") if final else row.get("planned_t1"))
        target_2 = _number(row.get("t2") if final else row.get("planned_t2"))
        geometry_ok = None not in (entry, stop, target_1) and cls._ordered(side, entry, stop, target_1)
        if not geometry_ok or not (final or prepared):
            return {
                "authority": "CANONICAL_DECISION_LEDGER",
                "authority_version": cls.VERSION,
                "state": "UNAVAILABLE",
                "valid": False,
                "research_valid": False,
                "block_reason": str(row.get("level_message") or row.get("reason") or "Canonical entry, stop and target are unavailable."),
            }

        state = "FINAL" if final else "RESEARCH"
        started_at = row.get("decision_as_of") or row.get("created_at") or row.get("updated_at")
        expires_at = row.get("thesis_expiry")
        return {
            "authority": "CANONICAL_DECISION_LEDGER",
            "authority_version": cls.VERSION,
            "state": state,
            "side": side,
            "entry": entry,
            "entry_zone": {"low": entry, "high": entry},
            "target_1": target_1,
            "target_2": target_2,
            "stop": stop,
            "room_rr": _number(row.get("rr") if final else row.get("planned_rr")),
            "valid": final,
            "research_valid": not final,
            "block_reason": "" if final else str(row.get("level_message") or "Research map is not authorised for production action."),
            "remaining_confirmations": list(row.get("rejection_reasons") or []),
            "time_axis": {
                "started_at": started_at,
                "expires_at": expires_at,
                "target_window": row.get("target_window"),
                "review_cadence": row.get("review_cadence"),
            },
            "visual_contract": {
                "kind": "RISK_REWARD_BOX",
                "line_style": "solid" if final else "dashed",
                "reward_tone": "green",
                "risk_tone": "red",
                "authority_label": "FINAL MODEL PAPER" if final else "RESEARCH MODEL PAPER",
            },
            "policy": "Projection only. It cannot promote Research geometry or grant broker/execution authority.",
        }
