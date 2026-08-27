from __future__ import annotations

"""Bounded background read model for Research/control HTTP surfaces.

The browser must never execute governance fan-out, WFA evidence aggregation, or
model-tournament reads synchronously. This service owns those reads on one
supervised background lane and exposes the latest immutable in-memory snapshot.
"""

import copy
import json
import threading
import time
from typing import Any, Dict

from core.forward_progress_service import ForwardProgressService
from core.forward_evidence_clock_service import ForwardEvidenceClockService
from core.model_tournament_service import ModelTournamentService
from models import now_iso


SERVICE_VERSION = "research-control-projection-2.0.0-cache-only-http"


class ResearchControlProjectionService:
    def __init__(self, app: Any):
        self.app = app
        self._lock = threading.RLock()
        self._refresh_lock = threading.Lock()
        self._cache: Dict[str, Any] = {
            "service_version": SERVICE_VERSION,
            "state": "WARMING",
            "last_refresh": None,
            "last_error": None,
            "quant_research_plane": {
                "ok": False, "state": "WARMING", "runtime": {},
                "publication_authority": {}, "model_lifecycle": {},
                "production_influence": False, "broker_authority": "NONE",
                "read_model": "RESEARCH_CONTROL_PROJECTION",
            },
            "forward_progress": {
                "ok": False, "state": "WARMING", "by_desk": {},
                "production_change_allowed": False,
                "read_model": "RESEARCH_CONTROL_PROJECTION",
            },
            "forward_clock": {
                "ok": False, "state": "WARMING", "by_desk": {},
                "production_ml_influence": 0.0, "broker_authority": "NONE",
                "read_model": "RESEARCH_CONTROL_PROJECTION",
            },
        }

    @staticmethod
    def _as_map(value: Any) -> Dict[str, Any]:
        if isinstance(value, dict):
            return dict(value)
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
                return parsed if isinstance(parsed, dict) else {}
            except Exception:
                return {}
        return {}

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._cache)

    def quant_research_plane(self) -> Dict[str, Any]:
        return dict(self.snapshot().get("quant_research_plane") or {})

    def forward_progress(self) -> Dict[str, Any]:
        return dict(self.snapshot().get("forward_progress") or {})

    def forward_clock(self) -> Dict[str, Any]:
        return dict(self.snapshot().get("forward_clock") or {})

    def _build_quant_projection(self) -> Dict[str, Any]:
        plane = dict(getattr(self.app, "status", {}).get("quant_research_plane") or {})
        store = getattr(self.app, "store", None)
        repository = (
            getattr(store, "production_model_governance_read_repository", None)
            or getattr(store, "production_model_governance_repository", None)
        )
        publication = {"ok": False, "state": "UNAVAILABLE", "reason": "GOVERNANCE_REPOSITORY_UNAVAILABLE"}
        if repository is not None and hasattr(repository, "training_publication_status"):
            try:
                publication = repository.training_publication_status()
            except Exception as exc:
                publication = {"ok": False, "state": "UNAVAILABLE", "reason": str(exc)[:240]}

        try:
            tournament = ModelTournamentService(store).status()
        except Exception:
            tournament = {"ok": False, "experiments": []}
        tournament_rows = [dict(row or {}) for row in (tournament.get("experiments") or [])]

        lifecycle: Dict[str, Any] = {}
        for desk in ("intraday", "delivery"):
            row = dict((publication.get("latest_by_desk") or {}).get(desk) or {})
            validation = self._as_map(row.get("validation_payload"))
            model = self._as_map(row.get("model_payload"))
            folds = validation.get("walk_forward_folds") or model.get("walk_forward_folds") or []
            fold_count = len(folds) if isinstance(folds, list) else int(folds or 0)
            production_weight = float(row.get("production_weight") or 0.0)
            authority = publication.get("authority")
            if row:
                state = row.get("validation_state") or model.get("training_state") or "UNVALIDATED"
                training_state = model.get("training_state") or model.get("state") or row.get("lifecycle_state") or "UNVALIDATED"
                lifecycle_state = row.get("lifecycle_state") or "UNVALIDATED"
                model_id = row.get("model_key") or model.get("model_id")
                created_at = row.get("created_at")
                evaluation_weight = float(row.get("evaluation_paper_weight") or 0.0)
                publication_id = row.get("publication_id")
            else:
                candidates = [item for item in tournament_rows if str(item.get("mode") or "").lower() == desk]
                priority = {"ACTIVE_PRODUCTION": 4, "ACTIVE_VALIDATION": 3, "REJECTED": 2, "EXPERIMENT": 1}
                candidates.sort(
                    key=lambda item: (
                        priority.get(str(item.get("lifecycle_state") or "").upper(), 0),
                        str(item.get("updated_at") or ""),
                    ),
                    reverse=True,
                )
                fallback = candidates[0] if candidates else {}
                lifecycle_state = str(fallback.get("lifecycle_state") or "NO_PUBLICATION").upper()
                state = lifecycle_state
                training_state = lifecycle_state
                model_id = fallback.get("model_key")
                created_at = fallback.get("updated_at")
                evaluation_weight = 0.0
                publication_id = None
                production_weight = float(fallback.get("production_weight") or 0.0) if lifecycle_state == "ACTIVE_PRODUCTION" else 0.0
                authority = "MODEL_TOURNAMENT_READ_ONLY_FALLBACK" if fallback else publication.get("authority")
            production_influence = production_weight > 0 and str(lifecycle_state).upper() == "ACTIVE_PRODUCTION"
            if not production_influence:
                production_weight = 0.0
            lifecycle[desk] = {
                "state": state,
                "training_state": training_state,
                "lifecycle_state": lifecycle_state,
                "model_id": model_id,
                "publication_id": publication_id,
                "walk_forward_folds": fold_count,
                "evaluation_paper_weight": evaluation_weight,
                "production_weight": production_weight,
                "production_influence": production_influence,
                "created_at": created_at,
                "authority": authority,
                "candidate_count": len([item for item in tournament_rows if str(item.get("mode") or "").lower() == desk]),
            }

        try:
            closure = store.get_kv("operations_control:lifecycle_closure:v1", {}) or {}
        except Exception:
            closure = {}
        historical_wfa = dict((closure.get("results") or {}).get("walk_forward") or {})
        for desk in ("intraday", "delivery"):
            report = dict(historical_wfa.get(desk) or {})
            arm_folds, approved_arms, rejected_arms = [], [], []
            for arm in ("heuristic", "quant", "hybrid"):
                validation = dict(((report.get("arms") or {}).get(arm) or {}).get("validation") or {})
                arm_folds.append(len(list(validation.get("folds") or [])))
                (approved_arms if validation.get("approved") is True else rejected_arms).append(arm)
            selector_forward_only = bool(report.get("forward_evidence_only"))
            persisted_folds = int(lifecycle[desk].get("walk_forward_folds") or 0)
            lifecycle[desk]["historical_wfa_folds"] = max([persisted_folds] + arm_folds)
            lifecycle[desk]["historical_wfa_state"] = (
                "PASS" if approved_arms and not rejected_arms else
                "PARTIAL" if approved_arms else
                "PERSISTED_HISTORICAL_WFA" if selector_forward_only and persisted_folds > 0 else
                "NOT_RUN" if selector_forward_only else
                "REJECTED" if report else "NOT_RUN"
            )
            lifecycle[desk]["historical_wfa_approved_arms"] = approved_arms
            lifecycle[desk]["historical_wfa_rejected_arms"] = rejected_arms
            lifecycle[desk]["historical_wfa_train_days"] = report.get("historical_training_days") or report.get("requested_min_train_days")
            lifecycle[desk]["selector_forward_maturity_state"] = report.get("state") if selector_forward_only else None
            lifecycle[desk]["selector_forward_evidence_pending"] = report.get("forward_evidence_pending") if selector_forward_only else None
            lifecycle[desk]["historical_wfa_diagnostic_fallback_used"] = False if selector_forward_only else bool(report.get("diagnostic_fallback_used"))

        runtime_ready = plane.get("state") == "READY" and plane.get("ok") is True
        return {
            "ok": runtime_ready and publication.get("ok") is True,
            "state": "READY" if runtime_ready and publication.get("ok") is True else "BLOCKED",
            "runtime": plane,
            "publication_authority": publication,
            "model_lifecycle": lifecycle,
            "production_influence": any(row.get("production_influence") for row in lifecycle.values()),
            "production_weight_policy": "GOVERNED_ACTIVE_CAP_15_WITH_DETERMINISTIC_FALLBACK",
            "maximum_model_influence_pct": 15,
            "broker_authority": "NONE",
            "training_data_policy": "PARQUET_DUCKDB_ONLY",
            "lifecycle_closure": {
                "state": closure.get("state"), "stage": closure.get("stage"),
                "progress_pct": closure.get("progress_pct"),
                "completed_at": closure.get("completed_at"), "last_error": closure.get("last_error"),
            },
            "read_model": "RESEARCH_CONTROL_PROJECTION",
        }

    def refresh(self) -> Dict[str, Any]:
        if not self._refresh_lock.acquire(blocking=False):
            return self.snapshot()
        try:
            quant = self._build_quant_projection()
            try:
                progress = ForwardProgressService(self.app.store).status()
            except Exception as exc:
                progress = {"ok": False, "state": "UNAVAILABLE", "error": str(exc)[:240], "by_desk": {}, "production_change_allowed": False}
            try:
                clock = ForwardEvidenceClockService(self.app.store).status()
            except Exception as exc:
                clock = {"ok": False, "state": "UNAVAILABLE", "error": str(exc)[:240], "by_desk": {}, "production_ml_influence": 0.0, "broker_authority": "NONE"}
            stamp = now_iso()
            quant = {**quant, "projected_at": stamp}
            progress = {**dict(progress or {}), "read_model": "RESEARCH_CONTROL_PROJECTION", "projected_at": stamp}
            clock = {**dict(clock or {}), "read_model": "RESEARCH_CONTROL_PROJECTION", "projected_at": stamp}
            with self._lock:
                self._cache = {
                    "service_version": SERVICE_VERSION,
                    "state": "READY",
                    "last_refresh": stamp,
                    "last_error": None,
                    "quant_research_plane": quant,
                    "forward_progress": progress,
                    "forward_clock": clock,
                }
            return self.snapshot()
        except Exception as exc:
            with self._lock:
                self._cache["state"] = "DEGRADED"
                self._cache["last_error"] = f"{type(exc).__name__}: {exc}"[:300]
            return self.snapshot()
        finally:
            self._refresh_lock.release()

    def run(self, sup=None, running_fn=lambda: True) -> None:
        time.sleep(4.0)
        while running_fn() and (sup is None or sup.running):
            if sup:
                sup.beat("research_control_projection")
            snap = self.refresh()
            if sup:
                sup.progress(
                    "research_control_projection",
                    token=f"{snap.get('state')}:{snap.get('last_refresh')}:{(snap.get('quant_research_plane') or {}).get('state')}",
                    stage="cache_only_research_projection",
                    completed_units=1,
                    total_units=1,
                    waiting_on=snap.get("last_error"),
                    expected_idle=False,
                )
            time.sleep(45.0)
