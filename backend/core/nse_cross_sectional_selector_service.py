"""Transparent NSE cross-sectional shadow selectors for Intraday and Delivery.

This service deliberately avoids claiming calibrated probability.  It ranks a
single point-in-time candidate population using compact India-relevant feature
families.  Outputs are shadow-only and cannot alter production decisions.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from core.numeric_semantics import finite_number
from core.production_mode_policy import require_production_mode, UnsupportedProductionMode

SELECTOR_VERSION = "nse-cross-sectional-shadow-1.2.0-strict-finite-zero-influence"
HYBRID_VERSION = "nse-hybrid-shadow-1.2.0-strict-finite-zero-influence"


def _num(value: Any) -> Optional[float]:
    return finite_number(value)


def _first(row: Mapping[str, Any], names: Sequence[str]) -> Optional[float]:
    for name in names:
        # A broad-radar full-day volume ratio is a discovery diagnostic, not a
        # time-of-day decision RVOL.  Likewise, governed participation can
        # explicitly mark a session/delivery metric non-actionable.  Legacy
        # rows without these metadata keys retain historical selector behavior.
        if name == "relative_volume" and row.get("relative_volume_decision_usable") is False:
            continue
        if name in {"session_relative_volume", "recent_volume_vs_base"} and row.get("participation_decision_usable") is False:
            continue
        value = _num(row.get(name))
        if value is not None:
            return value
    return None


def _rank_percentiles(values: Sequence[Optional[float]], *, higher_is_better: bool = True) -> List[Optional[float]]:
    available = [(index, value) for index, value in enumerate(values) if value is not None]
    result: List[Optional[float]] = [None] * len(values)
    if not available:
        return result
    ordered = sorted(available, key=lambda pair: (pair[1], pair[0]), reverse=higher_is_better)
    count = len(ordered)
    if count == 1:
        result[ordered[0][0]] = 1.0
        return result
    # Average rank for ties, then map best to 1 and worst to 0.
    cursor = 0
    while cursor < count:
        end = cursor + 1
        while end < count and ordered[end][1] == ordered[cursor][1]:
            end += 1
        average_position = (cursor + end - 1) / 2.0
        percentile = 1.0 - average_position / (count - 1)
        for pos in range(cursor, end):
            result[ordered[pos][0]] = percentile
        cursor = end
    return result


DELIVERY_FEATURES: Tuple[Tuple[str, Tuple[str, ...], float, bool], ...] = (
    ("momentum_20d", ("relative_strength_20d", "return_20d", "monthly_return"), 0.14, True),
    ("momentum_60d", ("relative_strength_60d", "return_60d", "quarterly_return"), 0.10, True),
    ("momentum_120d", ("relative_strength_120d", "return_120d"), 0.06, True),
    ("sector_relative", ("sector_relative_strength", "sector_relative_return"), 0.10, True),
    ("delivery_pct_surprise", ("delivery_pct_zscore", "delivery_percentage_zscore"), 0.10, True),
    ("delivered_quantity_surprise", ("delivered_qty_zscore", "delivered_quantity_zscore"), 0.12, True),
    ("volume_confirmation", ("recent_volume_vs_base", "session_relative_volume", "relative_volume"), 0.08, True),
    ("fundamental_quality", ("fundamental_score",), 0.10, True),
    ("structure", ("market_structure_score", "technical_score"), 0.08, True),
    ("liquidity", ("liquidity_score", "tradeability_score"), 0.07, True),
    ("volatility_penalty", ("volatility_pct", "atr_pct"), 0.03, False),
    ("event_risk_penalty", ("event_risk_score", "event_risk_days"), 0.02, False),
)

INTRADAY_FEATURES: Tuple[Tuple[str, Tuple[str, ...], float, bool], ...] = (
    ("intraday_relative_strength", ("intraday_relative_strength", "index_relative_strength", "change_pct"), 0.16, True),
    ("sector_relative", ("sector_relative_strength", "sector_relative_return"), 0.10, True),
    ("relative_volume", ("session_relative_volume", "recent_volume_vs_base", "relative_volume"), 0.15, True),
    ("vwap_alignment", ("vwap_distance_pct",), 0.08, True),
    ("trend_strength", ("adx",), 0.08, True),
    ("structure", ("market_structure_score", "technical_score"), 0.10, True),
    ("expected_net_move", ("expected_net_move_bps", "post_cost_target_move_bps"), 0.14, True),
    ("spread_penalty", ("spread_bps",), 0.08, False),
    ("quote_age_penalty", ("quote_age_seconds",), 0.04, False),
    ("liquidity", ("liquidity_score", "tradeability_score"), 0.07, True),
)


def feature_manifest() -> Dict[str, Any]:
    metadata = {
        "momentum_20d": ("momentum", "One-month relative persistence; ranked cross-sectionally."),
        "momentum_60d": ("momentum", "Quarterly persistence less sensitive to a single session."),
        "momentum_120d": ("momentum", "Medium-horizon trend confirmation."),
        "intraday_relative_strength": ("relative_strength", "Live strength versus the index/eligible universe."),
        "sector_relative": ("relative_strength", "Separates stock-specific strength from sector beta."),
        "delivery_pct_surprise": ("india_participation", "Delivery percentage versus the stock's own history; confirmation only."),
        "delivered_quantity_surprise": ("india_participation", "Absolute delivered quantity shock, avoiding percentage-only distortion."),
        "volume_confirmation": ("participation", "Price move confirmed by unusual trading participation."),
        "relative_volume": ("participation", "Intraday volume relative to a point-in-time baseline."),
        "fundamental_quality": ("fundamentals", "Compact quality/earnings evidence as known at decision time."),
        "structure": ("price_structure", "Breakout, pullback/retest and trend-structure quality."),
        "vwap_alignment": ("microstructure_proxy", "VWAP alignment; not a true footprint signal."),
        "trend_strength": ("price_structure", "Trend persistence context without independent authority."),
        "expected_net_move": ("execution_economics", "Post-cost movement available after the canonical India cost model."),
        "spread_penalty": ("execution_economics", "Penalises immediate crossing cost and weak executability."),
        "quote_age_penalty": ("data_quality", "Penalises stale decision-time market evidence."),
        "liquidity": ("execution_economics", "Controls tradability and expected impact."),
        "volatility_penalty": ("risk", "Penalises unstable moves and gap risk."),
        "event_risk_penalty": ("risk", "Penalises scheduled company/market event exposure."),
    }
    def pack(specs):
        rows = []
        for name, aliases, weight, higher in specs:
            family, rationale = metadata.get(name, ("other", "Retained compact NSE challenger input."))
            rows.append({
                "name": name, "aliases": list(aliases), "weight": weight,
                "higher_is_better": higher, "family": family, "economic_rationale": rationale,
                "point_in_time_required": True,
                "prediction_state": "SHADOW_ONLY",
                "authority": "SHADOW_ONLY",
                "decision_weight": 0.0,
                "selection_status": "ACTIVE_DETERMINISTIC_SELECTOR",
            })
        return rows
    return {
        "version": SELECTOR_VERSION,
        "delivery": pack(DELIVERY_FEATURES),
        "intraday": pack(INTRADAY_FEATURES),
        "retained_feature_count": len(DELIVERY_FEATURES) + len(INTRADAY_FEATURES),
        "research_factor_zoo_authority": "ZERO_UNLESS_EXPLICITLY_RETAINED_AND_VALIDATED",
        "true_footprint_available": False,
        "principles": [
            "point-in-time cross-sectional ranking",
            "delivery percentage is context, never sole alpha",
            "missing inputs reduce score",
            "no calibrated probability before settled evidence",
            "rank outputs directly drive automatic paper selection",
            "OHLCV participation is not labelled true footprint",
        ],
    }


FEATURE_MANIFEST_HASH = hashlib.sha256(
    json.dumps(feature_manifest(), sort_keys=True, separators=(",", ":")).encode("utf-8")
).hexdigest()


class NseCrossSectionalSelectorService:
    """Evaluate heuristic, quant and hybrid arms on exactly one population."""

    @staticmethod
    def _setup_family(row: Mapping[str, Any], mode: str) -> str:
        if mode == "delivery":
            setup = str(row.get("setup") or row.get("strategy") or "").lower()
            if "vcp" in setup or "contraction" in setup:
                return "VOLATILITY_COMPRESSION_BREAKOUT"
            if "pullback" in setup or "retest" in setup:
                return "TREND_PULLBACK_RETEST"
            if "accum" in setup or _num(row.get("delivery_pct_zscore")) not in (None, 0):
                return "DELIVERY_ACCUMULATION"
            return "CROSS_SECTIONAL_MOMENTUM"
        if row.get("orb_confirmed") is True:
            return "ORB_CONTINUATION"
        gap = str(row.get("gap_state") or "").lower()
        if "fail" in gap or "reversal" in gap:
            return "GAP_REVERSAL"
        if "continu" in gap:
            return "GAP_CONTINUATION"
        vwap_distance = _num(row.get("vwap_distance_pct"))
        if vwap_distance is not None and abs(vwap_distance) <= 0.20:
            return "VWAP_TREND_PULLBACK"
        if (_num(row.get("intraday_relative_strength")) or 0.0) > 0:
            return "SECTOR_LEADER_MOMENTUM"
        return "RANGE_MEAN_REVERSION"

    @staticmethod
    def _prediction_base(row: Mapping[str, Any], *, arm: str, score: float, rank: int,
                         percentile: float, coverage: float, setup_family: str,
                         feature_contributions: Dict[str, Any]) -> Dict[str, Any]:
        if score >= 70 and coverage >= 0.60:
            rank_tier = "HIGH"
        elif score >= 45 and coverage >= 0.35:
            rank_tier = "MIDDLE"
        else:
            rank_tier = "LOW"
        return {
            "candidate_id": row.get("candidate_id"),
            "symbol": str(row.get("symbol") or "").upper(),
            "mode": row.get("mode"),
            "arm": arm,
            "score": round(score, 4),
            "rank": int(rank),
            "percentile": round(percentile, 4),
            "feature_coverage": round(coverage, 4),
            "feature_contributions": feature_contributions,
            "setup_family": setup_family,
            # This is a descriptive cross-sectional rank tier, never an
            # accept/reject instruction or a calibrated meta-label.
            "rank_tier": rank_tier,
            "meta_label": None,
            "probability_positive": None,
            "expected_net_return": None,
            "calibration_state": "INSUFFICIENT_SETTLED_OUTCOMES",
            "prediction_state": "SHADOW_ONLY",
            "authority": "SHADOW_ONLY",
            "eligible_for_production": False,
            "decision_weight": 0.0,
            "broker_execution_weight": 0.0,
            "eligible_for_paper_decision": True,
            "production_status": row.get("production_status") if row.get("production_status") is not None else row.get("status"),
            "production_decision": row.get("production_decision") if row.get("production_decision") is not None else row.get("decision"),
            "model_version": SELECTOR_VERSION if arm == "quant" else HYBRID_VERSION if arm == "hybrid" else str(row.get("evidence_model_id") or "unvalidated-heuristic"),
        }

    def evaluate(self, rows: Iterable[Mapping[str, Any]], *, mode: str,
                 population_fingerprint: str) -> Dict[str, Any]:
        desk = require_production_mode(mode)
        raw_population = [dict(row or {}) for row in rows]
        population = []
        for row in raw_population:
            try:
                if require_production_mode(row.get("mode")) == desk:
                    population.append(row)
            except UnsupportedProductionMode:
                continue
        specs = DELIVERY_FEATURES if desk == "delivery" else INTRADAY_FEATURES
        if not population:
            return {
                "ok": True, "version": SELECTOR_VERSION, "mode": desk,
                "population_fingerprint": population_fingerprint,
                "arms": {"heuristic": [], "quant": [], "hybrid": []},
                "prediction_state": "MODEL_UNAVAILABLE",
                "decision_weight": 0.0,
                "policy": "no eligible population, so no simulated trade",
            }

        values_by_feature: Dict[str, List[Optional[float]]] = {
            name: [_first(row, aliases) for row in population]
            for name, aliases, _weight, _higher in specs
        }
        ranks_by_feature: Dict[str, List[Optional[float]]] = {
            name: _rank_percentiles(values_by_feature[name], higher_is_better=higher)
            for name, _aliases, _weight, higher in specs
        }
        quant_rows: List[Dict[str, Any]] = []
        total_weight = sum(weight for _name, _aliases, weight, _higher in specs)
        for index, row in enumerate(population):
            weighted = 0.0
            available_weight = 0.0
            contributions: Dict[str, Any] = {}
            for name, aliases, weight, higher in specs:
                raw = values_by_feature[name][index]
                rank_value = ranks_by_feature[name][index]
                if raw is None or rank_value is None:
                    contributions[name] = {"value": None, "percentile": None, "weight": weight, "state": "missing"}
                    continue
                weighted += rank_value * weight
                available_weight += weight
                contributions[name] = {
                    "value": round(raw, 6), "percentile": round(rank_value * 100.0, 4),
                    "weight": weight, "direction": "higher" if higher else "lower",
                    "state": "available",
                }
            coverage = available_weight / total_weight if total_weight else 0.0
            normalized = (weighted / available_weight * 100.0) if available_weight else 0.0
            # Missing data must reduce—not improve—the shadow score.
            quant_score = normalized * (0.70 + 0.30 * coverage)
            quant_rows.append({
                "row": row,
                "score": max(0.0, min(100.0, quant_score)),
                "coverage": coverage,
                "contributions": contributions,
                "setup_family": self._setup_family(row, desk),
            })

        def finalize(items: List[Dict[str, Any]], arm: str) -> List[Dict[str, Any]]:
            ordered = sorted(items, key=lambda item: (-float(item["score"]), str(item["row"].get("symbol") or "")))
            count = len(ordered)
            output = []
            for position, item in enumerate(ordered, start=1):
                percentile = 100.0 if count == 1 else (count - position) * 100.0 / (count - 1)
                output.append(self._prediction_base(
                    item["row"], arm=arm, score=item["score"], rank=position,
                    percentile=percentile, coverage=item.get("coverage", 1.0),
                    setup_family=item.get("setup_family") or self._setup_family(item["row"], desk),
                    feature_contributions=item.get("contributions") or {},
                ))
            return output

        heuristic_items = []
        hybrid_items = []
        for item in quant_rows:
            baseline = _num(item["row"].get("evidence_score"))
            if baseline is None:
                baseline = _num(item["row"].get("rank_score"))
            if baseline is None:
                baseline = _num(item["row"].get("score"))
            if baseline is None:
                baseline = 0.0
            heuristic_items.append({
                "row": item["row"], "score": baseline, "coverage": 1.0,
                "contributions": {"evidence_score": {"value": baseline, "state": "static_policy_baseline"}},
                "setup_family": item["setup_family"],
            })
            eligible = bool(
                item["row"].get("identity_verified") is True
                and item["row"].get("instrument_key")
                and str(item["row"].get("production_status") or item["row"].get("status") or "").upper() not in {"BLOCKED", "REJECTED"}
            )
            hybrid_score = (0.40 * baseline + 0.60 * item["score"]) if eligible else 0.0
            hybrid_items.append({
                "row": item["row"], "score": hybrid_score,
                "coverage": item["coverage"],
                "contributions": {
                    "baseline_evidence": {"value": baseline, "weight": 0.40},
                    "quant_rank": {"value": item["score"], "weight": 0.60},
                    "eligibility": {"value": eligible},
                },
                "setup_family": item["setup_family"],
            })

        return {
            "ok": True,
            "version": SELECTOR_VERSION,
            "hybrid_version": HYBRID_VERSION,
            "mode": desk,
            "population_fingerprint": str(population_fingerprint),
            "candidate_count": len(population),
            "arms": {
                "heuristic": finalize(heuristic_items, "heuristic"),
                "quant": finalize(quant_rows, "quant"),
                "hybrid": finalize(hybrid_items, "hybrid"),
            },
            "probability_policy": "probabilities remain unavailable until governed calibration evidence exists",
            "prediction_state": "SHADOW_ONLY",
            "decision_weight": 0.0,
            "broker_execution_weight": 0.0,
        }
