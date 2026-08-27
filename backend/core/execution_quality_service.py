"""Desk-aware post-cost execution-quality gate.

The service converts the planned entry/stop/target map into a conservative
post-cost reward-to-risk estimate.  Explicitly excessive spread or a target
whose edge is consumed by transaction costs vetoes promotion.  It never
improves evidence score and never places orders.
"""
from __future__ import annotations

from typing import Any, Dict, Mapping, Optional

from core.india_cost_model import IndiaCashCostModel
from core.production_mode_policy import policy_for, require_production_mode

EXECUTION_QUALITY_VERSION = "execution-quality-gate-1.1.0"


def _num(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class ExecutionQualityService:
    ASSUMED_NOTIONAL = 100000.0
    MAX_SPREAD_BPS = {"intraday": 30.0, "delivery": 75.0}
    # A positive ratio is not enough when the absolute move is too small to
    # survive brokerage, taxes, slippage and market impact.  These are
    # conservative advisory-desk floors on the *post-cost* target move.
    MIN_NET_REWARD_BPS = {"intraday": 50.0, "delivery": 100.0}
    MIN_EXPECTED_NET_RUPEES = {"intraday": 500.0, "delivery": 750.0}
    MAX_MINUTE_VOLUME_FRACTION = {"intraday": 0.10, "delivery": 0.20}

    def evaluate(self, candidate: Mapping[str, Any]) -> Dict[str, Any]:
        mode = require_production_mode(candidate.get("mode"))
        policy = policy_for(mode)
        entry = _num(candidate.get("entry") if candidate.get("entry") is not None else candidate.get("planned_entry"))
        stop = _num(candidate.get("sl") or candidate.get("stop") or candidate.get("planned_sl"))
        target = _num(candidate.get("t1") or candidate.get("target") or candidate.get("planned_t1"))
        spread_bps = _num(candidate.get("spread_bps"))
        blockers = []
        warnings = []
        if entry is None or entry <= 0 or stop is None or stop <= 0 or target is None or target <= 0 or entry == stop or entry == target:
            blockers.append("valid entry, stop and first target are required for execution-quality admission")
            return {
                "version": EXECUTION_QUALITY_VERSION,
                "state": "BLOCKED_MAP_MISSING",
                "gate": "BLOCK",
                "blockers": blockers,
                "warnings": warnings,
                "policy": "execution quality may veto but never improve evidence score",
            }
        planned_notional = _num(candidate.get("planned_notional") or candidate.get("notional")) or self.ASSUMED_NOTIONAL
        explicit_quantity = _num(candidate.get("quantity") or candidate.get("recommended_quantity"))
        quantity = max(1, int(explicit_quantity)) if explicit_quantity and explicit_quantity > 0 else max(1, int(planned_notional // entry))
        model = IndiaCashCostModel.for_evidence(mode, dict(candidate))
        side = str(candidate.get("side") or "LONG").upper()
        estimate = model.post_cost_rr(
            entry=entry, stop=stop, target=target, side=side, quantity=quantity,
            spread_bps=spread_bps,
        )
        if spread_bps is not None:
            if spread_bps > self.MAX_SPREAD_BPS[mode]:
                blockers.append(f"spread {spread_bps:.1f} bps exceeds {self.MAX_SPREAD_BPS[mode]:.1f} bps desk limit")
        else:
            warnings.append("live spread is unavailable; configured slippage estimate used")
        reward = float(estimate["gross_reward_points"])
        risk = float(estimate["gross_risk_points"])
        net_reward = float(estimate["post_cost_reward_points"])
        net_risk = float(estimate["post_cost_risk_points"])
        net_rr = float(estimate["post_cost_rr"])
        total_cost_per_share = float(estimate["reward_exit_cost_per_share"]) + float(estimate["spread_cost_per_share"])
        net_reward_bps = max(0.0, net_reward) * 10000.0 / entry
        expected_net_rupees = net_reward * quantity
        required_net_bps = float(self.MIN_NET_REWARD_BPS[mode])
        required_net_rupees = float(self.MIN_EXPECTED_NET_RUPEES[mode])
        if net_reward <= 0:
            blockers.append("estimated round-trip cost consumes the planned first-target reward")
        if net_rr + 1e-9 < float(policy.minimum_net_rr):
            blockers.append(f"post-cost R:R {net_rr:.2f} is below {policy.minimum_net_rr:.2f}")
        if net_reward_bps + 1e-9 < required_net_bps:
            blockers.append(f"post-cost target move {net_reward_bps:.1f} bps is below {required_net_bps:.1f} bps economic minimum")
        if expected_net_rupees + 1e-9 < required_net_rupees:
            blockers.append(f"estimated net reward ₹{expected_net_rupees:.0f} is below ₹{required_net_rupees:.0f} at the ₹{planned_notional:,.0f} reference notional")

        avg_minute_volume = _num(candidate.get("average_minute_volume") or candidate.get("avg_minute_volume"))
        if avg_minute_volume and avg_minute_volume > 0:
            participation = quantity / avg_minute_volume
            if participation > self.MAX_MINUTE_VOLUME_FRACTION[mode]:
                blockers.append(f"planned quantity is {participation*100:.1f}% of average minute volume; capacity limit is {self.MAX_MINUTE_VOLUME_FRACTION[mode]*100:.1f}%")
        else:
            warnings.append("order-book/depth capacity is unverified; use a limit order and confirm executable quantity manually")
        gate = "BLOCK" if blockers else "PASS"
        return {
            "version": EXECUTION_QUALITY_VERSION,
            "state": "BLOCKED" if blockers else "PASS",
            "gate": gate,
            "blockers": blockers,
            "warnings": warnings,
            "spread_bps": spread_bps,
            "spread_limit_bps": self.MAX_SPREAD_BPS[mode],
            "assumed_quantity": quantity,
            "reference_notional": round(planned_notional, 2),
            "gross_reward_points": round(reward, 6),
            "gross_risk_points": round(risk, 6),
            "estimated_cost_per_share": round(total_cost_per_share, 6),
            "reward_exit_cost_per_share": estimate["reward_exit_cost_per_share"],
            "stop_exit_cost_per_share": estimate["stop_exit_cost_per_share"],
            "post_cost_reward_points": round(net_reward, 6),
            "post_cost_risk_points": round(net_risk, 6),
            "post_cost_rr": round(net_rr, 6),
            "post_cost_reward_bps": round(net_reward_bps, 3),
            "minimum_post_cost_reward_bps": required_net_bps,
            "expected_net_reward_rupees": round(expected_net_rupees, 2),
            "minimum_expected_net_reward_rupees": required_net_rupees,
            "minimum_post_cost_rr": policy.minimum_net_rr,
            "cost_version": model.config.version,
            "cost_authority": "IndiaCashCostAuthority",
            "cost_authority_version": "1.2.0",
            "cost_exchange": model.config.exchange,
            "cost_bse_group": model.config.bse_group,
            "policy": "execution quality may veto but never improve evidence score or place orders",
        }
