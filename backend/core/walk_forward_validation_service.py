"""Purged walk-forward and quantitative model-risk validation authority.

Precomputed observations are signal evidence only.  Fold-local model validation
requires an immutable model artifact produced for each fold, while investable
drawdown requires a separate capital- and concurrency-constrained simulator.
Two explicit profiles are available:

* ``research`` preserves the lightweight historical-evidence contract.
* ``capital`` adds execution-cost coverage, lineage, look-ahead, universe,
  drawdown, bootstrap, multiple-testing and deflated-Sharpe gates.  Passing it
  means *backtest approved for shadow observation*, never live-capital approval.
"""
from __future__ import annotations

import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import random
import statistics
from statistics import NormalDist
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


from core.expectancy_semantics_authority import lane as expectancy_lane
from core.temporal_leakage_authority import DEFAULT_TEMPORAL_LEAKAGE_AUTHORITY
AUTHORITY_VERSION = "walk-forward-authority-2.5.0-oof-lineage-capital"
RESEARCH_PROFILE = "research"
CAPITAL_PROFILE = "capital"
PRECOMPUTED_SIGNAL_OBSERVATION_VALIDATION = "PRECOMPUTED_SIGNAL_OBSERVATION_VALIDATION"
FOLD_LOCAL_MODEL_VALIDATION = "FOLD_LOCAL_MODEL_VALIDATION"
FROZEN_OOF_MODEL_VALIDATION = "FROZEN_OOF_MODEL_VALIDATION"
PROSPECTIVE_IMMUTABLE_PREDICTION_VALIDATION = "PROSPECTIVE_IMMUTABLE_PREDICTION_VALIDATION"

_OUTCOME_FIELDS = frozenset({
    "forward_return", "benchmark_return", "cost_return", "outcome_as_of",
    "outcome", "label", "target", "realized_return", "net_return",
})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _f(value: Any) -> Optional[float]:
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def _parse(value: Any) -> Optional[datetime]:
    if value in (None, "", "—"):
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        stamp = datetime.fromisoformat(text)
        return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)
    except ValueError:
        try:
            return datetime.fromisoformat(text[:10]).replace(tzinfo=timezone.utc)
        except ValueError:
            return None


def _max_drawdown(returns: Sequence[float]) -> float:
    equity = peak = 1.0
    worst = 0.0
    for ret in returns:
        equity *= max(0.0, 1.0 + ret)
        peak = max(peak, equity)
        if peak > 0:
            worst = min(worst, equity / peak - 1.0)
    return worst


def _safe_std(values: Sequence[float]) -> float:
    return statistics.stdev(values) if len(values) >= 2 else 0.0


def _skew(values: Sequence[float]) -> float:
    if len(values) < 3:
        return 0.0
    mean = statistics.fmean(values)
    std = _safe_std(values)
    if std <= 0:
        return 0.0
    n = len(values)
    return (n / ((n - 1) * (n - 2))) * sum(((x - mean) / std) ** 3 for x in values)


def _kurtosis(values: Sequence[float]) -> float:
    if len(values) < 4:
        return 3.0
    mean = statistics.fmean(values)
    std = _safe_std(values)
    if std <= 0:
        return 3.0
    n = len(values)
    z4 = sum(((x - mean) / std) ** 4 for x in values)
    # Unbiased Pearson kurtosis.
    return ((n * (n + 1) / ((n - 1) * (n - 2) * (n - 3))) * z4
            - (3 * (n - 1) ** 2 / ((n - 2) * (n - 3)))) + 3.0


def _aggregate_daily(rows: Sequence[Dict[str, Any]]) -> Tuple[List[float], List[float]]:
    """Equal-weight same-day signal observations; not an investable portfolio."""
    by_day: Dict[str, List[Tuple[float, float]]] = {}
    for row in rows:
        gross = _f(row.get("forward_return"))
        if gross is None:
            continue
        cost = _f(row.get("cost_return")) or 0.0
        benchmark = _f(row.get("benchmark_return")) or 0.0
        net = gross - cost
        by_day.setdefault(str(row.get("date"))[:10], []).append((net, net - benchmark))
    net_daily, excess_daily = [], []
    for day in sorted(by_day):
        values = by_day[day]
        net_daily.append(statistics.fmean(v[0] for v in values))
        excess_daily.append(statistics.fmean(v[1] for v in values))
    return net_daily, excess_daily


def _bootstrap_mean_ci(values: Sequence[float], *, seed: int, samples: int = 500, alpha: float = 0.05) -> Tuple[float, float]:
    if not values:
        return 0.0, 0.0
    if len(values) == 1 or _safe_std(values) == 0:
        return float(values[0]), float(values[0])
    rng = random.Random(seed)
    n = len(values)
    means = []
    for _ in range(max(100, int(samples))):
        means.append(statistics.fmean(values[rng.randrange(n)] for _ in range(n)))
    means.sort()
    lo = means[max(0, min(len(means) - 1, int((alpha / 2) * len(means))))]
    hi = means[max(0, min(len(means) - 1, int((1 - alpha / 2) * len(means)) - 1))]
    return lo, hi


def _moving_block_bootstrap_mean_ci(values: Sequence[float], *, seed: int, block_length: int,
                                    samples: int = 500, alpha: float = 0.05) -> Tuple[float, float]:
    """Moving-block bootstrap CI preserving short-horizon serial dependence."""
    if not values:
        return 0.0, 0.0
    if len(values) == 1 or _safe_std(values) == 0:
        return float(values[0]), float(values[0])
    rng = random.Random(seed ^ 0x5A17)
    n = len(values)
    block = max(1, min(int(block_length), n))
    starts = list(range(0, max(1, n - block + 1)))
    means = []
    for _ in range(max(100, int(samples))):
        sample = []
        while len(sample) < n:
            start = starts[rng.randrange(len(starts))]
            sample.extend(values[start:start + block])
        means.append(statistics.fmean(sample[:n]))
    means.sort()
    lo = means[max(0, min(len(means) - 1, int((alpha / 2) * len(means))))]
    hi = means[max(0, min(len(means) - 1, int((1 - alpha / 2) * len(means)) - 1))]
    return lo, hi


def _hac_t_stat(values: Sequence[float], max_lag: int) -> float:
    """Newey-West/HAC t-statistic for a positive mean under overlap."""
    n = len(values)
    if n < 3:
        return 0.0
    mean = statistics.fmean(values)
    centered = [value - mean for value in values]
    gamma0 = sum(value * value for value in centered) / n
    long_run = gamma0
    lag_cap = max(0, min(int(max_lag), n - 1))
    for lag in range(1, lag_cap + 1):
        covariance = sum(centered[t] * centered[t - lag] for t in range(lag, n)) / n
        weight = 1.0 - lag / (lag_cap + 1.0)
        long_run += 2.0 * weight * covariance
    if long_run <= 0:
        return 999.0 if mean > 0 else 0.0
    return mean / math.sqrt(long_run / n)


