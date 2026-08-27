"""Canonical forward-evidence horizon and maturity policy.

This module is intentionally dependency-free so capture, settlement, research,
governance and UI surfaces cannot drift on desk/horizon naming.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Dict, Mapping, Tuple

POLICY_VERSION = "forward-horizon-policy-1.1.0-durability-tiers"

SUPPORTED_HORIZONS: Dict[str, Tuple[str, ...]] = {
    "intraday": ("5m", "15m", "30m", "60m", "eod"),
    "delivery": ("1d", "3d", "5d", "10d", "20d"),
}

PRIMARY_HORIZON = {
    "intraday": "eod",
    "delivery": "20d",
}

HORIZON_ALIASES = {
    ("intraday", "session"): "eod",
    ("intraday", "day"): "eod",
    ("delivery", "session"): "1d",
}


FORWARD_DURABILITY_TIERS = (
    {"tier": "INITIAL_QUALIFIED", "minimum_trading_days": 126, "minimum_settled_candidates": 300, "minimum_complete_populations": 20, "minimum_regimes": 3},
    {"tier": "MATURE_ANNUAL", "minimum_trading_days": 252, "minimum_settled_candidates": 750, "minimum_complete_populations": 40, "minimum_regimes": 3},
    {"tier": "STRONG_TWO_YEAR", "minimum_trading_days": 504, "minimum_settled_candidates": 1500, "minimum_complete_populations": 80, "minimum_regimes": 4},
    {"tier": "DEEP_CYCLE", "minimum_trading_days": 756, "minimum_settled_candidates": 2000, "minimum_complete_populations": 120, "minimum_regimes": 4},
)

def durability_status(evidence: Mapping[str, Any] | None) -> dict:
    row = dict(evidence or {})
    days = int(row.get("trading_days") or 0)
    settled = int(row.get("settled_candidates") or 0)
    populations = int(row.get("complete_populations") or 0)
    regimes = len(list(row.get("regimes") or [])) if not isinstance(row.get("regimes"), int) else int(row.get("regimes") or 0)
    checks = []
    achieved = "ACCUMULATING"
    next_tier = None
    for tier in FORWARD_DURABILITY_TIERS:
        passed = (
            days >= int(tier["minimum_trading_days"])
            and settled >= int(tier["minimum_settled_candidates"])
            and populations >= int(tier["minimum_complete_populations"])
            and regimes >= int(tier["minimum_regimes"])
        )
        checks.append({**tier, "passed": passed})
        if passed:
            achieved = str(tier["tier"])
        elif next_tier is None:
            next_tier = dict(tier)
    return {
        "policy_version": POLICY_VERSION,
        "achieved_tier": achieved,
        "next_tier": next_tier,
        "trading_days": days,
        "settled_candidates": settled,
        "complete_populations": populations,
        "regimes": regimes,
        "tiers": checks,
        "continuous_collection_required": True,
        "historical_replay_never_counts_as_forward_time": True,
    }


@dataclass(frozen=True)
class ForwardMaturityPolicy:
    desk: str
    primary_horizon: str
    minimum_complete_populations: int
    minimum_settled_candidates: int
    minimum_trading_days: int
    minimum_regimes: int
    minimum_hybrid_rank_ic: float
    minimum_hybrid_profit_factor: float
    minimum_hybrid_stressed_net_bps: float
    maximum_hybrid_drawdown: float
    required_walk_forward_profile: str = "capital"
    required_arms: Tuple[str, ...] = ("heuristic", "quant", "hybrid")

    def as_dict(self) -> dict:
        return {**asdict(self), "required_arms": list(self.required_arms), "policy_version": POLICY_VERSION}


# Level 5 is deliberately not a short sample gate.  Both desks must prove the
# same minimum forward-paper duration and population breadth before any model
# lineage may be called mature.
LEVEL5_POLICY: Dict[str, ForwardMaturityPolicy] = {
    "intraday": ForwardMaturityPolicy(
        desk="intraday",
        primary_horizon="eod",
        minimum_complete_populations=20,
        minimum_settled_candidates=300,
        minimum_trading_days=126,
        minimum_regimes=3,
        minimum_hybrid_rank_ic=0.0,
        minimum_hybrid_profit_factor=1.05,
        minimum_hybrid_stressed_net_bps=0.0,
        maximum_hybrid_drawdown=0.15,
    ),
    "delivery": ForwardMaturityPolicy(
        desk="delivery",
        primary_horizon="20d",
        minimum_complete_populations=20,
        minimum_settled_candidates=300,
        minimum_trading_days=126,
        minimum_regimes=3,
        minimum_hybrid_rank_ic=0.0,
        minimum_hybrid_profit_factor=1.05,
        minimum_hybrid_stressed_net_bps=0.0,
        maximum_hybrid_drawdown=0.15,
    ),
}


def normalise_desk(mode: str) -> str:
    desk = str(mode or "").strip().lower()
    if desk not in SUPPORTED_HORIZONS:
        raise ValueError("mode must be intraday or delivery")
    return desk


def canonical_horizon(mode: str, horizon: str | None = None) -> str:
    desk = normalise_desk(mode)
    raw = str(horizon or PRIMARY_HORIZON[desk]).strip().lower()
    value = HORIZON_ALIASES.get((desk, raw), raw)
    if value not in SUPPORTED_HORIZONS[desk]:
        raise ValueError(
            f"unsupported {desk} horizon {raw!r}; expected one of {', '.join(SUPPORTED_HORIZONS[desk])}"
        )
    return value


def maturity_policy(mode: str) -> ForwardMaturityPolicy:
    return LEVEL5_POLICY[normalise_desk(mode)]
