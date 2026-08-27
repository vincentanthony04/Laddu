"""Governed Monte Carlo and GBM robustness diagnostics.

The service is intentionally a *validation* component, not a signal generator.
Historical/block-bootstrap and regime-conditioned simulations are primary.
A geometric-Brownian-motion path is included only as a transparent baseline and
is never allowed to authorize capital or tune production thresholds.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import random
import statistics
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


SIMULATION_VERSION = "simulation-robustness-1.0.0"
MIN_DIAGNOSTIC_SAMPLES = 30
MIN_RESEARCH_SAMPLES = 100
DEFAULT_PATHS = 2000
DEFAULT_HORIZON = 100


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _f(value: Any) -> Optional[float]:
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def _loads(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    try:
        parsed = json.loads(value or "{}")
        return dict(parsed) if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _quantile(values: Sequence[float], probability: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    if len(ordered) == 1:
        return ordered[0]
    p = min(1.0, max(0.0, float(probability)))
    position = p * (len(ordered) - 1)
    low = int(math.floor(position))
    high = int(math.ceil(position))
    if low == high:
        return ordered[low]
    weight = position - low
    return ordered[low] * (1.0 - weight) + ordered[high] * weight


def _drawdown(equity: Sequence[float]) -> float:
    if not equity:
        return 0.0
    peak = float(equity[0])
    worst = 0.0
    for value in equity:
        current = float(value)
        peak = max(peak, current)
        if peak > 0:
            worst = min(worst, current / peak - 1.0)
    return worst


def _path_metrics(path: Sequence[float], *, unit: str) -> Tuple[float, float]:
    if unit == "return":
        equity = [1.0]
        for value in path:
            equity.append(max(0.0, equity[-1] * (1.0 + max(-0.999999, float(value)))))
        return equity[-1] - 1.0, _drawdown(equity)
    equity = [0.0]
    for value in path:
        equity.append(equity[-1] + float(value))
    peak = equity[0]
    worst = 0.0
    for value in equity:
        peak = max(peak, value)
        worst = min(worst, value - peak)
    return equity[-1], worst


def _sample_block_path(values: Sequence[float], *, horizon: int, block_length: int,
                       rng: random.Random) -> List[float]:
    data = list(values)
    if not data or horizon <= 0:
        return []
    block = max(1, min(int(block_length), len(data)))
    result: List[float] = []
    while len(result) < horizon:
        start = rng.randrange(0, len(data))
        for offset in range(block):
            result.append(data[(start + offset) % len(data)])
            if len(result) >= horizon:
                break
    return result


def _weighted_choice(weights: Dict[str, int], rng: random.Random) -> str:
    total = sum(max(0, int(weight)) for weight in weights.values())
    if total <= 0:
        return next(iter(weights), "unknown")
    draw = rng.uniform(0, total)
    running = 0.0
    for key, weight in weights.items():
        running += max(0, int(weight))
        if draw <= running:
            return key
    return next(reversed(weights))


def _regime_path(values: Sequence[float], regimes: Sequence[str], *, horizon: int,
                 rng: random.Random) -> Optional[List[float]]:
    if len(values) != len(regimes) or len(values) < MIN_DIAGNOSTIC_SAMPLES:
        return None
    buckets: Dict[str, List[float]] = {}
    transitions: Dict[str, Dict[str, int]] = {}
    for value, regime in zip(values, regimes):
        key = str(regime or "unknown").strip().lower() or "unknown"
        buckets.setdefault(key, []).append(float(value))
    usable = {key: bucket for key, bucket in buckets.items() if len(bucket) >= 5}
    if len(usable) < 2:
        return None
    cleaned = [str(regime or "unknown").strip().lower() or "unknown" for regime in regimes]
    for left, right in zip(cleaned, cleaned[1:]):
        if left in usable and right in usable:
            transitions.setdefault(left, {})[right] = transitions.setdefault(left, {}).get(right, 0) + 1
    frequencies = {key: len(bucket) for key, bucket in usable.items()}
    state = _weighted_choice(frequencies, rng)
    result: List[float] = []
    for _ in range(horizon):
        bucket = usable[state]
        result.append(bucket[rng.randrange(len(bucket))])
        options = transitions.get(state) or frequencies
        state = _weighted_choice(options, rng)
    return result


def _summary(terminals: Sequence[float], drawdowns: Sequence[float], *, unit: str) -> Dict[str, Any]:
    if not terminals:
        return {"state": "NOT_RUN", "paths": 0}
    threshold = -0.20 if unit == "return" else None
    result: Dict[str, Any] = {
        "state": "MEASURED",
        "paths": len(terminals),
        "terminal": {
            "p05": _quantile(terminals, 0.05),
            "p25": _quantile(terminals, 0.25),
            "median": _quantile(terminals, 0.50),
            "p75": _quantile(terminals, 0.75),
            "p95": _quantile(terminals, 0.95),
            "mean": statistics.fmean(terminals),
            "probability_of_loss": sum(value < 0 for value in terminals) / len(terminals),
            "expected_shortfall_5pct": statistics.fmean(sorted(terminals)[:max(1, int(math.ceil(len(terminals) * 0.05)))]),
        },
        "drawdown": {
            "p05_worst": _quantile(drawdowns, 0.05),
            "median": _quantile(drawdowns, 0.50),
            "p95_best": _quantile(drawdowns, 0.95),
            "mean": statistics.fmean(drawdowns),
        },
    }
    if threshold is not None:
        result["drawdown"]["probability_below_minus_20pct"] = sum(value <= threshold for value in drawdowns) / len(drawdowns)
    return result


@dataclass(frozen=True)
class SeriesEvidence:
    values: Tuple[float, ...]
    regimes: Tuple[str, ...]
    unit: str
    source: str
    desk: str
    normalized_coverage: float


class SimulationRobustnessService:
    """Run reproducible scenario diagnostics without modifying production."""

    RETURN_KEYS = ("net_return", "return_pct", "net_return_pct", "pnl_return", "forward_net_return")
    R_KEYS = ("r_multiple", "pnl_r", "net_r", "realized_r")

    def __init__(self, store: Any = None):
        self.store = store

    @classmethod
    def _extract_row(cls, row: Dict[str, Any]) -> Tuple[Optional[float], Optional[str], str]:
        features = _loads(row.get("feature_json"))
        proof = _loads(row.get("proof_json"))
        merged = dict(features)
        merged.update({key: value for key, value in proof.items() if value is not None})
        regime = str(merged.get("regime") or "unknown")
        for key in cls.RETURN_KEYS:
            value = _f(merged.get(key))
            if value is not None:
                # Percent-labelled values above 1 in magnitude are converted to decimals.
                if "pct" in key and abs(value) > 1.0:
                    value /= 100.0
                return value, "return", regime
        for key in cls.R_KEYS:
            value = _f(merged.get(key))
            if value is not None:
                return value, "r_multiple", regime
        value = _f(row.get("pnl_points"))
        return value, "points" if value is not None else None, regime

    def evidence(self, *, desk: str = "all") -> SeriesEvidence:
        rows: List[Dict[str, Any]] = []
        if self.store is not None:
            try:
                rows = [dict(row) for row in (self.store.outcome_learning_rows(limit=5000) or [])]
            except Exception:
                try:
                    raw = self.store.conn.execute(
                        "SELECT * FROM outcome_learning ORDER BY COALESCE(closed_at,created_at) ASC LIMIT 5000"
                    ).fetchall()
                    rows = [dict(row) for row in raw]
                except Exception:
                    rows = []
        desk_key = str(desk or "all").lower()
        if desk_key in {"intraday", "delivery"}:
            rows = [row for row in rows if str(row.get("mode") or "").lower() == desk_key]
        rows.sort(key=lambda row: str(row.get("closed_at") or row.get("created_at") or ""))
        extracted = [self._extract_row(row) for row in rows]
        normalized = [(value, unit, regime) for value, unit, regime in extracted if value is not None and unit in {"return", "r_multiple"}]
        if normalized:
            # Prefer percentage returns over R multiples when both are present.
            preferred = "return" if any(unit == "return" for _, unit, _ in normalized) else "r_multiple"
            chosen = [(value, regime) for value, unit, regime in normalized if unit == preferred]
            coverage = len(chosen) / len(rows) if rows else 0.0
            return SeriesEvidence(tuple(value for value, _ in chosen), tuple(regime for _, regime in chosen), preferred,
                                  "outcome_learning", desk_key, coverage)
        points = [(value, regime) for value, unit, regime in extracted if value is not None and unit == "points"]
        coverage = len(points) / len(rows) if rows else 0.0
        return SeriesEvidence(tuple(value for value, _ in points), tuple(regime for _, regime in points), "points",
                              "outcome_learning", desk_key, coverage)

    @staticmethod
    def simulate(values: Sequence[float], *, regimes: Optional[Sequence[str]] = None,
                 unit: str = "return", paths: int = DEFAULT_PATHS,
                 horizon: int = DEFAULT_HORIZON, seed: int = 7,
                 block_length: Optional[int] = None) -> Dict[str, Any]:
        data = [float(value) for value in values if _f(value) is not None]
        n = len(data)
        paths = max(100, min(int(paths), 20000))
        horizon = max(1, min(int(horizon), 1000))
        block = max(2, min(int(block_length or max(2, round(math.sqrt(max(1, n))))), max(2, n))) if n else 2
        if n < MIN_DIAGNOSTIC_SAMPLES:
            return {
                "state": "INSUFFICIENT_SAMPLE",
                "samples": n,
                "minimum_samples": MIN_DIAGNOSTIC_SAMPLES,
                "unit": unit,
                "production_change_allowed": False,
                "capital_authority": "NONE",
            }
        rng = random.Random(int(seed))
        iid_terminal: List[float] = []
        iid_dd: List[float] = []
        block_terminal: List[float] = []
        block_dd: List[float] = []
        regime_terminal: List[float] = []
        regime_dd: List[float] = []
        for _ in range(paths):
            iid = [data[rng.randrange(n)] for _ in range(horizon)]
            terminal, drawdown = _path_metrics(iid, unit=unit)
            iid_terminal.append(terminal); iid_dd.append(drawdown)
            blocked = _sample_block_path(data, horizon=horizon, block_length=block, rng=rng)
            terminal, drawdown = _path_metrics(blocked, unit=unit)
            block_terminal.append(terminal); block_dd.append(drawdown)
            if regimes:
                regime_path = _regime_path(data, list(regimes), horizon=horizon, rng=rng)
                if regime_path is not None:
                    terminal, drawdown = _path_metrics(regime_path, unit=unit)
                    regime_terminal.append(terminal); regime_dd.append(drawdown)

        gbm: Dict[str, Any] = {
            "state": "UNAVAILABLE",
            "role": "secondary_baseline_only",
            "reason": "GBM requires normalized percentage returns; point or R-multiple series are not prices.",
        }
        if unit == "return" and all(value > -1.0 for value in data):
            logs = [math.log1p(value) for value in data]
            mu = statistics.fmean(logs)
            sigma = statistics.stdev(logs) if len(logs) > 1 else 0.0
            gbm_terminal: List[float] = []
            gbm_dd: List[float] = []
            for _ in range(paths):
                path = [math.expm1(rng.gauss(mu, sigma)) for _ in range(horizon)]
                terminal, drawdown = _path_metrics(path, unit="return")
                gbm_terminal.append(terminal); gbm_dd.append(drawdown)
            gbm = _summary(gbm_terminal, gbm_dd, unit="return")
            gbm.update({
                "role": "secondary_baseline_only",
                "estimated_log_drift_per_step": mu,
                "estimated_log_volatility_per_step": sigma,
                "production_signal_allowed": False,
            })

        state = "RESEARCH_DIAGNOSTIC" if n >= MIN_RESEARCH_SAMPLES and unit != "points" else "DIAGNOSTIC_ONLY"
        return {
            "state": state,
            "samples": n,
            "paths": paths,
            "horizon_steps": horizon,
            "unit": unit,
            "seed": int(seed),
            "block_length": block,
            "iid_bootstrap": _summary(iid_terminal, iid_dd, unit=unit),
            "moving_block_bootstrap": _summary(block_terminal, block_dd, unit=unit),
            "regime_conditioned": _summary(regime_terminal, regime_dd, unit=unit) if regime_terminal else {
                "state": "UNAVAILABLE",
                "reason": "At least two regimes with five observations each are required.",
            },
            "gbm_baseline": gbm,
            "production_change_allowed": False,
            "capital_authority": "NONE",
            "policy": "Historical/block and regime simulations are primary. GBM is a secondary baseline and cannot create or approve signals.",
        }

    def status(self, *, desk: str = "all", paths: int = DEFAULT_PATHS,
               horizon: int = DEFAULT_HORIZON, seed: Optional[int] = None) -> Dict[str, Any]:
        evidence = self.evidence(desk=desk)
        fingerprint = hashlib.sha256(json.dumps({
            "values": evidence.values,
            "regimes": evidence.regimes,
            "unit": evidence.unit,
            "desk": evidence.desk,
        }, sort_keys=True, default=str).encode()).hexdigest()[:16]
        effective_seed = int(seed) if seed is not None else int(fingerprint[:8], 16)
        result = self.simulate(evidence.values, regimes=evidence.regimes, unit=evidence.unit,
                               paths=paths, horizon=horizon, seed=effective_seed)
        return {
            "ok": True,
            "simulation_version": SIMULATION_VERSION,
            "as_of": _now(),
            "desk": evidence.desk,
            "source": evidence.source,
            "series_fingerprint": fingerprint,
            "normalized_coverage": evidence.normalized_coverage,
            "evidence_state": "NORMALIZED" if evidence.unit in {"return", "r_multiple"} else "POINTS_ONLY",
            "result": result,
            "governance": {
                "shadow_only": True,
                "automatic_threshold_changes": False,
                "automatic_weight_changes": False,
                "capital_authority": "NONE",
                "full_universe_replay_required": True,
            },
        }