def _effective_sample_size(values: Sequence[float], max_lag: int) -> float:
    n = len(values)
    if n < 3:
        return float(n)
    mean = statistics.fmean(values)
    centered = [value - mean for value in values]
    denominator = sum(value * value for value in centered)
    if denominator <= 0:
        return float(n)
    adjustment = 0.0
    for lag in range(1, min(max(0, int(max_lag)), n - 1) + 1):
        rho = sum(centered[t] * centered[t - lag] for t in range(lag, n)) / denominator
        adjustment += (1.0 - lag / n) * rho
    return max(3.0, min(float(n), n / max(1e-9, 1.0 + 2.0 * adjustment)))


def _deflated_sharpe_probability(returns: Sequence[float], observed_sharpe: float, trial_count: int,
                                 effective_n: Optional[float] = None) -> float:
    """Conservative Bailey-style Deflated Sharpe approximation.

    This does not claim exact small-sample inference.  It explicitly adjusts the
    observed Sharpe for non-normality and the expected best result among the
    declared number of tried specifications.
    """
    n = float(effective_n if effective_n is not None else len(returns))
    if n < 3:
        return 0.0
    skew = _skew(returns)
    kurt = _kurtosis(returns)
    variance = max(1e-12, (1.0 - skew * observed_sharpe + ((kurt - 1.0) / 4.0) * observed_sharpe ** 2) / max(1, n - 1))
    sr_std = math.sqrt(variance)
    trials = max(1, int(trial_count))
    if trials == 1:
        expected_best = 0.0
    else:
        nd = NormalDist()
        gamma = 0.5772156649015329
        p1 = min(1 - 1e-9, max(1e-9, 1.0 - 1.0 / trials))
        p2 = min(1 - 1e-9, max(1e-9, 1.0 - 1.0 / (trials * math.e)))
        expected_best = sr_std * ((1.0 - gamma) * nd.inv_cdf(p1) + gamma * nd.inv_cdf(p2))
    return NormalDist().cdf((observed_sharpe - expected_best) / sr_std)


def _multiple_test_pvalue(excess_daily: Sequence[float], trial_count: int, max_lag: int = 0) -> float:
    if not excess_daily:
        return 1.0
    z = _hac_t_stat(excess_daily, max_lag=max_lag)
    raw = 0.5 * math.erfc(z / math.sqrt(2.0))  # one-sided positive-alpha test
    return min(1.0, raw * max(1, int(trial_count)))


