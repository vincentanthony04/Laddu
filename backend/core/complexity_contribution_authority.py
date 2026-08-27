"""Paired same-population proof that model complexity adds value over mathematics.

This authority is deliberately research-only.  It compares selector arms on the
same immutable candidate populations and tests the *incremental* post-cost edge
of a challenger over the deterministic mathematical baseline.  It never trains,
changes weights, promotes a model, or grants broker/capital authority.
"""
from __future__ import annotations

import hashlib
import math
import random
import statistics
from typing import Any, Iterable, Mapping, Sequence


AUTHORITY = "PAIRED_SAME_POPULATION_COMPLEXITY_CONTRIBUTION"
AUTHORITY_VERSION = "complexity-contribution-authority-1.0.0"


def _finite(value: Any) -> float | None:
    try:
        out = float(value)
        return out if math.isfinite(out) else None
    except (TypeError, ValueError):
        return None


def _population_key(row: Mapping[str, Any]) -> str:
    return str(
        row.get("population_fingerprint")
        or row.get("population_id")
        or str(row.get("observed_at") or "")[:19]
        or ""
    ).strip()


def _candidate_key(row: Mapping[str, Any]) -> str:
    return str(row.get("candidate_id") or row.get("prediction_id") or row.get("symbol") or "").strip()


def _selected_mean(rows: Sequence[Mapping[str, Any]], top_fraction: float) -> float | None:
    eligible = []
    for row in rows:
        realised = _finite(row.get("net_return_bps"))
        if realised is None:
            realised_raw = _finite(row.get("realised_return_net"))
            realised = realised_raw * 10000.0 if realised_raw is not None else None
        if realised is None:
            continue
        rank = _finite(row.get("rank"))
        score = _finite(row.get("score"))
        if score is None:
            score = _finite(row.get("predicted_rank"))
        # rank=1 is best when a governed rank exists.  Otherwise larger score is best.
        order_key = (0, rank) if rank is not None else (1, -(score if score is not None else -1e18))
        eligible.append((order_key, realised))
    if not eligible:
        return None
    eligible.sort(key=lambda item: item[0])
    top_n = max(1, math.ceil(len(eligible) * float(top_fraction)))
    return statistics.fmean(item[1] for item in eligible[:top_n])


