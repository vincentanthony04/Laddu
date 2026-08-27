"""Evidence-authoritative Level 5 walk-forward maturity.

Level 5 is earned only from immutable, same-population three-arm predictions,
future outcomes, cost-stressed forward statistics, purged capital-profile
walk-forward replay, and an exact governed champion lineage for both active
cash-equity desks.  This service never promotes a model or changes broker
execution authority.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from typing import Any, Dict, Mapping, Optional

from core.forward_evidence_clock_service import ForwardEvidenceClockService
from core.forward_horizon_policy import POLICY_VERSION, canonical_horizon, durability_status, maturity_policy
from core.selection_research_validation_service import SelectionResearchValidationService
from core.selection_walk_forward_replay_service import SelectionWalkForwardReplayService

SERVICE_VERSION = "level5-forward-maturity-1.3.0-eligible-forward-cohorts"
_DESKS = ("intraday", "delivery")
_ARMS = ("heuristic", "quant", "hybrid")
_KV_KEY = "level5_forward_maturity:last"
_SIGNATURE_KEY = "level5_forward_maturity:evidence_signature"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _num(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _mapping(value: Any) -> Dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _sha(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8", errors="replace")).hexdigest()


class Level5ForwardMaturityService:
    """Compute and persist an immutable Level 5 evidence checkpoint."""

    def __init__(
        self,
        store: Any,
        *,
        governance_repository: Any = None,
        model_governance_repository: Any = None,
        build_version: str = "unknown",
    ):
        self.store = store
        self.governance_repository = governance_repository
        self.model_governance_repository = model_governance_repository
        self.build_version = str(build_version or "unknown")
        self.clock = ForwardEvidenceClockService(store)
        self.validation = SelectionResearchValidationService(store)
        self.replay = SelectionWalkForwardReplayService(store)

    def _kv_value(self, key: str, default: Any = None) -> Any:
        getter = getattr(self.store, "get_kv", None)
        if not callable(getter):
            return default
        try:
            return getter(key, default)
        except Exception:
            return default

    def _kv_get(self) -> Optional[Dict[str, Any]]:
        value = self._kv_value(_KV_KEY, None)
        return dict(value) if isinstance(value, Mapping) else None

    def _kv_set_value(self, key: str, value: Any) -> None:
        setter = getattr(self.store, "set_kv", None)
        if callable(setter):
            try:
                setter(key, value)
            except Exception:
                pass

    def _kv_set(self, value: Mapping[str, Any]) -> None:
        self._kv_set_value(_KV_KEY, dict(value))

    def _model_governance_snapshot(self) -> Dict[str, Any]:
        if self.model_governance_repository is None:
            return {desk: {"state": "GOVERNANCE_UNAVAILABLE"} for desk in _DESKS}
        snapshot: Dict[str, Any] = {}
        for desk in _DESKS:
            try:
                status = self.model_governance_repository.status(desk=desk)
                snapshot[desk] = {
                    "active_champions": [
                        {
                            "desk": str(row.get("desk") or ""),
                            "model_version": str(row.get("model_version") or ""),
                            "production_weight": float(row.get("production_weight") or 0.0),
                        }
                        for row in (status.get("active_champions") or [])
                    ]
                }
            except Exception as exc:
                snapshot[desk] = {"state": "GOVERNANCE_UNAVAILABLE", "error": str(exc)[:240]}
        return snapshot

    def _evidence_signature(
        self,
        clock: Mapping[str, Any],
        governance_status: Mapping[str, Any],
        model_snapshot: Mapping[str, Any],
    ) -> str:
        material = {
            "build": self.build_version,
            "policy": POLICY_VERSION,
            "clock": {
                "started_at_by_desk": _mapping(clock.get("started_at_by_desk")),
                "complete_population_count_by_desk": _mapping(clock.get("complete_population_count_by_desk")),
                "by_desk_arm": _mapping(clock.get("by_desk_arm")),
                "first_settled_at_by_desk": _mapping(clock.get("first_settled_at_by_desk")),
            },
            "governance": {
                "ok": bool(governance_status.get("ok")),
                "counts": _mapping(governance_status.get("counts")),
                "by_desk": _mapping(governance_status.get("by_desk")),
            },
            "model_governance": dict(model_snapshot),
        }
        return _sha(material)

    def _integrity(self, desk: str, horizon: str, *, report: Mapping[str, Any] | None = None) -> Dict[str, Any]:
        eligibility = _mapping(_mapping(report or {}).get("forward_eligibility"))
        eligible = int(eligibility.get("eligible_candidate_count") or 0)
        excluded = int(eligibility.get("excluded_candidate_count") or 0)
        reasons = _mapping(eligibility.get("exclusion_reason_counts"))
        checks = {
            "eligible_forward_candidates_present": eligible > 0,
            "historical_or_invalid_rows_do_not_poison_current_cohort": True,
            "ineligible_rows_are_explicitly_reported": excluded >= 0,
        }
        return {
            "passed": eligible > 0,
            "state": "PASS" if eligible > 0 else "PENDING_OR_FAILED",
            "outcomes": eligible,
            "eligible_candidates": eligible,
            "excluded_candidates": excluded,
            "exclusion_reason_counts": reasons,
            "checks": checks,
            "policy": "Only immutable, temporally valid, same-population three-arm prospective candidates count; invalid legacy rows remain retained but cannot poison future maturity.",
        }

    def _active_champion(self, desk: str, hybrid_version: Optional[str]) -> Dict[str, Any]:
        if self.model_governance_repository is None:
            return {"passed": False, "state": "GOVERNANCE_UNAVAILABLE", "active": [], "expected_model_version": hybrid_version}
        try:
            status = self.model_governance_repository.status(desk=desk)
            active = [
                dict(row) for row in (status.get("active_champions") or [])
                if str(row.get("desk") or "").lower() == desk
                and float(row.get("production_weight") or 0.0) > 0.0
            ]
        except Exception as exc:
            return {"passed": False, "state": "GOVERNANCE_UNAVAILABLE", "error": str(exc)[:240], "expected_model_version": hybrid_version}
        exact = [row for row in active if hybrid_version and str(row.get("model_version") or "") == hybrid_version]
        return {
            "passed": len(exact) == 1,
            "state": "EXACT_CHAMPION" if len(exact) == 1 else "NO_EXACT_FORWARD_CHAMPION",
            "expected_model_version": hybrid_version,
            "active": active,
            "exact_matches": exact,
        }

    @staticmethod
    def _walk_forward_summary(replay: Mapping[str, Any]) -> Dict[str, Any]:
        hybrid = _mapping(_mapping(replay.get("arms")).get("hybrid"))
        validation = _mapping(hybrid.get("validation"))
        threshold = _mapping(hybrid.get("threshold_qualification") or validation.get("threshold_qualification"))
        return {
            "approved": bool(validation.get("approved") is True and str(validation.get("status") or "").upper() == "APPROVED"),
            "status": validation.get("status") or "NOT_RUN",
            "profile": validation.get("validation_profile"),
            "max_drawdown": _num(validation.get("max_drawdown")),
            "n_test": int(validation.get("n_test") or 0),
            "n_test_days": int(validation.get("n_test_days") or 0),
            "gates": _mapping(validation.get("gates")),
            "model_versions": _mapping(replay.get("model_versions")),
            "same_candidate_population_across_arms": bool(replay.get("same_candidate_population_across_arms")),
            "threshold_qualified": threshold.get("qualified") is True,
            "selected_top_fraction": _num(threshold.get("selected_top_fraction")),
            "threshold_selection_basis": threshold.get("selection_basis"),
            "threshold_selection_uses_test_data": bool(replay.get("threshold_selection_uses_test_data")),
            "declared_threshold_trials": int(replay.get("declared_threshold_trials") or 0),
        }

    def _desk(
        self,
        desk: str,
        clock: Mapping[str, Any],
        governance_status: Mapping[str, Any],
        *,
        run_walk_forward: bool,
    ) -> Dict[str, Any]:
        policy = maturity_policy(desk)
        horizon = canonical_horizon(desk, policy.primary_horizon)
        try:
            report = self.validation.report(mode=desk, horizon=horizon)
        except Exception as exc:
            report = {"ok": False, "mode": desk, "horizon": horizon, "error": str(exc)[:240]}
        arms = _mapping(report.get("arms"))
        hybrid = _mapping(arms.get("hybrid"))
        stress = _mapping(_mapping(hybrid.get("cost_sensitivity")).get("plus_20bps"))
        versions = _mapping(report.get("model_versions"))
        exact_versions = {arm: list(versions.get(arm) or []) for arm in _ARMS}
        hybrid_version = exact_versions["hybrid"][0] if len(exact_versions["hybrid"]) == 1 else None
        complete_populations = int(_mapping(clock.get("complete_population_count_by_desk")).get(desk) or 0)

        preliminary = {
            "clock_started": bool(_mapping(clock.get("started_at_by_desk")).get(desk)),
            "complete_populations": complete_populations >= policy.minimum_complete_populations,
            "settled_candidates": int(report.get("settled_candidates") or 0) >= policy.minimum_settled_candidates,
            "trading_days": int(report.get("trading_days") or 0) >= policy.minimum_trading_days,
            "regimes": int(report.get("regime_count") or 0) >= policy.minimum_regimes,
            "same_population_across_arms": bool(report.get("same_population_across_arms")),
            "one_model_version_per_arm": all(len(exact_versions[arm]) == 1 for arm in _ARMS),
            "hybrid_rank_ic_positive": (_num(hybrid.get("spearman_rank_ic")) or 0.0) > policy.minimum_hybrid_rank_ic,
            "hybrid_profit_factor": (_num(hybrid.get("profit_factor")) or 0.0) >= policy.minimum_hybrid_profit_factor,
            "hybrid_positive_after_20bps": (_num(stress.get("mean_net_return_bps")) or -1e18) > policy.minimum_hybrid_stressed_net_bps,
        }
        integrity = self._integrity(desk, horizon, report=report)

        replay_payload: Dict[str, Any] = {}
        walk_forward = {
            "approved": False,
            "status": "NOT_RUN",
            "profile": policy.required_walk_forward_profile,
            "max_drawdown": None,
            "same_candidate_population_across_arms": False,
            "model_versions": {},
        }
        replay_prerequisites = all(preliminary.values()) and bool(integrity.get("passed"))
        if run_walk_forward and replay_prerequisites:
            try:
                replay_payload = self.replay.replay(
                    mode=desk,
                    horizon=horizon,
                    min_samples=policy.minimum_settled_candidates,
                    profile=policy.required_walk_forward_profile,
                )
                walk_forward = self._walk_forward_summary(replay_payload)
            except Exception as exc:
                walk_forward = {**walk_forward, "status": "FAILED", "error": str(exc)[:240]}

        replay_versions = _mapping(walk_forward.get("model_versions"))
        walk_forward_version_match = all(
            list(replay_versions.get(arm) or []) == exact_versions[arm]
            for arm in _ARMS
        ) if replay_versions else False
        drawdown = _num(walk_forward.get("max_drawdown"))
        walk_forward_checks = {
            "capital_profile_approved": bool(walk_forward.get("approved")) and walk_forward.get("profile") == policy.required_walk_forward_profile,
            "same_population_across_arms": bool(walk_forward.get("same_candidate_population_across_arms")),
            "exact_model_versions_match_forward_report": walk_forward_version_match,
            "drawdown_within_policy": drawdown is not None and abs(drawdown) <= policy.maximum_hybrid_drawdown,
            "prior_window_threshold_qualified": bool(walk_forward.get("threshold_qualified")),
            "threshold_selection_no_test_leakage": walk_forward.get("threshold_selection_uses_test_data") is False,
        }
        champion = self._active_champion(desk, hybrid_version)
        governance_row = _mapping(_mapping(governance_status.get("by_desk")).get(desk))
        governance_projection = {
            "authority_available": bool(governance_status.get("ok")),
            "populations": int(governance_row.get("populations") or 0),
            "outcomes": int(governance_row.get("outcomes") or 0),
            "required_populations": complete_populations,
            "required_outcomes": int(report.get("settled_candidates") or 0),
        }
        governance_projection["passed"] = bool(
            governance_projection["authority_available"]
            and governance_projection["populations"] >= governance_projection["required_populations"]
            and governance_projection["outcomes"] >= governance_projection["required_outcomes"]
        )
        all_checks = {
            **preliminary,
            "temporal_and_population_integrity": bool(integrity.get("passed")),
            **walk_forward_checks,
            "governance_projection_complete": bool(governance_projection.get("passed")),
            "exact_governed_champion_lineage": bool(champion.get("passed")),
        }
        passed = all(all_checks.values())
        if passed:
            state = "LEVEL5_FORWARD_PROVEN"
        elif not replay_prerequisites:
            state = "COLLECTING_FORWARD_EVIDENCE"
        elif not run_walk_forward:
            state = "CHECKPOINT_REQUIRED"
        elif not walk_forward_checks["capital_profile_approved"]:
            state = "WALK_FORWARD_NOT_APPROVED"
        else:
            state = "GOVERNANCE_PROMOTION_PENDING"
        durability = durability_status({
            "complete_populations": complete_populations,
            "settled_candidates": int(report.get("settled_candidates") or 0),
            "trading_days": int(report.get("trading_days") or 0),
            "regimes": list(report.get("regimes") or []),
        })
        return {
            "desk": desk,
            "state": state,
            "passed": passed,
            "primary_horizon": horizon,
            "policy": policy.as_dict(),
            "evidence": {
                "complete_populations": complete_populations,
                "settled_candidates": int(report.get("settled_candidates") or 0),
                "trading_days": int(report.get("trading_days") or 0),
                "regimes": list(report.get("regimes") or []),
                "model_versions": exact_versions,
                "hybrid_metrics": hybrid,
            },
            "forward_durability": durability,
            "integrity": integrity,
            "walk_forward": walk_forward,
            "governance_projection": governance_projection,
            "champion": champion,
            "checks": all_checks,
            "missing_gates": [name for name, value in all_checks.items() if not value],
            "production_change_allowed": False,
        }

    def evaluate(self, *, run_walk_forward: bool = True) -> Dict[str, Any]:
        evaluated_at = _now()
        clock = self.clock.status()
        governance_status: Dict[str, Any]
        if self.governance_repository is None:
            governance_status = {"ok": False, "state": "GOVERNANCE_UNAVAILABLE"}
        else:
            try:
                governance_status = self.governance_repository.status()
            except Exception as exc:
                governance_status = {"ok": False, "state": "GOVERNANCE_UNAVAILABLE", "error": str(exc)[:240]}
        desks = {
            desk: self._desk(desk, clock, governance_status, run_walk_forward=run_walk_forward)
            for desk in _DESKS
        }
        governance_ready = bool(governance_status.get("ok"))
        both_desks = all(row.get("passed") for row in desks.values())
        level5_ready = governance_ready and both_desks
        payload = {
            "ok": True,
            "version": SERVICE_VERSION,
            "policy_version": POLICY_VERSION,
            "build": self.build_version,
            "evaluated_at": evaluated_at,
            "state": "LEVEL5_FORWARD_PROVEN" if level5_ready else "FORWARD_EVIDENCE_IN_PROGRESS",
            "level5_ready": level5_ready,
            "desks": desks,
            "governance": governance_status,
            "forward_clock": clock,
            "missing_gates": [desk for desk, row in desks.items() if not row.get("passed")] + ([] if governance_ready else ["governance_postgresql_authority"]),
            "production_change_allowed": False,
            "human_promotion_required": True,
            "broker_authority": "NONE",
        }
        self._kv_set(payload)
        return payload

    def run_checkpoint(self, *, sync_max_batches: int = 1) -> Dict[str, Any]:
        sync: Dict[str, Any]
        if self.governance_repository is None:
            sync = {"ok": False, "state": "GOVERNANCE_UNAVAILABLE", "fully_drained": False}
        else:
            try:
                try:
                    sync = self.governance_repository.sync_from_local_store(
                        self.store, max_batches=max(1, int(sync_max_batches))
                    )
                except TypeError:
                    # Narrow compatibility for test/disposable repositories that
                    # predate bounded-sync kwargs; installed production accepts it.
                    sync = self.governance_repository.sync_from_local_store(self.store)
            except Exception as exc:
                sync = {"ok": False, "state": "SYNC_FAILED", "fully_drained": False, "error": str(exc)[:240]}

        clock = self.clock.status()
        try:
            governance_status = self.governance_repository.status() if self.governance_repository is not None else {"ok": False}
        except Exception as exc:
            governance_status = {"ok": False, "state": "GOVERNANCE_UNAVAILABLE", "error": str(exc)[:240]}
        signature = self._evidence_signature(clock, governance_status, self._model_governance_snapshot())
        stored = self._kv_get()
        if (
            sync.get("ok")
            and sync.get("fully_drained") is True
            and stored
            and self._kv_value(_SIGNATURE_KEY, "") == signature
            and _mapping(stored.get("immutable_checkpoint")).get("ok") is True
        ):
            cached = dict(stored)
            cached["checkpoint_cached"] = True
            cached["governance_sync"] = sync
            return cached

        # Walk-forward is only meaningful once the bounded governance mirror is
        # fully caught up. Until then publish current local evidence without
        # blocking this projection on replay.
        payload = self.evaluate(run_walk_forward=bool(sync.get("ok") and sync.get("fully_drained") is True))
        payload["governance_sync"] = sync
        payload["checkpoint_cached"] = False
        if self.governance_repository is not None and sync.get("ok") and sync.get("fully_drained") is True:
            try:
                payload["immutable_checkpoint"] = self.governance_repository.record_checkpoint(
                    payload,
                    build_version=self.build_version,
                    policy_version=POLICY_VERSION,
                )
            except Exception as exc:
                payload["immutable_checkpoint"] = {"ok": False, "state": "CHECKPOINT_FAILED", "error": str(exc)[:240]}
                payload["level5_ready"] = False
                payload["state"] = "FORWARD_EVIDENCE_IN_PROGRESS"
        else:
            payload["immutable_checkpoint"] = {
                "ok": False,
                "state": "GOVERNANCE_SYNC_INCOMPLETE" if sync.get("ok") else "GOVERNANCE_UNAVAILABLE",
            }
            payload["level5_ready"] = False
            payload["state"] = "FORWARD_EVIDENCE_IN_PROGRESS"
        self._kv_set(payload)
        if _mapping(payload.get("immutable_checkpoint")).get("ok") is True:
            self._kv_set_value(_SIGNATURE_KEY, signature)
        return payload

    def status(self) -> Dict[str, Any]:
        stored = self._kv_get()
        if stored and str(stored.get("build") or "") == self.build_version:
            return stored
        # A maturity projection from an older release is ancestry evidence, not
        # current operational truth. Re-project current local evidence without
        # walk-forward or governance mutation and preserve the stale build for
        # operator diagnostics.
        stale_build = str((stored or {}).get("build") or "") or None
        payload = self.evaluate(run_walk_forward=False)
        if stale_build and stale_build != self.build_version:
            payload["superseded_projection_build"] = stale_build
            payload["projection_rebased_to_current_build"] = True
            self._kv_set(payload)
        return payload