def _calibration(rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    usable = []
    for row in rows:
        score = _f(row.get("rank_score") if row.get("rank_score") is not None else row.get("score"))
        gross = _f(row.get("forward_return"))
        if score is None or gross is None:
            continue
        net = gross - (_f(row.get("cost_return")) or 0.0)
        usable.append((score, net))
    if len(usable) < 30:
        return {"state": "INSUFFICIENT_DATA", "coverage": len(usable), "bins": [], "monotonic_pair_rate": None}
    usable.sort(key=lambda item: item[0])
    bin_count = min(5, max(3, len(usable) // 20))
    bins = []
    for idx in range(bin_count):
        start = idx * len(usable) // bin_count
        end = (idx + 1) * len(usable) // bin_count
        chunk = usable[start:end]
        if not chunk:
            continue
        bins.append({
            "bin": idx + 1,
            "n": len(chunk),
            "mean_score": statistics.fmean(x[0] for x in chunk),
            "mean_net_return": statistics.fmean(x[1] for x in chunk),
            "win_rate": sum(x[1] > 0 for x in chunk) / len(chunk),
        })
    comparable = max(0, len(bins) - 1)
    monotonic = sum(bins[i + 1]["mean_net_return"] >= bins[i]["mean_net_return"] for i in range(comparable)) / comparable if comparable else 0.0
    return {"state": "MEASURED", "coverage": len(usable), "bins": bins, "monotonic_pair_rate": monotonic}


@dataclass(frozen=True)
class FoldResult:
    fold: int
    train_start: str
    train_end: str
    test_start: str
    test_end: str
    purge_days: int
    embargo_days: int
    n_test: int
    n_test_days: int
    mean_net_return: float
    median_net_return: float
    mean_excess_return: float
    win_rate: float
    profit_factor: Optional[float]
    expectancy: float
    sharpe: Optional[float]
    sortino: Optional[float]
    precomputed_signal_equal_weight_drawdown: float
    fold_local_model_artifact: Optional[Dict[str, Any]] = None


class WalkForwardValidationService:
    def __init__(self, store: Any = None):
        self.store = store
        if store is not None and not hasattr(store, "write_lock"):
            store.write_lock = threading.Lock()

    @staticmethod
    def _model_artifact_hash(model_artifact: Any) -> str:
        try:
            encoded = json.dumps(
                model_artifact, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ValueError("model_artifact must be canonical JSON data") from exc
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _fold_artifact(
        *,
        fold: Dict[str, Any],
        artifact: Any,
        test_rows: Sequence[Dict[str, Any]],
        purge_days: int,
    ) -> Tuple[List[Dict[str, Any]], Optional[Dict[str, Any]], List[str]]:
        """Verify an immutable model artifact and bind every test prediction to it."""
        errors: List[str] = []
        if not isinstance(artifact, Mapping):
            return list(test_rows), None, ["FOLD_ARTIFACT_MISSING"]

        model_hash = str(artifact.get("model_hash") or "").strip().lower()
        model_artifact = artifact.get("model_artifact")
        if model_artifact is None:
            errors.append("IMMUTABLE_MODEL_ARTIFACT_MISSING")
        else:
            try:
                computed_hash = WalkForwardValidationService._model_artifact_hash(model_artifact)
            except ValueError:
                computed_hash = ""
                errors.append("MODEL_ARTIFACT_NOT_CANONICAL_JSON")
            if len(model_hash) != 64 or any(ch not in "0123456789abcdef" for ch in model_hash):
                errors.append("MODEL_HASH_INVALID")
            elif computed_hash and model_hash != computed_hash:
                errors.append("MODEL_HASH_MISMATCH")

        expected_train_start = str(fold["train_dates"][0])[:10]
        expected_train_end = str(fold["train_dates"][-1])[:10]
        expected_test_start = str(fold["test_dates"][0])[:10]
        train_start = str(artifact.get("train_start") or "")[:10]
        train_end = str(artifact.get("train_end") or "")[:10]
        if train_start != expected_train_start:
            errors.append("TRAIN_START_DOES_NOT_MATCH_FOLD")
        if train_end != expected_train_end:
            errors.append("TRAIN_END_DOES_NOT_MATCH_FOLD")

        train_end_at = _parse(train_end)
        test_start_at = _parse(expected_test_start)
        if train_end_at is None or test_start_at is None:
            errors.append("FOLD_TIME_INVALID")
        elif not train_end_at < test_start_at - timedelta(days=max(0, int(purge_days))):
            errors.append("TRAINING_END_NOT_BEFORE_PURGED_TEST_START")

        feature_cutoff_text = str(artifact.get("feature_cutoff") or "")
        feature_cutoff = _parse(feature_cutoff_text)
        trained_at_text = str(artifact.get("trained_at") or "")
        trained_at = _parse(trained_at_text)
        if feature_cutoff is None:
            errors.append("FEATURE_CUTOFF_MISSING_OR_INVALID")
        elif train_end_at is not None and feature_cutoff.date() > train_end_at.date():
            errors.append("FEATURE_CUTOFF_AFTER_TRAIN_END")
        if trained_at is None:
            errors.append("TRAINED_AT_MISSING_OR_INVALID")
        elif feature_cutoff is not None and trained_at < feature_cutoff:
            errors.append("TRAINED_AT_BEFORE_FEATURE_CUTOFF")

        expected: Dict[Tuple[str, str], Dict[str, Any]] = {}
        for row in test_rows:
            key = (str(row.get("date") or "")[:10], str(row.get("symbol") or "").upper())
            if not all(key) or key in expected:
                errors.append("TEST_OBSERVATION_IDENTITY_NOT_UNIQUE")
            expected[key] = dict(row)

        predictions = artifact.get("predictions")
        if not isinstance(predictions, list):
            predictions = []
            errors.append("PREDICTIONS_MISSING")
        prediction_rows: Dict[Tuple[str, str], Dict[str, Any]] = {}
        prediction_times: List[datetime] = []
        for prediction in predictions:
            if not isinstance(prediction, Mapping):
                errors.append("PREDICTION_NOT_AN_OBJECT")
                continue
            key = (
                str(prediction.get("date") or "")[:10],
                str(prediction.get("symbol") or "").upper(),
            )
            if key not in expected:
                errors.append("PREDICTION_OUTSIDE_TEST_FOLD")
                continue
            if key in prediction_rows:
                errors.append("DUPLICATE_TEST_PREDICTION")
                continue
            if str(prediction.get("model_hash") or "").strip().lower() != model_hash:
                errors.append("PREDICTION_MODEL_HASH_MISMATCH")
            predicted_at = _parse(prediction.get("prediction_timestamp"))
            if predicted_at is None:
                errors.append("PREDICTION_TIMESTAMP_MISSING_OR_INVALID")
            else:
                prediction_times.append(predicted_at)
                if trained_at is not None and predicted_at < trained_at:
                    errors.append("PREDICTION_BEFORE_MODEL_TRAINED")
                decision_at = _parse(expected[key].get("decision_as_of"))
                outcome_at = _parse(expected[key].get("outcome_as_of"))
                if decision_at is not None and predicted_at > decision_at:
                    errors.append("PREDICTION_AFTER_DECISION")
                if outcome_at is None:
                    errors.append("OUTCOME_TIMESTAMP_MISSING")
                elif predicted_at >= outcome_at:
                    errors.append("PREDICTION_NOT_BEFORE_OUTCOME")
            merged = dict(expected[key])
            merged.update(dict(prediction))
            prediction_rows[key] = merged

        if set(prediction_rows) != set(expected):
            errors.append("TEST_PREDICTION_COVERAGE_INCOMPLETE")

        lineage = {
            "model_hash": model_hash,
            "train_start": train_start,
            "train_end": train_end,
            "feature_cutoff": feature_cutoff_text,
            "trained_at": trained_at_text,
            "prediction_timestamp_start": min(prediction_times).isoformat() if prediction_times else None,
            "prediction_timestamp_end": max(prediction_times).isoformat() if prediction_times else None,
            "prediction_count": len(prediction_rows),
            "immutable_model_artifact_verified": not any(
                code in errors for code in (
                    "IMMUTABLE_MODEL_ARTIFACT_MISSING", "MODEL_ARTIFACT_NOT_CANONICAL_JSON",
                    "MODEL_HASH_INVALID", "MODEL_HASH_MISMATCH",
                )
            ),
        }
        return (
            [prediction_rows[key] for key in sorted(prediction_rows)] if not errors else list(test_rows),
            lineage,
            sorted(set(errors)),
        )

    @staticmethod
    def _oof_prediction_lineage(
        rows: Sequence[Dict[str, Any]], *, purge_days: int,
    ) -> Dict[str, Any]:
        """Verify row-bound historical OOF model lineage.

        This supports a historical walk-forward engine that explicitly retrains a
        model on each prior-only fold before producing the persisted OOF rows. It
        is intentionally distinct from prospectively frozen live predictions and
        from a single precomputed signal vector.
        """
        if not rows:
            return {"proven": False, "coverage": 0.0, "blockers": ["OOF_ROWS_EMPTY"], "models": 0}
        blockers: list[str] = []
        model_hashes: set[str] = set()
        valid = 0
        for row in rows:
            row_blockers: list[str] = []
            model_hash = str(row.get("oof_model_hash") or "").strip().lower()
            if len(model_hash) != 64 or any(ch not in "0123456789abcdef" for ch in model_hash):
                row_blockers.append("OOF_MODEL_HASH_INVALID")
            else:
                model_hashes.add(model_hash)
            train_start = _parse(row.get("oof_train_start"))
            train_end = _parse(row.get("oof_train_end"))
            feature_cutoff = _parse(row.get("oof_feature_cutoff"))
            predicted_at = _parse(row.get("oof_prediction_timestamp"))
            decision_at = _parse(row.get("decision_as_of"))
            outcome_at = _parse(row.get("outcome_as_of"))
            if train_start is None or train_end is None or train_end < train_start:
                row_blockers.append("OOF_TRAIN_WINDOW_INVALID")
            if feature_cutoff is None or train_end is None or feature_cutoff.date() > train_end.date():
                row_blockers.append("OOF_FEATURE_CUTOFF_INVALID")
            if predicted_at is None or decision_at is None:
                row_blockers.append("OOF_PREDICTION_OR_DECISION_TIME_MISSING")
            elif predicted_at > decision_at:
                row_blockers.append("OOF_PREDICTION_AFTER_DECISION")
            if outcome_at is None or predicted_at is None or predicted_at >= outcome_at:
                row_blockers.append("OOF_PREDICTION_NOT_BEFORE_OUTCOME")
            if train_end is not None and decision_at is not None:
                if not train_end < decision_at - timedelta(days=max(0, int(purge_days))):
                    row_blockers.append("OOF_TRAIN_END_NOT_BEFORE_PURGED_DECISION")
            if str(row.get("oof_artifact_kind") or "") != "HISTORICAL_FOLD_MODEL_BINARY_SHA256":
                row_blockers.append("OOF_ARTIFACT_KIND_INVALID")
            if row_blockers:
                blockers.extend(row_blockers)
            else:
                valid += 1
        coverage = valid / len(rows)
        return {
            "proven": coverage == 1.0 and not blockers,
            "coverage": coverage,
            "rows": len(rows),
            "valid_rows": valid,
            "models": len(model_hashes),
            "blockers": sorted(set(blockers)),
            "authority": "ROW_BOUND_PURGED_HISTORICAL_OOF_MODEL_LINEAGE",
        }

    @staticmethod
    def _prospective_prediction_lineage(rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
        """Verify immutable predictions that were created prospectively before outcomes.

        This lane is for genuine forward selector evidence.  It is not historical
        replay and does not pretend that a prospectively persisted signal was
        retrained inside a synthetic historical fold.
        """
        if not rows:
            return {"proven": False, "coverage": 0.0, "blockers": ["PROSPECTIVE_ROWS_EMPTY"], "models": 0}
        blockers: list[str] = []
        model_versions: set[str] = set()
        hashes: set[str] = set()
        valid = 0
        for row in rows:
            row_blockers: list[str] = []
            digest = str(row.get("prospective_prediction_hash") or "").strip().lower()
            if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
                row_blockers.append("PROSPECTIVE_PREDICTION_HASH_INVALID")
            else:
                hashes.add(digest)
            if not str(row.get("prospective_prediction_key") or "").strip():
                row_blockers.append("PROSPECTIVE_PREDICTION_KEY_MISSING")
            model_version = str(row.get("prospective_model_version") or "").strip()
            if not model_version:
                row_blockers.append("PROSPECTIVE_MODEL_VERSION_MISSING")
            else:
                model_versions.add(model_version)
            predicted_at = _parse(row.get("prospective_prediction_at"))
            decision_at = _parse(row.get("decision_as_of"))
            feature_at = _parse(row.get("feature_as_of"))
            outcome_at = _parse(row.get("outcome_as_of"))
            if predicted_at is None or decision_at is None:
                row_blockers.append("PROSPECTIVE_PREDICTION_OR_DECISION_TIME_MISSING")
            elif predicted_at > decision_at:
                row_blockers.append("PROSPECTIVE_PREDICTION_AFTER_DECISION")
            if feature_at is None or predicted_at is None or feature_at > predicted_at:
                row_blockers.append("PROSPECTIVE_FEATURE_AFTER_PREDICTION")
            if outcome_at is None or predicted_at is None or predicted_at >= outcome_at:
                row_blockers.append("PROSPECTIVE_PREDICTION_NOT_BEFORE_OUTCOME")
            authority = str(row.get("prospective_evidence_authority") or "")
            if authority not in {"GOVERNANCE_POSTGRESQL_SELECTOR_EVIDENCE", "LEGACY_SQLITE_READ_PROJECTION"}:
                row_blockers.append("PROSPECTIVE_EVIDENCE_AUTHORITY_INVALID")
            if row_blockers:
                blockers.extend(row_blockers)
            else:
                valid += 1
        coverage = valid / len(rows)
        return {
            "proven": coverage == 1.0 and not blockers,
            "coverage": coverage,
            "rows": len(rows),
            "valid_rows": valid,
            "models": len(model_versions),
            "prediction_hashes": len(hashes),
            "blockers": sorted(set(blockers)),
            "authority": "PROSPECTIVE_IMMUTABLE_PREDICTION_LINEAGE",
            "historical_replay": False,
        }

    @staticmethod
    def _capital_simulation(
        simulator: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]],
        *,
        model_id: str,
        observations: Sequence[Dict[str, Any]],
    ) -> Dict[str, Any]:
        if simulator is None:
            return {
                "proven": False,
                "blockers": ["CAPITAL_CONSTRAINED_PORTFOLIO_SIMULATOR_REQUIRED"],
                "max_drawdown": None,
            }
        try:
            report = simulator({
                "model_id": str(model_id),
                "observations": tuple(dict(row) for row in observations),
            })
        except Exception as exc:
            return {
                "proven": False,
                "blockers": [f"PORTFOLIO_SIMULATOR_FAILED:{type(exc).__name__}"],
                "max_drawdown": None,
            }
        if not isinstance(report, Mapping):
            return {
                "proven": False,
                "blockers": ["PORTFOLIO_SIMULATOR_REPORT_INVALID"],
                "max_drawdown": None,
            }

        blockers = []
        for field in (
            "capital_constraints_enforced", "concurrency_constraints_enforced",
            "position_sizing_enforced", "mark_to_market_enforced", "no_leverage",
        ):
            if report.get(field) is not True:
                blockers.append(f"{field.upper()}_NOT_PROVEN")
        initial_capital = _f(report.get("initial_capital"))
        max_concurrent = report.get("max_concurrent_positions")
        if initial_capital is None or initial_capital <= 0:
            blockers.append("INITIAL_CAPITAL_INVALID")
        try:
            max_concurrent = int(max_concurrent)
        except (TypeError, ValueError):
            max_concurrent = 0
        if max_concurrent <= 0:
            blockers.append("MAX_CONCURRENT_POSITIONS_INVALID")

        equity_curve = report.get("equity_curve")
        equities: List[float] = []
        timestamps: List[datetime] = []
        if not isinstance(equity_curve, list) or len(equity_curve) < 2:
            blockers.append("EQUITY_CURVE_INSUFFICIENT")
        else:
            for point in equity_curve:
                if not isinstance(point, Mapping):
                    blockers.append("EQUITY_POINT_INVALID")
                    continue
                stamp = _parse(point.get("timestamp"))
                equity = _f(point.get("equity"))
                if stamp is None or equity is None or equity <= 0:
                    blockers.append("EQUITY_POINT_INVALID")
                    continue
                timestamps.append(stamp)
                equities.append(equity)
            if len(timestamps) >= 2 and any(
                current <= previous for previous, current in zip(timestamps, timestamps[1:])
            ):
                blockers.append("EQUITY_TIMESTAMPS_NOT_STRICTLY_INCREASING")
        portfolio_returns = [
            equities[index] / equities[index - 1] - 1.0
            for index in range(1, len(equities))
            if equities[index - 1] > 0
        ]
        drawdown = _max_drawdown(portfolio_returns) if portfolio_returns else None
        return {
            "proven": not blockers,
            "blockers": sorted(set(blockers)),
            "initial_capital": initial_capital,
            "max_concurrent_positions": max_concurrent,
            "capital_constraints_enforced": report.get("capital_constraints_enforced") is True,
            "concurrency_constraints_enforced": report.get("concurrency_constraints_enforced") is True,
            "position_sizing_enforced": report.get("position_sizing_enforced") is True,
            "mark_to_market_enforced": report.get("mark_to_market_enforced") is True,
            "no_leverage": report.get("no_leverage") is True,
            "equity_points": len(equities),
            "equity_start": equities[0] if equities else None,
            "equity_end": equities[-1] if equities else None,
            "max_drawdown": drawdown,
        }

    @staticmethod
    def build_folds(dates: Iterable[str], min_train_days: int = 252, test_days: int = 63,
                    purge_days: int = 20, max_folds: int = 8, embargo_days: int = 0) -> List[Dict[str, Any]]:
        unique = sorted({str(d)[:10] for d in dates if d})
        folds = []
        test_start = max(1, int(min_train_days))
        step = max(1, int(test_days)) + max(0, int(embargo_days))
        while test_start < len(unique) and len(folds) < max(1, int(max_folds)):
            train_end_index = test_start - max(0, int(purge_days)) - 1
            if train_end_index < 0:
                test_start += step
                continue
            test_end = min(len(unique), test_start + max(1, int(test_days)))
            folds.append({
                "fold": len(folds) + 1,
                "train_dates": unique[:train_end_index + 1],
                "test_dates": unique[test_start:test_end],
                "purge_days": max(0, int(purge_days)),
                "embargo_days": max(0, int(embargo_days)),
            })
            test_start += step
        return [fold for fold in folds if fold["train_dates"] and fold["test_dates"]]

    @staticmethod
    def _metrics(rows: List[Dict[str, Any]], horizon_days: int = 1) -> Dict[str, float]:
        net = []
        for row in rows:
            gross = _f(row.get("forward_return"))
            if gross is None:
                continue
            net.append(gross - (_f(row.get("cost_return")) or 0.0))
        net_daily, excess_daily = _aggregate_daily(rows)
        if not net:
            return {"n": 0, "n_days": 0, "mean": 0.0, "median": 0.0, "excess": 0.0,
                    "win_rate": 0.0, "profit_factor": None, "profit_factor_state": "UNDEFINED_NO_TRADES", "expectancy": 0.0,
                    "sharpe": None, "sharpe_state": "UNDEFINED_NO_TRADES", "sortino": None, "sortino_state": "UNDEFINED_NO_TRADES",
                    "precomputed_signal_equal_weight_drawdown": 0.0,
                    "drawdown": 0.0}  # internal signal-metric compatibility only
        winners = [value for value in net if value > 0]
        losers = [value for value in net if value < 0]
        gross_profit = sum(winners)
        gross_loss = abs(sum(losers))
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else None
        profit_factor_state = "FINITE" if gross_loss > 0 else ("POSITIVE_NO_LOSSES" if gross_profit > 0 else "UNDEFINED_NO_GAINS_OR_LOSSES")
        mean_daily = statistics.fmean(net_daily) if net_daily else 0.0
        std_daily = _safe_std(net_daily)
        annualizer = math.sqrt(252.0 / max(1, int(horizon_days)))
        sharpe = mean_daily / std_daily * annualizer if std_daily > 0 else None
        sharpe_state = "FINITE" if std_daily > 0 else "UNDEFINED_ZERO_VARIANCE"
        downside = [min(0.0, value) for value in net_daily]
        downside_dev = math.sqrt(statistics.fmean(value * value for value in downside)) if downside else 0.0
        sortino = mean_daily / downside_dev * annualizer if downside_dev > 0 else None
        sortino_state = "FINITE" if downside_dev > 0 else "UNDEFINED_ZERO_DOWNSIDE_DEVIATION"
        signal_drawdown = _max_drawdown(net_daily)
        return {
            "n": len(net),
            "n_days": len(net_daily),
            "mean": statistics.fmean(net),
            "median": statistics.median(net),
            "excess": statistics.fmean(excess_daily) if excess_daily else 0.0,
            "win_rate": sum(value > 0 for value in net) / len(net),
            "profit_factor": profit_factor,
            "profit_factor_state": profit_factor_state,
            "expectancy": statistics.fmean(net),
            "sharpe": sharpe,
            "sharpe_state": sharpe_state,
            "sortino": sortino,
            "sortino_state": sortino_state,
            "precomputed_signal_equal_weight_drawdown": signal_drawdown,
            "drawdown": signal_drawdown,  # internal signal-metric compatibility only
        }

    def validate(self, model_id: str, observations: Iterable[Dict[str, Any]], horizon_days: int = 10,
                 min_train_days: int = 252, test_days: int = 63, purge_days: Optional[int] = None,
                 max_folds: int = 8, min_samples: int = 100, persist: bool = True,
                 profile: str = RESEARCH_PROFILE, trial_count: int = 1, embargo_days: int = 0,
                 bootstrap_samples: int = 500,
                 fold_trainer: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None,
                 fold_artifacts: Optional[Mapping[Any, Dict[str, Any]]] = None,
                 portfolio_simulator: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None,
                 ) -> Dict[str, Any]:
        profile = str(profile or RESEARCH_PROFILE).strip().lower()
        if profile not in (RESEARCH_PROFILE, CAPITAL_PROFILE):
            raise ValueError("profile must be research or capital")
        if fold_trainer is not None and fold_artifacts is not None:
            raise ValueError("provide fold_trainer or fold_artifacts, not both")
        rows = [dict(row) for row in observations if row and row.get("date")]
        rows.sort(key=lambda row: (str(row.get("date"))[:10], str(row.get("symbol") or "")))
        purge = max(int(horizon_days), int(purge_days or 0))
        folds = self.build_folds((row["date"] for row in rows), min_train_days, test_days, purge, max_folds, embargo_days)
        temporal_fold_proof = DEFAULT_TEMPORAL_LEAKAGE_AUTHORITY.validate_folds((row["date"] for row in rows), folds)
        temporal_canary_proof = DEFAULT_TEMPORAL_LEAKAGE_AUTHORITY.run_canary_suite()
        results: List[FoldResult] = []
        all_test_rows: List[Dict[str, Any]] = []
        fold_local_errors: List[Dict[str, Any]] = []
        fold_local_requested = fold_trainer is not None or fold_artifacts is not None
        for fold in folds:
            test_set = set(fold["test_dates"])
            test_rows = [row for row in rows if str(row["date"])[:10] in test_set]
            fold_lineage = None
            validated_rows = test_rows
            if fold_local_requested:
                if fold_trainer is not None:
                    train_set = set(fold["train_dates"])
                    train_rows = [row for row in rows if str(row["date"])[:10] in train_set]
                    prediction_inputs = [
                        {key: value for key, value in row.items() if key not in _OUTCOME_FIELDS}
                        for row in test_rows
                    ]
                    try:
                        artifact = fold_trainer({
                            "fold": int(fold["fold"]),
                            "train_dates": tuple(fold["train_dates"]),
                            "test_dates": tuple(fold["test_dates"]),
                            "purge_days": purge,
                            "embargo_days": max(0, int(embargo_days)),
                            "train_observations": tuple(dict(row) for row in train_rows),
                            "prediction_inputs": tuple(prediction_inputs),
                        })
                    except Exception as exc:
                        artifact = None
                        fold_local_errors.append({
                            "fold": int(fold["fold"]),
                            "blockers": [f"FOLD_TRAINER_FAILED:{type(exc).__name__}"],
                        })
                else:
                    artifact = (
                        fold_artifacts.get(fold["fold"])
                        or fold_artifacts.get(str(fold["fold"]))
                    )
                validated_rows, fold_lineage, artifact_errors = self._fold_artifact(
                    fold=fold, artifact=artifact, test_rows=test_rows, purge_days=purge,
                )
                if artifact_errors:
                    fold_local_errors.append({
                        "fold": int(fold["fold"]), "blockers": artifact_errors,
                    })
            all_test_rows.extend(validated_rows)
            metrics = self._metrics(validated_rows, horizon_days=horizon_days)
            results.append(FoldResult(
                fold=fold["fold"],
                train_start=fold["train_dates"][0],
                train_end=fold["train_dates"][-1],
                test_start=fold["test_dates"][0],
                test_end=fold["test_dates"][-1],
                purge_days=purge,
                embargo_days=max(0, int(embargo_days)),
                n_test=int(metrics["n"]),
                n_test_days=int(metrics["n_days"]),
                mean_net_return=metrics["mean"],
                median_net_return=metrics["median"],
                mean_excess_return=metrics["excess"],
                win_rate=metrics["win_rate"],
                profit_factor=metrics["profit_factor"],
                expectancy=metrics["expectancy"],
                sharpe=metrics["sharpe"],
                sortino=metrics["sortino"],
                precomputed_signal_equal_weight_drawdown=metrics["precomputed_signal_equal_weight_drawdown"],
                fold_local_model_artifact=fold_lineage,
            ))

        result_rows = [asdict(result) for result in results]
        n_test = sum(result.n_test for result in results)
        aggregate = self._metrics(all_test_rows, horizon_days=horizon_days)
        fold_local_training_proven = bool(
            fold_local_requested and results and not fold_local_errors
            and all(result.fold_local_model_artifact for result in results)
        )
        oof_lineage_proof = self._oof_prediction_lineage(all_test_rows, purge_days=purge)
        oof_model_lineage_proven = oof_lineage_proof.get("proven") is True
        prospective_lineage_proof = self._prospective_prediction_lineage(all_test_rows)
        prospective_prediction_lineage_proven = prospective_lineage_proof.get("proven") is True
        capital_model_training_proven = (
            fold_local_training_proven or oof_model_lineage_proven or prospective_prediction_lineage_proven
        )
        if not fold_local_requested and not oof_model_lineage_proven and not prospective_prediction_lineage_proven:
            fold_local_errors.append({
                "fold": None, "blockers": ["FOLD_LOCAL_OR_IMMUTABLE_OOF_OR_PROSPECTIVE_LINEAGE_REQUIRED"],
            })
        validation_kind = (
            FOLD_LOCAL_MODEL_VALIDATION if fold_local_training_proven else
            FROZEN_OOF_MODEL_VALIDATION if oof_model_lineage_proven else
            PROSPECTIVE_IMMUTABLE_PREDICTION_VALIDATION if prospective_prediction_lineage_proven else
            PRECOMPUTED_SIGNAL_OBSERVATION_VALIDATION
        )
        capital_simulation = self._capital_simulation(
            portfolio_simulator, model_id=str(model_id), observations=all_test_rows,
        )
        positive_folds = sum(result.mean_excess_return > 0 for result in results)
        stability = positive_folds / len(results) if results else 0.0
        net_daily, excess_daily = _aggregate_daily(all_test_rows)
        seed = int(hashlib.sha256(str(model_id).encode()).hexdigest()[:8], 16)
        ci_low, ci_high = _bootstrap_mean_ci(net_daily, seed=seed, samples=bootstrap_samples)
        block_length = max(2, int(horizon_days))
        block_ci_low, block_ci_high = _moving_block_bootstrap_mean_ci(
            net_daily, seed=seed, block_length=block_length, samples=bootstrap_samples,
        )
        hac_net_t = _hac_t_stat(net_daily, max_lag=max(0, block_length - 1))
        hac_excess_t = _hac_t_stat(excess_daily, max_lag=max(0, block_length - 1))
        adjusted_p = _multiple_test_pvalue(excess_daily, trial_count, max_lag=max(0, block_length - 1))
        daily_std = _safe_std(net_daily)
        daily_sharpe = statistics.fmean(net_daily) / daily_std if net_daily and daily_std > 0 else None
        effective_n = _effective_sample_size(net_daily, max_lag=max(0, block_length - 1))
        dsr = _deflated_sharpe_probability(net_daily, daily_sharpe, trial_count, effective_n=effective_n) if daily_sharpe is not None else 0.0
        calibration = _calibration(all_test_rows)
        symbols = {str(row.get("symbol") or "").upper() for row in all_test_rows if row.get("symbol")}
        regime_rows: Dict[str, List[Dict[str, Any]]] = {}
        for row in all_test_rows:
            regime = str(row.get("market_regime") or "UNKNOWN").upper()
            regime_rows.setdefault(regime, []).append(row)
        regime_performance = {
            regime: self._metrics(values, horizon_days=horizon_days)
            for regime, values in sorted(regime_rows.items())
            if regime != "UNKNOWN" and values
        }
        observed_regimes = sorted(regime_performance)
        positive_regimes = sum(
            metrics.get("mean", 0.0) > 0 and metrics.get("excess", 0.0) > 0
            for metrics in regime_performance.values()
        )
        regime_stability = positive_regimes / len(regime_performance) if regime_performance else 0.0
        regime_transition_coverage = (
            sum(row.get("regime_change_probability") is not None for row in all_test_rows) / len(all_test_rows)
            if all_test_rows else 0.0
        )

        cost_coverage = sum(row.get("cost_return") is not None for row in all_test_rows) / len(all_test_rows) if all_test_rows else 0.0
        benchmark_coverage = sum(row.get("benchmark_return") is not None for row in all_test_rows) / len(all_test_rows) if all_test_rows else 0.0
        lineage_fields = (
            "dataset_fingerprint", "feature_manifest_hash", "universe_id",
            "cost_model_version", "cost_model_profile", "execution_model_version",
            "admission_policy_version", "session_index_fingerprint",
        )
        lineage_coverage = sum(all(row.get(field) not in (None, "") for field in lineage_fields) for row in all_test_rows) / len(all_test_rows) if all_test_rows else 0.0
        corporate_action_coverage = sum(row.get("corporate_action_adjusted") is True for row in all_test_rows) / len(all_test_rows) if all_test_rows else 0.0
        survivorship_control_coverage = sum(row.get("survivorship_bias_controlled") is True for row in all_test_rows) / len(all_test_rows) if all_test_rows else 0.0
        admission_coverage = sum(bool(row.get("admission_policy_version")) for row in all_test_rows) / len(all_test_rows) if all_test_rows else 0.0
        session_lineage_coverage = sum(
            row.get("session_authority_ready") is True
            and bool(row.get("session_index_fingerprint"))
            and bool(row.get("session_authority"))
            and bool(row.get("session_authority_version"))
            for row in all_test_rows
        ) / len(all_test_rows) if all_test_rows else 0.0
        official_nse_lineage_coverage = sum(
            bool(row.get("official_nse_lineage_hash")) for row in all_test_rows
        ) / len(all_test_rows) if all_test_rows else 0.0
        official_nse_complete_coverage = sum(
            row.get("official_nse_complete") is True
            and int(row.get("official_nse_core_required_count") or 0) > 0
            and int(row.get("official_nse_core_source_count") or 0) >= int(row.get("official_nse_core_required_count") or 0)
            for row in all_test_rows
        ) / len(all_test_rows) if all_test_rows else 0.0
        optional_nse_enrichment_coverage = (
            sum(float(row.get("official_nse_optional_enrichment_coverage") or 0.0) for row in all_test_rows) / len(all_test_rows)
            if all_test_rows else 0.0
        )
        baseline_counts: Dict[str, int] = {}
        baseline_excess_values: Dict[str, List[float]] = {}
        for row in all_test_rows:
            gross = _f(row.get("forward_return"))
            net = None if gross is None else gross - (_f(row.get("cost_return")) or 0.0)
            for name, value in dict(row.get("baseline_returns") or {}).items():
                baseline = _f(value)
                if baseline is None or net is None:
                    continue
                key = str(name)
                baseline_counts[key] = baseline_counts.get(key, 0) + 1
                baseline_excess_values.setdefault(key, []).append(net - baseline)
        baseline_coverage = {
            name: count / len(all_test_rows) if all_test_rows else 0.0
            for name, count in baseline_counts.items()
        }
        baseline_mean_excess = {
            name: statistics.fmean(values) if values else 0.0
            for name, values in baseline_excess_values.items()
        }
        complete_baselines = sorted(name for name, coverage in baseline_coverage.items() if coverage == 1.0)

        point_in_time_coverage = 0.0
        feature_time_coverage = 0.0
        lookahead_violations = 0
        feature_lookahead_violations = 0
        temporal_observation_failures: list[Dict[str, Any]] = []
        for row in all_test_rows:
            proof = DEFAULT_TEMPORAL_LEAKAGE_AUTHORITY.validate_observation(row)
            blockers = set(proof.get("blockers") or [])
            if "DECISION_TIME_MISSING" not in blockers and "OUTCOME_TIME_MISSING" not in blockers:
                point_in_time_coverage += 1
            required_feature_blockers = {code for code in blockers if code.endswith("_MISSING") and code not in {"DECISION_TIME_MISSING", "OUTCOME_TIME_MISSING"}}
            if not required_feature_blockers:
                feature_time_coverage += 1
            lookahead_violations += int("DECISION_AFTER_OUTCOME" in blockers)
            feature_lookahead_violations += sum(code.endswith("_AFTER_DECISION") for code in blockers)
            if blockers:
                temporal_observation_failures.append({
                    "symbol": row.get("symbol"), "date": row.get("date"), "blockers": sorted(blockers)
                })
        point_in_time_coverage = point_in_time_coverage / len(all_test_rows) if all_test_rows else 0.0
        feature_time_coverage = feature_time_coverage / len(all_test_rows) if all_test_rows else 0.0

        signal_observation_gates = {
            "minimum_three_folds": len(results) >= 3,
            "minimum_samples": n_test >= int(min_samples),
            "positive_net_return": aggregate["mean"] > 0,
            "positive_excess_return": aggregate["excess"] > 0,
            "fold_stability_60pct": stability >= 0.60,
            "finite_metrics": (
                all(math.isfinite(value) for value in (
                    aggregate["mean"], aggregate["excess"], aggregate["win_rate"], stability,
                    aggregate["precomputed_signal_equal_weight_drawdown"],
                ))
                and aggregate.get("profit_factor_state") in {"FINITE", "POSITIVE_NO_LOSSES"}
                and aggregate.get("sharpe") is not None and math.isfinite(aggregate["sharpe"])
                and aggregate.get("sortino") is not None and math.isfinite(aggregate["sortino"])
            ),
        }
        research_gates = {
            **signal_observation_gates,
            "fold_local_training_proven": fold_local_training_proven,
        }
        if profile == RESEARCH_PROFILE:
            gates = research_gates
        else:
            gates = {
                "minimum_five_folds": len(results) >= 5,
                "minimum_300_samples": n_test >= max(300, int(min_samples)),
                "minimum_25_symbols": len(symbols) >= 25,
                "minimum_three_observed_regimes": len(observed_regimes) >= 3,
                "regime_transition_coverage_complete": regime_transition_coverage == 1.0,
                "regime_stability_two_thirds": len(observed_regimes) >= 3 and regime_stability >= (2.0 / 3.0),
                "positive_cost_adjusted_return": aggregate["mean"] > 0,
                "positive_benchmark_excess": aggregate["excess"] > 0,
                "fold_stability_70pct": stability >= 0.70,
                "profit_factor_1_10": (aggregate.get("profit_factor_state") == "POSITIVE_NO_LOSSES" or (aggregate.get("profit_factor") is not None and aggregate["profit_factor"] >= 1.10)),
                "moving_block_bootstrap_lower_bound_positive": block_ci_low > 0,
                "hac_net_tstat_1_645": hac_net_t >= 1.645,
                "hac_excess_tstat_1_645": hac_excess_t >= 1.645,
                "deflated_sharpe_95pct": dsr >= 0.95,
                "multiple_test_adjusted_p_05": adjusted_p <= 0.05,
                "capital_model_training_proven": capital_model_training_proven,
                "capital_constrained_portfolio_simulation_proven": capital_simulation["proven"] is True,
                "capital_constrained_portfolio_drawdown_within_25pct": (
                    capital_simulation["max_drawdown"] is not None
                    and capital_simulation["max_drawdown"] >= -0.25
                ),
                "cost_coverage_complete": cost_coverage == 1.0,
                "benchmark_coverage_complete": benchmark_coverage == 1.0,
                "minimum_three_complete_baselines": len(complete_baselines) >= 3,
                "outperforms_all_complete_baselines": len(complete_baselines) >= 3 and all(baseline_mean_excess.get(name, 0.0) > 0 for name in complete_baselines),
                "lineage_coverage_complete": lineage_coverage == 1.0,
                "official_nse_lineage_coverage_complete": official_nse_lineage_coverage == 1.0,
                "official_nse_core_source_family_coverage_complete": official_nse_complete_coverage == 1.0,
                "corporate_actions_adjusted_complete": corporate_action_coverage == 1.0,
                "survivorship_control_complete": survivorship_control_coverage == 1.0,
                "portfolio_admission_coverage_complete": admission_coverage == 1.0,
                "historical_session_lineage_coverage_complete": session_lineage_coverage == 1.0,
                "purge_embargo_fold_layout_valid": temporal_fold_proof.get("ok") is True,
                "leakage_canary_suite_passed": temporal_canary_proof.get("ok") is True,
                "point_in_time_coverage_complete": point_in_time_coverage == 1.0,
                "feature_time_coverage_complete": feature_time_coverage == 1.0,
                "no_lookahead_violations": lookahead_violations == 0 and feature_lookahead_violations == 0,
                "finite_metrics": signal_observation_gates["finite_metrics"],
            }
            if calibration["state"] == "MEASURED":
                gates["score_calibration_monotonic_60pct"] = float(calibration["monotonic_pair_rate"]) >= 0.60

        signal_observation_validated = all(signal_observation_gates.values())
        approved = all(gates.values())
        status = "APPROVED" if approved else "REJECTED"
        lifecycle = (
            "RESEARCH_APPROVED" if approved and profile == RESEARCH_PROFILE else
            "BACKTEST_APPROVED" if approved else
            "EXPERIMENTAL"
        )
        basis = {
            "model_id": str(model_id),
            "authority_version": AUTHORITY_VERSION,
            "validation_profile": profile,
            "validation_kind": validation_kind,
            "signal_observation_status": "VALIDATED" if signal_observation_validated else "REJECTED",
            "fold_local_training_requested": fold_local_requested,
            "fold_local_training_proven": fold_local_training_proven,
            "fold_local_training_proof": {
                "proven": fold_local_training_proven,
                "fold_count": len(results),
                "blockers": fold_local_errors,
            },
            "oof_model_lineage_proof": oof_lineage_proof,
            "prospective_prediction_lineage_proof": prospective_lineage_proof,
            "capital_model_training_proven": capital_model_training_proven,
            "horizon_days": int(horizon_days),
            "purge_days": purge,
            "embargo_days": max(0, int(embargo_days)),
            "temporal_leakage_authority": DEFAULT_TEMPORAL_LEAKAGE_AUTHORITY.authority,
            "temporal_leakage_authority_version": DEFAULT_TEMPORAL_LEAKAGE_AUTHORITY.authority_version,
            "temporal_fold_proof": temporal_fold_proof,
            "temporal_canary_proof": temporal_canary_proof,
            "temporal_observation_failure_count": len(temporal_observation_failures),
            "temporal_observation_failures": temporal_observation_failures[:50],
            "min_train_days": int(min_train_days),
            "test_days": int(test_days),
            "trial_count": max(1, int(trial_count)),
            "folds": result_rows,
            "n_test": n_test,
            "n_test_days": aggregate["n_days"],
            "universe_symbols": len(symbols),
            "observed_regimes": observed_regimes,
            "regime_count": len(observed_regimes),
            "regime_performance": regime_performance,
            "regime_stability": regime_stability,
            "regime_transition_coverage": regime_transition_coverage,
            "mean_net_return": aggregate["mean"],
            "median_net_return": aggregate["median"],
            "mean_excess_return": aggregate["excess"],
            "win_rate": aggregate["win_rate"],
            "profit_factor": aggregate["profit_factor"],
            "profit_factor_state": aggregate.get("profit_factor_state"),
            "expectancy_semantics": expectancy_lane("WALK_FORWARD_NET_RETURN_EXPECTANCY"),
            "expectancy_net_return": aggregate["expectancy"],
            "expectancy": aggregate["expectancy"],  # compatibility alias
            "sharpe": aggregate["sharpe"],
            "sharpe_state": aggregate.get("sharpe_state"),
            "sortino": aggregate["sortino"],
            "sortino_state": aggregate.get("sortino_state"),
            "fold_stability": stability,
            "precomputed_signal_equal_weight_drawdown": aggregate["precomputed_signal_equal_weight_drawdown"],
            "max_drawdown": capital_simulation["max_drawdown"],
            "capital_constrained_portfolio_simulation": capital_simulation,
            "iid_bootstrap_mean_net_return_95ci": {"low": ci_low, "high": ci_high},
            "moving_block_bootstrap_mean_net_return_95ci": {"low": block_ci_low, "high": block_ci_high, "block_length": block_length},
            "hac_net_tstat": hac_net_t,
            "hac_excess_tstat": hac_excess_t,
            "daily_sharpe_for_dsr": daily_sharpe,
            "effective_sample_size": effective_n,
            "deflated_sharpe_probability": dsr,
            "multiple_test_adjusted_pvalue": adjusted_p,
            "cost_coverage": cost_coverage,
            "benchmark_coverage": benchmark_coverage,
            "baseline_coverage": baseline_coverage,
            "baseline_mean_excess": baseline_mean_excess,
            "complete_baselines": complete_baselines,
            "lineage_coverage": lineage_coverage,
            "official_nse_lineage_coverage": official_nse_lineage_coverage,
            "official_nse_complete_coverage": official_nse_complete_coverage,
            "optional_nse_enrichment_coverage": optional_nse_enrichment_coverage,
            "corporate_action_coverage": corporate_action_coverage,
            "survivorship_control_coverage": survivorship_control_coverage,
            "admission_coverage": admission_coverage,
            "session_lineage_coverage": session_lineage_coverage,
            "point_in_time_coverage": point_in_time_coverage,
            "feature_time_coverage": feature_time_coverage,
            "lookahead_violations": lookahead_violations,
            "feature_lookahead_violations": feature_lookahead_violations,
            "calibration": calibration,
            "gates": gates,
        }
        approval_id = hashlib.sha256(json.dumps(basis, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()[:24]
        result = dict(
            basis,
            approval_id=approval_id,
            status=status,
            approved=approved,
            validated_at=_now(),
            lifecycle_state=lifecycle,
            capital_authority=(
                "PAPER_SIMULATION"
                if approved and profile == CAPITAL_PROFILE and capital_model_training_proven
                and capital_simulation["proven"] is True else "NONE"
            ),
            policy=(
                "Precomputed signal-observation validation is not investable proof. Immutable historical OOF lineage or genuine prospective persisted prediction lineage may satisfy signal-generation provenance. "
                "Capital-profile approval authorizes paper simulation only; broker orders remain disabled."
            ),
        )
        if persist and self.store is not None:
            self._persist(result)
        return result

    def validate_capital(self, model_id: str, observations: Iterable[Dict[str, Any]], **kwargs: Any) -> Dict[str, Any]:
        kwargs["profile"] = CAPITAL_PROFILE
        return self.validate(model_id, observations, **kwargs)

    @staticmethod
    def _ensure_schema(conn: Any) -> None:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS validation_approvals (
          approval_id TEXT PRIMARY KEY, model_id TEXT NOT NULL, authority_version TEXT NOT NULL,
          status TEXT NOT NULL, lifecycle_state TEXT NOT NULL, validated_at TEXT NOT NULL,
          payload_json TEXT NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS ix_validation_model_time ON validation_approvals(model_id, validated_at);
        """)
        conn.commit()

    def _persist(self, result: Dict[str, Any]) -> None:
        with self.store.write_lock:
            self._ensure_schema(self.store.conn)
            self.store.conn.execute(
                "INSERT OR REPLACE INTO validation_approvals(approval_id,model_id,authority_version,status,lifecycle_state,validated_at,payload_json) VALUES(?,?,?,?,?,?,?)",
                (result["approval_id"], result["model_id"], AUTHORITY_VERSION, result["status"], result["lifecycle_state"], result["validated_at"], json.dumps(result, sort_keys=True, default=str)),
            )
            self.store.conn.commit()

    def status(self, model_id: str = "", profile: str = "") -> Dict[str, Any]:
        if self.store is None:
            return {"ok": True, "authority_version": AUTHORITY_VERSION, "approvals": []}
        self._ensure_schema(self.store.conn)
        if model_id:
            rows = self.store.conn.execute(
                "SELECT payload_json FROM validation_approvals WHERE model_id=? ORDER BY validated_at DESC",
                (model_id,),
            ).fetchall()
        else:
            rows = self.store.conn.execute(
                "SELECT payload_json FROM validation_approvals ORDER BY validated_at DESC LIMIT 100"
            ).fetchall()
        approvals = [json.loads(row[0]) for row in rows]
        if profile:
            approvals = [row for row in approvals if str(row.get("validation_profile") or RESEARCH_PROFILE).lower() == str(profile).lower()]
        return {"ok": True, "authority_version": AUTHORITY_VERSION, "approvals": approvals}
