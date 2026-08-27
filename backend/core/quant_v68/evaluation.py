from __future__ import annotations

"""Point-in-time evaluation for frozen cross-sectional predictions.

Ranking metrics are computed *inside each frozen ranking population* and then
aggregated. Portfolio risk metrics are computed from one top-band net return per
population, never from an unordered mixture of individual stock outcomes. This
avoids the two common errors of global cross-sectional rank correlation and a
Sharpe ratio over individual candidate rows.
"""

from dataclasses import dataclass
import hashlib
import math
import random
from statistics import NormalDist, mean, pstdev
from typing import Any, Iterable, Mapping, Sequence


def _finite(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
        return out if math.isfinite(out) else default
    except (TypeError, ValueError):
        return default


def _rank(values: Sequence[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda i: (values[i], i))
    ranks = [0.0] * len(values)
    pos = 0
    while pos < len(order):
        end = pos + 1
        while end < len(order) and values[order[end]] == values[order[pos]]:
            end += 1
        average_rank = (pos + 1 + end) / 2.0
        for j in range(pos, end):
            ranks[order[j]] = average_rank
        pos = end
    return ranks


def _pearson(x: Sequence[float], y: Sequence[float]) -> float:
    if len(x) != len(y) or len(x) < 2:
        return 0.0
    mx, my = mean(x), mean(y)
    sx = sum((v - mx) ** 2 for v in x)
    sy = sum((v - my) ** 2 for v in y)
    if sx <= 0 or sy <= 0:
        return 0.0
    return sum((a - mx) * (b - my) for a, b in zip(x, y)) / math.sqrt(sx * sy)


def spearman_rank_ic(predicted: Sequence[float], realised: Sequence[float]) -> float:
    return _pearson(_rank(list(predicted)), _rank(list(realised)))


def ndcg_at_k(predicted: Sequence[float], realised: Sequence[float], k: int | None = None) -> float:
    if len(predicted) != len(realised) or not predicted:
        return 0.0
    k = min(len(predicted), max(1, int(k or len(predicted))))
    floor = min(realised)
    relevance = [max(0.0, value - floor) for value in realised]
    predicted_order = sorted(range(len(predicted)), key=lambda i: predicted[i], reverse=True)[:k]
    ideal_order = sorted(range(len(realised)), key=lambda i: realised[i], reverse=True)[:k]

    def dcg(order: Sequence[int]) -> float:
        return sum((2.0 ** min(20.0, relevance[i]) - 1.0) / math.log2(rank + 2.0) for rank, i in enumerate(order))

    ideal = dcg(ideal_order)
    return dcg(predicted_order) / ideal if ideal > 0 else 0.0


def expected_calibration_error(probabilities: Sequence[float], outcomes: Sequence[float], bins: int = 10) -> float:
    if len(probabilities) != len(outcomes) or not probabilities:
        return 1.0
    total = len(probabilities)
    error = 0.0
    bins = max(1, int(bins))
    for index in range(bins):
        low, high = index / bins, (index + 1) / bins
        members = [i for i, p in enumerate(probabilities) if low <= p < high or (index == bins - 1 and p == 1.0)]
        if not members:
            continue
        confidence = mean(probabilities[i] for i in members)
        accuracy = mean(outcomes[i] for i in members)
        error += len(members) / total * abs(confidence - accuracy)
    return error


def _drawdown(returns: Sequence[float]) -> float:
    equity = 1.0
    peak = 1.0
    worst = 0.0
    for value in returns:
        equity *= max(1e-9, 1.0 + value)
        peak = max(peak, equity)
        worst = min(worst, equity / peak - 1.0)
    return worst


def _cvar_95(returns: Sequence[float]) -> float:
    if not returns:
        return 0.0
    ordered = sorted(returns)
    count = max(1, math.ceil(len(ordered) * 0.05))
    return mean(ordered[:count])


def _lower_confidence_mean(values: Sequence[float], confidence: float = 0.95) -> float:
    if len(values) < 2:
        return values[0] if values else 0.0
    standard_error = pstdev(values) / math.sqrt(len(values))
    z = NormalDist().inv_cdf(0.5 + confidence / 2.0)
    return mean(values) - z * standard_error


def _population_key(row: Mapping[str, Any], index: int) -> str:
    value = row.get("population_id") or row.get("ranking_population_id") or row.get("as_of")
    # Backward-compatible unit fixtures without population identity represent
    # one frozen population, not N unrelated periods.
    return str(value) if value not in (None, "") else "__single_population__"


def _score(row: Mapping[str, Any]) -> float:
    value = row.get("predicted_rank")
    return _finite(value if value is not None else row.get("predicted_percentile"))


def _weighted_mean(pairs: Sequence[tuple[float, int]]) -> float:
    weight = sum(max(0, item[1]) for item in pairs)
    return sum(value * max(0, n) for value, n in pairs) / weight if weight else 0.0


@dataclass(frozen=True)
class EvaluationMetric:
    regime_label: str
    liquidity_band: str
    market_cap_band: str
    sample_size: int
    population_count: int
    rank_ic: float
    ndcg: float
    brier_score: float
    calibration_error: float
    net_expectancy: float
    sharpe: float
    sortino: float
    max_drawdown: float
    cvar_95: float
    turnover: float
    capacity_inr: float
    lower_confidence_net_expectancy: float

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def evaluate_prediction_rows(
    rows: Iterable[Mapping[str, Any]],
    *,
    regime_label: str,
    liquidity_band: str = "ALL",
    market_cap_band: str = "ALL",
    periods_per_year: float = 252.0,
    top_fraction: float = 0.20,
) -> EvaluationMetric:
    data = [dict(row) for row in rows]
    if not data:
        raise ValueError("evaluation population is empty")
    periods_per_year = float(periods_per_year)
    if not math.isfinite(periods_per_year) or periods_per_year <= 0:
        raise ValueError("periods_per_year must be positive and explicit")
    top_fraction = float(top_fraction)
    if not 0 < top_fraction <= 1:
        raise ValueError("top_fraction must be in (0, 1]")

    probabilities = [min(1.0, max(0.0, _finite(row.get("target_before_stop_probability"), 0.5))) for row in data]
    binary = [1.0 if str(row.get("outcome_class") or "").upper() == "TARGET_FIRST" else 0.0 for row in data]
    brier = mean((p - y) ** 2 for p, y in zip(probabilities, binary))

    populations: dict[str, list[dict[str, Any]]] = {}
    for index, row in enumerate(data):
        populations.setdefault(_population_key(row, index), []).append(row)

    rank_pairs: list[tuple[float, int]] = []
    ndcg_pairs: list[tuple[float, int]] = []
    population_returns: list[float] = []
    selected_rows: list[dict[str, Any]] = []
    for members in populations.values():
        scores = [_score(row) for row in members]
        realised = [_finite(row.get("realised_return_net")) for row in members]
        if len(members) >= 2:
            rank_pairs.append((spearman_rank_ic(scores, realised), len(members)))
            ndcg_pairs.append((ndcg_at_k(scores, realised, k=max(1, math.ceil(len(members) * top_fraction))), len(members)))
        top_count = max(1, math.ceil(len(members) * top_fraction))
        order = sorted(range(len(members)), key=lambda i: scores[i], reverse=True)[:top_count]
        chosen = [members[i] for i in order]
        selected_rows.extend(chosen)
        population_returns.append(mean(_finite(row.get("realised_return_net")) for row in chosen))

    avg = mean(population_returns)
    sigma = pstdev(population_returns) if len(population_returns) > 1 else 0.0
    downside = [min(0.0, value) for value in population_returns]
    downside_sigma = math.sqrt(mean(value * value for value in downside)) if downside else 0.0
    annualiser = math.sqrt(periods_per_year)
    sharpe = avg / sigma * annualiser if sigma > 0 else 0.0
    sortino = avg / downside_sigma * annualiser if downside_sigma > 0 else 0.0

    capacities = [
        _finite(row.get("capacity_inr"), float("nan"))
        for row in selected_rows
        if row.get("capacity_inr") not in (None, "")
    ]
    capacities = [value for value in capacities if math.isfinite(value)]
    return EvaluationMetric(
        regime_label=str(regime_label).upper(),
        liquidity_band=str(liquidity_band).upper(),
        market_cap_band=str(market_cap_band).upper(),
        sample_size=len(data),
        population_count=len(populations),
        rank_ic=_weighted_mean(rank_pairs),
        ndcg=_weighted_mean(ndcg_pairs),
        brier_score=brier,
        calibration_error=expected_calibration_error(probabilities, binary),
        net_expectancy=avg,
        sharpe=sharpe,
        sortino=sortino,
        max_drawdown=_drawdown(population_returns),
        cvar_95=_cvar_95(population_returns),
        turnover=mean(_finite(row.get("turnover")) for row in selected_rows),
        capacity_inr=min(capacities) if capacities else 0.0,
        lower_confidence_net_expectancy=_lower_confidence_mean(population_returns),
    )


def permutation_null_alpha_test(
    rows: Iterable[Mapping[str, Any]],
    *,
    top_fraction: float = 0.20,
    permutations: int = 500,
    alpha: float = 0.05,
    seed_material: str = "project-laddu-null-alpha",
) -> dict[str, Any]:
    """Structure-preserving falsification for cross-sectional rank alpha.

    Realised net returns are shuffled *within each frozen ranking population*
    while predictions, population sizes and return distributions stay fixed.
    Promotion requires both aggregate RankIC and selected-top-band expectancy to
    beat this null.  The seed is deterministic so identical evidence produces an
    identical governance decision.
    """
    data = [dict(row) for row in rows]
    top_fraction = float(top_fraction)
    if not 0 < top_fraction <= 1:
        raise ValueError("top_fraction must be in (0, 1]")
    population_map: dict[str, list[tuple[float, float]]] = {}
    for index, row in enumerate(data):
        realised = _finite(row.get("realised_return_net"), float("nan"))
        score = _score(row)
        if not math.isfinite(realised) or not math.isfinite(score):
            continue
        population_map.setdefault(_population_key(row, index), []).append((score, realised))
    populations = [members for members in population_map.values() if len(members) >= 2]
    sample_size = sum(len(members) for members in populations)
    if sample_size < 30 or len(populations) < 3:
        return {
            "passed": False,
            "state": "INSUFFICIENT_EVIDENCE",
            "sample_size": sample_size,
            "population_count": len(populations),
            "permutations": 0,
            "rank_ic": None,
            "rank_ic_pvalue": 1.0,
            "top_band_expectancy": None,
            "top_band_expectancy_pvalue": 1.0,
            "alpha": float(alpha),
            "policy": "within-population realised-return permutation must reject the no-ranking-skill null",
        }

    def metrics(population_values: Sequence[Sequence[tuple[float, float]]]) -> tuple[float, float]:
        rank_pairs: list[tuple[float, int]] = []
        top_returns: list[float] = []
        for members in population_values:
            scores = [item[0] for item in members]
            realised = [item[1] for item in members]
            rank_pairs.append((spearman_rank_ic(scores, realised), len(members)))
            top_count = max(1, math.ceil(len(members) * top_fraction))
            selected = sorted(range(len(members)), key=lambda idx: scores[idx], reverse=True)[:top_count]
            top_returns.append(mean(realised[idx] for idx in selected))
        return _weighted_mean(rank_pairs), mean(top_returns) if top_returns else 0.0

    observed_rank_ic, observed_expectancy = metrics(populations)
    runs = max(200, int(permutations))
    seed = int(hashlib.sha256(str(seed_material).encode("utf-8", errors="replace")).hexdigest()[:16], 16)
    rng = random.Random(seed)
    rank_extreme = 0
    expectancy_extreme = 0
    for _ in range(runs):
        permuted: list[list[tuple[float, float]]] = []
        for members in populations:
            scores = [item[0] for item in members]
            realised = [item[1] for item in members]
            rng.shuffle(realised)
            permuted.append(list(zip(scores, realised)))
        null_rank_ic, null_expectancy = metrics(permuted)
        rank_extreme += int(null_rank_ic >= observed_rank_ic)
        expectancy_extreme += int(null_expectancy >= observed_expectancy)
    rank_p = (rank_extreme + 1.0) / (runs + 1.0)
    expectancy_p = (expectancy_extreme + 1.0) / (runs + 1.0)
    passed = bool(
        observed_rank_ic > 0
        and observed_expectancy > 0
        and rank_p <= float(alpha)
        and expectancy_p <= float(alpha)
    )
    return {
        "passed": passed,
        "state": "PASSED" if passed else "FAILED",
        "sample_size": sample_size,
        "population_count": len(populations),
        "permutations": runs,
        "rank_ic": observed_rank_ic,
        "rank_ic_pvalue": rank_p,
        "top_band_expectancy": observed_expectancy,
        "top_band_expectancy_pvalue": expectancy_p,
        "alpha": float(alpha),
        "seed_sha256": hashlib.sha256(str(seed_material).encode("utf-8", errors="replace")).hexdigest(),
        "policy": "within-population realised-return permutation must reject the no-ranking-skill null for RankIC and top-band net expectancy",
    }


def evaluate_regime_strata(
    rows: Iterable[Mapping[str, Any]],
    *,
    periods_per_year: float = 252.0,
    top_fraction: float = 0.20,
) -> list[EvaluationMetric]:
    data = [dict(row) for row in rows]
    if not data:
        return []
    output = [evaluate_prediction_rows(
        data,
        regime_label="ALL",
        periods_per_year=periods_per_year,
        top_fraction=top_fraction,
    )]
    by_regime: dict[str, list[dict[str, Any]]] = {}
    for row in data:
        regime = str(row.get("regime_label") or "UNKNOWN").upper()
        by_regime.setdefault(regime, []).append(row)
    for regime, members in sorted(by_regime.items()):
        if regime in {"BULL", "BEAR", "VOLATILE", "RANGE", "SECTOR_ROTATION"}:
            output.append(evaluate_prediction_rows(
                members,
                regime_label=regime,
                periods_per_year=periods_per_year,
                top_fraction=top_fraction,
            ))
    return output
