"""First-class Delivery and Intraday architecture contract.

The two desks share the same engineering pillars and governance gates while
retaining desk-specific horizons, labels, risk controls and latency budgets.
Equal weight means equal architectural completeness and review authority; it
does not mean forcing identical features or identical portfolio weights.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, asdict
from typing import Any, Dict, Iterable, Mapping

from core.production_mode_policy import require_production_mode


SERVICE_VERSION = "dual-desk-architecture-1.0.0"
ARCHITECTURE_PILLARS = (
    "storage",
    "data_readiness",
    "feature_library",
    "prediction",
    "validation",
    "performance",
    "risk",
    "decision_pipeline",
    "operator_output",
)


@dataclass(frozen=True)
class DeskArchitecture:
    mode: str
    horizons: tuple[str, ...]
    source_intervals: tuple[str, ...]
    labels: tuple[str, ...]
    storage_namespaces: tuple[str, ...]
    feature_families: tuple[str, ...]
    prediction_targets: tuple[str, ...]
    validation_metrics: tuple[str, ...]
    performance_metrics: tuple[str, ...]
    risk_controls: tuple[str, ...]
    operator_fields: tuple[str, ...]
    latency_budgets_ms: Mapping[str, int]

    def pillars(self) -> Dict[str, Dict[str, Any]]:
        return {
            "storage": {
                "namespaces": list(self.storage_namespaces),
                "contract": "same point-in-time lineage, desk and horizon keys, immutable predictions/outcomes",
            },
            "data_readiness": {
                "source_intervals": list(self.source_intervals),
                "contract": "scanner and model authority require verified desk-specific freshness",
            },
            "feature_library": {
                "families": list(self.feature_families),
                "contract": "candidate features enter only through finite validation and ablation",
            },
            "prediction": {
                "horizons": list(self.horizons),
                "targets": list(self.prediction_targets),
                "contract": "calibrated predictions are horizon-specific and cost-aware",
            },
            "validation": {
                "labels": list(self.labels),
                "metrics": list(self.validation_metrics),
                "contract": "purged walk-forward, embargo, regime splits and multiple-testing control",
            },
            "performance": {
                "metrics": list(self.performance_metrics),
                "contract": "post-cost outcomes and confidence bounds decide promotion",
            },
            "risk": {
                "controls": list(self.risk_controls),
                "contract": "desk-specific admission and sizing remain independent of model training",
            },
            "decision_pipeline": {
                "contract": "SetupHypothesis -> Evidence -> Probability/EV -> Risk -> Canonical DecisionRecord",
            },
            "operator_output": {
                "fields": list(self.operator_fields),
                "contract": "same decision-first UI grammar for both desks with explicit freshness and blockers",
            },
        }


_SHARED_LABELS = (
    "net_return_bps",
    "target_before_stop",
    "mfe_bps",
    "mae_bps",
    "time_to_target",
    "time_to_stop",
    "realised_slippage_bps",
    "holding_efficiency",
)
_SHARED_VALIDATION = (
    "calibration_brier",
    "calibration_log_loss",
    "post_cost_expectancy_bps",
    "lower_confidence_bound_bps",
    "rank_ic",
    "top_bucket_lift_bps",
    "maximum_drawdown_bps",
    "turnover_bps",
    "regime_stability",
    "multiple_testing_adjusted_pvalue",
)
_SHARED_OPERATOR_FIELDS = (
    "symbol",
    "mode",
    "setup",
    "direction",
    "probability_positive",
    "expected_net_return_bps",
    "target_before_stop_probability",
    "downside_quantile_bps",
    "regime",
    "equilibrium_distance",
    "volatility_forecast",
    "liquidity_quality",
    "model_agreement",
    "blockers",
    "freshness",
    "lifecycle_state",
)

DESKS: Dict[str, DeskArchitecture] = {
    "delivery": DeskArchitecture(
        mode="delivery",
        horizons=("1d", "3d", "5d", "10d", "20d"),
        source_intervals=("1d", "1w", "1mo"),
        labels=_SHARED_LABELS,
        storage_namespaces=(
            "quant_feature_snapshots",
            "quant_label_vectors",
            "model_experiments",
            "model_predictions",
            "model_evaluations",
            "model_lifecycle_decisions",
        ),
        feature_families=(
            "technical_equilibrium",
            "fundamental_quality_value_growth",
            "delivery_participation",
            "sector_index_relative_strength",
            "regime_state",
            "volatility_tail",
            "liquidity_cost",
        ),
        prediction_targets=(
            "horizon_return_distribution",
            "positive_return_probability",
            "target_before_stop_probability",
            "downside_quantile",
        ),
        validation_metrics=_SHARED_VALIDATION,
        performance_metrics=(
            "1d_net_return",
            "3d_net_return",
            "5d_net_return",
            "10d_net_return",
            "20d_net_return",
            "portfolio_cvar",
            "portfolio_cdar",
            "sector_concentration",
            "capacity",
        ),
        risk_controls=(
            "portfolio_cvar",
            "portfolio_cdar",
            "sector_concentration",
            "single_name_concentration",
            "liquidity_capacity",
            "turnover_budget",
            "event_gap_risk",
            "overnight_risk",
        ),
        operator_fields=_SHARED_OPERATOR_FIELDS,
        latency_budgets_ms={"risk": 100, "decision": 500, "projection": 250, "research": 3_600_000},
    ),
    "intraday": DeskArchitecture(
        mode="intraday",
        horizons=("5m", "15m", "30m", "60m", "eod"),
        source_intervals=("tick", "1m", "3m", "5m", "15m"),
        labels=_SHARED_LABELS,
        storage_namespaces=(
            "quant_feature_snapshots",
            "quant_label_vectors",
            "model_experiments",
            "model_predictions",
            "model_evaluations",
            "model_lifecycle_decisions",
        ),
        feature_families=(
            "session_phase",
            "opening_gap_range",
            "vwap_structure",
            "volume_velocity",
            "sector_index_relative_strength",
            "microstructure_liquidity",
            "regime_state",
            "volatility_tail",
            "technical_equilibrium",
            "execution_cost",
        ),
        prediction_targets=(
            "horizon_return_distribution",
            "positive_return_probability",
            "target_before_stop_probability",
            "downside_quantile",
        ),
        validation_metrics=_SHARED_VALIDATION,
        performance_metrics=(
            "5m_net_return",
            "15m_net_return",
            "30m_net_return",
            "60m_net_return",
            "eod_net_return",
            "mfe_mae_ratio",
            "opportunity_detection_delay_ms",
            "realised_slippage_bps",
            "session_phase_stability",
        ),
        risk_controls=(
            "open_position_tick_risk",
            "maximum_intraday_positions",
            "gross_net_beta",
            "sector_concentration",
            "daily_loss_limit",
            "consecutive_loss_limit",
            "spread_depth_capacity",
            "feed_staleness_kill_switch",
            "slippage_kill_switch",
            "forced_flatten",
            "position_time_stop",
        ),
        operator_fields=_SHARED_OPERATOR_FIELDS,
        latency_budgets_ms={"risk": 25, "decision": 150, "projection": 100, "research": 3_600_000},
    ),
}


def architecture_for(mode: str) -> DeskArchitecture:
    return DESKS[require_production_mode(mode)]


def validate_architecture_parity(desks: Mapping[str, DeskArchitecture] | None = None) -> Dict[str, Any]:
    source = desks or DESKS
    required = {"delivery", "intraday"}
    missing = sorted(required.difference(source))
    issues: list[str] = []
    if missing:
        issues.append("missing desks: " + ", ".join(missing))
    pillar_sets: Dict[str, set[str]] = {}
    for name in sorted(required.intersection(source)):
        desk = source[name]
        pillars = desk.pillars()
        pillar_sets[name] = set(pillars)
        absent = [pillar for pillar in ARCHITECTURE_PILLARS if pillar not in pillars]
        if absent:
            issues.append(f"{name} missing pillars: {', '.join(absent)}")
        for pillar, payload in pillars.items():
            if not isinstance(payload, Mapping) or not payload:
                issues.append(f"{name}.{pillar} is empty")
    if len(pillar_sets) == 2 and pillar_sets["delivery"] != pillar_sets["intraday"]:
        issues.append("desk pillar sets are not equal")
    return {
        "ok": not issues,
        "version": SERVICE_VERSION,
        "principle": "Delivery and Intraday have equal architectural completeness and independent desk-specific methods.",
        "pillars": list(ARCHITECTURE_PILLARS),
        "issues": issues,
    }


class DualDeskArchitectureService:
    def status(self) -> Dict[str, Any]:
        parity = validate_architecture_parity()
        desks = {}
        for mode, desk in DESKS.items():
            desks[mode] = {
                **asdict(desk),
                "pillars": desk.pillars(),
                "architecture_weight": 1.0,
                "production_authority": "DESK_SPECIFIC_GOVERNED_PIPELINE",
            }
        return {
            **parity,
            "desks": deepcopy(desks),
            "shared_storage_rule": "Every model, prediction, evaluation and outcome is keyed by mode and horizon.",
            "no_shadow_rule": "Candidates are ACTIVE_VALIDATION with a finite decision deadline, ACTIVE_PRODUCTION, or REJECTED.",
            "equal_weight_clarification": "Equal architecture priority; prediction and portfolio weights remain evidence-derived.",
        }