def _quantile(values: Sequence[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = max(0.0, min(1.0, float(q))) * (len(ordered) - 1)
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _moving_block_bootstrap_lower(
    values: Sequence[float], *, runs: int, alpha: float, seed_material: str
) -> tuple[float | None, int, int]:
    data = list(values)
    n = len(data)
    if n < 3:
        return None, 0, 0
    block = max(2, min(n, int(round(math.sqrt(n)))))
    seed = int(hashlib.sha256(seed_material.encode("utf-8", errors="replace")).hexdigest()[:16], 16)
    rng = random.Random(seed)
    simulations: list[float] = []
    total_runs = max(500, int(runs))
    for _ in range(total_runs):
        sampled: list[float] = []
        while len(sampled) < n:
            start = rng.randrange(n)
            for offset in range(block):
                sampled.append(data[(start + offset) % n])
                if len(sampled) >= n:
                    break
        simulations.append(statistics.fmean(sampled))
    return _quantile(simulations, alpha), total_runs, block


class ComplexityContributionAuthority:
    """Evaluate incremental challenger value on frozen matched populations."""

    authority = AUTHORITY
    version = AUTHORITY_VERSION
    MIN_MATCHED_POPULATIONS = 30
    MIN_POSITIVE_POPULATION_FRACTION = 0.55
    MIN_REGIME_POPULATIONS = 5
    MIN_POSITIVE_REGIME_FRACTION = 2.0 / 3.0
    BOOTSTRAP_ALPHA = 0.05

    @classmethod
    def evaluate(
        cls,
        rows: Iterable[Mapping[str, Any]],
        *,
        baseline_arm: str = "heuristic",
        challenger_arm: str = "hybrid",
        top_fraction: float = 0.20,
        bootstrap_runs: int = 1000,
        seed_material: str = "project-laddu-complexity-contribution",
    ) -> dict[str, Any]:
        data = [dict(row) for row in rows]
        baseline_key = str(baseline_arm).strip().lower()
        challenger_key = str(challenger_arm).strip().lower()
        fraction = float(top_fraction)
        if not 0 < fraction <= 1:
            raise ValueError("top_fraction must be in (0, 1]")
        if baseline_key == challenger_key:
            raise ValueError("baseline_arm and challenger_arm must differ")

        populations: dict[str, dict[str, list[dict[str, Any]]]] = {}
        for row in data:
            arm = str(row.get("arm") or "").strip().lower()
            if arm not in {baseline_key, challenger_key}:
                continue
            pop = _population_key(row)
            if not pop:
                continue
            populations.setdefault(pop, {}).setdefault(arm, []).append(row)

        matched: list[dict[str, Any]] = []
        mismatched = 0
        for pop, arms in populations.items():
            base_rows = arms.get(baseline_key) or []
            challenger_rows = arms.get(challenger_key) or []
            if not base_rows or not challenger_rows:
                mismatched += 1
                continue
            base_ids = {_candidate_key(row) for row in base_rows}
            challenger_ids = {_candidate_key(row) for row in challenger_rows}
            if not base_ids or base_ids != challenger_ids:
                mismatched += 1
                continue
            base_mean = _selected_mean(base_rows, fraction)
            challenger_mean = _selected_mean(challenger_rows, fraction)
            if base_mean is None or challenger_mean is None:
                mismatched += 1
                continue
            observed = min(
                [str(row.get("observed_at") or "") for row in base_rows + challenger_rows if row.get("observed_at")]
                or [pop]
            )
            regime = str(
                next((row.get("market_regime") for row in challenger_rows if row.get("market_regime")), "UNKNOWN")
                or "UNKNOWN"
            ).upper()
            matched.append({
                "population": pop,
                "observed_at": observed,
                "regime": regime,
                "baseline_top_mean_bps": base_mean,
                "challenger_top_mean_bps": challenger_mean,
                "incremental_bps": challenger_mean - base_mean,
            })

        matched.sort(key=lambda row: (row["observed_at"], row["population"]))
        deltas = [float(row["incremental_bps"]) for row in matched]
        lower, runs, block = _moving_block_bootstrap_lower(
            deltas,
            runs=bootstrap_runs,
            alpha=cls.BOOTSTRAP_ALPHA,
            seed_material=f"{seed_material}:{baseline_key}:{challenger_key}:{len(deltas)}",
        )
        mean_delta = statistics.fmean(deltas) if deltas else None
        median_delta = statistics.median(deltas) if deltas else None
        positive_fraction = sum(value > 0 for value in deltas) / len(deltas) if deltas else 0.0

        regime_values: dict[str, list[float]] = {}
        for row in matched:
            regime_values.setdefault(str(row["regime"]), []).append(float(row["incremental_bps"]))
        regime_rows = []
        qualifying_regimes = 0
        positive_regimes = 0
        for regime, values in sorted(regime_values.items()):
            mean_value = statistics.fmean(values)
            qualifies = len(values) >= cls.MIN_REGIME_POPULATIONS
            qualifying_regimes += int(qualifies)
            positive_regimes += int(qualifies and mean_value > 0)
            regime_rows.append({
                "regime": regime,
                "population_count": len(values),
                "mean_incremental_bps": round(mean_value, 6),
                "qualifies_for_robustness": qualifies,
                "positive_increment": mean_value > 0,
            })
        positive_regime_fraction = (
            positive_regimes / qualifying_regimes if qualifying_regimes else 0.0
        )
        regime_robust = bool(
            qualifying_regimes >= 3
            and positive_regime_fraction >= cls.MIN_POSITIVE_REGIME_FRACTION
        )
        exact_population_match = mismatched == 0 and bool(matched)
        passed = bool(
            exact_population_match
            and len(matched) >= cls.MIN_MATCHED_POPULATIONS
            and mean_delta is not None and mean_delta > 0
            and lower is not None and lower > 0
            and positive_fraction >= cls.MIN_POSITIVE_POPULATION_FRACTION
            and regime_robust
        )
        return {
            "ok": True,
            "state": "PASSED" if passed else "FAILED" if matched else "INSUFFICIENT_EVIDENCE",
            "passed": passed,
            "authority": cls.authority,
            "authority_version": cls.version,
            "baseline_arm": baseline_key,
            "challenger_arm": challenger_key,
            "top_fraction": fraction,
            "matched_population_count": len(matched),
            "mismatched_population_count": mismatched,
            "exact_population_match": exact_population_match,
            "mean_incremental_bps": round(mean_delta, 6) if mean_delta is not None else None,
            "median_incremental_bps": round(float(median_delta), 6) if median_delta is not None else None,
            "positive_population_fraction": round(positive_fraction, 6),
            "bootstrap_lower_95_incremental_bps": round(float(lower), 6) if lower is not None else None,
            "bootstrap_runs": runs,
            "bootstrap_block_populations": block,
            "regime_robust": regime_robust,
            "qualifying_regime_count": qualifying_regimes,
            "positive_regime_fraction": round(positive_regime_fraction, 6),
            "by_regime": regime_rows,
            "policy": {
                "minimum_matched_populations": cls.MIN_MATCHED_POPULATIONS,
                "minimum_positive_population_fraction": cls.MIN_POSITIVE_POPULATION_FRACTION,
                "minimum_qualifying_regimes": 3,
                "minimum_populations_per_regime": cls.MIN_REGIME_POPULATIONS,
                "minimum_positive_regime_fraction": cls.MIN_POSITIVE_REGIME_FRACTION,
                "bootstrap_lower_95_must_be_positive": True,
                "same_candidate_population_required": True,
                "automatic_production_mutation": False,
                "broker_authority": "NONE",
            },
            "interpretation": (
                "The challenger must add positive post-cost top-band edge over the deterministic "
                "mathematical baseline on the same frozen populations; absolute challenger profitability alone is insufficient."
            ),
        }


DEFAULT_COMPLEXITY_CONTRIBUTION_AUTHORITY = ComplexityContributionAuthority()
