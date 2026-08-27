"""Fail-closed multi-stock ML population qualification.

Separates the ability to execute research code from the authority to train,
walk-forward validate, or influence Model Paper decisions.  A searched stock
may receive priority inference, but production training always uses the exact
point-in-time multi-stock population recorded by the research ledgers.
"""
from __future__ import annotations

from typing import Any, Dict, Mapping

from config import APP_VERSION, DATA_DIR
from core.forward_horizon_policy import canonical_horizon, durability_status, maturity_policy
from core.forward_progress_service import ForwardProgressService
from core.nse_cash_data_authority_service import NseCashDataAuthorityService
from core.level5_qualification_repository import Level5QualificationRepository
from models import now_iso


def _map(value: Any) -> Dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


class MLPopulationQualificationService:
    VERSION = "ml-population-qualification-1.3.0-cumulative-maturity-scope"
    KV_KEY = "ml_population_qualification:last"

    def __init__(self, app: Any):
        self.app = app

    @staticmethod
    def _source_map(authority: Mapping[str, Any]) -> Dict[str, Dict[str, Any]]:
        return {str(row.get("key")): dict(row) for row in authority.get("sources") or [] if isinstance(row, Mapping)}

    @staticmethod
    def _current(row: Mapping[str, Any]) -> bool:
        return str(_map(row.get("coverage")).get("state") or row.get("state") or "").upper() == "CURRENT"

    def _desk(self, desk: str, progress: Mapping[str, Any], forward: Mapping[str, Any], sources: Mapping[str, Mapping[str, Any]], evidence_start_ready: bool, all_source_maturity_ready: bool, scanner: Mapping[str, Any]) -> Dict[str, Any]:
        row = _map(_map(progress.get("by_desk")).get(desk))
        forward_row = _map(_map(forward.get("desks")).get(desk))
        evidence = _map(forward_row.get("evidence"))
        horizon = canonical_horizon(desk, "session" if desk == "intraday" else "20d")
        policy = maturity_policy(desk)
        population = int(row.get("population_candidates") or row.get("population_count") or 0)
        features = int(row.get("feature_rows") or 0)
        scanner_row = _map(_map(scanner.get("mode_scanners") or scanner.get("modes")).get(desk) or scanner.get(desk))
        scanner_analysis = _map(scanner_row.get("analysis")) or scanner_row
        scanner_analysed = int(scanner_analysis.get("cycle_scanned") or scanner_analysis.get("cycle_analyzed") or scanner_row.get("scanned") or 0)
        scanner_shortlisted = int(scanner_analysis.get("cycle_shortlisted") or scanner_analysis.get("shortlisted") or 0)
        scanner_blocked = int(scanner_analysis.get("cycle_blocked") or scanner_analysis.get("cycle_rejected") or scanner_row.get("blocked") or 0)
        current_population_settled = int(row.get("settled_outcomes") or 0)
        settled = int(evidence.get("settled_candidates") or current_population_settled)
        complete_populations = int(evidence.get("complete_populations") or 0)
        trading_days = int(evidence.get("trading_days") or 0)
        regimes = len(list(evidence.get("regimes") or []))
        durability = durability_status(evidence)
        gates = {
            "evidence_start_data_authority": evidence_start_ready,
            "all_source_maturity_authority": all_source_maturity_ready,
            "point_in_time_identity": self._current(sources.get("mii_security_file", {})),
            "corporate_action_authority": self._current(sources.get("corporate_actions", {})),
            "population_present": population > 0,
            "feature_population_complete": population > 0 and features == population,
            "complete_populations": complete_populations >= int(policy.minimum_complete_populations),
            "mature_labels": settled >= int(policy.minimum_settled_candidates),
            "trading_day_maturity": trading_days >= int(policy.minimum_trading_days),
            "same_population_three_arm": bool(row.get("same_population_three_arm")),
            "regime_coverage": regimes >= int(policy.minimum_regimes),
            "governance_projection": bool(_map(forward_row.get("governance_projection")).get("passed")),
            "forward_authority": bool(forward_row.get("passed")),
        }
        # Evidence-start sources are the governed boundary for beginning shadow
        # population construction/training. Enhancement/maturity sources (for
        # example corporate actions) remain hard gates for walk-forward maturity
        # but must not freeze all shadow learning until the full source programme
        # is complete. No production influence is granted here.
        can_construct_population = all(gates[key] for key in (
            "evidence_start_data_authority", "point_in_time_identity",
            "population_present", "feature_population_complete",
        ))
        can_train = can_construct_population
        evidence_clock_eligible = can_train and gates["same_population_three_arm"]
        can_walk_forward = (
            evidence_clock_eligible
            and gates["complete_populations"]
            and gates["mature_labels"]
            and gates["trading_day_maturity"]
            and gates["regime_coverage"]
            and gates["corporate_action_authority"]
            and gates["all_source_maturity_authority"]
        )
        can_influence = can_walk_forward and gates["governance_projection"] and gates["forward_authority"]
        if can_influence:
            state = "GOVERNED_INFLUENCE_ELIGIBLE"
        elif can_walk_forward:
            state = "WALK_FORWARD_AND_GOVERNANCE_PENDING"
        elif evidence_clock_eligible:
            state = "FORWARD_EVIDENCE_ACCUMULATING"
        elif can_train:
            state = "SHADOW_TRAINING_ELIGIBLE"
        elif population <= 0:
            state = "POPULATION_NOT_STARTED"
        elif features != population:
            state = "FEATURE_POPULATION_INCOMPLETE"
        elif settled < int(policy.minimum_settled_candidates):
            state = "LABEL_MATURITY_INCOMPLETE"
        else:
            state = "DATA_AUTHORITY_INCOMPLETE"
        return {
            "desk": desk,
            "state": state,
            "horizon": horizon,
            "population": population,
            "feature_rows": features,
            "current_population_settled_labels": current_population_settled,
            "settled_labels": settled,
            "settled_label_authority": "CUMULATIVE_GOVERNED_FORWARD_EVIDENCE",
            "complete_populations": complete_populations,
            "required_complete_populations": int(policy.minimum_complete_populations),
            "required_settled_labels": int(policy.minimum_settled_candidates),
            "trading_days": trading_days,
            "required_trading_days": int(policy.minimum_trading_days),
            "regimes": regimes,
            "required_regimes": int(policy.minimum_regimes),
            "forward_durability": durability,
            "can_execute_code": True,
            "can_construct_population": can_construct_population,
            "can_train": can_train,
            "evidence_clock_eligible": evidence_clock_eligible,
            "can_walk_forward": can_walk_forward,
            "can_influence": can_influence,
            "production_weight": 0.15 if can_influence else 0.0,
            "gates": gates,
            "missing_gates": [key for key, passed in gates.items() if not passed],
            "population_fingerprint": row.get("population_fingerprint"),
            "scanner_input": {"shortlisted": scanner_shortlisted, "analysed": scanner_analysed, "blocked": scanner_blocked, "state": scanner_analysis.get("state") or scanner_row.get("state")},
            "blocker": ("Scanner produced no analysed candidates, so immutable research population capture could not start." if population <= 0 and scanner_analysed <= 0 else row.get("blocker") or ("All qualification gates passed." if can_influence else "One or more hard qualification gates are incomplete.")),
        }

    def status(self) -> Dict[str, Any]:
        # One canonical NSE point-in-time evidence root. App-local aliases can
        # point at compatibility/runtime folders and must never fork maturity.
        authority_service = NseCashDataAuthorityService(getattr(self.app, "store", None), DATA_DIR)
        cached_reader = getattr(authority_service, "cached_status", None)
        authority = cached_reader() if callable(cached_reader) else authority_service.status()
        progress = ForwardProgressService(self.app.store).status()
        try:
            scanner_snapshot = self.app.scanner_status() or {}
            scanner = _map(scanner_snapshot.get("scanner")) or _map(scanner_snapshot)
        except Exception:
            scanner = {}
        try:
            forward = self.app.level5_forward_maturity.status()
        except Exception as exc:
            forward = {"ok": False, "state": "UNAVAILABLE", "error": str(exc)[:240], "desks": {}}
        summary = _map(authority.get("summary"))
        source_count = int(summary.get("source_count") or 0)
        current_count = int(summary.get("current_count") or 0)
        authority_ready = source_count > 0 and current_count == source_count
        tiers = _map(summary.get("tiers"))
        evidence_start_ready = bool(summary.get("evidence_start_ready") or _map(tiers.get("evidence_start")).get("ready"))
        sources = self._source_map(authority)
        desks = {desk: self._desk(desk, progress, forward, sources, evidence_start_ready, authority_ready, scanner) for desk in ("delivery", "intraday")}
        payload = {
            "ok": True,
            "version": self.VERSION,
            "build": APP_VERSION,
            "state": "INFLUENCE_ELIGIBLE" if all(row["can_influence"] for row in desks.values()) else "QUALIFICATION_IN_PROGRESS",
            "official_source_coverage": {"current": current_count, "total": source_count, "ready": authority_ready, "tiers": tiers, "evidence_start_ready": evidence_start_ready},
            "evidence_program_state": "FORWARD_CLOCK_RUNNING" if all(row["evidence_clock_eligible"] for row in desks.values()) else ("PARTIAL_FORWARD_START" if any(row["evidence_clock_eligible"] for row in desks.values()) else "EVIDENCE_START_PENDING"),
            "desks": desks,
            "training_policy": "AUTHORITATIVE_MULTI_STOCK_POINT_IN_TIME_POPULATION_ONLY",
            "priority_stock_policy": "SELECTED_STOCK_GETS_DATA_MATH_FEATURES_INFERENCE_AND_DECISION_EVIDENCE; NEVER_SINGLE_STOCK_PRODUCTION_TRAINING",
            "production_change_allowed": False,
            "broker_authority": "NONE",
            "captured_at": now_iso(),
        }
        try:
            payload["append_only_projection"] = Level5QualificationRepository(self.app.store).persist_ml(payload)
        except Exception as exc:
            payload["append_only_projection"] = {"state": "PROJECTION_FAILED", "persisted": False, "error": f"{type(exc).__name__}: {exc}"[:240]}
        try:
            self.app.store.set_kv(self.KV_KEY, payload)
        except Exception:
            pass
        return payload
