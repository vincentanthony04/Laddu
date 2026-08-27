from __future__ import annotations

from dataclasses import asdict, dataclass
import math
from statistics import NormalDist
from typing import Any


def _finite(name: str, value: float) -> float:
    out = float(value)
    if not math.isfinite(out):
        raise ValueError(f"{name} must be finite")
    return out


def _prob(name: str, value: float) -> float:
    out = _finite(name, value)
    if not 0.0 <= out <= 1.0:
        raise ValueError(f"{name} must be between 0 and 1")
    return out


def _unit_reducer(name: str, value: float) -> float:
    out = _prob(name, value)
    return min(1.0, max(0.0, out))


@dataclass(frozen=True)
class CostBreakdown:
    brokerage: float = 0.0
    stt: float = 0.0
    exchange_fees: float = 0.0
    ipft: float = 0.0
    sebi_fees: float = 0.0
    gst: float = 0.0
    stamp_duty: float = 0.0
    spread: float = 0.0
    slippage: float = 0.0
    market_impact: float = 0.0
    dp_charges: float = 0.0
    other_costs: float = 0.0

    @property
    def total(self) -> float:
        values = [
            self.brokerage, self.stt, self.exchange_fees, self.ipft, self.sebi_fees,
            self.gst, self.stamp_duty, self.spread, self.slippage, self.market_impact,
            self.dp_charges, self.other_costs,
        ]
        checked = [_finite("cost", value) for value in values]
        if any(value < 0 for value in checked):
            raise ValueError("cost components cannot be negative")
        return sum(checked)


@dataclass(frozen=True)
class ExpectedValueInputs:
    target_first_probability: float
    stop_first_probability: float
    neither_probability: float
    expected_gain: float
    expected_loss: float
    expected_neither_return: float
    costs: CostBreakdown
    sample_size: int
    net_return_standard_error: float
    confidence_level: float = 0.95
    target_costs: CostBreakdown | None = None
    stop_costs: CostBreakdown | None = None
    neither_costs: CostBreakdown | None = None
    provided_lower_confidence_bound: float | None = None
    provided_upper_confidence_bound: float | None = None
    uncertainty_method: str = "NORMAL_STANDARD_ERROR"


@dataclass(frozen=True)
class ExpectedValueResult:
    gross_expected_value: float
    total_cost: float
    net_expected_value: float
    lower_confidence_bound: float
    upper_confidence_bound: float
    confidence_level: float
    uncertainty_method: str
    admissible: bool
    blockers: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def calculate_expected_value(inputs: ExpectedValueInputs) -> ExpectedValueResult:
    pt = _prob("target_first_probability", inputs.target_first_probability)
    ps = _prob("stop_first_probability", inputs.stop_first_probability)
    pn = _prob("neither_probability", inputs.neither_probability)
    if abs((pt + ps + pn) - 1.0) > 1e-6:
        raise ValueError("outcome probabilities must sum to 1")
    gain = _finite("expected_gain", inputs.expected_gain)
    loss = _finite("expected_loss", inputs.expected_loss)
    neither = _finite("expected_neither_return", inputs.expected_neither_return)
    if gain < 0 or loss < 0:
        raise ValueError("expected_gain and expected_loss must be non-negative magnitudes")
    if inputs.sample_size < 2:
        raise ValueError("sample_size must be at least 2")
    se = _finite("net_return_standard_error", inputs.net_return_standard_error)
    if se < 0:
        raise ValueError("net_return_standard_error cannot be negative")
    confidence = _prob("confidence_level", inputs.confidence_level)
    if not 0.5 < confidence < 1.0:
        raise ValueError("confidence_level must be between 0.5 and 1")
    gross = pt * gain - ps * loss + pn * neither
    if any(value is not None for value in (inputs.target_costs, inputs.stop_costs, inputs.neither_costs)):
        if any(value is None for value in (inputs.target_costs, inputs.stop_costs, inputs.neither_costs)):
            raise ValueError("all three scenario cost breakdowns are required together")
        total_cost = (
            pt * inputs.target_costs.total
            + ps * inputs.stop_costs.total
            + pn * inputs.neither_costs.total
        )
    else:
        total_cost = inputs.costs.total
    net = gross - total_cost
    supplied = (inputs.provided_lower_confidence_bound, inputs.provided_upper_confidence_bound)
    if any(value is not None for value in supplied):
        if any(value is None for value in supplied):
            raise ValueError("provided confidence bounds must include both lower and upper values")
        lower = _finite("provided_lower_confidence_bound", supplied[0])
        upper = _finite("provided_upper_confidence_bound", supplied[1])
        if lower > upper:
            raise ValueError("provided lower confidence bound cannot exceed upper bound")
        uncertainty_method = str(inputs.uncertainty_method or "PROVIDED_INTERVAL").strip().upper()
    else:
        z = NormalDist().inv_cdf(0.5 + confidence / 2.0)
        margin = z * se
        lower, upper = net - margin, net + margin
        uncertainty_method = "NORMAL_STANDARD_ERROR"
    blockers = []
    if net <= 0:
        blockers.append("NET_EXPECTED_VALUE_NOT_POSITIVE")
    if lower <= 0:
        blockers.append("LOWER_CONFIDENCE_EXPECTANCY_NOT_POSITIVE")
    return ExpectedValueResult(
        gross, total_cost, net, lower, upper, confidence, uncertainty_method,
        not blockers, tuple(blockers),
    )


