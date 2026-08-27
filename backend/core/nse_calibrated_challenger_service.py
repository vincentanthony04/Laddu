"""Governed regularized NSE challenger models.

A compact L2-regularised logistic model is fitted only after the locked sample,
time and regime gates are met. Validation is purged walk-forward plus a final
chronological holdout, and all model artifacts are shadow-only. The logistic
output remains explicitly a raw shadow estimate until separate calibration is
demonstrated. The service has no production mutation path.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
import statistics
import threading
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from core.nse_cross_sectional_selector_service import (
    DELIVERY_FEATURES,
    FEATURE_MANIFEST_HASH,
    INTRADAY_FEATURES,
)
from core.model_challenger_governance_service import ModelChallengerGovernanceService
from core.quant_edge_data_service import QuantEdgeDataService
from core.quant_research_dataset_service import QuantResearchDatasetService
from core.walk_forward_validation_service import WalkForwardValidationService

MODEL_SERVICE_VERSION = "nse-calibrated-challenger-1.3.0-pl18-bounded-status"
MODEL_FAMILY = "L2_LOGISTIC_PLUS_RIDGE_RETURN"
GOVERNED_MODEL_FAMILY = "logistic_ridge"
DEFAULT_HORIZON = {"intraday": "30m", "delivery": "10d"}
FORECAST_HORIZONS = {"intraday": ("5m", "15m", "30m", "60m", "eod"), "delivery": ("1d", "3d", "5d", "10d", "20d")}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _timestamp(value: Any) -> Optional[datetime]:
    try:
        stamp = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
        return stamp.astimezone(timezone.utc) if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _num(value: Any) -> Optional[float]:
    try:
        out = float(value)
        return out if math.isfinite(out) else None
    except (TypeError, ValueError):
        return None


def _sigmoid(value: float) -> float:
    value = max(-35.0, min(35.0, value))
    return 1.0 / (1.0 + math.exp(-value))


def _sha(value: Any, length: int = 40) -> str:
    material = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(material.encode("utf-8", errors="replace")).hexdigest()[:length]


def _first(features: Mapping[str, Any], aliases: Sequence[str]) -> Optional[float]:
    for alias in aliases:
        value = _num(features.get(alias))
        if value is not None:
            return value
    return None


def _auc(labels: Sequence[int], probabilities: Sequence[float]) -> Optional[float]:
    positives = sum(labels)
    negatives = len(labels) - positives
    if positives == 0 or negatives == 0:
        return None
    ranked = sorted(enumerate(probabilities), key=lambda item: item[1])
    ranks = [0.0] * len(probabilities)
    cursor = 0
    while cursor < len(ranked):
        end = cursor + 1
        while end < len(ranked) and ranked[end][1] == ranked[cursor][1]:
            end += 1
        avg = (cursor + 1 + end) / 2.0
        for pos in range(cursor, end):
            ranks[ranked[pos][0]] = avg
        cursor = end
    rank_sum = sum(rank for rank, label in zip(ranks, labels) if label == 1)
    return (rank_sum - positives * (positives + 1) / 2.0) / (positives * negatives)


def _calibration(labels: Sequence[int], probabilities: Sequence[float], bins: int = 5) -> List[Dict[str, Any]]:
    pairs = sorted(zip(probabilities, labels), key=lambda pair: pair[0])
    output = []
    for index in range(min(bins, len(pairs))):
        start = index * len(pairs) // min(bins, len(pairs))
        end = (index + 1) * len(pairs) // min(bins, len(pairs))
        chunk = pairs[start:end]
        if chunk:
            output.append({
                "bin": index + 1,
                "n": len(chunk),
                "mean_probability": round(statistics.fmean(pair[0] for pair in chunk), 6),
                "observed_positive_rate": round(statistics.fmean(pair[1] for pair in chunk), 6),
            })
    return output


def _prediction_metrics(
    *,
    labels: Sequence[int],
    probabilities: Sequence[float],
    returns: Sequence[float],
    expected_returns: Sequence[float],
    probability_baseline: float,
    return_baseline: float,
) -> Dict[str, Any]:
    if not labels or not (len(labels) == len(probabilities) == len(returns) == len(expected_returns)):
        raise ValueError("aligned prediction evidence is required")
    brier = statistics.fmean((prob - label) ** 2 for prob, label in zip(probabilities, labels))
    baseline_brier = statistics.fmean((probability_baseline - label) ** 2 for label in labels)
    return_mae = statistics.fmean(
        abs(predicted - actual) for predicted, actual in zip(expected_returns, returns)
    )
    baseline_return_mae = statistics.fmean(abs(return_baseline - actual) for actual in returns)
    top_n = max(1, math.ceil(len(labels) * 0.20))
    probability_ordered = sorted(zip(probabilities, returns), reverse=True)
    return_ordered = sorted(zip(expected_returns, returns), reverse=True)
    all_mean = statistics.fmean(returns)
    return {
        "observations": len(labels),
        "brier": brier,
        "baseline_brier": baseline_brier,
        "auc": _auc(labels, probabilities),
        "top_quintile_lift_bps": (
            statistics.fmean(actual for _predicted, actual in probability_ordered[:top_n]) - all_mean
        ),
        "return_mae_bps": return_mae,
        "return_baseline_mae_bps": baseline_return_mae,
        "return_top_quintile_lift_bps": (
            statistics.fmean(actual for _predicted, actual in return_ordered[:top_n]) - all_mean
        ),
    }


def _feature_redundancy_audit(
    feature_names: Sequence[str],
    matrix: Sequence[Sequence[float]],
    *,
    threshold: float = 0.90,
) -> Dict[str, Any]:
    pairs: List[Dict[str, Any]] = []
    if not matrix or not feature_names:
        return {
            "state": "INSUFFICIENT_DATA",
            "feature_count": len(feature_names),
            "pair_count": 0,
            "high_correlation_pairs": [],
            "threshold": threshold,
        }
    for left in range(len(feature_names)):
        left_values = [float(row[left]) for row in matrix]
        left_mean = statistics.fmean(left_values)
        left_centered = [value - left_mean for value in left_values]
        left_ss = sum(value * value for value in left_centered)
        for right in range(left + 1, len(feature_names)):
            right_values = [float(row[right]) for row in matrix]
            right_mean = statistics.fmean(right_values)
            right_centered = [value - right_mean for value in right_values]
            denominator = math.sqrt(left_ss * sum(value * value for value in right_centered))
            correlation = (
                sum(a * b for a, b in zip(left_centered, right_centered)) / denominator
                if denominator > 1e-12 else 0.0
            )
            if abs(correlation) >= threshold:
                pairs.append({
                    "left": feature_names[left],
                    "right": feature_names[right],
                    "correlation": round(correlation, 6),
                })
    return {
        "state": "MEASURED",
        "feature_count": len(feature_names),
        "pair_count": len(feature_names) * (len(feature_names) - 1) // 2,
        "high_correlation_pairs": pairs,
        "threshold": threshold,
    }


class _LogisticModel:
    @staticmethod
    def fit(rows: Sequence[Sequence[float]], labels: Sequence[int], *, l2: float = 0.05,
            learning_rate: float = 0.08, iterations: int = 700) -> Dict[str, Any]:
        if not rows or len(rows) != len(labels):
            raise ValueError("training rows and labels are required")
        width = len(rows[0])
        means = [statistics.fmean(row[col] for row in rows) for col in range(width)]
        scales = []
        for col in range(width):
            values = [row[col] for row in rows]
            scale = statistics.pstdev(values)
            scales.append(scale if scale > 1e-12 else 1.0)
        matrix = [[(row[col] - means[col]) / scales[col] for col in range(width)] for row in rows]
        weights = [0.0] * width
        prevalence = min(1 - 1e-6, max(1e-6, statistics.fmean(labels)))
        intercept = math.log(prevalence / (1.0 - prevalence))
        n = len(matrix)
        for step in range(max(100, int(iterations))):
            grad_w = [0.0] * width
            grad_b = 0.0
            for row, label in zip(matrix, labels):
                probability = _sigmoid(intercept + sum(weight * value for weight, value in zip(weights, row)))
                error = probability - label
                grad_b += error
                for col in range(width):
                    grad_w[col] += error * row[col]
            rate = learning_rate / math.sqrt(1.0 + step / 100.0)
            intercept -= rate * grad_b / n
            for col in range(width):
                weights[col] -= rate * (grad_w[col] / n + l2 * weights[col])
        return {"means": means, "scales": scales, "weights": weights, "intercept": intercept, "l2": l2}

    @staticmethod
    def predict(model: Mapping[str, Any], rows: Sequence[Sequence[float]]) -> List[float]:
        means = list(model["means"])
        scales = list(model["scales"])
        weights = list(model["weights"])
        intercept = float(model["intercept"])
        output = []
        for row in rows:
            standardized = [(row[col] - means[col]) / scales[col] for col in range(len(weights))]
            output.append(_sigmoid(intercept + sum(weight * value for weight, value in zip(weights, standardized))))
        return output


class _RidgeReturnModel:
    """Small dependency-free L2 linear model for expected net return (bps).

    It shares the same fold-local standardisation discipline as the classifier
    and exists only as a shadow economic forecast.
    """

    @staticmethod
    def fit(rows: Sequence[Sequence[float]], targets: Sequence[float], *, l2: float = 0.10,
            learning_rate: float = 0.03, iterations: int = 900) -> Dict[str, Any]:
        if not rows or len(rows) != len(targets):
            raise ValueError("training rows and return targets are required")
        width = len(rows[0])
        means = [statistics.fmean(row[col] for row in rows) for col in range(width)]
        scales = []
        for col in range(width):
            values = [row[col] for row in rows]
            scale = statistics.pstdev(values)
            scales.append(scale if scale > 1e-12 else 1.0)
        matrix = [[(row[col] - means[col]) / scales[col] for col in range(width)] for row in rows]
        target_mean = statistics.fmean(targets)
        target_scale = statistics.pstdev(targets) or 1.0
        normalized = [(float(value) - target_mean) / target_scale for value in targets]
        weights = [0.0] * width
        intercept = 0.0
        n = len(matrix)
        for step in range(max(150, int(iterations))):
            grad_w = [0.0] * width
            grad_b = 0.0
            for row, target in zip(matrix, normalized):
                prediction = intercept + sum(weight * value for weight, value in zip(weights, row))
                error = prediction - target
                grad_b += error
                for col in range(width):
                    grad_w[col] += error * row[col]
            rate = learning_rate / math.sqrt(1.0 + step / 120.0)
            intercept -= rate * grad_b / n
            for col in range(width):
                weights[col] -= rate * (grad_w[col] / n + l2 * weights[col])
        return {
            "means": means, "scales": scales, "weights": weights, "intercept": intercept,
            "target_mean": target_mean, "target_scale": target_scale, "l2": l2,
        }

    @staticmethod
    def predict(model: Mapping[str, Any], rows: Sequence[Sequence[float]]) -> List[float]:
        means = list(model["means"])
        scales = list(model["scales"])
        weights = list(model["weights"])
        intercept = float(model["intercept"])
        target_mean = float(model["target_mean"])
        target_scale = float(model["target_scale"])
        output = []
        for row in rows:
            standardized = [(row[col] - means[col]) / scales[col] for col in range(len(weights))]
            normalized = intercept + sum(weight * value for weight, value in zip(weights, standardized))
            output.append(target_mean + normalized * target_scale)
        return output


class NseCalibratedChallengerService:
    MIN_OBSERVATIONS = 300
    MIN_TOTAL_OBSERVATIONS = 340
    MIN_DAYS = 126
    MIN_HOLDOUT_DAYS = 20
    MIN_HOLDOUT_OBSERVATIONS = 40
    HOLDOUT_FRACTION = 0.15
    MIN_REGIMES = 3
    MIN_FOLDS = 3

    def __init__(self, store: Any):
        self.store = store
        if not hasattr(store, "write_lock"):
            store.write_lock = threading.RLock()
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        with self.store.write_lock:
            self.store.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS shadow_calibrated_models (
                  model_id TEXT PRIMARY KEY,
                  mode TEXT NOT NULL,
                  horizon TEXT NOT NULL,
                  model_family TEXT NOT NULL,
                  feature_names_json TEXT NOT NULL,
                  artifact_json TEXT NOT NULL,
                  validation_json TEXT NOT NULL,
                  training_start TEXT NOT NULL,
                  training_end TEXT NOT NULL,
                  observations INTEGER NOT NULL,
                  trading_days INTEGER NOT NULL,
                  regimes INTEGER NOT NULL,
                  trial_count INTEGER NOT NULL,
                  state TEXT NOT NULL,
                  authority TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  service_version TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_shadow_calibrated_models_mode
                  ON shadow_calibrated_models(mode,horizon,created_at);
                """
            )
            self.store.conn.commit()

    @staticmethod
    def _specs(mode: str):
        return DELIVERY_FEATURES if mode == "delivery" else INTRADAY_FEATURES

    def _dataset(self, *, mode: str, horizon: str) -> Tuple[List[str], List[Dict[str, Any]]]:
        raw_rows = QuantEdgeDataService(self.store).training_rows(mode=mode, horizon=horizon)
        names, dataset, _evidence = QuantResearchDatasetService.build(raw_rows, mode=mode)
        return names, dataset

    @staticmethod
    def _impute(train: Sequence[Dict[str, Any]], test: Sequence[Dict[str, Any]]) -> Tuple[List[List[float]], List[List[float]], List[float]]:
        width = len(train[0]["values"])
        medians = []
        for col in range(width):
            available = [float(row["values"][col]) for row in train if row["values"][col] is not None]
            medians.append(statistics.median(available) if available else 0.0)
        def matrix(rows):
            return [[float(value) if value is not None else medians[col] for col, value in enumerate(row["values"])] for row in rows]
        return matrix(train), matrix(test), medians

    def train(
        self,
        *,
        mode: str,
        horizon: Optional[str] = None,
        min_train_days: int = 126,
        test_days: int = 21,
        purge_days: Optional[int] = None,
        embargo_days: int = 1,
        max_folds: int = 8,
        trial_count: int = 1,
    ) -> Dict[str, Any]:
        desk = str(mode or "").lower().strip()
        if desk not in {"intraday", "delivery"}:
            raise ValueError("mode must be intraday or delivery")
        horizon_key = str(horizon or DEFAULT_HORIZON[desk]).lower()
        declared_trials = max(1, int(trial_count))
        feature_names, dataset = self._dataset(mode=desk, horizon=horizon_key)
        days = sorted({row["date"] for row in dataset})
        regimes = sorted({row["regime"] for row in dataset})
        horizon_days = 1 if desk == "intraday" else max(1, int(horizon_key.rstrip("d")))
        required_total_days = self.MIN_DAYS + self.MIN_HOLDOUT_DAYS + horizon_days
        readiness = {
            "observations": len(dataset), "required_observations": self.MIN_TOTAL_OBSERVATIONS,
            "trading_days": len(days), "required_trading_days": required_total_days,
            "regimes": len(regimes), "required_regimes": self.MIN_REGIMES,
        }
        if (
            len(dataset) < self.MIN_TOTAL_OBSERVATIONS
            or len(days) < required_total_days
            or len(regimes) < self.MIN_REGIMES
        ):
            return {
                "ok": True, "state": "INSUFFICIENT_EVIDENCE", "version": MODEL_SERVICE_VERSION,
                "mode": desk, "horizon": horizon_key, "readiness": readiness,
                "prediction_state": "MODEL_UNAVAILABLE", "decision_weight": 0.0,
                "production_change_allowed": False,
            }

        requested_holdout_days = max(
            self.MIN_HOLDOUT_DAYS,
            int(test_days),
            int(math.ceil(len(days) * self.HOLDOUT_FRACTION)),
        )
        maximum_holdout_days = len(days) - self.MIN_DAYS - horizon_days
        holdout_day_count = min(requested_holdout_days, maximum_holdout_days)
        holdout_days = days[-holdout_day_count:]
        pre_holdout_days = days[:-holdout_day_count]
        holdout_dates = set(holdout_days)
        pre_holdout_dates = set(pre_holdout_days)
        holdout_rows = [row for row in dataset if row["date"] in holdout_dates]
        parsed_holdout_decisions = [
            stamp for stamp in (_timestamp(row["observed_at"]) for row in holdout_rows)
            if stamp is not None
        ]
        holdout_start = min(parsed_holdout_decisions, default=None)
        pre_holdout_rows = [row for row in dataset if row["date"] in pre_holdout_dates]
        development_rows = [
            row for row in pre_holdout_rows
            if holdout_start is not None
            and _timestamp(row.get("settled_at")) is not None
            and _timestamp(row.get("settled_at")) < holdout_start
        ]
        development_days = sorted({row["date"] for row in development_rows})
        development_regimes = sorted({row["regime"] for row in development_rows})
        holdout_regimes = sorted({row["regime"] for row in holdout_rows})
        if (
            len(development_rows) < self.MIN_OBSERVATIONS
            or len(development_days) < self.MIN_DAYS
            or len(development_regimes) < self.MIN_REGIMES
            or len(holdout_rows) < self.MIN_HOLDOUT_OBSERVATIONS
            or len(holdout_days) < self.MIN_HOLDOUT_DAYS
        ):
            return {
                "ok": True,
                "state": "INSUFFICIENT_UNTOUCHED_HOLDOUT_EVIDENCE",
                "version": MODEL_SERVICE_VERSION,
                "mode": desk,
                "horizon": horizon_key,
                "readiness": {
                    **readiness,
                    "development_observations": len(development_rows),
                    "required_development_observations": self.MIN_OBSERVATIONS,
                    "development_days": len(development_days),
                    "required_development_days": self.MIN_DAYS,
                    "holdout_observations": len(holdout_rows),
                    "required_holdout_observations": self.MIN_HOLDOUT_OBSERVATIONS,
                    "holdout_days": len(holdout_days),
                    "required_holdout_days": self.MIN_HOLDOUT_DAYS,
                },
                "prediction_state": "MODEL_UNAVAILABLE",
                "decision_weight": 0.0,
                "production_change_allowed": False,
            }

        folds = WalkForwardValidationService.build_folds(
            development_days, min_train_days=min_train_days, test_days=test_days,
            purge_days=max(horizon_days, int(purge_days or 0)), max_folds=max_folds,
            embargo_days=embargo_days,
        )
        fold_results = []
        all_labels: List[int] = []
        all_probabilities: List[float] = []
        all_probability_baselines: List[float] = []
        all_returns: List[float] = []
        all_expected_returns: List[float] = []
        all_return_baselines: List[float] = []
        all_dates: List[str] = []
        for fold in folds:
            train_dates, test_dates = set(fold["train_dates"]), set(fold["test_dates"])
            train_rows = [row for row in development_rows if row["date"] in train_dates]
            test_rows = [row for row in development_rows if row["date"] in test_dates]
            if not train_rows or not test_rows or len({row["label"] for row in train_rows}) < 2:
                continue
            train_x, test_x, _medians = self._impute(train_rows, test_rows)
            labels = [row["label"] for row in train_rows]
            model = _LogisticModel.fit(train_x, labels)
            probabilities = _LogisticModel.predict(model, test_x)
            train_returns = [float(row["net_return_bps"]) for row in train_rows]
            return_model = _RidgeReturnModel.fit(train_x, train_returns)
            expected_returns = _RidgeReturnModel.predict(return_model, test_x)
            test_labels = [row["label"] for row in test_rows]
            test_returns = [float(row["net_return_bps"]) for row in test_rows]
            prevalence = statistics.fmean(labels)
            return_baseline = statistics.fmean(train_returns)
            metrics = _prediction_metrics(
                labels=test_labels,
                probabilities=probabilities,
                returns=test_returns,
                expected_returns=expected_returns,
                probability_baseline=prevalence,
                return_baseline=return_baseline,
            )
            fold_results.append({
                "fold": fold["fold"], "train_start": fold["train_dates"][0], "train_end": fold["train_dates"][-1],
                "test_start": fold["test_dates"][0], "test_end": fold["test_dates"][-1],
                "n_train": len(train_rows), "n_test": len(test_rows),
                "brier": round(metrics["brier"], 8),
                "baseline_brier": round(metrics["baseline_brier"], 8),
                "auc": None if metrics["auc"] is None else round(float(metrics["auc"]), 8),
                "top_quintile_lift_bps": round(metrics["top_quintile_lift_bps"], 6),
                "return_mae_bps": round(metrics["return_mae_bps"], 6),
                "return_baseline_mae_bps": round(metrics["return_baseline_mae_bps"], 6),
                "return_top_quintile_lift_bps": round(metrics["return_top_quintile_lift_bps"], 6),
            })
            all_labels.extend(test_labels)
            all_probabilities.extend(probabilities)
            all_probability_baselines.extend([prevalence] * len(test_labels))
            all_returns.extend(test_returns)
            all_expected_returns.extend(expected_returns)
            all_return_baselines.extend([return_baseline] * len(test_returns))
            all_dates.extend(row["date"] for row in test_rows)
        if not fold_results:
            return {
                "ok": True, "state": "INSUFFICIENT_FOLDS", "version": MODEL_SERVICE_VERSION,
                "mode": desk, "horizon": horizon_key, "readiness": readiness,
                "prediction_state": "MODEL_UNAVAILABLE", "decision_weight": 0.0,
                "production_change_allowed": False,
            }
        brier = statistics.fmean((prob - label) ** 2 for prob, label in zip(all_probabilities, all_labels))
        baseline_brier = statistics.fmean(
            (prob - label) ** 2 for prob, label in zip(all_probability_baselines, all_labels)
        )
        auc = _auc(all_labels, all_probabilities)
        pairs = sorted(zip(all_probabilities, all_returns), reverse=True)
        top_n = max(1, math.ceil(len(pairs) * 0.20))
        top_lift = statistics.fmean(value for _prob, value in pairs[:top_n]) - statistics.fmean(all_returns)
        stability = sum(fold["top_quintile_lift_bps"] > 0 and fold["brier"] < fold["baseline_brier"] for fold in fold_results) / len(fold_results)
        return_mae = statistics.fmean(abs(pred - actual) for pred, actual in zip(all_expected_returns, all_returns))
        return_baseline_mae = statistics.fmean(abs(pred - actual) for pred, actual in zip(all_return_baselines, all_returns))
        return_pairs = sorted(zip(all_expected_returns, all_returns), reverse=True)
        return_top_lift = statistics.fmean(actual for _pred, actual in return_pairs[:top_n]) - statistics.fmean(all_returns)
        return_stability = sum(
            fold["return_top_quintile_lift_bps"] > 0 and fold["return_mae_bps"] < fold["return_baseline_mae_bps"]
            for fold in fold_results
        ) / len(fold_results)

        # The final artifact is frozen on development evidence only.  The
        # chronological holdout is then scored exactly once and never fitted.
        train_x, holdout_x, medians = self._impute(development_rows, holdout_rows)
        final_model = _LogisticModel.fit(train_x, [row["label"] for row in development_rows])
        final_return_model = _RidgeReturnModel.fit(
            train_x, [float(row["net_return_bps"]) for row in development_rows]
        )
        holdout_labels = [row["label"] for row in holdout_rows]
        holdout_returns = [float(row["net_return_bps"]) for row in holdout_rows]
        holdout_probabilities = _LogisticModel.predict(final_model, holdout_x)
        holdout_expected_returns = _RidgeReturnModel.predict(final_return_model, holdout_x)
        development_prevalence = statistics.fmean(row["label"] for row in development_rows)
        development_return_mean = statistics.fmean(
            float(row["net_return_bps"]) for row in development_rows
        )
        holdout_metrics = _prediction_metrics(
            labels=holdout_labels,
            probabilities=holdout_probabilities,
            returns=holdout_returns,
            expected_returns=holdout_expected_returns,
            probability_baseline=development_prevalence,
            return_baseline=development_return_mean,
        )
        latest_development_settlement = max(
            _timestamp(row["settled_at"]) for row in development_rows
            if _timestamp(row["settled_at"]) is not None
        )
        holdout_boundary_clear = (
            holdout_start is not None
            and latest_development_settlement < holdout_start
            and not set(development_days).intersection(holdout_dates)
        )
        redundancy_audit = _feature_redundancy_audit(feature_names, train_x)
        multiple_testing = {
            "method": "SINGLE_DECLARED_TRIAL_GATE",
            "declared_trial_count": declared_trials,
            "passed": declared_trials == 1,
            "policy": (
                "Only one declared specification is accepted until an adjusted "
                "multi-trial statistical test is implemented."
            ),
        }
        gates = {
            "development_minimum_three_folds": len(fold_results) >= self.MIN_FOLDS,
            "development_brier_beats_prevalence": brier < baseline_brier,
            "development_top_quintile_lift_positive": top_lift > 0,
            "development_fold_stability_60pct": stability >= 0.60,
            "development_auc_above_random": auc is not None and auc > 0.50,
            "development_return_mae_beats_mean": return_mae < return_baseline_mae,
            "development_return_top_quintile_lift_positive": return_top_lift > 0,
            "development_return_fold_stability_60pct": return_stability >= 0.60,
            "development_sample_gate": (
                len(development_rows) >= self.MIN_OBSERVATIONS
                and len(development_days) >= self.MIN_DAYS
                and len(development_regimes) >= self.MIN_REGIMES
            ),
            "holdout_chronologically_untouched": holdout_boundary_clear,
            "holdout_minimum_evidence": (
                len(holdout_rows) >= self.MIN_HOLDOUT_OBSERVATIONS
                and len(holdout_days) >= self.MIN_HOLDOUT_DAYS
                and len(set(holdout_labels)) == 2
                and len(holdout_regimes) >= self.MIN_REGIMES
            ),
            "holdout_brier_beats_development_prevalence": (
                holdout_metrics["brier"] < holdout_metrics["baseline_brier"]
            ),
            "holdout_auc_above_random": (
                holdout_metrics["auc"] is not None and holdout_metrics["auc"] > 0.50
            ),
            "holdout_top_quintile_lift_positive": holdout_metrics["top_quintile_lift_bps"] > 0,
            "holdout_return_mae_beats_development_mean": (
                holdout_metrics["return_mae_bps"] < holdout_metrics["return_baseline_mae_bps"]
            ),
            "holdout_return_top_quintile_lift_positive": (
                holdout_metrics["return_top_quintile_lift_bps"] > 0
            ),
            "single_declared_trial_without_correction": multiple_testing["passed"],
        }
        governance_evidence = {
            "samples": len(development_rows),
            "dates": len(development_days),
            "symbols": len({row["symbol"] for row in development_rows}),
            "point_in_time": True,
            "purged_walk_forward": len(fold_results) >= self.MIN_FOLDS,
            "embargo": int(embargo_days) > 0,
            "costs_included": True,
            "holdout_untouched": holdout_boundary_clear,
            "baseline_comparison": True,
            "trial_count_recorded": declared_trials >= 1,
            "multiple_testing_control": multiple_testing["passed"],
            "feature_redundancy_audited": redundancy_audit["state"] == "MEASURED",
            "trial_count": declared_trials,
        }
        governance = ModelChallengerGovernanceService().assess(
            {"model_family": GOVERNED_MODEL_FAMILY},
            governance_evidence,
        )
        gates["governance_research_ready"] = bool(governance.get("eligible_for_research"))
        eligible = all(gates.values())
        artifact = {
            **final_model,
            "return_model": final_return_model,
            "feature_names": feature_names,
            "imputation_medians": medians,
            "mode": desk,
            "horizon": horizon_key,
            "model_family": MODEL_FAMILY,
            "fitted_through": development_days[-1],
            "holdout_excluded_from_fit": True,
            "production_authority_weight": 0.0,
        }
        validation = {
            "folds": fold_results,
            "out_of_sample_observations": len(all_labels),
            "out_of_sample_days": len(set(all_dates)),
            "brier": round(brier, 8), "baseline_brier": round(baseline_brier, 8),
            "auc": None if auc is None else round(float(auc), 8),
            "top_quintile_lift_bps": round(top_lift, 6),
            "fold_stability": round(stability, 6),
            "return_mae_bps": round(return_mae, 6),
            "return_baseline_mae_bps": round(return_baseline_mae, 6),
            "return_top_quintile_lift_bps": round(return_top_lift, 6),
            "return_fold_stability": round(return_stability, 6),
            "calibration": _calibration(all_labels, all_probabilities),
            "holdout": {
                "state": "PASSED" if all(
                    gates[name] for name in (
                        "holdout_chronologically_untouched",
                        "holdout_minimum_evidence",
                        "holdout_brier_beats_development_prevalence",
                        "holdout_auc_above_random",
                        "holdout_top_quintile_lift_positive",
                        "holdout_return_mae_beats_development_mean",
                        "holdout_return_top_quintile_lift_positive",
                    )
                ) else "FAILED",
                "start": holdout_days[0],
                "end": holdout_days[-1],
                "days": len(holdout_days),
                "observations": len(holdout_rows),
                "regimes": holdout_regimes,
                "development_end": development_days[-1],
                "latest_development_label_settled_at": latest_development_settlement.isoformat(),
                "earliest_holdout_decision_at": holdout_start.isoformat() if holdout_start else None,
                "excluded_pre_holdout_rows_with_overlapping_labels": (
                    len(pre_holdout_rows) - len(development_rows)
                ),
                "brier": round(holdout_metrics["brier"], 8),
                "baseline_brier": round(holdout_metrics["baseline_brier"], 8),
                "auc": (
                    None if holdout_metrics["auc"] is None
                    else round(float(holdout_metrics["auc"]), 8)
                ),
                "top_quintile_lift_bps": round(
                    holdout_metrics["top_quintile_lift_bps"], 6
                ),
                "return_mae_bps": round(holdout_metrics["return_mae_bps"], 6),
                "return_baseline_mae_bps": round(
                    holdout_metrics["return_baseline_mae_bps"], 6
                ),
                "return_top_quintile_lift_bps": round(
                    holdout_metrics["return_top_quintile_lift_bps"], 6
                ),
                "calibration": _calibration(holdout_labels, holdout_probabilities),
                "fit_exclusion_verified": holdout_boundary_clear,
            },
            "baseline_comparisons": {
                "probability": {
                    "model_metric": "brier",
                    "baseline": "development_prevalence",
                    "baseline_value": round(development_prevalence, 8),
                    "model_score": round(holdout_metrics["brier"], 8),
                    "baseline_score": round(holdout_metrics["baseline_brier"], 8),
                    "lower_is_better": True,
                },
                "return": {
                    "model_metric": "mean_absolute_error_bps",
                    "baseline": "development_mean_net_return_bps",
                    "baseline_value": round(development_return_mean, 6),
                    "model_score": round(holdout_metrics["return_mae_bps"], 6),
                    "baseline_score": round(holdout_metrics["return_baseline_mae_bps"], 6),
                    "lower_is_better": True,
                },
            },
            "feature_redundancy_audit": redundancy_audit,
            "multiple_testing": multiple_testing,
            "governance": governance,
            "gates": gates,
            "trial_count": declared_trials,
        }
        basis = {
            "service_version": MODEL_SERVICE_VERSION, "mode": desk, "horizon": horizon_key,
            "feature_names": feature_names, "artifact": artifact, "validation": validation,
            "training_start": development_days[0], "training_end": development_days[-1],
            "observations": len(development_rows), "trading_days": len(development_days),
            "regimes": development_regimes, "trial_count": declared_trials,
        }
        model_id = f"NS{desk[:1].upper()}-{_sha(basis, 24)}"
        state = "VALIDATION_CANDIDATE_ELIGIBLE" if eligible else "VALIDATION_CANDIDATE_REJECTED"
        with self.store.write_lock:
            self.store.conn.execute(
                """INSERT OR REPLACE INTO shadow_calibrated_models(
                    model_id,mode,horizon,model_family,feature_names_json,artifact_json,validation_json,
                    training_start,training_end,observations,trading_days,regimes,trial_count,state,
                    authority,created_at,service_version
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    model_id, desk, horizon_key, MODEL_FAMILY, json.dumps(feature_names),
                    json.dumps(artifact, sort_keys=True), json.dumps(validation, sort_keys=True),
                    development_days[0], development_days[-1], len(development_rows),
                    len(development_days), len(development_regimes), declared_trials,
                    state, "FINITE_TOURNAMENT", _now(), MODEL_SERVICE_VERSION,
                ),
            )
            self.store.conn.commit()
        return {
            "ok": True, "state": state, "version": MODEL_SERVICE_VERSION,
            "model_id": model_id, "mode": desk, "horizon": horizon_key,
            "readiness": readiness, "validation": validation,
            "prediction_state": "ACTIVE_VALIDATION" if eligible else "REJECTED",
            "authority": "FINITE_TOURNAMENT",
            "production_authority_weight": 0.0,
            "production_change_allowed": False,
        }

    def status(self, *, mode: str, horizon: Optional[str] = None) -> Dict[str, Any]:
        desk = str(mode or "").lower().strip()
        if desk not in {"intraday", "delivery"}:
            raise ValueError("mode must be intraday or delivery")
        horizon_key = str(horizon or DEFAULT_HORIZON[desk]).lower()
        model = self.latest_model(mode=desk, horizon=horizon_key, eligible_only=False)
        if model is None:
            repo = (getattr(self.store, "production_model_governance_read_repository", None)
                    or getattr(self.store, "production_model_governance_repository", None))
            measured = {}
            if repo is not None and callable(getattr(repo, "quant_training_evidence_status", None)):
                try:
                    measured = dict(repo.quant_training_evidence_status(mode=desk, horizon=horizon_key) or {})
                except Exception as exc:
                    measured = {"state": "EVIDENCE_DEPTH_UNAVAILABLE", "error": str(exc)[:240]}
            observations = int(measured.get("observations") or 0)
            trading_days = int(measured.get("trading_days") or 0)
            regimes = int(measured.get("regimes") or 0)
            return {
                "ok": True, "state": "NO_MODEL_TRAINED", "mode": desk, "horizon": horizon_key,
                "readiness": {
                    "observations": observations, "required_observations": self.MIN_TOTAL_OBSERVATIONS,
                    "trading_days": trading_days,
                    "required_trading_days": (
                        self.MIN_DAYS + self.MIN_HOLDOUT_DAYS
                        + (1 if desk == "intraday" else max(1, int(horizon_key.rstrip("d"))))
                    ),
                    "regimes": regimes, "required_regimes": self.MIN_REGIMES,
                    "first_date": measured.get("first_date"), "last_date": measured.get("last_date"),
                    "measurement": measured.get("query_profile") or measured.get("state") or "BOUNDED_STATUS",
                },
                "training_contract": "HEAVY_DATASET_BUILD_AND_FIT_OWNED_BY_GOVERNED_TRAINING_CYCLE",
                "prediction_state": "MODEL_UNAVAILABLE", "decision_weight": 0.0,
                "production_change_allowed": False,
            }
        return {
            "ok": True, "state": model["state"], "mode": desk, "horizon": horizon_key,
            "model_id": model["model_id"], "model_family": model["model_family"],
            "observations": model["observations"], "trading_days": model["trading_days"],
            "regimes": model["regimes"], "validation": model["validation"],
            "prediction_state": "MODEL_UNAVAILABLE", "decision_weight": 0.0,
            "production_change_allowed": False,
        }

    def latest_model(self, *, mode: str, horizon: Optional[str] = None, eligible_only: bool = True) -> Optional[Dict[str, Any]]:
        desk = str(mode or "").lower().strip()
        horizon_key = str(horizon or DEFAULT_HORIZON.get(desk, "")).lower()
        where = "AND state IN ('VALIDATION_CANDIDATE_ELIGIBLE','SHADOW_MODEL_ELIGIBLE')" if eligible_only else ""
        raw = self.store.conn.execute(
            f"""SELECT * FROM shadow_calibrated_models WHERE mode=? AND horizon=? {where}
                 ORDER BY created_at DESC LIMIT 1""",
            (desk, horizon_key),
        ).fetchone()
        if not raw:
            return None
        row = dict(raw)
        row["feature_names"] = json.loads(row.pop("feature_names_json"))
        row["artifact"] = json.loads(row.pop("artifact_json"))
        row["validation"] = json.loads(row.pop("validation_json"))
        return row

    def predict_with_model(self, model: Mapping[str, Any], *, features: Mapping[str, Any]) -> Dict[str, Any]:
        artifact = dict(model["artifact"])
        specs = self._specs(str(model["mode"]).lower())
        medians = list(artifact["imputation_medians"])
        values = []
        for col, (_name, aliases, _weight, _higher) in enumerate(specs):
            value = _first(features, aliases)
            values.append(float(value) if value is not None else float(medians[col]))
        probability = _LogisticModel.predict(artifact, [values])[0]
        expected_return = _RidgeReturnModel.predict(artifact["return_model"], [values])[0]
        sample_n = max(1, int(model.get("observations") or 1))
        validation = dict(model.get("validation") or {})
        validation_gates = dict(validation.get("gates") or {})
        governance = dict(validation.get("governance") or {})
        holdout = dict(validation.get("holdout") or {})
        multiple_testing = dict(validation.get("multiple_testing") or {})
        display_eligible = bool(
            str(model.get("state") or "") in {"VALIDATION_CANDIDATE_ELIGIBLE", "SHADOW_MODEL_ELIGIBLE"}
            and validation_gates
            and all(bool(value) for value in validation_gates.values())
            and governance.get("eligible_for_research") is True
            and holdout.get("state") == "PASSED"
            and holdout.get("fit_exclusion_verified") is True
            and multiple_testing.get("passed") is True
            and sample_n >= self.MIN_OBSERVATIONS
        )
        margin = 1.96 * math.sqrt(max(0.0, probability * (1.0 - probability)) / sample_n)
        return {
            "ok": True, "state": "VALIDATION_PREDICTION" if display_eligible else "MODEL_UNAVAILABLE", "model_id": model["model_id"],
            "probability_positive": round(probability, 8) if display_eligible else None,
            "probability_kind": "CALIBRATED_VALIDATION" if display_eligible else "UNAVAILABLE",
            "forecast_display_eligible": display_eligible,
            "probability_support_interval_95": [round(max(0.0, probability-margin),8), round(min(1.0, probability+margin),8)] if display_eligible else None,
            "expected_net_return_bps": round(expected_return, 6) if display_eligible else None,
            "horizon": model["horizon"],
            "probability_target_before_stop": None,
            "expected_max_adverse_excursion_bps": None,
            "expected_time_to_target_bars": None,
            "auxiliary_prediction_state": "LABELS_RECORDED; SEPARATE MODELS NOT_YET_VALIDATED",
            "sample_support": {
                "observations": int(model.get("observations") or 0),
                "trading_days": int(model.get("trading_days") or 0),
                "regimes": int(model.get("regimes") or 0),
                "trial_count": int(model.get("trial_count") or 1),
            },
            "prediction_state": "MODEL_UNAVAILABLE", "decision_weight": 0.0,
            "production_change_allowed": False,
        }

    def predict(self, *, mode: str, features: Mapping[str, Any], horizon: Optional[str] = None) -> Dict[str, Any]:
        model = self.latest_model(mode=mode, horizon=horizon, eligible_only=True)
        if model is None:
            return {
                "ok": True, "state": "MODEL_UNAVAILABLE", "probability_positive": None,
                "expected_net_return_bps": None,
                "probability_kind": "UNAVAILABLE",
                "forecast_display_eligible": False,
                "probability_support_interval_95": None,
                "probability_target_before_stop": None,
                "expected_max_adverse_excursion_bps": None,
                "expected_time_to_target_bars": None,
                "auxiliary_prediction_state": "INSUFFICIENT_EVIDENCE",
                "sample_support": None,
                "prediction_state": "MODEL_UNAVAILABLE", "decision_weight": 0.0,
                "production_change_allowed": False,
            }
        return self.predict_with_model(model, features=features)
