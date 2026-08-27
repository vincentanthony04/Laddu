"""Immutable Research-vs-Final multidimensional attribution (AC-069).

The service is intentionally a read model across two PostgreSQL authorities:

* Governance PostgreSQL owns immutable Research populations, arm predictions and
  counterfactual fixed-horizon outcomes.
* Operational PostgreSQL owns canonical Final decisions and Model Paper
  admissions/economics.

The join key is only an origin decision/signal ID frozen into the Research
candidate at capture.  Symbol/time inference is forbidden. Historical Research
without those IDs is labelled ``UNLINKED_HISTORICAL`` rather than guessed.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import math
import statistics
from typing import Any, Dict, Iterable, Mapping, Sequence

from core.signal_age_authority import DEFAULT_SIGNAL_AGE_AUTHORITY
from core.outcome_accuracy_taxonomy import DEFAULT_OUTCOME_ACCURACY_TAXONOMY
from core.model_portfolio_performance_service import ModelPortfolioPerformanceService


SERVICE_VERSION = "research-final-attribution-1.2.0-ac071-unit-fidelity"
CONFIDENCE_BUCKET_VERSION = "research-selection-rank-confidence-1.0.0"
PERIODS = {"7D": 7, "30D": 30, "90D": 90, "ALL": None}
ARM_LABELS = {"heuristic": "BASELINE", "quant": "ML", "hybrid": "HYBRID"}


def _dt(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _num(value: Any) -> float | None:
    try:
        parsed = float(value)
        return parsed if math.isfinite(parsed) else None
    except (TypeError, ValueError):
        return None


def _list(value: Any) -> list[str]:
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value if str(item).strip()]
    if value is None or value == "":
        return []
    return [str(value)]


def _confidence_bucket(percentile: Any) -> str:
    value = _num(percentile)
    if value is None:
        return "MISSING"
    if value >= 80.0:
        return "HIGH"
    if value >= 50.0:
        return "MEDIUM"
    return "LOW"


def _research_risk_attribution(row: Mapping[str, Any]) -> Dict[str, Any]:
    """Normalize counterfactual Research return to frozen initial risk.

    This is deliberately not realized capital P&L.  It uses the immutable
    Research trade map and net fixed-horizon outcome bps only.
    """
    entry = _num(row.get("planned_entry"))
    stop = _num(row.get("planned_stop"))
    net_bps = _num(row.get("net_return_bps"))
    if entry is None or stop is None or net_bps is None or entry <= 0:
        return {
            "research_initial_risk_bps": None, "research_net_r": None,
            "research_r_attribution_state": "MISSING_FROZEN_RISK_GEOMETRY",
        }
    risk_bps = abs(entry - stop) / entry * 10000.0
    if not math.isfinite(risk_bps) or risk_bps <= 0:
        return {
            "research_initial_risk_bps": None, "research_net_r": None,
            "research_r_attribution_state": "INVALID_FROZEN_RISK_GEOMETRY",
        }
    return {
        "research_initial_risk_bps": round(risk_bps, 6),
        "research_net_r": round(net_bps / risk_bps, 6),
        "research_r_attribution_state": "COUNTERFACTUAL_NET_BPS_OVER_FROZEN_INITIAL_RISK",
    }


def _metrics(rows: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    candidate_ids = {str(row.get("candidate_id") or "") for row in rows if row.get("candidate_id")}
    returns = [value for value in (_num(row.get("net_return_bps")) for row in rows) if value is not None]
    research_r = [value for value in (_num(row.get("research_net_r")) for row in rows) if value is not None]
    return {
        "observations": len(rows),
        "candidates": len(candidate_ids),
        "positive": sum(value > 0 for value in returns),
        "negative": sum(value < 0 for value in returns),
        "breakeven": sum(value == 0 for value in returns),
        "positive_rate_pct": round(sum(value > 0 for value in returns) * 100.0 / len(returns), 4) if returns else None,
        "mean_net_return_bps": round(statistics.fmean(returns), 6) if returns else None,
        "median_net_return_bps": round(float(statistics.median(returns)), 6) if returns else None,
        "research_r_observations": len(research_r),
        "mean_research_net_r": round(statistics.fmean(research_r), 6) if research_r else None,
        "median_research_net_r": round(float(statistics.median(research_r)), 6) if research_r else None,
    }


def _candidate_metrics(rows: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    unique: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        key = str(row.get("candidate_id") or "")
        if key and key not in unique:
            unique[key] = row
    return _metrics(list(unique.values()))


class ResearchFinalAttributionService:
    """Source-authoritative AC-069 read model with no compatibility SQLite path."""

    def __init__(self, store: Any):
        self.store = store
        self.governance = getattr(store, "production_model_governance_repository", None)
        self.final = getattr(store, "production_model_portfolio_repository", None)

    @staticmethod
    def _decision_maps(lineage: Mapping[str, Any]) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
        by_decision: dict[str, dict[str, Any]] = {}
        by_signal: dict[str, dict[str, Any]] = {}
        positions: dict[str, dict[str, Any]] = {}
        for raw in lineage.get("decisions") or []:
            row = dict(raw or {})
            decision_id = str(row.get("decision_id") or "").strip()
            signal_id = str(row.get("signal_id") or "").strip()
            if decision_id:
                by_decision[decision_id] = row
            if signal_id:
                by_signal[signal_id] = row
        for raw in lineage.get("positions") or []:
            row = dict(raw or {})
            for key in (row.get("source_signal_id"), row.get("decision_id")):
                ref = str(key or "").strip()
                if ref:
                    positions[ref] = row
        return by_decision, by_signal, positions

    @staticmethod
    def _disposition(
        row: Mapping[str, Any], *, decision: Mapping[str, Any] | None, position: Mapping[str, Any] | None,
    ) -> tuple[str, str]:
        if position:
            return "ADMITTED", "MODEL_PAPER_ADMITTED_EXACT_ORIGIN_ID"
        if decision:
            state = str(decision.get("state") or "").upper()
            publication = str(decision.get("publication_authority") or "").upper()
            rejection = _list(decision.get("rejection_reasons"))
            if state in {"REJECTED", "INVALIDATED"}:
                return "REJECTED", rejection[0] if rejection else f"CANONICAL_{state}"
            if publication in {"MODEL_PAPER", "CAPITAL"} or state in {"TRIGGERED", "CONFIRMED", "COMPLETED"}:
                return "PROMOTED", str(decision.get("decision_action") or publication or state)
        source_status = str(row.get("origin_production_status") or "").upper()
        source_decision = str(row.get("origin_production_decision") or "").upper()
        reasons = _list(row.get("origin_rejection_reasons")) + _list(row.get("origin_promotion_blocked_by"))
        qualifier = str(row.get("origin_qualification_blocker") or "").strip()
        if source_status in {"REJECTED", "BLOCKED", "INVALIDATED"} or reasons:
            return "REJECTED", reasons[0] if reasons else qualifier or f"ORIGIN_{source_status}"
        if source_status in {"PROMOTED", "SIGNAL_OPEN", "CONFIRMED", "TRIGGERED", "FINAL"}:
            return "PROMOTED", source_decision or f"ORIGIN_{source_status}"
        if not str(row.get("origin_decision_id") or "").strip() and not str(row.get("origin_signal_id") or "").strip():
            return "UNLINKED_HISTORICAL", "ORIGIN_ID_NOT_FROZEN_HISTORICALLY"
        return "RESEARCH_ONLY", qualifier or "NO_FINAL_PROMOTION_OR_ADMISSION_YET"

    @staticmethod
    def _slice(rows: Sequence[Mapping[str, Any]], key: str) -> list[Dict[str, Any]]:
        values = sorted({str(row.get(key) or "MISSING") for row in rows})
        return [
            {"value": value, **_metrics([row for row in rows if str(row.get(key) or "MISSING") == value])}
            for value in values
        ]

    @staticmethod
    def _group(rows: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
        by_arm = {}
        for raw_arm, label in ARM_LABELS.items():
            selected = [row for row in rows if str(row.get("arm") or "") == raw_arm]
            by_arm[label] = _metrics(selected)
        return {"candidate_metrics": _candidate_metrics(rows), "by_arm": by_arm}

    @staticmethod
    def _final_metrics(rows: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
        """Aggregate Final economics through the canonical Performance authority.

        AC-071 must never invent its own accuracy/drawdown semantics.  The same
        settled-row aggregator used by Model Portfolio Performance owns signal
        accuracy, economic win/loss counts, expectancy and INR curve drawdown.
        This read model only adds normalized-R summaries and explicit units.
        """
        normalized = [dict(raw or {}) for raw in rows]
        base = ModelPortfolioPerformanceService._aggregate(normalized)
        taxonomy = DEFAULT_OUTCOME_ACCURACY_TAXONOMY
        eligible = [
            row for row in normalized
            if taxonomy.performance_eligible(
                taxonomy.normalize_signal(row.get("signal_outcome")),
                taxonomy.normalize_economic(row.get("economic_outcome")),
            )
        ]
        realized_r = [value for value in (_num(row.get("realized_r")) for row in eligible) if value is not None]
        return {
            "closed_observations": base.get("closed_observations", len(normalized)),
            "settled_trades": base.get("settled_trades", 0),
            "excluded_unscorable": base.get("excluded_unscorable", 0),
            "success": base.get("success", 0),
            "failure": base.get("failure", 0),
            "neutral": base.get("neutral", 0),
            "unscorable": base.get("unscorable", 0),
            "accuracy_denominator": base.get("accuracy_denominator", 0),
            "accuracy_pct": base.get("accuracy_pct"),
            "wins": base.get("wins", 0),
            "losses": base.get("losses", 0),
            "breakeven": base.get("breakeven", 0),
            "win_rate_pct": base.get("win_rate_pct"),
            "gross_pnl_inr": base.get("gross_pnl", 0.0),
            "costs_inr": base.get("costs", 0.0),
            "net_pnl_inr": base.get("net_pnl", 0.0),
            "expectancy_inr_per_trade": base.get("realized_expectancy_inr_per_trade"),
            "max_drawdown_inr": base.get("max_drawdown", 0.0),
            "profit_factor": base.get("profit_factor"),
            "realized_r_observations": len(realized_r),
            "total_realized_r": round(sum(realized_r), 6) if realized_r else None,
            "mean_realized_r": round(statistics.fmean(realized_r), 6) if realized_r else None,
            "median_realized_r": round(float(statistics.median(realized_r)), 6) if realized_r else None,
            "units": {
                "gross_pnl_inr": "INR_REALIZED",
                "costs_inr": "INR_REALIZED_EXECUTION_COSTS",
                "net_pnl_inr": "INR_REALIZED_NET_OF_EXECUTION_COSTS",
                "expectancy_inr_per_trade": "INR_PER_SETTLED_TRADE",
                "max_drawdown_inr": "INR_EQUITY_CURVE_DRAWDOWN",
                "accuracy_pct": "PERCENT_SIGNAL_ACCURACY_DECISIVE_ONLY",
                "win_rate_pct": "PERCENT_ECONOMIC_WIN_RATE_DECISIVE_ONLY",
                "normalized_return": "R_REALIZED_NET_PNL_OVER_IMMUTABLE_INITIAL_RISK",
            },
            "outcome_taxonomy_version": taxonomy.authority_version,
            "performance_aggregation_authority": ModelPortfolioPerformanceService.VERSION,
        }

    @staticmethod
    def _filter(rows: Iterable[Dict[str, Any]], filters: Mapping[str, Any]) -> list[Dict[str, Any]]:
        result = list(rows)
        mapping = {
            "desk": "mode", "sector": "sector", "regime": "market_regime",
            "signal_age_bucket": "signal_age_bucket", "confidence_bucket": "confidence_bucket",
            "model_version": "model_version", "feature_manifest_hash": "feature_manifest_hash",
            "arm": "arm_label", "disposition": "disposition", "promotion_reason": "promotion_reason",
        }
        for requested, field in mapping.items():
            value = str(filters.get(requested) or "").strip()
            if not value or value.lower() == "all":
                continue
            result = [row for row in result if str(row.get(field) or "").upper() == value.upper()]
        return result

    def report(
        self, *, period: str = "ALL", desk: str = "all", horizon: str | None = None,
        sector: str = "all", regime: str = "all", signal_age_bucket: str = "all",
        confidence_bucket: str = "all", model_version: str = "all",
        feature_manifest_hash: str = "all", arm: str = "all", disposition: str = "all",
        promotion_reason: str = "all", as_of: datetime | None = None, limit: int = 5000,
    ) -> Dict[str, Any]:
        if self.governance is None or not callable(getattr(self.governance, "selector_attribution_rows", None)):
            return {
                "ok": False, "state": "GOVERNANCE_RESEARCH_AUTHORITY_UNAVAILABLE",
                "version": SERVICE_VERSION, "broker_authority": "NONE",
            }
        if self.final is None or not callable(getattr(self.final, "research_final_lineage", None)):
            return {
                "ok": False, "state": "OPERATIONAL_FINAL_LINEAGE_UNAVAILABLE",
                "version": SERVICE_VERSION, "broker_authority": "NONE",
            }
        if not callable(getattr(self.final, "settled_final_economics", None)):
            return {
                "ok": False, "state": "OPERATIONAL_FINAL_ECONOMICS_UNAVAILABLE",
                "version": SERVICE_VERSION, "broker_authority": "NONE",
            }
        period_key = str(period or "ALL").upper()
        if period_key not in PERIODS:
            period_key = "ALL"
        desk_key = str(desk or "all").lower()
        if desk_key not in {"all", "delivery", "intraday"}:
            desk_key = "all"
        requested_horizon = str(horizon or "").strip().lower() or None
        raw_rows = self.governance.selector_attribution_rows(
            mode=None if desk_key == "all" else desk_key,
            horizon=requested_horizon,
        )
        now = as_of or datetime.now(timezone.utc)
        now = now.astimezone(timezone.utc) if now.tzinfo else now.replace(tzinfo=timezone.utc)
        days = PERIODS[period_key]
        cutoff = now - timedelta(days=days) if days is not None else None
        # Research attribution is point-in-time: settlements after ``as_of``
        # are future information and must never enter the selected population.
        if cutoff is not None:
            raw_rows = [
                row for row in raw_rows
                if (settled := _dt(row.get("settled_at"))) is not None
                and cutoff <= settled <= now
            ]
        else:
            raw_rows = [
                row for row in raw_rows
                if (settled := _dt(row.get("settled_at"))) is not None
                and settled <= now
            ]
        available_horizons = sorted({
            str(row.get("horizon") or "").strip().lower()
            for row in raw_rows if str(row.get("horizon") or "").strip()
        })
        # Research outcome bps are horizon-specific. Never average different
        # holding horizons into one selection-uplift number. Period filtering
        # happens first so an irrelevant historical horizon cannot block the
        # current comparison window.
        if requested_horizon is None and len(available_horizons) > 1:
            return {
                "ok": True, "state": "HORIZON_SELECTION_REQUIRED", "version": SERVICE_VERSION,
                "period": period_key, "requested_horizon": "all",
                "available_horizons": available_horizons,
                "horizon_policy": "NO_CROSS_HORIZON_RETURN_BLENDING",
                "authority": {
                    "research": "GOVERNANCE_POSTGRESQL_IMMUTABLE_SELECTOR_EVIDENCE",
                    "final": "OPERATIONAL_POSTGRESQL_EXACT_ID_ONLY",
                    "join_policy": "EXACT_FROZEN_ORIGIN_ID_ONLY_NO_SYMBOL_TIME_INFERENCE",
                    "broker_authority": "NONE",
                },
                "selection_effectiveness": None, "final_realized": None,
                "records": [], "record_count": 0,
                "production_change_allowed": False, "broker_authority": "NONE",
            }
        resolved_horizon = requested_horizon or (available_horizons[0] if len(available_horizons) == 1 else None)
        raw_rows = list(raw_rows)[-max(1, min(int(limit), 20000)):]

        decision_ids = [str(row.get("origin_decision_id") or "") for row in raw_rows]
        signal_ids = [str(row.get("origin_signal_id") or "") for row in raw_rows]
        lineage = self.final.research_final_lineage(decision_ids=decision_ids, signal_ids=signal_ids)
        by_decision, by_signal, positions = self._decision_maps(lineage)

        enriched: list[Dict[str, Any]] = []
        for raw in raw_rows:
            row = dict(raw or {})
            decision_id = str(row.get("origin_decision_id") or "").strip()
            signal_id = str(row.get("origin_signal_id") or "").strip()
            decision = by_decision.get(decision_id) or by_signal.get(signal_id)
            position = positions.get(signal_id) or positions.get(decision_id)
            disposition_value, reason = self._disposition(row, decision=decision, position=position)
            observed_at = row.get("candidate_observed_at") or row.get("observed_at")
            age = DEFAULT_SIGNAL_AGE_AUTHORITY.measure(
                generated_at=row.get("generated_at"), opened_at=observed_at,
                at=observed_at, mode=row.get("mode"),
            )
            raw_arm = str(row.get("arm") or "").lower()
            row.update(_research_risk_attribution(row))
            row.update({
                "arm_label": ARM_LABELS.get(raw_arm, raw_arm.upper() or "UNKNOWN"),
                "sector": str(row.get("sector") or "UNKNOWN").upper(),
                "market_regime": str(row.get("market_regime") or "UNKNOWN").upper(),
                "signal_age_bucket": str(age.get("decision_delay_bucket") or "MISSING"),
                "signal_age_bucket_policy_version": age.get("age_bucket_policy_version"),
                "confidence_bucket": _confidence_bucket(row.get("predicted_percentile")),
                "confidence_bucket_version": CONFIDENCE_BUCKET_VERSION,
                "confidence_semantics": "RANK_PERCENTILE_NOT_CALIBRATED_PROBABILITY",
                "disposition": disposition_value,
                "promotion_reason": str(reason or "UNKNOWN"),
                "final_decision_id": decision.get("decision_id") if decision else None,
                "final_position_id": position.get("position_id") if position else None,
                "final_lineage_state": "EXACT_ID_LINKED" if (decision or position) else (
                    "HISTORICAL_ID_MISSING" if disposition_value == "UNLINKED_HISTORICAL" else "NO_FINAL_ROW_YET"
                ),
            })
            enriched.append(row)

        filters = {
            "desk": desk_key, "sector": sector, "regime": regime,
            "signal_age_bucket": signal_age_bucket, "confidence_bucket": confidence_bucket,
            "model_version": model_version, "feature_manifest_hash": feature_manifest_hash,
            "arm": arm, "disposition": disposition, "promotion_reason": promotion_reason,
        }
        filtered = self._filter(enriched, filters)
        promoted = [row for row in filtered if row["disposition"] in {"PROMOTED", "ADMITTED"}]
        admitted = [row for row in filtered if row["disposition"] == "ADMITTED"]
        rejected = [row for row in filtered if row["disposition"] == "REJECTED"]
        research_only = [row for row in filtered if row["disposition"] == "RESEARCH_ONLY"]
        unlinked = [row for row in filtered if row["disposition"] == "UNLINKED_HISTORICAL"]

        all_candidates = _candidate_metrics(filtered)
        admitted_candidates = _candidate_metrics(admitted)
        rejected_candidates = _candidate_metrics(rejected)
        all_mean = all_candidates.get("mean_net_return_bps")
        admitted_mean = admitted_candidates.get("mean_net_return_bps")
        rejected_mean = rejected_candidates.get("mean_net_return_bps")
        all_mean_r = all_candidates.get("mean_research_net_r")
        admitted_mean_r = admitted_candidates.get("mean_research_net_r")
        rejected_mean_r = rejected_candidates.get("mean_research_net_r")
        unique_candidates: dict[str, Dict[str, Any]] = {}
        for row in filtered:
            unique_candidates.setdefault(str(row.get("candidate_id")), row)
        positives = [row for row in unique_candidates.values() if (_num(row.get("net_return_bps")) or 0.0) > 0]
        positive_total = len(positives)
        admitted_positive = sum(row.get("disposition") == "ADMITTED" for row in positives)
        rejected_positive = sum(row.get("disposition") == "REJECTED" for row in positives)

        linked_position_ids = sorted({str(row.get("final_position_id")) for row in filtered if row.get("final_position_id")})
        final_all_rows = self.final.settled_final_economics(
            closed_since=cutoff, mode=None if desk_key == "all" else desk_key, position_ids=None, limit=100000,
        )
        final_linked_rows = self.final.settled_final_economics(
            closed_since=cutoff, mode=None if desk_key == "all" else desk_key,
            position_ids=linked_position_ids, limit=max(1, len(linked_position_ids) or 1),
        )
        final_all_metrics = self._final_metrics(final_all_rows)
        final_linked_metrics = self._final_metrics(final_linked_rows)

        slices = {
            key: self._slice(filtered, key)
            for key in (
                "mode", "sector", "market_regime", "signal_age_bucket", "confidence_bucket",
                "model_version", "feature_manifest_hash", "arm_label", "disposition", "promotion_reason",
            )
        }
        return {
            "ok": True,
            "state": "SOURCE_ATTRIBUTION_AVAILABLE",
            "version": SERVICE_VERSION,
            "period": period_key,
            "horizon": resolved_horizon or "all",
            "available_horizons": available_horizons,
            "horizon_policy": "NO_CROSS_HORIZON_RETURN_BLENDING",
            "filters": filters,
            "authority": {
                "research": "GOVERNANCE_POSTGRESQL_IMMUTABLE_SELECTOR_EVIDENCE",
                "final": lineage.get("authority") or "OPERATIONAL_POSTGRESQL_EXACT_ID_ONLY",
                "join_policy": "EXACT_FROZEN_ORIGIN_ID_ONLY_NO_SYMBOL_TIME_INFERENCE",
                "broker_authority": "NONE",
            },
            "groups": {
                "ALL": self._group(filtered),
                "PROMOTED": self._group(promoted),
                "REJECTED": self._group(rejected),
                "ADMITTED": self._group(admitted),
                "RESEARCH_ONLY": self._group(research_only),
                "UNLINKED_HISTORICAL": self._group(unlinked),
            },
            "selection_effectiveness": {
                "admitted_vs_all_mean_uplift_bps": (
                    round(float(admitted_mean) - float(all_mean), 6)
                    if admitted_mean is not None and all_mean is not None else None
                ),
                "admitted_vs_rejected_mean_uplift_bps": (
                    round(float(admitted_mean) - float(rejected_mean), 6)
                    if admitted_mean is not None and rejected_mean is not None else None
                ),
                "admitted_vs_all_mean_uplift_r": (
                    round(float(admitted_mean_r) - float(all_mean_r), 6)
                    if admitted_mean_r is not None and all_mean_r is not None else None
                ),
                "admitted_vs_rejected_mean_uplift_r": (
                    round(float(admitted_mean_r) - float(rejected_mean_r), 6)
                    if admitted_mean_r is not None and rejected_mean_r is not None else None
                ),
                "opportunity_capture_pct": round(admitted_positive * 100.0 / positive_total, 4) if positive_total else None,
                "missed_opportunity_rejected_pct": round(rejected_positive * 100.0 / positive_total, 4) if positive_total else None,
                "positive_research_candidates": positive_total,
                "interpretation": "Counterfactual Research return bps/R only; never blended into realized Model Paper INR P&L.",
            },
            "final_realized": {
                "same_period_same_desk": final_all_metrics,
                "linked_to_filtered_research": final_linked_metrics,
                "linked_position_ids": linked_position_ids,
                "period_start": cutoff.isoformat().replace("+00:00", "Z") if cutoff else None,
                "period_end": now.isoformat().replace("+00:00", "Z"),
                "desk": desk_key,
                "authority": "OPERATIONAL_POSTGRESQL_CANONICAL_MODEL_PAPER",
                "book": "MODEL_PAPER",
            },
            "normalized_comparison": {
                "research_all_mean_net_r": all_candidates.get("mean_research_net_r"),
                "research_admitted_mean_net_r": admitted_candidates.get("mean_research_net_r"),
                "final_linked_mean_realized_r": final_linked_metrics.get("mean_realized_r"),
                "final_same_period_desk_mean_realized_r": final_all_metrics.get("mean_realized_r"),
                "research_r_semantics": "COUNTERFACTUAL_NET_BPS_OVER_FROZEN_INITIAL_RISK",
                "final_r_semantics": "REALIZED_NET_PNL_OVER_IMMUTABLE_INITIAL_RISK",
                "cross_lane_policy": "SIDE_BY_SIDE_NORMALIZED_R_ONLY; RESEARCH_BPS_AND_FINAL_INR_ARE_NEVER_ARITHMETICALLY_BLENDED",
            },
            "unit_contract": {
                "research_net_return": "BPS_COUNTERFACTUAL_FIXED_HORIZON",
                "research_normalized_return": "R_COUNTERFACTUAL_FROZEN_INITIAL_RISK",
                "final_economic_pnl": "INR_REALIZED_NET_OF_EXECUTION_COSTS",
                "final_normalized_return": "R_REALIZED_NET_PNL_OVER_IMMUTABLE_INITIAL_RISK",
            },
            "slices": slices,
            "records": filtered,
            "record_count": len(filtered),
            "candidate_count": len({row.get("candidate_id") for row in filtered if row.get("candidate_id")}),
            "confidence_policy": {
                "version": CONFIDENCE_BUCKET_VERSION,
                "HIGH": "predicted rank percentile >= 80",
                "MEDIUM": "predicted rank percentile >= 50 and < 80",
                "LOW": "predicted rank percentile < 50",
                "MISSING": "rank percentile unavailable",
                "probability_claim": "NONE",
            },
            "production_change_allowed": False,
            "broker_authority": "NONE",
        }
