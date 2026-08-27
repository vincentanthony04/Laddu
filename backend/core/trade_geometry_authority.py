"""Canonical entry/stop/target geometry authority for Delivery and Intraday.

The desk policy owns ATR multipliers; StructuralTradeMapService owns obstacle
ranking/target clamping.  This authority composes those two intentional pieces
and preserves the established nearest-S/R stop/fallback clamps so every caller
projects the same geometry instead of hard-coding its own multipliers.
"""
from __future__ import annotations

import math
from typing import Any, Dict, Optional
from core.numeric_semantics import finite_number

from core.production_mode_policy import policy_for, require_production_mode
from core.structural_trade_map_service import StructuralTradeMapService


class TradeGeometryAuthority:
    authority = "TradeGeometryAuthority"
    authority_version = "1.1.0-structural-invalidation-risk-budget"

    @staticmethod
    def _num(value: Any) -> Optional[float]:
        return finite_number(value)

    @classmethod
    def validate_map(
        cls,
        *,
        side: str,
        entry: Any,
        stop: Any,
        target_1: Any,
        target_2: Any = None,
        support: Any = None,
        resistance: Any = None,
    ) -> Dict[str, Any]:
        side_u = str(side or "").upper()
        e, sl = cls._num(entry), cls._num(stop)
        t1, t2 = cls._num(target_1), cls._num(target_2)
        sup, res = cls._num(support), cls._num(resistance)
        blockers: list[str] = []
        if side_u not in {"LONG", "SHORT"} or e is None or sl is None or t1 is None:
            blockers.append("INCOMPLETE_ACTIONABLE_LEVEL_MAP")
        elif side_u == "LONG":
            if not sl < e: blockers.append("LONG_STOP_NOT_BELOW_ENTRY")
            if not t1 > e or (t2 is not None and t2 < t1): blockers.append("LONG_TARGET_ORDER_INVALID")
            if sup is not None and sup >= e: blockers.append("LONG_SUPPORT_NOT_BELOW_ENTRY")
            if res is not None and res <= e: blockers.append("LONG_RESISTANCE_NOT_ABOVE_ENTRY")
        else:
            if not sl > e: blockers.append("SHORT_STOP_NOT_ABOVE_ENTRY")
            if not t1 < e or (t2 is not None and t2 > t1): blockers.append("SHORT_TARGET_ORDER_INVALID")
            if res is not None and res <= e: blockers.append("SHORT_RESISTANCE_NOT_ABOVE_ENTRY")
            if sup is not None and sup >= e: blockers.append("SHORT_SUPPORT_NOT_BELOW_ENTRY")
        return {
            "valid": not blockers,
            "authority": cls.authority,
            "authority_version": cls.authority_version,
            "blockers": blockers,
            "message": "Side-aware S/R map valid" if not blockers else "; ".join(blockers),
        }

    @classmethod
    def project(
        cls,
        *,
        mode: str,
        side: str,
        entry: Any,
        atr: Any,
        level_report: Optional[Dict[str, Any]] = None,
        nearest_support: Any = None,
        nearest_resistance: Any = None,
        current_price: Any = None,
    ) -> Dict[str, Any]:
        desk = require_production_mode(mode)
        side_u = str(side or "").upper()
        e, atr_value = cls._num(entry), cls._num(atr)
        if side_u not in {"LONG", "SHORT"} or e is None or e <= 0 or atr_value is None or atr_value <= 0:
            return {
                "ok": False, "authority": cls.authority, "authority_version": cls.authority_version,
                "state": "INVALID_INPUT", "reason": "side, positive entry and positive ATR are required",
            }
        policy = policy_for(desk)
        sup, res = cls._num(nearest_support), cls._num(nearest_resistance)
        sign = 1.0 if side_u == "LONG" else -1.0
        raw_stop = e - sign * atr_value * float(policy.sl_atr)
        raw_t1 = e + sign * atr_value * float(policy.t1_atr)
        raw_t2 = e + sign * atr_value * float(policy.t2_atr)

        stop = raw_stop
        stop_source = "desk_atr_policy"
        fallback_t1, fallback_t2 = raw_t1, raw_t2
        fallback_target_source = "desk_atr_policy"
        structural_invalidation_required = None
        structural_risk_budget_block = False
        structural_risk_reason = None
        invalidation_buffer = max(atr_value * 0.15, e * 0.0015)
        # Structure owns invalidation. If the structurally correct stop would
        # require more risk than the desk ATR budget permits, reject the setup
        # instead of hiding the stop inside ordinary support/resistance noise.
        if side_u == "LONG":
            if sup is not None and sup < e:
                required = sup - invalidation_buffer
                structural_invalidation_required = required
                if raw_stop <= required:
                    stop = required
                    stop_source = "structural_support_invalidation"
                else:
                    structural_risk_budget_block = True
                    structural_risk_reason = f"STRUCTURAL_STOP_OUTSIDE_RISK_BUDGET: support {sup:.4f} requires stop <= {required:.4f}, desk ATR stop is {raw_stop:.4f}"
            if res is not None and res > e:
                if fallback_t1 > res:
                    fallback_t1 = max(e + (res - e) * 0.85, e); fallback_target_source = "nearest_resistance_fallback"
                if fallback_t2 > res:
                    fallback_t2 = res; fallback_target_source = "nearest_resistance_fallback"
        else:
            if res is not None and res > e:
                required = res + invalidation_buffer
                structural_invalidation_required = required
                if raw_stop >= required:
                    stop = required
                    stop_source = "structural_resistance_invalidation"
                else:
                    structural_risk_budget_block = True
                    structural_risk_reason = f"STRUCTURAL_STOP_OUTSIDE_RISK_BUDGET: resistance {res:.4f} requires stop >= {required:.4f}, desk ATR stop is {raw_stop:.4f}"
            if sup is not None and sup < e:
                if fallback_t1 < sup:
                    fallback_t1 = min(e - (e - sup) * 0.85, e); fallback_target_source = "nearest_support_fallback"
                if fallback_t2 < sup:
                    fallback_t2 = sup; fallback_target_source = "nearest_support_fallback"

        report = dict(level_report or {})
        has_structural_authority = report.get("ok") is True
        proposed_t1 = raw_t1 if has_structural_authority else fallback_t1
        proposed_t2 = raw_t2 if has_structural_authority else fallback_t2
        structural_map = StructuralTradeMapService.build(
            side=side_u,
            entry=e,
            stop=stop,
            proposed_t1=proposed_t1,
            proposed_t2=proposed_t2,
            atr=atr_value,
            level_report=report if has_structural_authority else None,
            minimum_rr=float(policy.minimum_net_rr),
            current_price=current_price,
        )
        if structural_map.get("ok"):
            target_1 = cls._num(structural_map.get("t1"))
            target_2 = cls._num(structural_map.get("t2"))
            target_source = (
                str(structural_map.get("target_source") or fallback_target_source)
                if has_structural_authority else fallback_target_source
            )
        else:
            target_1, target_2 = fallback_t1, fallback_t2
            target_source = fallback_target_source

        validation = cls.validate_map(
            side=side_u, entry=e, stop=stop, target_1=target_1, target_2=target_2,
            support=sup, resistance=res,
        )
        promotion_allowed = bool(structural_map.get("promotion_allowed", True)) and validation["valid"] and not structural_risk_budget_block
        if structural_risk_budget_block:
            structural_map = dict(structural_map)
            structural_map["promotion_allowed"] = False
            structural_map["block_reason"] = structural_risk_reason
            structural_map["state"] = "structural_risk_budget_blocked"
        return {
            "ok": True,
            "authority": cls.authority,
            "authority_version": cls.authority_version,
            "desk_policy_version": policy.policy_version,
            "mode": desk,
            "side": side_u,
            "entry": round(e, 4),
            "atr": round(atr_value, 4),
            "raw_stop": round(raw_stop, 4),
            "raw_target_1": round(raw_t1, 4),
            "raw_target_2": round(raw_t2, 4),
            "stop": round(stop, 4),
            "target_1": round(target_1, 4) if target_1 is not None else None,
            "target_2": round(target_2, 4) if target_2 is not None else None,
            "stop_source": stop_source,
            "target_source": target_source,
            "minimum_net_rr": float(policy.minimum_net_rr),
            "sl_atr": float(policy.sl_atr),
            "t1_atr": float(policy.t1_atr),
            "t2_atr": float(policy.t2_atr),
            "structural_map": structural_map,
            "map_validation": validation,
            "promotion_allowed": promotion_allowed,
            "structural_invalidation_required": round(structural_invalidation_required, 4) if structural_invalidation_required is not None else None,
            "structural_invalidation_buffer": round(invalidation_buffer, 4),
            "structural_risk_budget_block": structural_risk_budget_block,
            "structural_risk_reason": structural_risk_reason,
            "policy": "Price structure owns invalidation. A structurally correct stop may be tightened within the desk ATR budget; if the required structural stop is wider than that budget the trade is rejected. Structural obstacles then own target feasibility.",
        }


DEFAULT_TRADE_GEOMETRY_AUTHORITY = TradeGeometryAuthority()
