"""Deterministic open-position lifecycle and profit-protection authority.

The service never changes the original thesis silently.  It records the
original stop, managed stop, partial-profit state, high-water mark and exact
reason for every transition so a later audit can reconstruct the advice.
"""
from __future__ import annotations

import math
from typing import Any, Dict, Optional

from core.india_cost_model import IndiaCashCostModel


def _f(value: Any) -> Optional[float]:
    try:
        out = float(value)
        return out if math.isfinite(out) else None
    except (TypeError, ValueError):
        return None


def _r(value: Any):
    out = _f(value)
    return round(out, 2) if out is not None else None


class PositionLifecycleService:
    # Legacy SQLite/diagnostic compatibility only. Production Model Paper lifecycle
    # is CanonicalTradeLifecycleAuthority and this class has zero production influence.
    production_influence = 0
    lifecycle_scope = "LEGACY_RESEARCH_ONLY"
    VERSION = "position-lifecycle-1.1.0-nonnegative-excursions"
    PARTIAL_FRACTION = 0.50
    BREAKEVEN_TRIGGER_R = 1.00
    TRAILING_TRIGGER_R = 1.50
    TRAIL_ATR_MULT = 0.80
    TRAIL_RISK_MULT = 0.35
    MFE_RETRACE_FRACTION = 0.35

    @classmethod
    def _directional_move(cls, side: str, entry: float, price: float) -> float:
        return price - entry if side == "LONG" else entry - price

    @classmethod
    def _blended_pnl(cls, side: str, entry: float, exit_price: float, payload: Dict[str, Any]) -> float:
        fraction = min(1.0, max(0.0, _f(payload.get("secured_fraction")) or 0.0))
        secured_price = _f(payload.get("secured_price"))
        secured_move = cls._directional_move(side, entry, secured_price) if secured_price is not None and fraction > 0 else 0.0
        remaining_move = cls._directional_move(side, entry, exit_price) * (1.0 - fraction)
        return round(secured_move * fraction + remaining_move, 2)

    @classmethod
    def evaluate_tick(cls, row: Dict[str, Any], payload: Optional[Dict[str, Any]], ltp: Any) -> Dict[str, Any]:
        payload = dict(payload or {})
        side = str(row.get("side") or payload.get("side") or "").upper()
        entry = _f(row.get("entry") if row.get("entry") is not None else payload.get("entry"))
        original_sl = _f(row.get("sl") if row.get("sl") is not None else payload.get("sl"))
        t1 = _f(row.get("t1") if row.get("t1") is not None else payload.get("t1"))
        t2 = _f(row.get("t2") if row.get("t2") is not None else payload.get("t2"))
        price = _f(ltp)
        if side not in ("LONG", "SHORT") or entry is None or original_sl is None or price is None or entry == original_sl:
            return {"ok": False, "status": "OPEN", "result": "OPEN", "reason": "incomplete lifecycle inputs", "payload": payload}

        # Reject an economically impossible token/price-scale jump.
        if entry <= 0 or price <= 0 or not (0.5 <= price / entry <= 2.0):
            payload["lifecycle_state"] = "PRICE_SCALE_REJECTED"
            return {"ok": False, "status": "OPEN", "result": "OPEN_PRICE_SCALE_REJECTED", "reason": "price scale inconsistent with entry", "payload": payload}

        risk = abs(entry - original_sl)
        atr = _f(payload.get("atr14") if payload.get("atr14") is not None else payload.get("atr"))
        prior_high = _f(payload.get("high_water_price"))
        prior_low = _f(payload.get("low_water_price"))
        # Excursion baselines include the actual entry so MFE/MAE are always
        # non-negative magnitudes.  An adverse first tick therefore has MFE=0,
        # not a negative favourable excursion; the inverse holds for MAE.
        high_candidates = [entry, price]
        low_candidates = [entry, price]
        if prior_high is not None:
            high_candidates.append(prior_high)
        if prior_low is not None:
            low_candidates.append(prior_low)
        high_water = max(high_candidates)
        low_water = min(low_candidates)
        if side == "LONG":
            favourable = max(0.0, high_water - entry)
            adverse = max(0.0, entry - low_water)
        else:
            favourable = max(0.0, entry - low_water)
            adverse = max(0.0, high_water - entry)
        mfe_r = favourable / risk if risk > 0 else 0.0
        mae_r = adverse / risk if risk > 0 else 0.0

        payload.update({
            "lifecycle_version": cls.VERSION,
            "original_sl": _r(original_sl),
            "high_water_price": _r(high_water),
            "low_water_price": _r(low_water),
            "mfe": _r(favourable),
            "mae": _r(adverse),
            "mfe_r": round(mfe_r, 3),
            "mae_r": round(mae_r, 3),
        })

        secured_fraction = min(1.0, max(0.0, _f(payload.get("secured_fraction")) or 0.0))
        secured_price = _f(payload.get("secured_price"))
        managed_sl = _f(payload.get("managed_sl"))
        mode = str(row.get("mode") or payload.get("mode") or "delivery").strip().lower()
        if mode not in {"intraday", "delivery"}:
            mode = "delivery"
        breakeven_estimate = IndiaCashCostModel.for_evidence(mode, {**dict(payload), **dict(row)}).breakeven_exit(
            entry=entry, side=side, quantity=int(payload.get("reference_quantity") or 100),
        )
        breakeven = float(breakeven_estimate["breakeven_exit"])
        cost_buffer = abs(breakeven - entry)
        payload["cost_profile"] = breakeven_estimate["profile"]
        payload["cost_version"] = breakeven_estimate["cost_version"]
        payload["breakeven_cost_estimate"] = breakeven_estimate
        transitions = list(payload.get("lifecycle_transitions") or [])
        first_obstacle = _f(payload.get("first_obstacle") or payload.get("first_obstacle_low") or payload.get("resistance" if side == "LONG" else "support"))
        obstacle_touched = bool(payload.get("obstacle_touched"))
        touches_obstacle = (first_obstacle is not None and price >= first_obstacle) if side == "LONG" else (first_obstacle is not None and price <= first_obstacle)
        if touches_obstacle:
            obstacle_touched = True
            payload["obstacle_touched"] = True
            payload["obstacle_touch_price"] = _r(price)

        def transition(state: str, reason: str):
            if not transitions or transitions[-1].get("state") != state:
                transitions.append({"state": state, "price": _r(price), "reason": reason})
            payload["lifecycle_state"] = state
            payload["lifecycle_reason"] = reason

        # T1 secures part of the position.  The remaining quantity is protected
        # at cost-adjusted breakeven instead of retaining the original loss stop.
        t1_hit = (t1 is not None and price >= t1) if side == "LONG" else (t1 is not None and price <= t1)
        if t1_hit and secured_fraction < cls.PARTIAL_FRACTION:
            secured_fraction = cls.PARTIAL_FRACTION
            secured_price = t1
            managed_sl = max(managed_sl if managed_sl is not None else -math.inf, breakeven) if side == "LONG" else min(managed_sl if managed_sl is not None else math.inf, breakeven)
            transition("PARTIAL_PROFIT_SECURED", "T1 reached: 50% secured and remainder protected at cost-adjusted breakeven")

        # Even before the formal T1, one full R of favourable excursion earns a
        # breakeven guard. This prevents a meaningful open profit from becoming a
        # full original-stop loss solely because T1 was placed too ambitiously.
        if mfe_r >= cls.BREAKEVEN_TRIGGER_R:
            managed_sl = max(managed_sl if managed_sl is not None else -math.inf, breakeven) if side == "LONG" else min(managed_sl if managed_sl is not None else math.inf, breakeven)
            if secured_fraction <= 0:
                transition("BREAKEVEN_PROTECTED", "Maximum favourable excursion reached 1R")

        # After 1.5R, trail from the high/low-water mark.  Use the wider of ATR
        # and a fraction of original risk so normal noise does not force an exit.
        if mfe_r >= cls.TRAILING_TRIGGER_R:
            trail_gap = max((atr or 0.0) * cls.TRAIL_ATR_MULT, risk * cls.TRAIL_RISK_MULT, cost_buffer)
            trailing = high_water - trail_gap if side == "LONG" else low_water + trail_gap
            managed_sl = max(managed_sl if managed_sl is not None else -math.inf, trailing, breakeven) if side == "LONG" else min(managed_sl if managed_sl is not None else math.inf, trailing, breakeven)
            transition("TRAILING_PROFIT", f"Favourable excursion {mfe_r:.2f}R; trailing gap {trail_gap:.2f}")

        extended = bool(secured_fraction > 0 or mfe_r >= 0.75)
        payload.update({
            "secured_fraction": round(secured_fraction, 4),
            "secured_price": _r(secured_price),
            "managed_sl": _r(managed_sl),
            "breakeven_price": _r(breakeven),
            "add_allowed": not extended,
            "fomo_guard": "NO_ADD_AFTER_EXTENSION" if extended else "ADD_ONLY_ON_ORIGINAL_APPROVED_MAP",
            "reentry_policy": "A protected/closed position requires a new fully approved signal; do not chase the prior move.",
            "lifecycle_transitions": transitions[-20:],
        })

        # T2 is evaluated before the structural-rejection/trailing stop on a
        # latest-tick path. Candle sequence ambiguity is handled separately by
        # candle audit, while live ticks preserve the real order.
        t2_hit = (t2 is not None and price >= t2) if side == "LONG" else (t2 is not None and price <= t2)
        if t2_hit:
            pnl = cls._blended_pnl(side, entry, t2, payload)
            transition("CLOSED_T2", "Final target reached after managed lifecycle")
            return {"ok": True, "status": "SUCCESS", "result": "SUCCESS_T2_MANAGED", "exit": _r(t2), "pnl": pnl, "reason": payload["lifecycle_reason"], "payload": payload}

        retracement = (high_water - price) if side == "LONG" else (price - low_water)
        retrace_fraction = (retracement / favourable) if favourable > 0 else 0.0
        payload["mfe_retrace"] = _r(retracement)
        payload["mfe_retrace_fraction"] = round(retrace_fraction, 4)
        structural_min_move = max(risk * 0.35, cost_buffer * 4.0)
        if obstacle_touched and favourable >= structural_min_move and retrace_fraction >= cls.MFE_RETRACE_FRACTION:
            pnl = cls._blended_pnl(side, entry, price, payload)
            transition(
                "CLOSED_STRUCTURAL_REJECTION",
                f"Known structural obstacle was touched and price retraced {retrace_fraction * 100:.1f}% of MFE",
            )
            payload["reentry_allowed"] = False
            return {
                "ok": True,
                "status": "SUCCESS" if pnl > 0 else "FAIL" if pnl < 0 else "SUCCESS",
                "result": "STRUCTURAL_REJECTION_EXIT",
                "exit": _r(price), "pnl": pnl,
                "reason": payload["lifecycle_reason"], "payload": payload,
            }

        active_stop = managed_sl if managed_sl is not None else original_sl
        stop_hit = price <= active_stop if side == "LONG" else price >= active_stop
        if stop_hit:
            pnl = cls._blended_pnl(side, entry, active_stop, payload)
            if managed_sl is not None:
                result = "PROTECTED_EXIT_AFTER_T1" if secured_fraction > 0 else "BREAKEVEN_OR_TRAILING_EXIT"
                transition("CLOSED_PROTECTED", "Managed profit-protection stop reached")
                status = "SUCCESS" if pnl > 0 else "FAIL" if pnl < 0 else "SUCCESS"
            else:
                result = "FAIL_SL"
                transition("CLOSED_ORIGINAL_SL", "Original thesis stop reached before profit protection")
                status = "FAIL"
            return {"ok": True, "status": status, "result": result, "exit": _r(active_stop), "pnl": pnl, "reason": payload["lifecycle_reason"], "payload": payload}

        if not payload.get("lifecycle_state"):
            transition("OPEN_RISK", "Position has not yet earned profit protection")
        result = "T1_PARTIAL_SECURED" if secured_fraction > 0 else "OPEN_PROTECTED" if managed_sl is not None else "OPEN"
        return {"ok": True, "status": "OPEN", "result": result, "exit": None, "pnl": None, "reason": payload.get("lifecycle_reason"), "payload": payload}