@dataclass(frozen=True)
class RankingUtilityInputs:
    expected_net_return: float
    downside_risk: float
    market_impact: float
    prediction_uncertainty: float
    portfolio_correlation_penalty: float
    downside_weight: float = 1.0
    impact_weight: float = 1.0
    uncertainty_weight: float = 1.0
    correlation_weight: float = 1.0


def calculate_ranking_utility(inputs: RankingUtilityInputs) -> float:
    expected = _finite("expected_net_return", inputs.expected_net_return)
    penalties = (
        _finite("downside_risk", inputs.downside_risk) * _finite("downside_weight", inputs.downside_weight),
        _finite("market_impact", inputs.market_impact) * _finite("impact_weight", inputs.impact_weight),
        _finite("prediction_uncertainty", inputs.prediction_uncertainty) * _finite("uncertainty_weight", inputs.uncertainty_weight),
        _finite("portfolio_correlation_penalty", inputs.portfolio_correlation_penalty) * _finite("correlation_weight", inputs.correlation_weight),
    )
    if any(value < 0 for value in penalties):
        raise ValueError("ranking penalties and weights cannot create negative penalties")
    return expected - sum(penalties)


@dataclass(frozen=True)
class PositionSizingInputs:
    current_equity: float
    desk_risk_limit_fraction: float
    strategy_risk_limit_fraction: float
    stop_risk_per_share: float
    reference_price: float
    available_cash: float
    max_position_value: float
    lot_size: int = 1
    model_reliability: float = 1.0
    regime_compatibility: float = 1.0
    liquidity_adjustment: float = 1.0
    drawdown_adjustment: float = 1.0


@dataclass(frozen=True)
class PositionSizingResult:
    base_risk_budget: float
    adjusted_risk_budget: float
    raw_quantity: int
    quantity: int
    notional: float
    risk_at_stop: float
    blockers: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def calculate_position_size(inputs: PositionSizingInputs) -> PositionSizingResult:
    equity = _finite("current_equity", inputs.current_equity)
    cash = _finite("available_cash", inputs.available_cash)
    stop_risk = _finite("stop_risk_per_share", inputs.stop_risk_per_share)
    price = _finite("reference_price", inputs.reference_price)
    max_value = _finite("max_position_value", inputs.max_position_value)
    if equity <= 0 or cash < 0 or stop_risk <= 0 or price <= 0 or max_value < 0:
        raise ValueError("equity, stop risk and price must be positive; cash/max value non-negative")
    desk = _prob("desk_risk_limit_fraction", inputs.desk_risk_limit_fraction)
    strategy = _prob("strategy_risk_limit_fraction", inputs.strategy_risk_limit_fraction)
    lot = int(inputs.lot_size)
    if lot <= 0:
        raise ValueError("lot_size must be positive")
    reducers = (
        _unit_reducer("model_reliability", inputs.model_reliability),
        _unit_reducer("regime_compatibility", inputs.regime_compatibility),
        _unit_reducer("liquidity_adjustment", inputs.liquidity_adjustment),
        _unit_reducer("drawdown_adjustment", inputs.drawdown_adjustment),
    )
    base = equity * desk * strategy
    adjusted = base * math.prod(reducers)
    by_risk = int(adjusted // stop_risk)
    by_cash = int(min(cash, max_value) // price)
    raw = max(0, min(by_risk, by_cash))
    quantity = (raw // lot) * lot
    notional = quantity * price
    risk = quantity * stop_risk
    blockers = []
    if adjusted <= 0:
        blockers.append("ADJUSTED_RISK_BUDGET_ZERO")
    if raw <= 0:
        blockers.append("INSUFFICIENT_RISK_OR_CASH_CAPACITY")
    if quantity <= 0 and raw > 0:
        blockers.append("BELOW_LOT_SIZE")
    if risk > adjusted + 1e-9:
        blockers.append("RISK_BUDGET_BREACH")
    if notional > min(cash, max_value) + 1e-9:
        blockers.append("NOTIONAL_LIMIT_BREACH")
    return PositionSizingResult(base, adjusted, raw, quantity, notional, risk, tuple(blockers))
