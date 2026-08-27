"""Canonical production desk policy.

This module is the sole authority for production modes, thresholds, evidence
weights and ATR risk geometry. Only Intraday and Delivery are accepted at every runtime and persistence boundary.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from types import MappingProxyType
from typing import Any, Dict, Mapping, Tuple

from config import MIN_RR_INTRADAY, MIN_RR_DELIVERY

POLICY_VERSION = "dual-desk-production-policy-1.0.0"
FINAL_PROMOTION_AUTHORITY = "DecisionEngineService"
FINAL_DECISION_PIPELINE_VERSION = "dual-desk-final-decision-1.0.0"
PRODUCTION_MODES = frozenset({"intraday", "delivery"})


class UnsupportedProductionMode(ValueError):
    """Raised when an unsupported or legacy desk reaches a production entry point."""


@dataclass(frozen=True)
class ModePolicy:
    mode: str
    promotion_threshold: int
    watch_threshold: int
    evidence_ready_threshold: int
    component_weights: Tuple[Tuple[str, int], ...]
    minimum_net_rr: float
    sl_atr: float
    t1_atr: float
    t2_atr: float
    extension_limit_pct: float
    required_inputs: Tuple[str, ...]
    confirmation_rules: Tuple[str, ...]
    same_day: bool
    needs_live_quote: bool
    requires_fundamentals: bool
    policy_version: str = POLICY_VERSION

    @property
    def weights(self) -> Mapping[str, int]:
        return MappingProxyType(dict(self.component_weights))

    def to_dict(self) -> Dict[str, Any]:
        out = asdict(self)
        out["component_weights"] = dict(self.component_weights)
        return out


POLICIES: Mapping[str, ModePolicy] = MappingProxyType({
    "intraday": ModePolicy(
        mode="intraday",
        promotion_threshold=70,
        watch_threshold=52,
        evidence_ready_threshold=65,
        component_weights=(
            ("setup", 30),
            ("technical", 25),
            ("participation", 20),
            ("tradeability", 15),
            ("regime", 10),
        ),
        minimum_net_rr=float(MIN_RR_INTRADAY),
        sl_atr=1.2,
        t1_atr=1.8,
        t2_atr=2.8,
        extension_limit_pct=0.8,
        required_inputs=(
            "verified_live_quote",
            "fresh_completed_5m_candle",
            "validated_entry_stop_target_map",
            "market_open_with_exit_time",
        ),
        confirmation_rules=(
            "at_least_two_of_confirmed_orb_aligned_vwap_expanding_volume",
            "same_session_exit_only",
        ),
        same_day=True,
        needs_live_quote=True,
        requires_fundamentals=False,
    ),
    "delivery": ModePolicy(
        mode="delivery",
        promotion_threshold=74,
        watch_threshold=42,
        evidence_ready_threshold=65,
        component_weights=(
            ("institutional", 30),
            ("technical", 25),
            ("participation", 20),
            ("tradeability", 15),
            ("regime", 10),
        ),
        minimum_net_rr=float(MIN_RR_DELIVERY),
        sl_atr=2.0,
        t1_atr=3.0,
        t2_atr=5.0,
        extension_limit_pct=3.5,
        required_inputs=(
            "validated_daily_weekly_monthly_structure",
            "verified_fundamental_contract",
            "validated_entry_stop_target_map",
            "institutional_delivery_evidence",
        ),
        confirmation_rules=(
            "weekly_and_monthly_non_bearish",
            "long_only_delivery_accumulation",
        ),
        same_day=False,
        needs_live_quote=False,
        requires_fundamentals=True,
    ),
})


def normalise_mode(value: Any) -> str:
    return str(value or "").strip().lower()


def is_production_mode(value: Any) -> bool:
    return normalise_mode(value) in PRODUCTION_MODES


def require_production_mode(value: Any) -> str:
    mode = normalise_mode(value)
    if mode not in PRODUCTION_MODES:
        supplied = mode or "<missing>"
        raise UnsupportedProductionMode(
            f"unsupported production mode '{supplied}'; allowed modes are intraday and delivery"
        )
    return mode


def policy_for(value: Any) -> ModePolicy:
    return POLICIES[require_production_mode(value)]


def production_policy_snapshot() -> Dict[str, Dict[str, Any]]:
    return {mode: policy.to_dict() for mode, policy in POLICIES.items()}
