from __future__ import annotations

"""Post-cost, uncertainty-aware decision mathematics for Model Paper admission.

This service does not create direction, targets, stops, or model predictions. It
only evaluates an already-governed candidate against one frozen champion
prediction. Missing semantics, stale/missing governance, incomplete outcome
probabilities, or non-positive lower-bound expectancy fail closed.
"""

from dataclasses import asdict
import math
from typing import Any, Mapping

from core.india_cash_cost_service import IndiaCashCostService
from core.execution_slippage_calibration_authority import DEFAULT_EXECUTION_SLIPPAGE_CALIBRATION_AUTHORITY
from core.expectancy_semantics_authority import lane as expectancy_lane
from core.production_mode_policy import policy_for, require_production_mode
from core.quant_v68.decision_math import (
    CostBreakdown,
    ExpectedValueInputs,
    RankingUtilityInputs,
    calculate_expected_value,
    calculate_ranking_utility,
)


SERVICE_VERSION = "production-decision-math-1.0.0-post-cost-lcb"
SUPPORTED_RETURN_BASES = {
    "GROSS_POSITION_RETURN_BEFORE_COSTS",
    "NET_POSITION_RETURN_AFTER_COSTS",
}


def _number(value: Any) -> float | None:
    try:
        out = float(value)
        return out if math.isfinite(out) else None
    except (TypeError, ValueError):
        return None


def _probability(value: Any) -> float | None:
    out = _number(value)
    return out if out is not None and 0.0 <= out <= 1.0 else None


