"""Outcome ledger and evidence report for the three-arm selector platform.

The service compares the frozen heuristic baseline, NSE quantitative challenger
and hybrid challenger against the same immutable candidate observations.  It is
research-only: reports never change production authority or selector weights.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
import statistics
import threading
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from core.expectancy_semantics_authority import lane as expectancy_lane
from core.selection_platform_service import SelectionPlatformService
from core.forward_horizon_policy import canonical_horizon, normalise_desk
from core.forward_evidence_eligibility import classify_forward_evidence
from core.complexity_contribution_authority import DEFAULT_COMPLEXITY_CONTRIBUTION_AUTHORITY

VALIDATION_VERSION = "selection-research-validation-1.1.0-complexity-contribution"
PRIMARY_AMBIGUITY_POLICY = "SAME_BAR_STOP_FIRST_PRIMARY"
VALID_RESULTS = {"SUCCESS", "FAIL", "BREAKEVEN", "EXPIRED", "INVALIDATED", "STOP_FIRST"}
COST_STRESS_BPS = (0.0, 5.0, 10.0, 20.0)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _sha(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8", errors="replace")).hexdigest()


def _float(value: Any) -> Optional[float]:
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def _average_ranks(values: Sequence[float]) -> List[float]:
    indexed = sorted(enumerate(values), key=lambda item: item[1])
    ranks = [0.0] * len(values)
    cursor = 0
    while cursor < len(indexed):
        end = cursor + 1
        while end < len(indexed) and indexed[end][1] == indexed[cursor][1]:
            end += 1
        average = (cursor + 1 + end) / 2.0
        for pos in range(cursor, end):
            ranks[indexed[pos][0]] = average
        cursor = end
    return ranks


def _pearson(x: Sequence[float], y: Sequence[float]) -> Optional[float]:
    if len(x) != len(y) or len(x) < 3:
        return None
    mx, my = statistics.fmean(x), statistics.fmean(y)
    dx = [value - mx for value in x]
    dy = [value - my for value in y]
    denom = math.sqrt(sum(value * value for value in dx) * sum(value * value for value in dy))
    if denom <= 0:
        return None
    return sum(a * b for a, b in zip(dx, dy)) / denom


def _spearman(scores: Sequence[float], outcomes: Sequence[float]) -> Optional[float]:
    value = _pearson(_average_ranks(scores), _average_ranks(outcomes))
    return round(value, 6) if value is not None else None


def _median(values: Sequence[float]) -> Optional[float]:
    return round(float(statistics.median(values)), 6) if values else None


def _profit_factor(values: Sequence[float]) -> Optional[float]:
    wins = sum(value for value in values if value > 0)
    losses = abs(sum(value for value in values if value < 0))
    if losses:
        return round(wins / losses, 6)
    if wins:
        return 999.0
    return 0.0 if values else None


def _probability_evidence(rows: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    pairs = []
    for row in rows:
        probability = _float(row.get("probability_positive"))
        if probability is None or not 0.0 <= probability <= 1.0:
            continue
        label = 1.0 if float(row["net_return_bps"]) > 0 else 0.0
        pairs.append((probability, label))
    if not rows or len(pairs) != len(rows):
        return {
            "state": "UNAVAILABLE",
            "observations": len(pairs),
            "required_rows": len(rows),
            "reason": "Every settled shadow row requires a bounded probability estimate.",
        }
    brier = statistics.fmean((probability - label) ** 2 for probability, label in pairs)
    prevalence = statistics.fmean(label for _probability, label in pairs)
    baseline_brier = statistics.fmean((prevalence - label) ** 2 for _probability, label in pairs)
    bins = []
    weighted_gap = 0.0
    for index in range(5):
        lower, upper = index / 5.0, (index + 1) / 5.0
        bucket = [
            pair for pair in pairs
            if lower <= pair[0] < upper or (index == 4 and pair[0] == 1.0)
        ]
        if not bucket:
            continue
        mean_probability = statistics.fmean(pair[0] for pair in bucket)
        observed_rate = statistics.fmean(pair[1] for pair in bucket)
        weighted_gap += abs(mean_probability - observed_rate) * len(bucket) / len(pairs)
        bins.append({
            "lower": lower,
            "upper": upper,
            "n": len(bucket),
            "mean_probability": round(mean_probability, 6),
            "observed_positive_rate": round(observed_rate, 6),
        })
    return {
        "state": "DIAGNOSTIC_ONLY" if len(pairs) < 30 else "LIVE_SHADOW_EVALUABLE",
        "observations": len(pairs),
        "brier_score": round(brier, 8),
        "prevalence_baseline_brier": round(baseline_brier, 8),
        "brier_beats_prevalence": brier < baseline_brier,
        "expected_calibration_error_5bin": round(weighted_gap, 8),
        "reliability_bins": bins,
        "interpretation": "Live shadow probability evidence; not a win-rate or profit guarantee.",
    }


class SelectionResearchValidationService:
    """Persist immutable outcomes and compare selector arms without authority."""

    DIAGNOSTIC_MIN_OBSERVATIONS = 100
    DIAGNOSTIC_MIN_DAYS = 60
    SHADOW_MIN_OBSERVATIONS = 300
    SHADOW_MIN_DAYS = 126
    SHADOW_MIN_REGIMES = 3

    def __init__(self, store: Any):
        self.store = store
        self.production_governance_required = bool(
            getattr(store, "production_model_governance_required", False)
        )
        self.governance_repository = getattr(
            store, "production_model_governance_repository", None
        )
        if self.production_governance_required:
            required = ("selector_member", "record_selector_outcome", "selector_joined_rows")
            if (
                self.governance_repository is None
                or getattr(self.governance_repository, "authority", None) is None
                or any(not callable(getattr(self.governance_repository, name, None)) for name in required)
            ):
                raise RuntimeError("PRODUCTION_SELECTION_VALIDATION_REQUIRES_POSTGRES_GOVERNANCE_REPOSITORY")
        if not hasattr(store, "write_lock"):
            store.write_lock = threading.RLock()
        if not self.production_governance_required:
            SelectionPlatformService(store)
            self._ensure_schema()

    def _ensure_schema(self) -> None:
        with self.store.write_lock:
            self.store.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS selector_candidate_outcomes (
                  candidate_id TEXT NOT NULL,
                  horizon TEXT NOT NULL,
                  population_fingerprint TEXT NOT NULL,
                  symbol TEXT NOT NULL,
                  mode TEXT NOT NULL,
                  observed_at TEXT NOT NULL,
                  settled_at TEXT NOT NULL,
                  market_regime TEXT NOT NULL,
                  result TEXT NOT NULL,
                  gross_return_bps REAL,
                  net_return_bps REAL NOT NULL,
                  same_bar_ambiguous INTEGER NOT NULL DEFAULT 0,
                  primary_ambiguity_policy TEXT NOT NULL,
                  actual_cost_bps REAL,
                  proof_json TEXT NOT NULL,
                  record_hash TEXT NOT NULL,
                  validation_version TEXT NOT NULL,
                  PRIMARY KEY(candidate_id, horizon),
                  FOREIGN KEY(candidate_id) REFERENCES candidate_population_observations(candidate_id)
                );
                CREATE INDEX IF NOT EXISTS ix_selector_candidate_outcomes_mode
                  ON selector_candidate_outcomes(mode,horizon,observed_at);
                CREATE INDEX IF NOT EXISTS ix_selector_candidate_outcomes_population
                  ON selector_candidate_outcomes(population_fingerprint,candidate_id);
                """
            )
            self.store.conn.commit()

    def record_outcome(
        self,
        *,
        candidate_id: str,
        horizon: str,
        result: str,
        net_return_bps: float,
        gross_return_bps: Optional[float] = None,
        settled_at: Optional[str] = None,
        market_regime: str = "UNKNOWN",
        same_bar_ambiguous: bool = False,
        actual_cost_bps: Optional[float] = None,
        proof: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        candidate_key = str(candidate_id or "").strip()
        horizon_key = str(horizon or "").strip().lower()
        result_key = str(result or "").strip().upper()
        net_bps = _float(net_return_bps)
        gross_bps = _float(gross_return_bps)
        if not candidate_key or not horizon_key:
            raise ValueError("candidate_id and horizon are required")
        if result_key not in VALID_RESULTS:
            raise ValueError(f"result must be one of {sorted(VALID_RESULTS)}")
        if net_bps is None:
            raise ValueError("net_return_bps must be finite")
        if same_bar_ambiguous:
            result_key = "STOP_FIRST"
            if net_bps > 0:
                raise ValueError("same-bar primary stop-first outcome cannot have positive net_return_bps")

        if self.production_governance_required:
            candidate = self.governance_repository.selector_member(candidate_key)
        else:
            candidate = self.store.conn.execute(
                """SELECT candidate_id,population_fingerprint,symbol,mode,observed_at
                   FROM candidate_population_observations WHERE candidate_id=?""",
                (candidate_key,),
            ).fetchone()
        if not candidate:
            raise ValueError("candidate_id is not present in the immutable candidate population ledger")
        row = dict(candidate)
        payload = {
            "candidate_id": candidate_key,
            "horizon": horizon_key,
            "population_fingerprint": row["population_fingerprint"],
            "symbol": row["symbol"],
            "mode": row["mode"],
            "observed_at": row["observed_at"],
            "settled_at": str(settled_at or _now()),
            "market_regime": str(market_regime or "UNKNOWN").strip().upper(),
            "result": result_key,
            "gross_return_bps": gross_bps,
            "net_return_bps": net_bps,
            "same_bar_ambiguous": bool(same_bar_ambiguous),
            "primary_ambiguity_policy": PRIMARY_AMBIGUITY_POLICY,
            "actual_cost_bps": _float(actual_cost_bps),
            "proof": dict(proof or {}),
            "validation_version": VALIDATION_VERSION,
        }
        record_hash = _sha(payload)
        if self.production_governance_required:
            saved = self.governance_repository.record_selector_outcome({
                **payload, "proof_payload": payload["proof"], "record_hash": record_hash,
            })
            return {**saved, **payload, "record_hash": record_hash}
        existing = self.store.conn.execute(
            "SELECT record_hash FROM selector_candidate_outcomes WHERE candidate_id=? AND horizon=?",
            (candidate_key, horizon_key),
        ).fetchone()
        if existing:
            if str(existing[0]) != record_hash:
                raise ValueError("outcome is immutable; conflicting settlement rejected")
            return {"ok": True, "inserted": False, **payload, "record_hash": record_hash}

        with self.store.write_lock:
            self.store.conn.execute(
                """INSERT INTO selector_candidate_outcomes(
                    candidate_id,horizon,population_fingerprint,symbol,mode,observed_at,settled_at,
                    market_regime,result,gross_return_bps,net_return_bps,same_bar_ambiguous,
                    primary_ambiguity_policy,actual_cost_bps,proof_json,record_hash,validation_version
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    candidate_key, horizon_key, row["population_fingerprint"], row["symbol"], row["mode"],
                    row["observed_at"], payload["settled_at"], payload["market_regime"], result_key,
                    gross_bps, net_bps, int(bool(same_bar_ambiguous)), PRIMARY_AMBIGUITY_POLICY,
                    payload["actual_cost_bps"], _canonical(payload["proof"]), record_hash, VALIDATION_VERSION,
                ),
            )
            self.store.conn.commit()
        return {"ok": True, "inserted": True, **payload, "record_hash": record_hash}

    def _joined_rows(self, *, mode: str, horizon: str) -> List[Dict[str, Any]]:
        if self.production_governance_required:
            return self.governance_repository.selector_joined_rows(mode=mode, horizon=horizon)
        rows = self.store.conn.execute(
            """SELECT p.arm,p.model_version,p.candidate_id,p.population_fingerprint,p.symbol,p.mode,
                      p.score,p.rank,p.percentile,p.probability_positive,p.expected_net_return,
                      p.created_at AS prediction_at,o.population_fingerprint AS outcome_population_fingerprint,
                      o.horizon,o.observed_at,o.settled_at,o.market_regime,o.result,
                      o.gross_return_bps,o.net_return_bps,o.same_bar_ambiguous,
                      o.primary_ambiguity_policy,o.actual_cost_bps
               FROM shadow_selector_predictions p
               JOIN selector_candidate_outcomes o ON o.candidate_id=p.candidate_id
                  AND o.population_fingerprint=p.population_fingerprint
               WHERE p.mode=? AND o.horizon=?
               ORDER BY p.arm,o.observed_at,p.rank,p.symbol""",
            (str(mode).lower(), str(horizon).lower()),
        ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _arm_metrics(rows: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
        scores = [float(row["score"]) for row in rows]
        returns = [float(row["net_return_bps"]) for row in rows]
        ordered = sorted(rows, key=lambda row: (int(row["rank"]), str(row["symbol"])))
        top_n = max(1, math.ceil(len(ordered) * 0.20)) if ordered else 0
        top_returns = [float(row["net_return_bps"]) for row in ordered[:top_n]]
        all_mean = statistics.fmean(returns) if returns else None
        top_mean = statistics.fmean(top_returns) if top_returns else None
        stresses = {}
        for extra in COST_STRESS_BPS:
            stressed = [value - extra for value in returns]
            stresses[f"plus_{int(extra)}bps"] = {
                "mean_net_return_bps": round(statistics.fmean(stressed), 6) if stressed else None,
                "positive_rate_pct": round(sum(value > 0 for value in stressed) * 100.0 / len(stressed), 4) if stressed else None,
                "profit_factor": _profit_factor(stressed),
            }
        probability_evidence = _probability_evidence(rows)
        return {
            "observations": len(rows),
            "positive_rate_pct": round(sum(value > 0 for value in returns) * 100.0 / len(returns), 4) if returns else None,
            "mean_net_return_bps": round(all_mean, 6) if all_mean is not None else None,
            "median_net_return_bps": _median(returns),
            "profit_factor": _profit_factor(returns),
            "spearman_rank_ic": _spearman(scores, returns),
            "top_quintile_observations": len(top_returns),
            "top_quintile_mean_net_return_bps": round(top_mean, 6) if top_mean is not None else None,
            "top_quintile_lift_bps": round(top_mean - all_mean, 6) if top_mean is not None and all_mean is not None else None,
            "cost_sensitivity": stresses,
            "probability_prediction_coverage": probability_evidence.get("observations", 0),
            "shadow_probability_evidence": probability_evidence,
        }

    def report(self, *, mode: str, horizon: str) -> Dict[str, Any]:
        desk = normalise_desk(mode)
        horizon_key = canonical_horizon(desk, horizon)
        raw_joined = self._joined_rows(mode=desk, horizon=horizon_key)
        eligibility = classify_forward_evidence(raw_joined)
        joined = list(eligibility.get("rows") or [])
        by_arm: Dict[str, List[Dict[str, Any]]] = {"heuristic": [], "quant": [], "hybrid": []}
        for row in joined:
            by_arm.setdefault(str(row["arm"]), []).append(row)
        candidate_sets = {arm: {row["candidate_id"] for row in rows} for arm, rows in by_arm.items()}
        common = set.intersection(*(values for values in candidate_sets.values())) if all(candidate_sets.values()) else set()
        observed_dates = {str(row["observed_at"])[:10] for row in joined}
        regimes = {
            str(row["market_regime"] or "UNKNOWN").upper()
            for row in joined
            if str(row.get("market_regime") or "UNKNOWN").upper() not in {"", "UNKNOWN", "NONE", "UNAVAILABLE"}
        }
        distinct_candidates = {row["candidate_id"] for row in joined}
        observations = len(distinct_candidates)
        days = len(observed_dates)
        diagnostic_ready = observations >= self.DIAGNOSTIC_MIN_OBSERVATIONS and days >= self.DIAGNOSTIC_MIN_DAYS
        shadow_ready = (
            observations >= self.SHADOW_MIN_OBSERVATIONS
            and days >= self.SHADOW_MIN_DAYS
            and len(regimes) >= self.SHADOW_MIN_REGIMES
            and all(candidate_sets[arm] == common for arm in ("heuristic", "quant", "hybrid"))
        )
        metrics = {arm: self._arm_metrics(rows) for arm, rows in by_arm.items()}
        complexity_contribution = {
            "quant_vs_mathematics": DEFAULT_COMPLEXITY_CONTRIBUTION_AUTHORITY.evaluate(
                joined, baseline_arm="heuristic", challenger_arm="quant",
                seed_material=f"{desk}:{horizon_key}:quant-vs-heuristic",
            ),
            "hybrid_vs_mathematics": DEFAULT_COMPLEXITY_CONTRIBUTION_AUTHORITY.evaluate(
                joined, baseline_arm="heuristic", challenger_arm="hybrid",
                seed_material=f"{desk}:{horizon_key}:hybrid-vs-heuristic",
            ),
        }
        model_versions = {
            arm: sorted({str(row.get("model_version") or "").strip() for row in rows if str(row.get("model_version") or "").strip()})
            for arm, rows in by_arm.items()
        }
        return {
            "ok": True,
            "version": VALIDATION_VERSION,
            "mode": desk,
            "horizon": horizon_key,
            "primary_ambiguity_policy": PRIMARY_AMBIGUITY_POLICY,
            "settled_candidates": observations,
            "trading_days": days,
            "regimes": sorted(regimes),
            "regime_count": len(regimes),
            "same_population_across_arms": bool(common) and all(candidate_sets[arm] == common for arm in ("heuristic", "quant", "hybrid")),
            "common_candidate_count": len(common),
            "forward_eligibility": {key: value for key, value in eligibility.items() if key != "rows"},
            "arms": metrics,
            "complexity_contribution": complexity_contribution,
            "expectancy_semantics": expectancy_lane("FORWARD_SELECTION_EXPECTANCY"),
            "model_versions": model_versions,
            "readiness": {
                "diagnostic_ready": diagnostic_ready,
                "diagnostic_requirements": {"observations": self.DIAGNOSTIC_MIN_OBSERVATIONS, "trading_days": self.DIAGNOSTIC_MIN_DAYS},
                "shadow_approval_ready": shadow_ready,
                "shadow_requirements": {"observations": self.SHADOW_MIN_OBSERVATIONS, "trading_days": self.SHADOW_MIN_DAYS, "regimes": self.SHADOW_MIN_REGIMES},
                "production_change_allowed": False,
            },
            "selection_edge": "NOT_YET_STATISTICALLY_VALIDATED" if not shadow_ready else "ELIGIBLE_FOR_INDEPENDENT_SHADOW_REVIEW",
            "notes": [
                "All three arms are compared on the same immutable candidate observations.",
                "Base net returns already include the recorded cost assumption; stress rows subtract additional slippage.",
                "A readiness gate permits review only and never grants production authority automatically.",
                "Hybrid/ML complexity must prove paired incremental post-cost value over the mathematical baseline on the same frozen populations.",
            ],
        }
