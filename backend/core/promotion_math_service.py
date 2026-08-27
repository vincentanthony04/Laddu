"""Auditable probability/payoff mathematics for final promotion.

The service never manufactures probabilities from indicator scores.  It uses
explicit calibrated probabilities when present, computes payoff from the
persisted trade map, subtracts India cash-market costs, applies uncertainty and
tradability penalties, and emits PASS/BLOCK/SHADOW.  Missing statistical inputs
remain SHADOW rather than being silently treated as favourable.
"""
from __future__ import annotations

import math
from typing import Any, Dict, Mapping, Optional

from core.india_cost_model import IndiaCashCostModel
from core.production_mode_policy import require_production_mode

PROMOTION_MATH_VERSION = "promotion-math-1.0.0"


def _number(value: Any) -> Optional[float]:
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _probability(value: Any) -> Optional[float]:
    number = _number(value)
    if number is None:
        return None
    if number > 1.0 and number <= 100.0:
        number /= 100.0
    return number if 0.0 <= number <= 1.0 else None


def _nested(row: Mapping[str, Any], *path: str) -> Any:
    current: Any = row
    for key in path:
        if not isinstance(current, Mapping):
            return None
        current = current.get(key)
    return current


class PromotionMathService:
    ASSUMED_NOTIONAL = 100000.0
    UNCERTAINTY_PENALTY_WEIGHT = 0.50

    @classmethod
    def _probabilities(cls, candidate: Mapping[str, Any]) -> tuple[Optional[float], Optional[float], Optional[list[float]], list[str]]:
        missing = []
        p_trigger = None
        for value in (
            candidate.get("probability_trigger"), candidate.get("p_trigger"),
            candidate.get("trigger_probability"), _nested(candidate, "prediction", "p_trigger"),
        ):
            p_trigger = _probability(value)
            if p_trigger is not None:
                break
        if p_trigger is None:
            missing.append("P(trigger)")

        calibrated = candidate.get("calibrated_edge") if isinstance(candidate.get("calibrated_edge"), Mapping) else {}
        p_success = None
        for value in (
            candidate.get("probability_success_given_trigger"), candidate.get("p_success_given_trigger"),
            candidate.get("success_probability"), _nested(candidate, "prediction", "p_success_given_trigger"),
            calibrated.get("posterior_probability_positive"),
        ):
            p_success = _probability(value)
            if p_success is not None:
                break
        if p_success is None:
            missing.append("P(success|trigger)")

        interval = calibrated.get("probability_interval_90")
        if not (isinstance(interval, (list, tuple)) and len(interval) == 2):
            interval = candidate.get("probability_interval_90")
        parsed_interval = None
        if isinstance(interval, (list, tuple)) and len(interval) == 2:
            low, high = _probability(interval[0]), _probability(interval[1])
            if low is not None and high is not None and high >= low:
                parsed_interval = [low, high]
        return p_trigger, p_success, parsed_interval, missing

    @classmethod
    def _payoff(cls, candidate: Mapping[str, Any]) -> tuple[Optional[float], Optional[float], Optional[float], Dict[str, Any], list[str]]:
        mode = require_production_mode(candidate.get("mode"))
        side = str(candidate.get("side") or "").upper()
        entry = _number(candidate.get("entry") if candidate.get("entry") is not None else candidate.get("planned_entry"))
        stop = _number(candidate.get("sl") if candidate.get("sl") is not None else candidate.get("stop"))
        target = _number(candidate.get("t1") if candidate.get("t1") is not None else candidate.get("target"))
        missing = []
        if entry is None or entry <= 0:
            missing.append("entry")
        if stop is None or stop <= 0:
            missing.append("stop")
        if target is None or target <= 0:
            missing.append("target")
        if side not in {"LONG", "SHORT"}:
            missing.append("side")
        if missing:
            return None, None, None, {"state": "UNAVAILABLE", "reason": ", ".join(missing) + " missing"}, missing

        risk_points = (entry - stop) if side == "LONG" else (stop - entry)
        reward_points = (target - entry) if side == "LONG" else (entry - target)
        if risk_points <= 0:
            missing.append("positive initial risk distance")
        if reward_points <= 0:
            missing.append("positive target distance")
        if missing:
            return None, None, None, {"state": "INVALID_TRADE_MAP", "reason": ", ".join(missing)}, missing

        gain_r = reward_points / risk_points
        loss_r = 1.0
        quantity = max(1, int(cls.ASSUMED_NOTIONAL // entry))
        cost_model = IndiaCashCostModel.for_evidence(mode, dict(candidate))
        costs = cost_model.round_trip(entry, target, quantity)
        cost_per_share = float(costs["costs"]["total"]) / quantity
        cost_r = cost_per_share / risk_points
        return gain_r, loss_r, cost_r, {
            "state": "ESTIMATED",
            "entry": entry,
            "stop": stop,
            "target": target,
            "initial_risk_points": round(risk_points, 6),
            "reward_points": round(reward_points, 6),
            "gross_reward_r": round(gain_r, 6),
            "loss_r": loss_r,
            "quantity_assumption": quantity,
            "notional_assumption": round(quantity * entry, 2),
            "round_trip_cost": costs["costs"]["total"],
            "cost_per_share": round(cost_per_share, 6),
            "cost_r": round(cost_r, 6),
            "cost_model_version": cost_model.config.version,
            "cost_authority": costs.get("cost_authority"),
            "cost_authority_version": costs.get("cost_authority_version"),
            "cost_exchange": cost_model.config.exchange,
            "cost_bse_group": cost_model.config.bse_group,
        }, []

    @staticmethod
    def _tradability(candidate: Mapping[str, Any]) -> tuple[Optional[float], list[str]]:
        for value in (
            candidate.get("tradability"), candidate.get("tradability_score"), candidate.get("liquidity_score"),
            _nested(candidate, "execution_quality", "tradability"), _nested(candidate, "execution_quality", "score"),
        ):
            score = _number(value)
            if score is None:
                continue
            if score > 1.0 and score <= 100.0:
                score /= 100.0
            if 0.0 <= score <= 1.0:
                return score, []
        return None, ["tradability"]

    @classmethod
    def evaluate(cls, candidate: Mapping[str, Any]) -> Dict[str, Any]:
        mode = require_production_mode(candidate.get("mode"))
        p_trigger, p_success, interval, missing_probabilities = cls._probabilities(candidate)
        gain_r, loss_r, cost_r, payoff, missing_payoff = cls._payoff(candidate)
        tradability, missing_tradability = cls._tradability(candidate)
        missing = list(dict.fromkeys(missing_probabilities + missing_payoff + missing_tradability))

        uncertainty = None
        if interval is not None:
            uncertainty = max(0.0, interval[1] - interval[0])
        elif p_success is not None:
            # No confidence interval means uncertainty is not measured; do not
            # invent a narrow interval from the point estimate.
            missing.append("probability uncertainty interval")

        expected_conditional_r = None
        expected_pre_penalty_r = None
        expected_net_r = None
        uncertainty_penalty_r = None
        if not missing and None not in (p_trigger, p_success, gain_r, loss_r, cost_r, tradability, uncertainty):
            expected_conditional_r = p_success * gain_r - (1.0 - p_success) * loss_r - cost_r
            expected_pre_penalty_r = p_trigger * expected_conditional_r * tradability
            uncertainty_penalty_r = uncertainty * cls.UNCERTAINTY_PENALTY_WEIGHT
            expected_net_r = expected_pre_penalty_r - uncertainty_penalty_r

        if missing:
            gate = "SHADOW"
            state = "INSUFFICIENT_MEASURED_INPUTS"
            reason = "promotion mathematics incomplete: " + ", ".join(dict.fromkeys(missing))
        elif expected_net_r is None:
            gate = "SHADOW"
            state = "UNCOMPUTABLE"
            reason = "promotion mathematics could not be computed"
        elif expected_net_r <= 0:
            gate = "BLOCK"
            state = "NON_POSITIVE_POST_COST_EXPECTANCY"
            reason = "probability-weighted post-cost expectancy is not positive after uncertainty and tradability penalties"
        else:
            gate = "PASS"
            state = "POSITIVE_POST_COST_EXPECTANCY"
            reason = "probability-weighted post-cost expectancy is positive after uncertainty and tradability penalties"

        return {
            "version": PROMOTION_MATH_VERSION,
            "mode": mode,
            "state": state,
            "gate": gate,
            "reason": reason,
            "p_trigger": round(p_trigger, 6) if p_trigger is not None else None,
            "p_success_given_trigger": round(p_success, 6) if p_success is not None else None,
            "probability_interval_90": [round(v, 6) for v in interval] if interval is not None else None,
            "uncertainty_width": round(uncertainty, 6) if uncertainty is not None else None,
            "tradability": round(tradability, 6) if tradability is not None else None,
            "gain_r": round(gain_r, 6) if gain_r is not None else None,
            "loss_r": round(loss_r, 6) if loss_r is not None else None,
            "cost_r": round(cost_r, 6) if cost_r is not None else None,
            "expected_conditional_r": round(expected_conditional_r, 6) if expected_conditional_r is not None else None,
            "expected_pre_penalty_r": round(expected_pre_penalty_r, 6) if expected_pre_penalty_r is not None else None,
            "uncertainty_penalty_r": round(uncertainty_penalty_r, 6) if uncertainty_penalty_r is not None else None,
            "expected_net_r": round(expected_net_r, 6) if expected_net_r is not None else None,
            "payoff": payoff,
            "missing_inputs": list(dict.fromkeys(missing)),
            "policy": "explicit calibrated inputs only; missing mathematics stays shadow and validated non-positive expectancy vetoes promotion",
        }