def _price(candidate: Mapping[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = _number(candidate.get(key))
        if value is not None:
            return value
    return None


def _cost_breakdown(costs: Mapping[str, Any], *, spread_cost: float = 0.0) -> CostBreakdown:
    return CostBreakdown(
        brokerage=_number(costs.get("brokerage")) or 0.0,
        stt=_number(costs.get("stt")) or 0.0,
        exchange_fees=_number(costs.get("exchange_transaction")) or 0.0,
        ipft=_number(costs.get("ipft")) or 0.0,
        sebi_fees=_number(costs.get("sebi_fee")) or 0.0,
        gst=_number(costs.get("gst")) or 0.0,
        stamp_duty=_number(costs.get("stamp_duty")) or 0.0,
        spread=max(0.0, float(spread_cost or 0.0)),
        slippage=_number(costs.get("slippage")) or 0.0,
        market_impact=_number(costs.get("impact")) or 0.0,
        dp_charges=_number(costs.get("dp_charge")) or 0.0,
    )


class ProductionDecisionMathService:
    def __init__(self, *, cost_service: IndiaCashCostService | None = None):
        self.cost_service = cost_service or IndiaCashCostService()

    @staticmethod
    def unavailable(reason_code: str, reason: str) -> dict[str, Any]:
        return {
            "ok": False,
            "service_version": SERVICE_VERSION,
            "state": "MODEL_MATH_UNAVAILABLE",
            "capital_admissible": False,
            "blockers": [str(reason_code)],
            "reason": str(reason),
            "probability_source": "NONE",
            "heuristic_score_is_probability": False,
        }

    def evaluate(
        self,
        candidate: Mapping[str, Any],
        governed_signal: Mapping[str, Any] | None,
        *,
        quantity: int,
    ) -> dict[str, Any]:
        signal = dict(governed_signal or {})
        if signal.get("eligible") is not True:
            return self.unavailable(
                str(signal.get("state") or "NO_ACTIVE_CHAMPION"),
                str(signal.get("reason") or "No effective governed champion prediction is available."),
            )
        prediction = dict(signal.get("prediction_contract") or {})
        if not prediction:
            return self.unavailable("PREDICTION_CONTRACT_MISSING", "Champion inference lacks the frozen prediction contract.")

        mode = require_production_mode(candidate.get("mode"))
        side = str(candidate.get("side") or "").strip().upper()
        if side not in {"LONG", "SHORT"}:
            return self.unavailable("DIRECTION_MISSING", "Decision mathematics requires an explicit LONG or SHORT side.")
        qty = int(quantity or 0)
        if qty <= 0:
            return self.unavailable("QUANTITY_ZERO", "Decision mathematics requires a positive bounded quantity.")
        try:
            venue = self.cost_service.venue_identity(
                exchange=str(candidate.get("exchange") or ""),
                bse_group=(
                    str(candidate.get("bse_group") or "").strip().upper() or None
                ),
            )
        except ValueError as exc:
            return self.unavailable("VENUE_COST_IDENTITY_INVALID", str(exc))

        entry = _price(candidate, "entry", "planned_entry")
        stop = _price(candidate, "sl", "stop", "planned_sl")
        target = _price(candidate, "target", "t1", "planned_target", "planned_t1")
        prediction_entry = _number(prediction.get("observation_price"))
        prediction_target = _number(prediction.get("target_price"))
        prediction_stop = _number(prediction.get("stop_price"))
        blockers: list[str] = []
        if entry is None or stop is None or target is None or min(entry, stop, target) <= 0:
            return self.unavailable("TRADE_MAP_INCOMPLETE", "Entry, stop and target are required for post-cost expectancy.")
        if prediction_entry is None or prediction_target is None or prediction_stop is None:
            blockers.append("PREDICTION_TRADE_MAP_MISSING")
        else:
            tolerance = max(entry * 0.001, 0.01)
            if abs(prediction_entry - entry) > tolerance:
                blockers.append("PREDICTION_ENTRY_MISMATCH")
            if abs(prediction_target - target) > tolerance:
                blockers.append("PREDICTION_TARGET_MISMATCH")
            if abs(prediction_stop - stop) > tolerance:
                blockers.append("PREDICTION_STOP_MISMATCH")
        geometry_ok = (
            side == "LONG" and target > entry > stop
        ) or (
            side == "SHORT" and target < entry < stop
        )
        if not geometry_ok:
            blockers.append("INVALID_TRADE_GEOMETRY")

        pt = _probability(prediction.get("target_before_stop_probability"))
        ps = _probability(prediction.get("stop_before_target_probability"))
        pn = _probability(prediction.get("neither_probability"))
        if pt is None or ps is None or pn is None:
            blockers.append("OUTCOME_PROBABILITY_CONTRACT_INCOMPLETE")
        elif abs(pt + ps + pn - 1.0) > 1e-6:
            blockers.append("OUTCOME_PROBABILITIES_DO_NOT_SUM_TO_ONE")

        q05 = _number(prediction.get("return_q05"))
        q50 = _number(prediction.get("return_q50"))
        q95 = _number(prediction.get("return_q95"))
        if q05 is None or q50 is None or q95 is None or not q05 <= q50 <= q95:
            blockers.append("RETURN_DISTRIBUTION_INVALID")
        lower_return = _number(prediction.get("uncertainty_lower"))
        upper_return = _number(prediction.get("uncertainty_upper"))
        uncertainty_method = str(prediction.get("uncertainty_method") or "").strip().upper()
        if uncertainty_method in {"CONFORMAL_INTERVAL", "EMPIRICAL_BOOTSTRAP_INTERVAL"}:
            if lower_return is None or upper_return is None or lower_return > upper_return:
                blockers.append("UNCERTAINTY_INTERVAL_INVALID")
        elif uncertainty_method != "NORMAL_STANDARD_ERROR":
            blockers.append("UNCERTAINTY_METHOD_UNSUPPORTED")

        return_basis = str(prediction.get("return_basis") or "").strip().upper()
        if return_basis not in SUPPORTED_RETURN_BASES:
            blockers.append("RETURN_BASIS_MISSING")
        sample_size = int(_number(prediction.get("effective_sample_size")) or 0)
        standard_error = _number(prediction.get("net_return_standard_error"))
        if sample_size < 2:
            blockers.append("EFFECTIVE_SAMPLE_SIZE_INSUFFICIENT")
        if standard_error is None or standard_error < 0:
            blockers.append("STANDARD_ERROR_INVALID")
        if blockers:
            return {
                **self.unavailable("MATHEMATICAL_CONTRACT_FAILED", "Frozen champion prediction is incomplete or inconsistent."),
                "blockers": sorted(set(blockers)),
                "prediction_id": signal.get("prediction_id"),
                "model_id": signal.get("model_id"),
            }

        schedule = self.cost_service.schedule_for()
        schedule_slippage_bps = schedule.intraday_slippage_bps if mode == "intraday" else schedule.delivery_slippage_bps
        execution_model = DEFAULT_EXECUTION_SLIPPAGE_CALIBRATION_AUTHORITY.contract(
            candidate, mode=mode, quantity=qty, schedule_slippage_bps=schedule_slippage_bps,
        )
        notional = entry * qty

        def scenario(exit_price: float) -> tuple[float, CostBreakdown, dict[str, Any]]:
            report = self.cost_service.round_trip(
                mode, side, entry, exit_price, qty, execution_model=execution_model,
                exchange=str(venue["exchange"]), bse_group=venue["bse_group"],
            )
            return float(report["gross_pnl"]), _cost_breakdown(report["costs"], spread_cost=_number(report["costs"].get("spread")) or 0.0), report

        target_gross, target_costs, target_report = scenario(target)
        stop_gross, stop_costs, stop_report = scenario(stop)
        if target_gross <= 0 or stop_gross >= 0:
            return self.unavailable("TRADE_GEOMETRY_PNL_INVALID", "Target must be profitable and stop must be loss-making for the declared side.")

        if return_basis == "NET_POSITION_RETURN_AFTER_COSTS":
            neither_net = q50 * notional
            neither_exit = entry * (1.0 + q50 if side == "LONG" else 1.0 - q50)
            if neither_exit <= 0:
                return self.unavailable("NEITHER_EXIT_INVALID", "Median predicted return implies an invalid exit price.")
            neither_gross = neither_net
            neither_costs = CostBreakdown()
            neither_report = {"net_pnl": neither_net, "gross_pnl": neither_net, "costs": {"total": 0.0}}
        else:
            neither_exit = entry * (1.0 + q50 if side == "LONG" else 1.0 - q50)
            if neither_exit <= 0:
                return self.unavailable("NEITHER_EXIT_INVALID", "Median predicted return implies an invalid exit price.")
            neither_gross, neither_costs, neither_report = scenario(neither_exit)

        provided_lower = provided_upper = None
        if uncertainty_method in {"CONFORMAL_INTERVAL", "EMPIRICAL_BOOTSTRAP_INTERVAL"}:
            expected_cost = pt * target_costs.total + ps * stop_costs.total + pn * neither_costs.total
            if return_basis == "NET_POSITION_RETURN_AFTER_COSTS":
                provided_lower = lower_return * notional
                provided_upper = upper_return * notional
            else:
                provided_lower = lower_return * notional - expected_cost
                provided_upper = upper_return * notional - expected_cost

        ev = calculate_expected_value(ExpectedValueInputs(
            target_first_probability=pt,
            stop_first_probability=ps,
            neither_probability=pn,
            expected_gain=target_gross,
            expected_loss=abs(stop_gross),
            expected_neither_return=neither_gross,
            costs=CostBreakdown(),
            sample_size=sample_size,
            net_return_standard_error=standard_error * notional,
            target_costs=target_costs,
            stop_costs=stop_costs,
            neither_costs=neither_costs,
            provided_lower_confidence_bound=provided_lower,
            provided_upper_confidence_bound=provided_upper,
            uncertainty_method=uncertainty_method,
        ))
        target_net = float(target_report["net_pnl"])
        stop_net = float(stop_report["net_pnl"])
        post_cost_rr = target_net / abs(stop_net) if target_net > 0 and stop_net < 0 else 0.0
        minimum_rr = float(policy_for(mode).minimum_net_rr)
        math_blockers = list(ev.blockers)
        if post_cost_rr + 1e-9 < minimum_rr:
            math_blockers.append("POST_COST_REWARD_RISK_BELOW_DESK_MINIMUM")

        width = max(0.0, q95 - q05)
        expected_net_return = ev.net_expected_value / notional if notional > 0 else 0.0
        downside_risk = max(0.0, -q05, abs(stop_net) / notional * ps)
        expected_impact = (
            pt * target_costs.market_impact
            + ps * stop_costs.market_impact
            + pn * neither_costs.market_impact
        ) / notional
        correlation_penalty = max(0.0, _number(candidate.get("portfolio_correlation_penalty")) or 0.0)
        utility = calculate_ranking_utility(RankingUtilityInputs(
            expected_net_return=expected_net_return,
            downside_risk=downside_risk,
            market_impact=expected_impact,
            prediction_uncertainty=width / 2.0,
            portfolio_correlation_penalty=correlation_penalty,
        ))
        admissible = not math_blockers
        return {
            "ok": True,
            "service_version": SERVICE_VERSION,
            "state": "ADMISSIBLE" if admissible else "BLOCKED",
            "capital_admissible": admissible,
            "blockers": sorted(set(math_blockers)),
            "probability_source": "FROZEN_POSTGRES_CHAMPION_PREDICTION",
            "heuristic_score_is_probability": False,
            "model_id": signal.get("model_id"),
            "prediction_id": signal.get("prediction_id"),
            "assignment_id": signal.get("assignment_id"),
            "cost_model_version": prediction.get("cost_model_version"),
            "active_cost_authority_version": self.cost_service.authority_version,
            "active_tariff_schedule_version": schedule.version,
            "execution_model": execution_model,
            "return_basis": return_basis,
            "outcome_probabilities": {"target_first": pt, "stop_first": ps, "neither": pn},
            "return_distribution": {"q05": q05, "q50": q50, "q95": q95},
            "expected_value": ev.as_dict(),
            "expectancy_semantics": expectancy_lane("PROSPECTIVE_MODEL_EV"),
            "prospective_net_ev_inr": ev.net_expected_value,
            "prospective_net_ev_fraction_of_notional": expected_net_return,
            "prospective_net_ev_lower_bound_inr": ev.lower_confidence_bound,
            "expected_net_return": expected_net_return,  # compatibility alias; see expectancy_semantics
            "ranking_utility": utility,
            "post_cost_reward_risk": post_cost_rr,
            "minimum_post_cost_reward_risk": minimum_rr,
            "scenario_costs": {
                "target": asdict(target_costs),
                "stop": asdict(stop_costs),
                "neither": asdict(neither_costs),
            },
            "scenario_net_pnl": {
                "target": target_net,
                "stop": stop_net,
                "neither": float(neither_report.get("net_pnl") or 0.0),
            },
            "quantity": qty,
            "exchange": venue["exchange"],
            "bse_group": venue["bse_group"],
            "notional": notional,
            "uncertainty_method": uncertainty_method,
            "policy": "Capital admission requires positive post-cost expectancy and a positive uncertainty lower bound from a fresh frozen champion prediction.",
        }
