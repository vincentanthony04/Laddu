from __future__ import annotations
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone

from core.india_time import india_now
from typing import Any, Dict, List, Optional


def now_iso() -> str:
    """Canonical application timestamp in fixed IST, independent of host locale."""
    return india_now().isoformat(timespec="seconds")


@dataclass
class Instrument:
    instrument_key: str
    exchange: str
    segment: str
    trading_symbol: str
    name: str = ""
    instrument_type: str = ""
    isin: str = ""
    expiry: str = ""
    strike: float | None = None
    option_type: str = ""
    lot_size: int | None = None


@dataclass
class Quote:
    instrument_key: str
    symbol: str
    exchange: str
    ltp: float | None = None
    open: float | None = None
    high: float | None = None
    low: float | None = None
    close: float | None = None
    volume: float | None = None
    oi: float | None = None
    iv: float | None = None
    change_pct: float | None = None
    timestamp: str = field(default_factory=now_iso)
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Decision:
    symbol: str
    exchange: str
    mode: str
    side: str
    decision: str
    ltp: float | None
    entry: float | None
    t1: float | None
    t2: float | None
    sl: float | None
    rr: float | None
    score: int
    confidence: str
    setup: str
    risk: str
    reason: str
    status: str
    price_freshness: str
    last_refresh: str
    last_ai_validation: str
    holding_policy: str
    open: float | None = None
    change_pct: float | None = None
    index_context: str = "pending"
    sector_context: str = "pending"
    rsi: float | None = None
    adx: float | None = None
    vwap: float | None = None
    volume_state: str = "pending"
    support: float | None = None
    resistance: float | None = None
    fundamental_score: float | None = None
    technical_score: float | None = None
    fundamental_weight_pct: int | None = None
    quality_score: float | None = None
    growth_score: float | None = None
    safety_score: float | None = None
    valuation_score: float | None = None
    fundamental_state: str = "pending"
    market_structure: str = "pending"
    market_structure_score: int | None = None
    volume_profile: str = "pending"
    volume_profile_score: int | None = None
    orb_state: str = "pending"
    orb_score: int | None = None
    orb_phase: str = "pending"
    orb_confirmed: bool = False
    orb_high: float | None = None
    orb_low: float | None = None
    previous_day_high: float | None = None
    previous_day_low: float | None = None
    pivot: float | None = None
    cpr_bottom: float | None = None
    cpr_top: float | None = None
    session_relative_volume: float | None = None
    session_structure_state: str = "pending"
    session_structure_score: float | None = None
    session_support_source: str | None = None
    session_resistance_source: str | None = None
    session_entry_trigger: float | None = None
    session_a_plus: bool | None = None
    nse_confirmation_score: float | None = None
    nse_confirmation_reasons: List[str] = field(default_factory=list)
    participation_authority: str | None = None
    participation_authority_version: str | None = None
    participation_lane: str | None = None
    participation_source_time: str | None = None
    participation_decision_usable: bool | None = None
    weekly_state: str = "pending"
    monthly_state: str = "pending"
    market_context_score: float | None = None
    evidence: List[str] = field(default_factory=list)
    # v35.5: financial-logic upgrade fields.
    quantity: int | None = None
    risk_amount: float | None = None
    est_net_rr: float | None = None
    rr_gate_min: float | None = None
    sl_source: str = "atr"          # "atr" or "structure" (clamped to S/R)
    target_source: str = "atr"
    # v36.9 trust layer: every decision carries freshness and level-map audit info.
    freshness_state: str = "unknown"      # live / delayed / stale / historical / pending / invalid
    quote_age_seconds: int | None = None
    candle_age_seconds: int | None = None
    candle_state: str = "unknown"
    level_status: str = "unchecked"       # valid / invalid / reference_only / unchecked
    level_message: str = ""
    trade_map_valid: bool = False
    planned_entry: float | None = None
    planned_sl: float | None = None
    planned_t1: float | None = None
    planned_t2: float | None = None
    planned_rr: float | None = None
    planned_map_valid: bool = False
    prepared_state: str = "OBSERVING"
    # v65.26.27: structure-aware target and managed-lifecycle audit fields.
    atr14: float | None = None
    first_obstacle: float | None = None
    first_obstacle_low: float | None = None
    first_obstacle_high: float | None = None
    first_obstacle_touches: int | None = None
    room_to_obstacle: float | None = None
    obstacle_rr: float | None = None
    structural_target_state: str = "unchecked"
    structural_target_reason: str = ""
    profit_protection_plan: Dict[str, Any] = field(default_factory=dict)
    # Empirical strategy admission is separate from deterministic mathematics.
    # This hash-bound object must be present and QUALIFIED before a row can own
    # actionable production state.
    strategy_version: str | None = None
    strategy_contract_hash: str | None = None
    strategy_qualification_state: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ScannerEvent:
    level: str
    module: str
    message: str
    timestamp: str = field(default_factory=now_iso)
    detail: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
