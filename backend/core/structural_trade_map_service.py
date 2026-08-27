"""Structure-aware trade-map authority.

ATR is useful for sizing volatility, but ATR alone does not know whether a
repeatedly defended swing level sits between the proposed entry and target.
This service converts the full support/resistance ladder into an auditable
first-obstacle gate and structure-clamped targets.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
import math
from typing import Any, Dict, Iterable, List, Optional


def _f(value: Any) -> Optional[float]:
    try:
        out = float(value)
        return out if math.isfinite(out) else None
    except (TypeError, ValueError):
        return None


def _round(value: Any, digits: int = 2):
    num = _f(value)
    return round(num, digits) if num is not None else None


@dataclass(frozen=True)
class StructuralMapPolicy:
    obstacle_buffer_atr: float = 0.20
    obstacle_buffer_pct: float = 0.20
    merge_tolerance_pct: float = 0.55
    provisional_weight: float = 0.50
    minimum_touches: int = 2
    max_entry_distance_atr: float = 2.50
    max_entry_distance_pct: float = 4.00
    visual_line_span_pct: float = 14.0
    minimum_validated_importance: float = 45.0
    minimum_provisional_importance: float = 60.0


class StructuralTradeMapService:
    VERSION = "structural-trade-map-1.4.0-structure-first-target-authority"

    @classmethod
    def _level_rows(cls, level_report: Dict[str, Any], side: str, entry: float) -> List[Dict[str, Any]]:
        side = str(side or "").upper()
        if side == "LONG":
            validated = list(level_report.get("resistance") or [])
            provisional = list(level_report.get("provisional_resistance") or [])
            major = level_report.get("major_resistance")
            direction_ok = lambda price: price > entry
            kind = "resistance"
        else:
            validated = list(level_report.get("support") or [])
            provisional = list(level_report.get("provisional_support") or [])
            major = level_report.get("major_support")
            direction_ok = lambda price: price < entry
            kind = "support"

        rows: List[Dict[str, Any]] = []
        for raw in validated:
            price = _f((raw or {}).get("price"))
            importance = _f((raw or {}).get("importance_score")) or 0.0
            freshness = str((raw or {}).get("freshness") or "CURRENT").upper()
            timeframe = str((raw or {}).get("timeframe") or "")
            institutional = timeframe in {"1D", "1W", "1M", "VOLUME", "GAP"}
            if price is None or not direction_ok(price):
                continue
            if importance < StructuralMapPolicy.minimum_validated_importance and not institutional:
                continue
            if freshness in {"OLD", "STALE", "MISSING"} and not institutional:
                continue
            rows.append({
                **dict(raw or {}),
                "price": price,
                "validated": True,
                "confidence_weight": 1.0 + min(0.5, importance / 200.0),
                "source": (raw or {}).get("source_level") or "swing_cluster",
                "kind": (raw or {}).get("kind") or kind,
            })
        for raw in provisional:
            price = _f((raw or {}).get("price"))
            importance = _f((raw or {}).get("importance_score")) or 0.0
            freshness = str((raw or {}).get("freshness") or "").upper()
            if price is None or not direction_ok(price):
                continue
            if importance < StructuralMapPolicy.minimum_provisional_importance or freshness not in {"CURRENT", "RECENT"}:
                continue
            rows.append({
                **dict(raw or {}),
                "price": price,
                "validated": False,
                "confidence_weight": StructuralMapPolicy.provisional_weight,
                "source": "provisional_swing",
                "kind": (raw or {}).get("kind") or kind,
            })
        if isinstance(major, dict):
            price = _f(major.get("price"))
            if price is not None and direction_ok(price):
                rows.append({
                    **major,
                    "price": price,
                    "validated": True,
                    "confidence_weight": 1.25,
                    "source": "major_reversal_cluster",
                    "kind": major.get("kind") or f"major_{kind}",
                })
        return rows

    @classmethod
    def _zones(cls, rows: Iterable[Dict[str, Any]], policy: StructuralMapPolicy) -> List[Dict[str, Any]]:
        ordered = sorted((dict(row) for row in rows), key=lambda row: float(row["price"]))
        zones: List[Dict[str, Any]] = []
        for row in ordered:
            price = float(row["price"])
            target = None
            for zone in zones:
                mid = float(zone["price"])
                if mid and abs(price - mid) / mid * 100.0 <= policy.merge_tolerance_pct:
                    target = zone
                    break
            if target is None:
                target = {
                    "low": price,
                    "high": price,
                    "price": price,
                    "touches": 0,
                    "validated": False,
                    "sources": [],
                    "members": [],
                    "confidence_score": 0.0,
                }
                zones.append(target)
            target["members"].append(row)
            target["low"] = min(float(target["low"]), price)
            target["high"] = max(float(target["high"]), price)
            weighted = [(float(item["price"]), max(1.0, float(item.get("touches") or 1)) * float(item.get("confidence_weight") or 1.0)) for item in target["members"]]
            denom = sum(weight for _, weight in weighted) or 1.0
            target["price"] = sum(value * weight for value, weight in weighted) / denom
            target["touches"] = int(sum(max(1, int(item.get("touches") or 1)) for item in target["members"]))
            target["validated"] = any(bool(item.get("validated")) for item in target["members"]) or target["touches"] >= policy.minimum_touches
            target["sources"] = sorted({str(item.get("source") or "unknown") for item in target["members"]})
            target["confidence_score"] = round(sum(float(item.get("confidence_weight") or 0.0) * max(1, int(item.get("touches") or 1)) for item in target["members"]), 2)
            target["importance_score"] = round(max(float(item.get("importance_score") or 0.0) for item in target["members"]), 2)
            freshness_rank = {"CURRENT": 3, "RECENT": 2, "OLD": 1, "STALE": 0, "MISSING": 0}
            target["freshness"] = max((str(item.get("freshness") or "MISSING").upper() for item in target["members"]), key=lambda value: freshness_rank.get(value, 0))
            target["breakout_retest_states"] = sorted({str(item.get("breakout_retest_state") or "UNTESTED") for item in target["members"]})
        for zone in zones:
            zone["low"] = round(float(zone["low"]), 2)
            zone["high"] = round(float(zone["high"]), 2)
            zone["price"] = round(float(zone["price"]), 2)
        return zones

    @classmethod
    def build(
        cls,
        *,
        side: str,
        entry: Any,
        stop: Any,
        proposed_t1: Any,
        proposed_t2: Any,
        atr: Any,
        level_report: Optional[Dict[str, Any]],
        minimum_rr: float,
        current_price: Any = None,
        policy: Optional[StructuralMapPolicy] = None,
    ) -> Dict[str, Any]:
        policy = policy or StructuralMapPolicy()
        side = str(side or "").upper()
        e, sl = _f(entry), _f(stop)
        t1, t2, atr_value = _f(proposed_t1), _f(proposed_t2), _f(atr)
        report = dict(level_report or {})
        if side not in ("LONG", "SHORT") or e is None or sl is None or e == sl:
            return {
                "ok": False,
                "version": cls.VERSION,
                "state": "invalid",
                "reason": "side, entry and stop are required",
                "promotion_allowed": False,
            }

        geometry_errors = []
        if side == "LONG":
            if sl >= e:
                geometry_errors.append("LONG stop must be below entry")
            if t1 is not None and t1 <= e:
                geometry_errors.append("LONG target 1 must be above entry")
            if t2 is not None and t2 <= e:
                geometry_errors.append("LONG target 2 must be above entry")
            if t1 is not None and t2 is not None and t2 < t1:
                geometry_errors.append("LONG target 2 must not be below target 1")
        else:
            if sl <= e:
                geometry_errors.append("SHORT stop must be above entry")
            if t1 is not None and t1 >= e:
                geometry_errors.append("SHORT target 1 must be below entry")
            if t2 is not None and t2 >= e:
                geometry_errors.append("SHORT target 2 must be below entry")
            if t1 is not None and t2 is not None and t2 > t1:
                geometry_errors.append("SHORT target 2 must not be above target 1")
        if geometry_errors:
            return {
                "ok": False,
                "version": cls.VERSION,
                "state": "invalid_geometry",
                "reason": "; ".join(geometry_errors),
                "geometry_errors": geometry_errors,
                "side": side,
                "entry": _round(e),
                "stop": _round(sl),
                "target_1": _round(t1),
                "target_2": _round(t2),
                "promotion_allowed": False,
            }

        risk = abs(e - sl)
        current = _f(current_price)
        entry_distance = abs(e - current) if current is not None else None
        max_entry_distance = max(
            (atr_value or 0.0) * policy.max_entry_distance_atr,
            (current or e) * policy.max_entry_distance_pct / 100.0,
        )
        entry_near_price = entry_distance is None or entry_distance <= max_entry_distance
        rows = cls._level_rows(report, side, e)
        all_zones = cls._zones(rows, policy)
        all_zones = sorted(all_zones, key=lambda zone: zone["low"] if side == "LONG" else -zone["high"])
        # A level immediately adjacent to the trigger belongs to the trigger
        # zone; counting it again as the first obstacle manufactures tiny
        # 0.0xR rooms and double-counts the same structure. Preserve those
        # levels for evidence, but obstacle authority begins beyond the wider
        # of 0.18 ATR or 0.15% of entry. Session-structure zones are precise
        # enough that the old 0.35/0.35 noise floor could hide real obstacles.
        trigger_zone_distance = max((atr_value or 0.0) * 0.18, e * 0.0015)
        trigger_zones = [
            zone for zone in all_zones
            if (float(zone["low"]) - e if side == "LONG" else e - float(zone["high"])) <= trigger_zone_distance
        ]
        obstacle_candidates = [zone for zone in all_zones if zone not in trigger_zones]
        # Targets and promotion gates use only validated, sufficiently important
        # obstacles.  Provisional levels remain visible as research evidence but
        # cannot silently clamp a target or block a canonical setup.
        zones = [
            zone for zone in obstacle_candidates
            if zone.get("validated") and float(zone.get("importance_score") or 0.0) >= policy.minimum_validated_importance
        ]
        first = zones[0] if zones else None
        second = zones[1] if len(zones) > 1 else None
        buffer_value = max((atr_value or 0.0) * policy.obstacle_buffer_atr, e * policy.obstacle_buffer_pct / 100.0)

        first_executable = None
        obstacle_room = None
        obstacle_rr = None
        if first:
            first_executable = (float(first["low"]) - buffer_value) if side == "LONG" else (float(first["high"]) + buffer_value)
            obstacle_room = (first_executable - e) if side == "LONG" else (e - first_executable)
            obstacle_rr = obstacle_room / risk if risk > 0 else None

        out_t1, out_t2 = t1, t2
        source = "atr_no_near_structural_obstacle"
        # Structure first, reachability second. The first validated obstacle
        # owns T1 when it sits inside the desk's ATR reach envelope. If the
        # first obstacle is farther than raw T2, ATR owns T1 and the distant
        # structure remains context rather than an optimistic target.
        reach_envelope = abs((t2 if t2 is not None else t1 if t1 is not None else e) - e)
        first_within_reach = bool(first_executable is not None and obstacle_room is not None and obstacle_room > 0 and (reach_envelope <= 0 or obstacle_room <= reach_envelope + 1e-9))
        if first_within_reach:
            out_t1 = first_executable
            source = "first_structural_obstacle"
            if second is not None:
                second_executable = (float(second["low"]) - buffer_value) if side == "LONG" else (float(second["high"]) + buffer_value)
                second_room = (second_executable - e) if side == "LONG" else (e - second_executable)
                out_t2 = second_executable if second_room > obstacle_room else None
                if out_t2 is not None: source = "first_and_second_structural_obstacles"
            else:
                out_t2 = None
                source = "first_structural_obstacle_then_breakout_required"

        promotion_allowed = bool(entry_near_price)
        block_reason = None if entry_near_price else (
            f"entry is {entry_distance:.2f} away from current price; allowed {max_entry_distance:.2f}"
        )
        if promotion_allowed and first and bool(first.get("validated")):
            if obstacle_room is None or obstacle_room <= 0:
                promotion_allowed = False
                block_reason = "entry is inside the first validated structural obstacle"
            elif obstacle_rr is None or obstacle_rr + 1e-9 < float(minimum_rr):
                promotion_allowed = False
                block_reason = f"room to first validated obstacle is only {obstacle_rr:.2f}R; requires {float(minimum_rr):.2f}R"

        profit_plan = {
            "partial_fraction_at_t1": 0.50,
            "breakeven_trigger_r": 1.00,
            "trailing_trigger_r": 1.50,
            "retrace_exit_fraction_of_mfe": 0.35,
            "structural_rejection_exit": True,
            "policy": "Secure part of the position at T1, protect the remainder at cost-adjusted breakeven, trail after extension, and exit a verified obstacle rejection after a 35% retracement of MFE. Never hold an unchanged original SL after profit is secured.",
        }
        return {
            "ok": True,
            "version": cls.VERSION,
            "state": "ready" if promotion_allowed else "waiting_for_near_price_trigger",
            "side": side,
            "current_price": _round(current),
            "entry_distance": _round(entry_distance),
            "max_entry_distance": _round(max_entry_distance),
            "entry_near_price": bool(entry_near_price),
            "entry": _round(e),
            "stop": _round(sl),
            "risk": _round(risk),
            "atr": _round(atr_value),
            "zones": zones,
            "validated_obstacles": zones,
            "trigger_zones": trigger_zones,
            "trigger_zone_distance": _round(trigger_zone_distance),
            "research_zones": all_zones,
            "first_obstacle": first,
            "second_obstacle": second,
            "obstacle_buffer": _round(buffer_value),
            "first_executable_price": _round(first_executable),
            "room_to_first_obstacle": _round(obstacle_room),
            "room_rr": round(obstacle_rr, 3) if obstacle_rr is not None else None,
            "minimum_rr": float(minimum_rr),
            "promotion_allowed": promotion_allowed,
            "block_reason": block_reason,
            "t1": _round(out_t1),
            "t2": _round(out_t2),
            "target_source": source,
            "first_obstacle_within_atr_reach": first_within_reach,
            "atr_reach_envelope": _round(reach_envelope),
            "profit_protection_plan": profit_plan,
            "visual_contract": {
                "line_span_pct": policy.visual_line_span_pct,
                "entry": {"price": _round(e), "style": "solid", "label": "ENTRY"},
                "stop": {"price": _round(sl), "style": "solid", "label": "SL / INVALIDATION"},
                "target_1": {"price": _round(out_t1), "style": "solid", "label": "T1"},
                "target_2": {"price": _round(out_t2), "style": "solid", "label": "T2"},
            },
            "explanation": (
                "No validated structural obstacle was found before the ATR target."
                if first is None
                else f"First {('resistance' if side == 'LONG' else 'support')} zone {first['low']}-{first['high']} with {first['touches']} weighted touches; executable room {obstacle_rr:.2f}R."
            ),
        }
