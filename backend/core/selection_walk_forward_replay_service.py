"""Purged walk-forward replay for the exact stored selector outputs.

The replay never rebuilds scores with a separate strategy implementation.  It
uses the immutable scores that the live shadow platform actually persisted,
selects each arm's top cross-sectional candidates, and evaluates those rows
through the shared WalkForwardValidationService.
"""
from __future__ import annotations

import hashlib
import json
import math
from typing import Any, Dict, List, Mapping, Optional, Sequence

from core.walk_forward_validation_service import WalkForwardValidationService
from core.forward_horizon_policy import canonical_horizon, normalise_desk
from core.forward_evidence_eligibility import classify_forward_evidence

REPLAY_VERSION = "selection-walk-forward-replay-1.4.0-pl18-nonopaque-evidence-state"


DEFAULT_THRESHOLD_GRID = (0.10, 0.15, 0.20, 0.25, 0.30)
THRESHOLD_POLICY_VERSION = "threshold-freeze-policy-1.0.0"



def _num(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _horizon_days(horizon: str) -> int:
    text = str(horizon or "").lower().strip()
    if text == "session":
        return 1
    try:
        return max(1, int(text.rstrip("d")))
    except ValueError:
        return 1


class SelectionWalkForwardReplayService:
    def __init__(self, store: Any):
        self.store = store
        self.validator = WalkForwardValidationService(store)

    def _rows(self, *, mode: str, horizon: str) -> List[Dict[str, Any]]:
        repo = getattr(self.store, "production_model_governance_read_repository", None) or getattr(self.store, "production_model_governance_repository", None)
        raw_rows = None
        if repo is not None and callable(getattr(repo, "selector_replay_rows", None)):
            raw_rows = repo.selector_replay_rows(desk=mode, horizon=horizon)
            self._evidence_authority = "GOVERNANCE_POSTGRESQL_SELECTOR_EVIDENCE"
            if not raw_rows:
                migration = repo.legacy_research_migration_status(self.store)
                local_count = 0
                try:
                    local_count = int(self.store.conn.execute(
                        "SELECT count(*) FROM candidate_populations WHERE mode=?", (str(mode).lower(),)
                    ).fetchone()[0])
                except Exception:
                    pass
                if local_count > 0 and migration.get("count_verified") is not True:
                    # Fail closed on authority selection, but do not turn an evidence-depth
                    # condition into an opaque HTTP 503. The caller needs a truthful WFA
                    # blocker so the evidence pipeline can repair/mature it.
                    self._evidence_blocker = {
                        "code": "CANONICAL_SELECTOR_EVIDENCE_NOT_MIGRATED",
                        "legacy_local_rows": local_count,
                        "migration_state": migration.get("state"),
                        "count_verified": migration.get("count_verified"),
                        "hash_verified": migration.get("hash_verified"),
                        "authority": "GOVERNANCE_POSTGRESQL_REQUIRED",
                    }
                    raw_rows = []
        if raw_rows is None:
            self._evidence_authority = "LEGACY_SQLITE_READ_PROJECTION"
            raw_rows = self.store.conn.execute(
                """SELECT p.arm,p.model_version,p.population_fingerprint,p.candidate_id,p.symbol,p.mode,
                          p.score,p.rank,p.percentile,p.created_at prediction_at,
                          o.observed_at,o.settled_at,o.market_regime,o.net_return_bps,o.actual_cost_bps,
                          o.proof_json,o.same_bar_ambiguous,o.primary_ambiguity_policy,
                          c.feature_json,c.feature_hash,
                          pop.universe_id,pop.dataset_fingerprint,pop.feature_manifest_hash
                   FROM shadow_selector_predictions p
                   JOIN selector_candidate_outcomes o ON o.candidate_id=p.candidate_id
                   JOIN candidate_population_observations c ON c.candidate_id=p.candidate_id
                   JOIN candidate_populations pop ON pop.population_fingerprint=p.population_fingerprint
                   WHERE p.mode=? AND o.horizon=?
                   ORDER BY p.arm,p.population_fingerprint,p.rank,p.symbol""",
                (str(mode).lower(), str(horizon).lower()),
            ).fetchall()
        rows = []
        for raw in raw_rows:
            row = dict(raw)
            raw_features = row.get("feature_json") or {}
            raw_proof = row.get("proof_json") or {}
            if isinstance(raw_features, Mapping):
                payload = dict(raw_features)
                row["features"] = dict(payload.get("features") or payload)
                if isinstance(payload.get("quant_snapshot"), Mapping):
                    row["features"].setdefault("quant_snapshot", dict(payload["quant_snapshot"]))
            else:
                try:
                    row["features"] = json.loads(raw_features or "{}")
                except Exception:
                    row["features"] = {}
            if isinstance(raw_proof, Mapping):
                row["proof"] = dict(raw_proof)
            else:
                try:
                    row["proof"] = json.loads(raw_proof or "{}")
                except Exception:
                    row["proof"] = {}
            rows.append(row)
        eligibility = classify_forward_evidence(rows)
        self._eligibility_summary = {key: value for key, value in eligibility.items() if key != "rows"}
        return list(eligibility.get("rows") or [])

    @staticmethod
    def _deterministic_candidate(rows: List[Mapping[str, Any]], salt: str) -> Mapping[str, Any]:
        return min(rows, key=lambda row: hashlib.sha256(f"{salt}|{row['candidate_id']}".encode()).hexdigest())

    def _arm_observations(self, rows: List[Dict[str, Any]], *, arm: str, top_fraction: float) -> List[Dict[str, Any]]:
        arm_rows = [row for row in rows if row["arm"] == arm]
        by_population: Dict[str, List[Dict[str, Any]]] = {}
        for row in arm_rows:
            by_population.setdefault(str(row["population_fingerprint"]), []).append(row)
        observations: List[Dict[str, Any]] = []
        for population, candidates in sorted(by_population.items()):
            candidates.sort(key=lambda row: (int(row["rank"]), str(row["symbol"])))
            count = max(1, math.ceil(len(candidates) * max(0.01, min(1.0, top_fraction))))
            selected = candidates[:count]
            all_return = sum(float(row["net_return_bps"]) for row in candidates) / len(candidates) / 10000.0
            random_row = self._deterministic_candidate(candidates, population)
            random_return = float(random_row["net_return_bps"]) / 10000.0
            liquidity_row = max(
                candidates,
                key=lambda row: _num((row.get("features") or {}).get("liquidity_score")) or -1e9,
            )
            liquidity_return = float(liquidity_row["net_return_bps"]) / 10000.0
            for row in selected:
                features = dict(row.get("features") or {})
                proof = dict(row.get("proof") or {})
                net_return = float(row["net_return_bps"]) / 10000.0
                decision_at = str(row.get("prediction_at") or row["observed_at"])
                prediction_hash = str(row.get("prediction_payload_sha256") or "").strip().lower()
                if not prediction_hash:
                    prediction_hash = hashlib.sha256(json.dumps({
                        "arm": row.get("arm"), "model_version": row.get("model_version"),
                        "population_fingerprint": population, "candidate_id": row.get("candidate_id"),
                        "prediction_at": row.get("prediction_at"), "score": row.get("score"),
                        "rank": row.get("rank"), "percentile": row.get("percentile"),
                    }, sort_keys=True, default=str).encode()).hexdigest()
                observations.append({
                    "date": str(row["settled_at"])[:10],
                    "symbol": row["symbol"],
                    "mode": row["mode"],
                    "rank_score": row["score"],
                    "forward_return": net_return,
                    "cost_return": 0.0,
                    "benchmark_return": all_return,
                    "baseline_returns": {
                        "all_eligible_equal": all_return,
                        "random_eligible_deterministic": random_return,
                        "highest_liquidity_eligible": liquidity_return,
                    },
                    "dataset_fingerprint": row["dataset_fingerprint"],
                    "feature_manifest_hash": row["feature_manifest_hash"],
                    "universe_id": row["universe_id"],
                    "cost_model_version": proof.get("cost_version") or "recorded_cost_unknown_version",
                    "cost_model_profile": row["mode"],
                    "execution_model_version": proof.get("settlement_version") or "selection-outcome-settlement",
                    "admission_policy_version": "selection-platform-shadow-top-cross-section-1.0.0",
                    "corporate_action_adjusted": features.get("corporate_action_adjusted") is True,
                    "survivorship_bias_controlled": features.get("survivorship_bias_controlled") is True,
                    "decision_as_of": decision_at,
                    "outcome_as_of": row["settled_at"],
                    "feature_as_of": features.get("feature_as_of") or row.get("observed_at") or decision_at,
                    "prospective_prediction_at": row.get("prediction_at") or decision_at,
                    "prospective_prediction_hash": prediction_hash,
                    "prospective_prediction_key": row.get("prediction_key") or hashlib.sha256(f"{row.get('arm')}|{population}|{row.get('candidate_id')}".encode()).hexdigest()[:32],
                    "prospective_model_version": str(row.get("model_version") or ""),
                    "prospective_evidence_authority": getattr(self, "_evidence_authority", "UNKNOWN"),
                    "universe_as_of": features.get("universe_as_of") or decision_at,
                    "fundamental_as_of": features.get("fundamental_as_of") or (decision_at if row["mode"] != "delivery" else None),
                    "population_fingerprint": population,
                    "candidate_id": row["candidate_id"],
                    "market_regime": row["market_regime"],
                    "same_bar_ambiguous": bool(row["same_bar_ambiguous"]),
                    "primary_ambiguity_policy": row["primary_ambiguity_policy"],
                })
        return observations

    def _qualify_threshold(
        self,
        rows: List[Dict[str, Any]],
        *,
        arm: str,
        train_dates: Sequence[str],
        threshold_grid: Sequence[float],
        horizon_days: int,
        minimum_train_samples: int,
    ) -> Dict[str, Any]:
        """Select one threshold using only the first purged training window.

        No test-fold observation participates in threshold selection.  If no
        candidate passes the training robustness policy, the authority fails
        closed and returns no selected threshold.
        """
        train_set = {str(value)[:10] for value in train_dates}
        candidates = []
        for fraction in sorted({round(float(value), 6) for value in threshold_grid if 0.0 < float(value) <= 1.0}):
            observations = [
                row for row in self._arm_observations(rows, arm=arm, top_fraction=fraction)
                if str(row.get("date") or "")[:10] in train_set
            ]
            metrics = self.validator._metrics(observations, horizon_days=horizon_days)
            regimes: Dict[str, List[Dict[str, Any]]] = {}
            for observation in observations:
                regime = str(observation.get("market_regime") or "UNKNOWN").upper()
                if regime != "UNKNOWN":
                    regimes.setdefault(regime, []).append(observation)
            regime_metrics = {
                regime: self.validator._metrics(values, horizon_days=horizon_days)
                for regime, values in sorted(regimes.items())
            }
            positive_regimes = sum(
                value.get("mean", 0.0) > 0 and value.get("excess", 0.0) > 0
                for value in regime_metrics.values()
            )
            regime_stability = positive_regimes / len(regime_metrics) if regime_metrics else 0.0
            qualified = bool(
                metrics.get("n", 0) >= max(30, int(minimum_train_samples))
                and metrics.get("n_days", 0) >= 63
                and metrics.get("mean", 0.0) > 0
                and metrics.get("excess", 0.0) > 0
                and metrics.get("profit_factor", 0.0) >= 1.0
                and metrics.get("drawdown", -1.0) >= -0.25
                and len(regime_metrics) >= 2
                and regime_stability >= 0.50
            )
            candidates.append({
                "top_fraction": fraction,
                "qualified": qualified,
                "n": int(metrics.get("n", 0)),
                "n_days": int(metrics.get("n_days", 0)),
                "mean_net_return": float(metrics.get("mean", 0.0)),
                "mean_excess_return": float(metrics.get("excess", 0.0)),
                "profit_factor": float(metrics.get("profit_factor", 0.0)),
                "max_drawdown": float(metrics.get("drawdown", 0.0)),
                "regime_count": len(regime_metrics),
                "regime_stability": regime_stability,
            })
        qualified_rows = [row for row in candidates if row["qualified"]]
        selected = max(
            qualified_rows,
            key=lambda row: (
                row["mean_excess_return"],
                row["mean_net_return"],
                row["regime_stability"],
                row["profit_factor"],
                row["max_drawdown"],
                -row["top_fraction"],
            ),
            default=None,
        )
        return {
            "policy_version": THRESHOLD_POLICY_VERSION,
            "selection_basis": "FIRST_PURGED_TRAIN_WINDOW_ONLY",
            "train_start": min(train_set) if train_set else None,
            "train_end": max(train_set) if train_set else None,
            "train_date_count": len(train_set),
            "candidate_count": len(candidates),
            "selected_top_fraction": selected["top_fraction"] if selected else None,
            "qualified": selected is not None,
            "candidates": candidates,
            "test_data_used_for_selection": False,
        }

    def replay(
        self,
        *,
        mode: str,
        horizon: str,
        top_fraction: float = 0.20,
        threshold_grid: Optional[Sequence[float]] = None,
        min_train_days: int = 252,
        test_days: int = 63,
        max_folds: int = 8,
        embargo_days: int = 1,
        min_samples: int = 300,
        threshold_min_train_samples: int = 100,
        profile: str = "capital",
    ) -> Dict[str, Any]:
        desk = normalise_desk(mode)
        horizon = canonical_horizon(desk, horizon)
        horizon_days = _horizon_days(horizon)
        rows = self._rows(mode=desk, horizon=horizon)
        versions = {(row["arm"], row["model_version"]) for row in rows}
        grid = tuple(threshold_grid or DEFAULT_THRESHOLD_GRID)
        if top_fraction not in grid:
            grid = tuple(grid) + (float(top_fraction),)
        fold_dates = [str(row.get("settled_at") or row.get("observed_at") or "")[:10] for row in rows]
        folds = self.validator.build_folds(
            fold_dates, min_train_days=min_train_days, test_days=test_days,
            purge_days=horizon_days, max_folds=max_folds, embargo_days=embargo_days,
        )
        first_train_dates = folds[0]["train_dates"] if folds else []
        model_versions = {
            arm: sorted({str(row.get("model_version") or "").strip() for row in rows if row.get("arm") == arm and str(row.get("model_version") or "").strip()})
            for arm in ("heuristic", "quant", "hybrid")
        }
        trial_count = max(1, len(versions) * max(1, len(set(grid))))
        arm_reports = {}
        threshold_qualification = {}
        for arm in ("heuristic", "quant", "hybrid"):
            qualification = self._qualify_threshold(
                rows, arm=arm, train_dates=first_train_dates, threshold_grid=grid,
                horizon_days=horizon_days, minimum_train_samples=threshold_min_train_samples,
            )
            threshold_qualification[arm] = qualification
            selected_fraction = qualification.get("selected_top_fraction")
            diagnostic_fraction = float(selected_fraction if selected_fraction is not None else top_fraction)
            observations = self._arm_observations(rows, arm=arm, top_fraction=diagnostic_fraction)
            report = self.validator.validate(
                model_id=f"{arm}:{desk}:{horizon}:threshold={diagnostic_fraction:.6f}", observations=observations,
                horizon_days=horizon_days, min_train_days=min_train_days,
                test_days=test_days, max_folds=max_folds, min_samples=min_samples,
                profile=profile, trial_count=trial_count, embargo_days=embargo_days,
                persist=False,
            )
            threshold_gate = qualification.get("qualified") is True
            report["statistical_approval_before_threshold_gate"] = bool(report.get("approved"))
            report["threshold_qualification"] = qualification
            report.setdefault("gates", {})["prior_window_threshold_qualified"] = threshold_gate
            report["approved"] = bool(report.get("approved")) and threshold_gate
            report["status"] = "APPROVED" if report["approved"] else "REJECTED"
            if not threshold_gate:
                report["lifecycle"] = "EXPERIMENTAL"
            arm_reports[arm] = {
                "observations": len(observations),
                "selected_top_fraction": selected_fraction,
                "threshold_qualification": qualification,
                "validation": report,
                "role": "BACKTEST_DIAGNOSTIC",
                "broker_execution_weight": 0.0,
            }
        settled_dates = sorted({str(row.get("settled_at") or row.get("observed_at") or "")[:10] for row in rows if row.get("settled_at") or row.get("observed_at")})
        repo = getattr(self.store, "production_model_governance_read_repository", None) or getattr(self.store, "production_model_governance_repository", None)
        evidence_status = {}
        if repo is not None and callable(getattr(repo, "selector_evidence_status", None)):
            try:
                evidence_status = dict(repo.selector_evidence_status(desk) or {})
            except Exception as exc:
                evidence_status = {"state": "EVIDENCE_STATUS_UNAVAILABLE", "error": str(exc)[:240]}
        required_calendar_days = max(1, int(min_train_days)) + max(1, int(test_days)) + max(0, int(embargo_days)) + horizon_days
        if not rows:
            fold_blocker = "NO_SETTLED_SELECTOR_EVIDENCE"
        elif not folds:
            fold_blocker = (
                f"INSUFFICIENT_SETTLED_DATE_DEPTH:{len(settled_dates)}_days; "
                f"requires chronological train/test depth for min_train={min_train_days}, test={test_days}, purge={horizon_days}, embargo={embargo_days}"
            )
        else:
            fold_blocker = None
        populations = {row["population_fingerprint"] for row in rows}
        candidate_ids_by_arm = {
            arm: {row["candidate_id"] for row in rows if row["arm"] == arm}
            for arm in ("heuristic", "quant", "hybrid")
        }
        common = set.intersection(*candidate_ids_by_arm.values()) if all(candidate_ids_by_arm.values()) else set()
        evidence_blocker = getattr(self, "_evidence_blocker", None)
        replay_state = (
            "EVIDENCE_NOT_READY" if evidence_blocker or not rows else
            "INSUFFICIENT_CHRONOLOGICAL_DEPTH" if not folds else
            "EVALUATED"
        )
        return {
            "ok": True,
            "state": replay_state,
            "version": REPLAY_VERSION,
            "evidence_blocker": evidence_blocker,
            "evidence_authority": getattr(self, "_evidence_authority", "UNKNOWN"),
            "forward_eligibility": getattr(self, "_eligibility_summary", {}),
            "mode": desk,
            "horizon": str(horizon).lower(),
            "top_fraction": top_fraction,
            "threshold_policy_version": THRESHOLD_POLICY_VERSION,
            "threshold_grid": sorted(set(float(value) for value in grid)),
            "threshold_qualification": threshold_qualification,
            "threshold_selection_uses_test_data": False,
            "population_count": len(populations),
            "settled_candidate_count": len(common),
            "settled_date_count": len(settled_dates),
            "settled_first_date": settled_dates[0] if settled_dates else None,
            "settled_last_date": settled_dates[-1] if settled_dates else None,
            "requested_min_train_days": int(min_train_days),
            "requested_test_days": int(test_days),
            "requested_purge_days": int(horizon_days),
            "requested_embargo_days": int(embargo_days),
            "minimum_calendar_depth_hint": required_calendar_days,
            "fold_blocker": fold_blocker,
            "evidence_status": evidence_status,
            "same_candidate_population_across_arms": bool(common) and all(ids == common for ids in candidate_ids_by_arm.values()),
            "declared_trial_count": trial_count,
            "declared_model_version_trials": max(1, len(versions)),
            "declared_threshold_trials": max(1, len(set(grid))),
            "model_versions": model_versions,
            "arms": arm_reports,
            "production_authority": "UNCHANGED_HEURISTIC_BASELINE",
            "selection_edge": "NOT_YET_STATISTICALLY_VALIDATED",
            "policy": "Thresholds are selected only on the first purged training window and frozen before unseen folds. Backtest approval authorizes shadow review only; never automatic production promotion.",
        }
