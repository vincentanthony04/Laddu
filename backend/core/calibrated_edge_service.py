"""Empirical-Bayes post-cost edge calibration for final candidate admission.

This service never creates direction or adds evidence points.  It maps the
canonical desk score into an observed outcome segment, shrinks small samples
towards a neutral prior, subtracts the configured desk cost estimate and emits
an auditable PASS/BLOCK/SHADOW gate.  Only validated negative lower-confidence
expectancy can veto a promotion; insufficient samples preserve the deterministic
champion and remain shadow-only.
"""
from __future__ import annotations

from core.production_mode_policy import require_production_mode

import json
import math
import statistics
import threading
import time
from typing import Any, Dict, Iterable, Mapping, Optional

from core.india_cost_model import IndiaCashCostModel
from core.production_mode_policy import require_production_mode

CALIBRATED_EDGE_VERSION = "empirical-bayes-edge-1.0.0"


def _num(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _canonical_mode(value: Any) -> Optional[str]:
    raw = str(value or "").lower().strip()
    try:
        return require_production_mode(raw)
    except ValueError:
        return ""
    return None


def _features(row: Mapping[str, Any]) -> Dict[str, Any]:
    raw = row.get("feature_json")
    if isinstance(raw, Mapping):
        return dict(raw)
    try:
        value = json.loads(str(raw or "{}"))
        return dict(value) if isinstance(value, Mapping) else {}
    except Exception:
        return {}


def _score_band(value: Any) -> str:
    score = _num(value)
    if score is None:
        return "unknown"
    if score < 60:
        return "<60"
    if score < 70:
        return "60-69"
    if score < 80:
        return "70-79"
    return "80+"


def _regime(value: Any) -> str:
    text = str(value or "unknown").lower().strip().replace(" ", "_")
    if not text:
        return "unknown"
    if any(token in text for token in ("stress", "panic", "risk_off", "bear")):
        return "risk_off"
    if any(token in text for token in ("range", "chop", "sideway", "neutral")):
        return "range"
    if any(token in text for token in ("trend", "bull", "supportive", "up")):
        return "trend"
    return text[:40]


def _wilson(wins: int, total: int, z: float = 1.6448536269514722) -> tuple[Optional[float], Optional[float]]:
    if total <= 0:
        return None, None
    p = wins / total
    denom = 1.0 + z * z / total
    centre = (p + z * z / (2.0 * total)) / denom
    margin = z * math.sqrt((p * (1.0 - p) / total) + (z * z / (4.0 * total * total))) / denom
    return max(0.0, centre - margin), min(1.0, centre + margin)


def _cross_fitted_binary_metrics(values: list[float]) -> tuple[Optional[float], Optional[float]]:
    binary = [1.0 if value > 0 else 0.0 for value in values if value != 0]
    total = len(binary)
    if total < 2:
        return None, None
    wins = sum(binary)
    squared = []
    log_losses = []
    for outcome in binary:
        # Leave-one-out Beta(2,2) posterior: the row being scored never
        # contributes to its own probability estimate.
        p = ((wins - outcome) + 2.0) / ((total - 1) + 4.0)
        p = min(1.0 - 1e-9, max(1e-9, p))
        squared.append((p - outcome) ** 2)
        log_losses.append(-(outcome * math.log(p) + (1.0 - outcome) * math.log(1.0 - p)))
    return statistics.fmean(squared), statistics.fmean(log_losses)


class CalibratedEdgeService:
    MIN_SAMPLE = 30
    MIN_DECISIVE = 20
    CACHE_SECONDS = 300.0
    ASSUMED_NOTIONAL = 100000.0

    def __init__(self, store: Any = None):
        self.store = store
        self._lock = threading.Lock()
        self._cached_at = 0.0
        self._cached_rows: list[Dict[str, Any]] = []

    def _rows(self) -> list[Dict[str, Any]]:
        if self.store is None or not hasattr(self.store, "outcome_learning_rows"):
            return []
        now = time.time()
        with self._lock:
            if self._cached_rows and now - self._cached_at < self.CACHE_SECONDS:
                return list(self._cached_rows)
            try:
                rows = [dict(row) for row in (self.store.outcome_learning_rows(limit=5000) or [])]
            except Exception:
                rows = []
            self._cached_rows = rows
            self._cached_at = now
            return list(rows)

    @staticmethod
    def _normalise(rows: Iterable[Mapping[str, Any]]) -> list[Dict[str, Any]]:
        out = []
        for row in rows:
            mode = _canonical_mode(row.get("mode"))
            if mode not in {"intraday", "delivery"}:
                continue
            features = _features(row)
            outcome_r = _num(features.get("outcome_r_multiple"))
            out.append({
                "mode": mode,
                "side": str(row.get("side") or "unknown").upper(),
                "score_band": _score_band(features.get("score")),
                "regime": _regime(features.get("regime") or features.get("market_structure")),
                "outcome_r": outcome_r,
                "scale_state": "initial_r_multiple" if outcome_r is not None else "legacy_unscaled",
            })
        return out

    def _segment(self, candidate: Mapping[str, Any]) -> tuple[str, list[Dict[str, Any]]]:
        rows = self._normalise(self._rows())
        mode = require_production_mode(candidate.get("mode"))
        side = str(candidate.get("side") or "unknown").upper()
        score_band = _score_band(candidate.get("rank_score") if candidate.get("rank_score") is not None else candidate.get("score"))
        regime = _regime(candidate.get("regime") or candidate.get("index_context") or candidate.get("market_structure"))
        filters = [
            ("desk_side_score_regime", lambda r: r["mode"] == mode and r["side"] == side and r["score_band"] == score_band and r["regime"] == regime, 40),
            ("desk_side_score", lambda r: r["mode"] == mode and r["side"] == side and r["score_band"] == score_band, 30),
            ("desk_side", lambda r: r["mode"] == mode and r["side"] == side, 30),
            ("desk", lambda r: r["mode"] == mode, 40),
            ("all_desks", lambda r: True, 60),
        ]
        best_name, best_rows, best_scaled = "none", [], -1
        for name, predicate, minimum in filters:
            subset = [row for row in rows if predicate(row)]
            scaled_count = sum(row.get("outcome_r") is not None for row in subset)
            if scaled_count > best_scaled:
                best_name, best_rows, best_scaled = name, subset, scaled_count
            if scaled_count >= minimum:
                return name, subset
        return best_name, best_rows

    @classmethod
    def _cost_in_r(cls, candidate: Mapping[str, Any], mode: str) -> tuple[Optional[float], Dict[str, Any]]:
        entry = _num(candidate.get("entry") if candidate.get("entry") is not None else candidate.get("planned_entry"))
        stop = _num(candidate.get("sl") or candidate.get("stop") or candidate.get("planned_sl"))
        if entry is None or entry <= 0 or stop is None or stop <= 0 or entry == stop:
            return None, {"state": "unavailable", "reason": "valid entry/original stop missing"}
        risk_points = abs(entry - stop)
        quantity = max(1, int(cls.ASSUMED_NOTIONAL // entry))
        model = IndiaCashCostModel.for_evidence(mode, dict(candidate))
        costs = model.round_trip(entry, entry, quantity)
        per_share = float(costs["costs"]["total"]) / quantity
        cost_r = per_share / risk_points
        return cost_r, {
            "state": "estimated",
            "quantity_assumption": quantity,
            "notional_assumption": round(quantity * entry, 2),
            "initial_risk_points": round(risk_points, 6),
            "round_trip_cost": costs["costs"]["total"],
            "cost_per_share": round(per_share, 6),
            "cost_r_multiple": round(cost_r, 6),
            "cost_version": model.config.version,
            "cost_authority": "IndiaCashCostAuthority",
            "cost_authority_version": "1.2.0",
            "cost_exchange": model.config.exchange,
            "cost_bse_group": model.config.bse_group,
        }

    def evaluate(self, candidate: Mapping[str, Any]) -> Dict[str, Any]:
        mode = require_production_mode(candidate.get("mode"))
        segment_name, rows = self._segment(candidate)
        scaled = [row for row in rows if row.get("outcome_r") is not None]
        outcomes_r = [float(row["outcome_r"]) for row in scaled]
        wins = sum(value > 0 for value in outcomes_r)
        losses = sum(value < 0 for value in outcomes_r)
        decisive = wins + losses
        sample = len(outcomes_r)
        unscaled_sample = len(rows) - sample
        # Beta(2,2) prior prevents tiny samples from masquerading as certainty.
        posterior_probability = (wins + 2.0) / (decisive + 4.0) if decisive >= 0 else 0.5
        ci_low, ci_high = _wilson(wins, decisive)
        mean = statistics.fmean(outcomes_r) if outcomes_r else None
        stdev = statistics.stdev(outcomes_r) if len(outcomes_r) >= 2 else None
        lower_mean = None
        if mean is not None:
            lower_mean = mean if not stdev else mean - 1.6448536269514722 * stdev / math.sqrt(sample)
        cost_r, cost = self._cost_in_r(candidate, mode)
        expected_net = mean - cost_r if mean is not None and cost_r is not None else None
        lower_net = lower_mean - cost_r if lower_mean is not None and cost_r is not None else None
        brier, log_loss = _cross_fitted_binary_metrics(outcomes_r)
        calibration_better_than_neutral = brier is not None and brier < 0.25
        validated = (
            sample >= self.MIN_SAMPLE
            and decisive >= self.MIN_DECISIVE
            and cost_r is not None
            and calibration_better_than_neutral
        )
        if not validated:
            gate = "SHADOW"
            state = "INSUFFICIENT_SAMPLE"
            reason = (f"{sample} scale-valid R outcomes / {decisive} decisive; "
                      f"{self.MIN_SAMPLE}/{self.MIN_DECISIVE} required and cross-fitted Brier must beat 0.25")
        elif lower_net is not None and lower_net <= 0:
            gate = "BLOCK"
            state = "NEGATIVE_LOWER_CONFIDENCE_EDGE"
            reason = "post-cost lower-confidence expected edge is not positive"
        else:
            gate = "PASS"
            state = "VALIDATED_POSITIVE_EDGE"
            reason = "post-cost lower-confidence expected edge is positive"
        return {
            "version": CALIBRATED_EDGE_VERSION,
            "state": state,
            "gate": gate,
            "reason": reason,
            "segment": segment_name,
            "samples": sample,
            "legacy_unscaled_samples": unscaled_sample,
            "outcome_scale": "initial_r_multiple",
            "decisive_samples": decisive,
            "wins": wins,
            "losses": losses,
            "posterior_probability_positive": round(posterior_probability, 6),
            "probability_interval_90": [round(ci_low, 6), round(ci_high, 6)] if ci_low is not None else None,
            "cross_fitted_brier": round(brier, 6) if brier is not None else None,
            "cross_fitted_log_loss": round(log_loss, 6) if log_loss is not None else None,
            "neutral_brier_baseline": 0.25,
            "calibration_better_than_neutral": calibration_better_than_neutral,
            "expected_gross_r": round(mean, 6) if mean is not None else None,
            "expected_net_r": round(expected_net, 6) if expected_net is not None else None,
            "lower_confidence_net_r": round(lower_net, 6) if lower_net is not None else None,
            "cost_estimate": cost,
            "validated": validated,
            "policy": "may veto a validated negative edge; may never add score, direction or capital authority",
        }
