"""Operator-visible job, log and safe-recovery control surface.

This service converts fragmented runtime snapshots into one truthful operations
model.  It exposes useful progress, not animated activity, and keeps all
mutations on a small allow-list.  It never changes risk limits, canonical
ledger rows, positions, model weights or database schemas.
"""
from __future__ import annotations

from collections import deque
import copy
from dataclasses import dataclass
from pathlib import Path
import hashlib
import json
import threading
import time
import re
from typing import Any, Dict, Iterable, Mapping

from config import APP_VERSION, LOG_DIR, ML_DELIVERY_TRAIN_MIN_DAYS, ML_DELIVERY_TRAIN_REFERENCE_DAYS, ML_DELIVERY_TRAIN_MAX_DAYS
from models import now_iso


@dataclass(frozen=True)
class OperationAction:
    action: str
    safety: str
    description: str


ACTIONS = {
    "recover_component": OperationAction("recover_component", "SAFE_COMPONENT", "Run the registered bounded recovery playbook"),
    "clear_circuit": OperationAction("clear_circuit", "SAFE_COMPONENT", "Clear a component circuit after the cause is addressed"),
    "force_history_sync": OperationAction("force_history_sync", "SAFE_COMPONENT", "Schedule an exact-gap selected-stock history sync"),
    "rebuild_selected_stock": OperationAction("rebuild_selected_stock", "SAFE_COMPONENT", "Rebuild MTF, levels and the selected-stock read model"),
    "rebuild_market_snapshot": OperationAction("rebuild_market_snapshot", "SAFE_COMPONENT", "Refresh required market and sector projections"),
    "resume_priority_pipeline": OperationAction("resume_priority_pipeline", "SAFE_COMPONENT", "Resume the selected-stock checkpointed pipeline"),
    "evaluate_controller": OperationAction("evaluate_controller", "SAFE_COMPONENT", "Run one controller diagnosis and action-selection cycle"),
    "pause_bulk": OperationAction("pause_bulk", "SAFE_COMPONENT", "Temporarily yield P4/P5 bulk workloads"),
    "resume_bulk": OperationAction("resume_bulk", "SAFE_COMPONENT", "Release the operator bulk-work pause"),
    "recover_all_safe_stuck": OperationAction("recover_all_safe_stuck", "SAFE_COMPONENT", "Recover up to five eligible safe stuck/no-progress components"),
    "advance_full_lifecycle": OperationAction("advance_full_lifecycle", "SAFE_COMPONENT", "Run the bounded scanner-to-research-to-WFA lifecycle closure proof in the background"),
    "run_end_to_end": OperationAction("run_end_to_end", "SAFE_COMPONENT", "Run the complete governed end-to-end process with automatic monitoring/recovery and reconciliation agents"),
}


class OperationsControlService:
    VERSION = "operations-control-centre-2.6.0-pl44-fold-local-capital-wfa"
    KV_KEY = "operations_control:actions:v1"
    LIFECYCLE_KV_KEY = "operations_control:lifecycle_closure:v1"

    def __init__(self, app: Any):
        self.app = app
        self._lock = threading.RLock()
        self._samples: Dict[str, tuple[float, float | None]] = {}
        self._action_ids: Dict[str, Dict[str, Any]] = {}
        self._recent_actions: deque[Dict[str, Any]] = deque(maxlen=300)
        self._projection_lock = threading.RLock()
        self._refresh_lock = threading.Lock()
        self._projection: Dict[str, Any] = {
            "ok": True, "state": "STARTING", "version": self.VERSION, "build": APP_VERSION,
            "time": now_iso(), "counts": {}, "jobs": [], "workload_governor": {},
            "database_pools": {}, "controller": {"state": "STARTING", "blockers": []},
            "recent_actions": [], "projection_age_sec": 0.0,
        }
        self._projection_monotonic = time.monotonic()
        self._projection_generation = 0
        self._log_projection: Dict[str, Any] = {
            "ok": True, "state": "STARTING", "time": now_iso(),
            "source_files": [], "lines": [],
            "message": "Backend log projection is warming; operations reads never scan files synchronously.",
        }
        self._log_projection_monotonic = time.monotonic()
        self._lifecycle_lock = threading.Lock()
        self._lifecycle_status: Dict[str, Any] = {
            "ok": True, "state": "NOT_RUN", "stage": "not_run",
            "completed": 0, "total": 8, "progress_pct": 0.0,
            "started_at": None, "completed_at": None, "last_error": None,
            "results": {}, "production_influence": 0.0, "broker_authority": "NONE",
            "agents": {
                "monitoring_recovery": {"state": "READY", "always_on": True, "actions": []},
                "reconciliation": {"state": "READY", "always_on": True, "checks": {}},
            },
        }
        try:
            persisted = app.store.get_kv(self.LIFECYCLE_KV_KEY, {}) or {}
            if isinstance(persisted, dict) and persisted:
                self._lifecycle_status = dict(persisted)
                if str(self._lifecycle_status.get("state") or "").upper() == "RUNNING":
                    self._lifecycle_status.update({
                        "state": "INTERRUPTED", "stage": "restart_reconciliation_required",
                        "last_error": "Prior lifecycle closure was interrupted by process restart; safe to run again.",
                    })
        except Exception:
            pass
        try:
            stored = app.store.get_kv(self.KV_KEY, []) or []
            if isinstance(stored, list):
                for row in stored[-300:]:
                    if isinstance(row, dict):
                        self._recent_actions.append(dict(row))
                        if row.get("action_id"):
                            self._action_ids[str(row["action_id"])] = dict(row)
        except Exception:
            pass

    @staticmethod
    def _num(value: Any) -> float | None:
        try:
            return float(value)
        except Exception:
            return None

    @staticmethod
    def _pct(done: Any, total: Any) -> float | None:
        try:
            done_f, total_f = float(done), float(total)
            if total_f <= 0:
                return None
            return round(max(0.0, min(100.0, done_f * 100.0 / total_f)), 1)
        except Exception:
            return None

    @staticmethod
    def _projection_progress_signature(payload: Mapping[str, Any]) -> str:
        """Hash only operator-relevant business state, never projection clocks.

        This signature is exposed for longitudinal UI/evidence comparison.  The
        projection worker itself uses an independent completed-generation counter
        because successfully rebuilding an unchanged read model is valid projection
        work while unchanged scanner/research counters must remain unchanged here.
        """
        jobs = []
        for raw in payload.get("jobs") or []:
            row = dict(raw or {})
            jobs.append({
                "job_id": row.get("job_id"),
                "state": row.get("state"),
                "stage": row.get("stage"),
                "completed": row.get("completed"),
                "total": row.get("total"),
                "progress_token": row.get("progress_token"),
                "waiting_on": row.get("waiting_on"),
                "last_error": row.get("last_error"),
            })
        jobs.sort(key=lambda row: str(row.get("job_id") or ""))
        controller = dict(payload.get("controller") or {})
        blocker = dict(controller.get("primary_blocker") or {})
        material = {
            "jobs": jobs,
            "controller": {
                "state": controller.get("state"),
                "primary_blocker": {
                    "key": blocker.get("key") or blocker.get("code"),
                    "state": blocker.get("state"),
                    "component": blocker.get("component"),
                },
            },
        }
        return hashlib.sha256(
            json.dumps(material, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
        ).hexdigest()

    def _rate(self, job_id: str, completed: float | None) -> float | None:
        now = time.time()
        with self._lock:
            previous = self._samples.get(job_id)
            self._samples[job_id] = (now, completed)
        if previous is None or completed is None or previous[1] is None:
            return None
        elapsed = now - previous[0]
        if elapsed <= 0:
            return None
        delta = completed - previous[1]
        # Scanner counters reset at a new sweep/generation. A reset is not a
        # negative processing rate; expose it as an unmeasured new generation.
        if delta < 0:
            return None
        return round(delta * 60.0 / elapsed, 2)

    @staticmethod
    def _normalise_state(row: Mapping[str, Any]) -> str:
        state = str(row.get("state") or "UNKNOWN").upper()
        if bool(row.get("expected_idle")) or state in {"EXPECTED_IDLE", "PAUSED", "MARKET_CLOSED", "WAITING_RETRY"}:
            return "EXPECTED_IDLE"
        waiting = str(row.get("waiting_on") or "").lower()
        if state == "RUNNING" and any(token in waiting for token in ("retry schedule", "next governed cycle", "higher priority", "market closed", "scheduled retry")):
            # A supervised worker that is deliberately waiting on its declared
            # retry/yield condition is healthy expected-idle, not STUCK.
            return "EXPECTED_IDLE"
        progress_age = float(row.get("progress_age_sec") or 0.0)
        heartbeat_age = float(row.get("heartbeat_age_sec") or 0.0)
        if state == "RUNNING":
            if heartbeat_age >= 180:
                return "STUCK"
            if int(row.get("progress_count") or 0) == 0 and progress_age >= 120:
                return "UNINSTRUMENTED" if row.get("completed_units") is None else "NO_PROGRESS"
            if row.get("progress_token") in {None, ""} and progress_age >= 300:
                return "UNINSTRUMENTED"
        return state

    def _allowed_actions(self, component: str, row: Mapping[str, Any]) -> list[str]:
        safety = str(row.get("safety_class") or "SAFE_COMPONENT").upper()
        if safety in {"RISK_AUTHORITY", "LEDGER_AUTHORITY", "DATABASE_AUTHORITY"}:
            return []
        actions = []
        state = self._normalise_state(row)
        if row.get("recovery_available") and state in {"NO_PROGRESS", "UNINSTRUMENTED", "STUCK", "FAILED", "DEAD", "RECOVERED_WITH_ERROR", "CIRCUIT_OPEN"}:
            actions.append("recover_component")
        if row.get("circuit_open"):
            actions.append("clear_circuit")
        if component in {"deep_history_backfill", "data_conveyor", "delivery_scanner", "intraday_scanner"}:
            actions.append("pause_bulk")
        return list(dict.fromkeys(actions))

    def _supervisor_jobs(self) -> list[Dict[str, Any]]:
        snapshot = dict(self.app.supervisor.snapshot() or {})
        jobs = []
        worker_health = dict((getattr(self.app, "status", {}) or {}).get("worker_health") or {})
        for component, raw in sorted(snapshot.items()):
            row = dict(raw or {})
            cadence = dict(worker_health.get(component) or {})
            done = self._num(row.get("completed_units"))
            total = self._num(row.get("total_units"))
            state = self._normalise_state(row)
            title_map = {
                "intraday_scanner": "Intraday Live Analysis",
                "intraday_coverage": "Intraday Universe Sweep",
                "delivery_scanner": "Delivery Deep Analysis",
                "delivery_coverage": "Delivery Universe Sweep",
            }
            # Coverage and deep/live analysis are separate authorities.  Preserve
            # the last completed immutable sweep while a new sweep is in progress
            # so operator/UI projections cannot look as though a proven full sweep
            # regressed back to zero.
            desk = "intraday" if component.startswith("intraday_") else "delivery" if component.startswith("delivery_") else None
            progress = {}
            if desk and component.endswith("_coverage"):
                mode_root = dict(((getattr(self.app, "status", {}) or {}).get("mode_scanners") or {}).get(desk) or {})
                progress = dict(mode_root.get("progress_contract") or {})
                if progress:
                    current = self._num(progress.get("current_sweep_scanned"))
                    population = self._num(progress.get("population_count"))
                    if current is not None:
                        done = current
                    if population is not None:
                        total = population

            job = {
                "job_id": f"loop:{component}",
                "component": component,
                "title": title_map.get(component, component.replace("_", " ").title()),
                "state": state,
                "stage": row.get("stage"),
                "current_item": row.get("current_item"),
                "completed": int(done) if done is not None else None,
                "total": int(total) if total is not None else None,
                "progress_pct": self._pct(done, total),
                "rate_per_min": self._rate(f"loop:{component}", done),
                "heartbeat_age_sec": row.get("heartbeat_age_sec"),
                "progress_age_sec": row.get("progress_age_sec"),
                "last_progress_at": row.get("last_progress_at"),
                "last_heartbeat_at": row.get("last_heartbeat_at"),
                "last_cycle_at": cadence.get("last_completed_at"),
                "next_cycle_at": cadence.get("next_run_at"),
                "seconds_to_next": cadence.get("seconds_to_next"),
                "display_state": ("SLEEPING" if state == "EXPECTED_IDLE" and component in {"intraday_scanner","delivery_scanner","intraday_coverage","delivery_coverage"} and cadence.get("next_run_at") else state),
                "waiting_on": row.get("waiting_on"),
                "last_error": row.get("last_error"),
                "recovery_available": bool(row.get("recovery_available")),
                "recovery_count": int(row.get("recovery_count") or 0),
                "last_recovery_action": row.get("last_recovery_action"),
                "last_recovery_result": row.get("last_recovery_result"),
                "circuit_open": bool(row.get("circuit_open")),
                "safety_class": row.get("safety_class") or "SAFE_COMPONENT",
                "allowed_actions": self._allowed_actions(component, row),
                "progress_token": row.get("progress_token"),
                "current_sweep_number": progress.get("current_sweep_number") if progress else None,
                "current_sweep_scanned": progress.get("current_sweep_scanned") if progress else None,
                "last_completed_sweep_count": progress.get("last_completed_sweep_count") if progress else None,
                "last_completed_at": progress.get("last_completed_at") if progress else None,
                "full_sweep_proven": bool(progress and self._num(progress.get("last_completed_sweep_count")) is not None and self._num(progress.get("population_count")) is not None and self._num(progress.get("last_completed_sweep_count")) >= self._num(progress.get("population_count"))),
                "display_detail": progress.get("display_detail") if progress else None,
            }
            jobs.append(job)
        return jobs

    def lifecycle_status(self) -> Dict[str, Any]:
        # P0-04: a shallow dict(...) here shared the caller's live, still-
        # mutating nested "results"/"agents" objects with every previously
        # returned snapshot. A caller could hold a status from stage
        # "research_reconciliation" (completed 1/8) and see it silently
        # display stage 4's results once _run_full_lifecycle mutated the same
        # dict in place. Every read must be causally frozen at read time.
        with self._lifecycle_lock:
            return copy.deepcopy(self._lifecycle_status)

    def _publish_lifecycle(self, **updates: Any) -> Dict[str, Any]:
        # P0-04: deep-copy incoming updates before they enter persisted state.
        # _run_full_lifecycle passes the SAME "results"/"agents" dict objects
        # into every publish call and keeps mutating them afterwards; without
        # a deep copy here, an earlier "published" snapshot is not actually a
        # snapshot -- it is a live view into state that has not happened yet.
        with self._lifecycle_lock:
            frozen_updates = copy.deepcopy(updates)
            self._lifecycle_status = {**self._lifecycle_status, **frozen_updates}
            payload = copy.deepcopy(self._lifecycle_status)
        try:
            self.app.store.set_kv(self.LIFECYCLE_KV_KEY, payload)
        except Exception:
            pass
        return payload

    @staticmethod
    def _wfa_fold_count(report: Mapping[str, Any]) -> int:
        counts = []
        for arm in ("heuristic", "quant", "hybrid"):
            validation = dict(((report.get("arms") or {}).get(arm) or {}).get("validation") or {})
            counts.append(len(list(validation.get("folds") or [])))
        return max(counts or [0])

    RESEARCH_RECOVERABLE_BLOCKERS = {"THREE_ARM_INCOMPLETE", "FEATURES_INCOMPLETE"}
    RESEARCH_TERMINAL_BLOCKED = {"PAPER_ADMISSION_BLOCKED", "PAPER_MODEL_TRAINING_BLOCKED"}
    # Everything else (NOT_STARTED, FEATURE_EVIDENCE_PENDING, *_WAITING,
    # MONITORING, *_PENDING, SETTLEMENT_ACTIVE, PAPER_ADMISSION_NOT_SELECTED)
    # is expected-wait: the lifecycle is progressing normally on its own
    # cadence and is not an actionable blocker.

    def _research_semantic_blockers(self) -> Dict[str, Any]:
        """Classify Research lifecycle state per desk as recoverable /
        expected-wait / terminal, independent of supervisor worker liveness.

        P0-03: the monitoring/recovery agent previously only looked at
        supervisor job states (NO_PROGRESS/STUCK/FAILED/DEAD/...) and could
        report HEALTHY while Research itself was semantically blocked
        (FEATURES_INCOMPLETE / THREE_ARM_CAPTURE_INCOMPLETE) because no
        worker process had actually crashed.
        """
        from core.research_lifecycle_reconciliation_service import ResearchLifecycleReconciliationService

        try:
            status = ResearchLifecycleReconciliationService(self.app.store).status()
        except Exception as exc:
            return {"ok": False, "error": str(exc)[:300], "recoverable": [], "terminal": []}
        by_desk = dict(status.get("by_desk") or {})
        recoverable, terminal = [], []
        for desk, row in by_desk.items():
            state = str((row or {}).get("state") or "").upper()
            if state in self.RESEARCH_RECOVERABLE_BLOCKERS:
                recoverable.append(desk)
            elif state in self.RESEARCH_TERMINAL_BLOCKED:
                terminal.append(desk)
        return {"ok": True, "by_desk": by_desk, "recoverable": recoverable, "terminal": terminal}

    def _monitoring_agent_pass(self, *, reason: str) -> Dict[str, Any]:
        """Run one bounded monitoring/recovery pass without weakening any authority.

        The always-on supervisor/autonomic controller remains the continuous
        monitoring authority.  The one-click lifecycle asks it for an immediate
        evaluation and then applies only existing SAFE_COMPONENT recovery
        playbooks.  Risk, ledger, database and broker authorities are never
        mutated here.
        """
        actionable_states = {
            "NO_PROGRESS", "UNINSTRUMENTED", "STUCK", "FAILED", "DEAD",
            "RECOVERED_WITH_ERROR", "CIRCUIT_OPEN",
        }
        attempts: list[Dict[str, Any]] = []
        priority_recovery: Dict[str, Any] = {}
        try:
            priority_recovery = dict(self.app.priority_pipeline.recover_stale(max_recoveries=3) or {})
        except Exception as exc:
            priority_recovery = {"ok": False, "state": "PRIORITY_RECOVERY_EXCEPTION", "error": str(exc)[:300]}
        for job in self._supervisor_jobs():
            if len(attempts) >= 5:
                break
            if str(job.get("safety_class") or "").upper() != "SAFE_COMPONENT":
                continue
            if "recover_component" not in list(job.get("allowed_actions") or []):
                continue
            if str(job.get("state") or "").upper() not in actionable_states:
                continue
            component = str(job.get("component") or "")
            if not component:
                continue
            try:
                row = dict(self.app.supervisor.recover(component, reason=reason, action="E2E_MONITORING_AGENT") or {})
            except Exception as exc:
                row = {"ok": False, "state": "RECOVERY_EXCEPTION", "error": str(exc)[:300]}
            attempts.append({"component": component, **row})
        try:
            controller = dict(self.app.autonomic_controller.request_evaluation(allow_action=True, reason=reason) or {})
        except Exception as exc:
            controller = {"ok": False, "state": "CONTROLLER_EXCEPTION", "error": str(exc)[:300]}
        active = [
            row for row in self._supervisor_jobs()
            if str(row.get("state") or "").upper() in actionable_states
        ]
        research = self._research_semantic_blockers()
        if research.get("recoverable"):
            from core.research_lifecycle_advance_service import ResearchLifecycleAdvanceService
            try:
                research_repair = ResearchLifecycleAdvanceService(self.app).run(
                    settlement_limit=160, advance_settlement=False,
                )
            except Exception as exc:
                research_repair = {"ok": False, "state": "RESEARCH_REPAIR_EXCEPTION", "error": str(exc)[:300]}
            attempts.append({
                "component": "research_lifecycle", "ok": bool(research_repair.get("ok")),
                "state": research_repair.get("state") or ("RECOVERED" if research_repair.get("ok") else "RECOVERY_REQUIRED"),
                "desks": research.get("recoverable"), "detail": "semantic_research_blocker_bounded_repair",
            })
        failures = [row for row in attempts if not bool(row.get("ok"))]
        research_semantic_blocker = bool(research.get("recoverable")) or bool(research.get("terminal"))
        state = (
            "RECOVERY_REQUIRED" if failures
            else "RESEARCH_BLOCKED" if research.get("terminal")
            else "RECOVERED" if attempts
            else "WATCHING" if (active or research_semantic_blocker)
            else "HEALTHY"
        )
        return {
            "ok": not bool(failures) and not research.get("terminal"),
            "state": state,
            "always_on": True,
            "evaluated_at": now_iso(),
            "reason": reason,
            "attempts": attempts,
            "active_actionable_count": len(active),
            "research_semantic_blockers": research,
            "priority_pipeline_recovery": priority_recovery,
            "controller": controller,
            "policy": "bounded SAFE_COMPONENT recovery only; risk/ledger/database/broker authorities are immutable",
        }

    def _reconciliation_agent_pass(self, *, reason: str) -> Dict[str, Any]:
        """Run the existing deterministic reconciliation authorities as one agent."""
        from core.decision_surface_reconciliation_service import DecisionSurfaceReconciliationService
        from core.research_lifecycle_reconciliation_service import ResearchLifecycleReconciliationService
        checks: Dict[str, Any] = {}
        try:
            checks["model_paper_settlement"] = dict(self.app.settlement_reconciliation.run_once(limit=200) or {})
        except Exception as exc:
            checks["model_paper_settlement"] = {"state": "ERROR", "error": str(exc)[:300]}
        try:
            checks["signal_lifecycle"] = dict(self.app.signal_lifecycle_reconciliation.run_once(limit=500) or {})
        except Exception as exc:
            checks["signal_lifecycle"] = {"state": "ERROR", "error": str(exc)[:300]}
        try:
            checks["research_lifecycle"] = dict(ResearchLifecycleReconciliationService(self.app.store).status() or {})
        except Exception as exc:
            checks["research_lifecycle"] = {"state": "ERROR", "error": str(exc)[:300]}
        try:
            checks["decision_surfaces"] = dict(DecisionSurfaceReconciliationService(self.app).status() or {})
        except Exception as exc:
            checks["decision_surfaces"] = {"state": "ERROR", "error": str(exc)[:300]}
        try:
            checks["priority_pipeline"] = dict(self.app.priority_pipeline.recovery_status() or {})
        except Exception as exc:
            checks["priority_pipeline"] = {"state": "ERROR", "error": str(exc)[:300]}
        errors = [name for name,row in checks.items() if str((row or {}).get("state") or "").upper() in {"ERROR","FAILED","EXCEPTION"}]
        blockers = []
        for name,row in checks.items():
            state = str((row or {}).get("state") or "").upper()
            if any(token in state for token in ("BLOCKED", "INCOMPLETE", "PENDING")):
                blockers.append(name)
        return {
            "ok": not bool(errors),
            "state": "ERROR" if errors else ("RECONCILED_WITH_BLOCKERS" if blockers else "RECONCILED"),
            "always_on": True,
            "evaluated_at": now_iso(),
            "reason": reason,
            "checks": checks,
            "errors": errors,
            "blockers": blockers,
            "policy": "durable-authority reconciliation only; no inferred trades, outcomes, alpha or promotion",
        }

    def _run_full_lifecycle(self) -> None:
        from core.research_lifecycle_advance_service import ResearchLifecycleAdvanceService
        from core.selection_walk_forward_replay_service import SelectionWalkForwardReplayService
        stages = [
            "request_scanners", "research_reconciliation", "settlement",
            "historical_delivery_training_capital_wfa",
            "delivery_forward_maturity", "intraday_forward_maturity",
            "refresh_read_models", "final_reconciliation",
        ]
        results: Dict[str, Any] = {}
        governor = getattr(self.app, "workload_governor", None)
        if governor is not None:
            try:
                # PL44: Run End-to-End must not pause the P5 historical trainer it
                # is explicitly responsible for advancing.  Release any stale
                # operator pause and let the governor yield only to real live-risk,
                # database-recovery or interactive priority.
                governor.resume_bulk()
                results["workload_window"] = "BACKGROUND_BULK_ENABLED_FOR_E2E"
            except Exception as exc:
                results["workload_window_warning"] = str(exc)[:240]
        agents = {
            "monitoring_recovery": {"state": "STARTING", "always_on": True, "actions": []},
            "reconciliation": {"state": "STARTING", "always_on": True, "checks": {}},
        }
        self._publish_lifecycle(
            ok=True, state="RUNNING", stage=stages[0], completed=0, total=len(stages),
            progress_pct=0.0, started_at=now_iso(), completed_at=None, last_error=None,
            results=results, agents=agents, production_influence=0.0, broker_authority="NONE",
        )
        try:
            # Stage 1: request both governed scanners. This is asynchronous and
            # never claims sweep completion merely because the request is accepted.
            scan_requests = {}
            for desk in ("intraday", "delivery"):
                try:
                    scan_requests[desk] = dict(self.app.scan_orchestration.request_scan(desk) or {})
                except Exception as exc:
                    scan_requests[desk] = {"accepted": False, "error": str(exc)[:300]}
            results["scan_requests"] = scan_requests
            agents["monitoring_recovery"] = self._monitoring_agent_pass(reason="e2e_after_scanner_request")
            results["monitoring_recovery_agent"] = agents["monitoring_recovery"]
            self._publish_lifecycle(stage=stages[1], completed=1, progress_pct=round(100/len(stages),1), results=results, agents=agents)

            # Stages 2/3: recover immutable Research populations from the current
            # canonical decision/opportunity evidence, evaluate all three arms via
            # the deterministic bootstrap/qualified model path, and advance due
            # forward settlement. Invalid geometry/identity/freshness remains a blocker.
            advance = ResearchLifecycleAdvanceService(self.app).run(settlement_limit=160, advance_settlement=True)
            results["research_advance"] = advance
            agents["reconciliation"] = self._reconciliation_agent_pass(reason="e2e_after_research_advance")
            results["reconciliation_agent"] = agents["reconciliation"]
            self._publish_lifecycle(stage=stages[3], completed=3, progress_pct=round(300/len(stages),1), results=results, agents=agents)

            # Stage 4: execute the real retained-history Delivery training path.
            # This owns catalogue/corporate-action reconciliation, fold-local
            # purged WFA training and atomic research+capital publication.  It is
            # deliberately separate from the prospective selector maturity replay.
            training_service = getattr(self.app, "historical_pit_sweep", None)
            if training_service is None or not callable(getattr(training_service, "run_on_demand", None)):
                historical_training = {
                    "ok": False, "state": "HISTORICAL_TRAINING_AUTHORITY_UNAVAILABLE",
                    "error": "canonical HistoricalPitSweepService.run_on_demand is unavailable",
                }
            else:
                try:
                    historical_training = dict(training_service.run_on_demand(
                        running_fn=lambda: bool(getattr(getattr(self.app, "supervisor", None), "running", True)),
                        reason="run_end_to_end_fold_local_capital_wfa",
                    ) or {})
                except Exception as exc:
                    historical_training = {
                        "ok": False, "state": "HISTORICAL_TRAINING_EXECUTION_ERROR",
                        "error": f"{type(exc).__name__}: {exc}"[:800],
                    }
            results["historical_delivery_training"] = historical_training
            capital_payload = dict(historical_training.get("capital_validation") or {})
            # A no-op trainer check may omit the already-persisted payload. Read it
            # back from the authoritative governance repository so Run End-to-End
            # never reports NOT_RUN/503 merely because no new fit was required.
            model_id = str(historical_training.get("model_id") or "").strip()
            capital_readback_source = "trainer_result" if capital_payload else None
            if (not capital_payload or str(capital_payload.get("status") or "").upper() not in {"APPROVED", "REJECTED"}) and model_id:
                read_repo = (
                    getattr(self.app.store, "production_model_governance_read_repository", None)
                    or getattr(self.app.store, "production_model_governance_repository", None)
                )
                if read_repo is not None and callable(getattr(read_repo, "training_validation_evidence", None)):
                    try:
                        evidence_rows = list((read_repo.training_validation_evidence(
                            model_key=model_id, profile="capital", limit=1
                        ) or {}).get("evidence") or [])
                        if evidence_rows:
                            capital_payload = dict(evidence_rows[0] or {})
                            capital_readback_source = "governance_postgresql"
                    except Exception as exc:
                        results["historical_capital_wfa_readback_warning"] = str(exc)[:300]
            results["historical_capital_wfa"] = {
                "status": capital_payload.get("status"),
                "approved": bool(capital_payload.get("approved")),
                "validation_kind": capital_payload.get("validation_kind"),
                "fold_local_training_requested": capital_payload.get("fold_local_training_requested"),
                "fold_local_training_proven": capital_payload.get("fold_local_training_proven"),
                "capital_model_training_proven": capital_payload.get("capital_model_training_proven"),
                "approval_id": capital_payload.get("approval_id"),
                "readback_source": capital_readback_source,
                "publication": historical_training.get("publication"),
            }
            agents["monitoring_recovery"] = self._monitoring_agent_pass(reason="e2e_after_historical_capital_wfa")
            results["monitoring_recovery_agent"] = agents["monitoring_recovery"]
            self._publish_lifecycle(stage=stages[4], completed=4, progress_pct=round(400/len(stages),1), results=results, agents=agents)

            # Stages 5/6 are prospective selector maturity only. They never stand
            # in for historical model training or persisted capital WFA.
            replay_service = SelectionWalkForwardReplayService(self.app.store)
            wfa = {}
            repo = getattr(self.app.store, "production_model_governance_read_repository", None) or getattr(self.app.store, "production_model_governance_repository", None)
            for idx, desk in enumerate(("delivery", "intraday"), start=5):
                horizon = "10d" if desk == "delivery" else "30m"
                evidence = {}
                if repo is not None and callable(getattr(repo, "selector_evidence_status", None)):
                    try:
                        evidence = dict(repo.selector_evidence_status(desk) or {})
                    except Exception as exc:
                        evidence = {"error": str(exc)[:240]}
                label_days = int(evidence.get("label_days") or 0)
                purge_hint = 10 if desk == "delivery" else 1
                options = (1260, 1008, 756, 504, 252)
                viable = [days for days in options if label_days >= days + 63 + 1 + purge_hint]
                chosen = max(viable) if viable else 252
                evidence_error = str(evidence.get("error") or "").strip()
                complete_snapshots = int(evidence.get("complete_snapshots") or 0)
                minimum_calendar_depth = chosen + 63 + 1 + purge_hint
                if evidence_error:
                    primary = {
                        "ok": False, "state": "EVIDENCE_STATUS_UNAVAILABLE",
                        "error": evidence_error[:800],
                        "fold_blocker": "SELECTOR_EVIDENCE_STATUS_UNAVAILABLE",
                        "production_influence": 0.0, "broker_authority": "NONE",
                    }
                elif complete_snapshots <= 0 or label_days < minimum_calendar_depth:
                    # Prospective selector evidence is intentionally short early in
                    # product life. It controls future promotion/maturity only and
                    # must never block historical ML training/WFA from retained PIT
                    # history. Keep the deficit explicit without calling it a
                    # historical-training failure.
                    primary = {
                        "ok": True, "state": "FORWARD_MATURITY_PENDING",
                        "fold_blocker": None,
                        "forward_evidence_pending": (
                            f"FORWARD_SELECTOR_EVIDENCE_PENDING:complete_snapshots={complete_snapshots};"
                            f"label_days={label_days};required_calendar_days={minimum_calendar_depth}"
                        ),
                        "arms": {}, "population_count": 0, "settled_candidate_count": 0,
                        "production_influence": 0.0, "broker_authority": "NONE",
                    }
                else:
                    try:
                        primary = replay_service.replay(
                            mode=desk, horizon=horizon, min_train_days=chosen, test_days=63,
                            max_folds=1000, embargo_days=1, min_samples=300, profile="capital",
                        )
                    except Exception as exc:
                        # One WFA desk is an isolated research evaluation stage. Its
                        # execution failure must remain explicit evidence, but must not
                    # abort the orchestration before read-model refresh and final
                        # reconciliation can expose the complete product truth.
                        primary = {
                            "ok": False, "state": "EXECUTION_ERROR",
                            "error": f"{type(exc).__name__}: {exc}"[:800],
                            "fold_blocker": "WFA_EXECUTION_ERROR",
                            "production_influence": 0.0, "broker_authority": "NONE",
                        }
                primary["requested_min_train_days"] = int(ML_DELIVERY_TRAIN_MIN_DAYS)
                primary["historical_training_reference_days"] = int(ML_DELIVERY_TRAIN_REFERENCE_DAYS)
                primary["historical_training_days"] = None
                primary["historical_training_depth_semantics"] = "resolved independently by retained eligible history per symbol/mode; reference is not a cap"
                primary["historical_training_minimum_days"] = int(ML_DELIVERY_TRAIN_MIN_DAYS)
                primary["historical_training_maximum_days"] = int(ML_DELIVERY_TRAIN_MAX_DAYS)
                primary["historical_training_window_policy"] = "ADAPTIVE_ALL_ELIGIBLE_HISTORY_BY_SYMBOL_AND_MODE"
                primary["historical_training_authority"] = "RETAINED_PIT_PARQUET_DUCKDB"
                primary["forward_selector_requested_min_train_days"] = chosen
                primary["forward_evidence_only"] = True
                primary["deepest_predeclared_training_options"] = list(options)
                primary["selector_evidence_status_before_replay"] = evidence
                primary["diagnostic_fallback_used"] = chosen == 252 and not viable
                primary["qualification_minimum_train_days"] = 252
                primary["qualification_weakened"] = False
                wfa[desk] = primary
                results["walk_forward"] = wfa
                agents["monitoring_recovery"] = self._monitoring_agent_pass(reason=f"e2e_after_{desk}_wfa")
                results["monitoring_recovery_agent"] = agents["monitoring_recovery"]
                self._publish_lifecycle(stage=stages[idx], completed=idx, progress_pct=round(idx*100/len(stages),1), results=results, agents=agents)

            # Stage 6: refresh background materialized views so Model Paper,
            # Accuracy/Performance and Research immediately project the new truth.
            try:
                self.app.operator_read_models.refresh()
            except Exception as exc:
                results["read_model_refresh_warning"] = str(exc)[:300]
            self._publish_lifecycle(stage=stages[6], completed=6, progress_pct=round(600/len(stages),1), results=results)

            # Stage 8: exact reconciled lifecycle status. No false success: explicit
            # blockers remain in the result while orchestration itself completes.
            from core.research_lifecycle_reconciliation_service import ResearchLifecycleReconciliationService
            self._publish_lifecycle(stage=stages[7], completed=7, progress_pct=round(700/len(stages),1), results=results, agents=agents)
            agents["reconciliation"] = self._reconciliation_agent_pass(reason="e2e_final_reconciliation")
            results["reconciliation_agent"] = agents["reconciliation"]
            reconciliation = ResearchLifecycleReconciliationService(self.app.store).status()
            results["final_reconciliation"] = reconciliation
            wfa_execution_errors = [desk for desk, row in wfa.items() if str((row or {}).get("state") or "").upper() == "EXECUTION_ERROR"]
            historical_training_error = (
                historical_training.get("ok") is False
                or str(results.get("historical_capital_wfa", {}).get("status") or "").upper() not in {"APPROVED", "REJECTED"}
                or results.get("historical_capital_wfa", {}).get("fold_local_training_requested") is not True
                or results.get("historical_capital_wfa", {}).get("fold_local_training_proven") is not True
            )
            research_blocked = any(
                str((reconciliation.get("by_desk") or {}).get(desk, {}).get("state") or "") in {"NOT_STARTED", "FEATURES_INCOMPLETE", "FEATURE_EVIDENCE_PENDING", "THREE_ARM_INCOMPLETE", "PAPER_ADMISSION_PENDING"}
                for desk in ("delivery", "intraday")
            )
            execution_errors = list(wfa_execution_errors)
            if historical_training_error:
                execution_errors.append("historical_delivery_training_capital_wfa")
            state = "COMPLETE_WITH_EXECUTION_ERRORS" if execution_errors else ("COMPLETE_WITH_EXPLICIT_BLOCKERS" if research_blocked else "COMPLETE")
            self._publish_lifecycle(
                ok=not bool(execution_errors), state=state, stage="complete", completed=len(stages), total=len(stages),
                progress_pct=100.0, completed_at=now_iso(), results=results, agents=agents, last_error=None,
            )
        except Exception as exc:
            self._publish_lifecycle(
                ok=False, state="FAILED", completed_at=now_iso(),
                last_error=f"{type(exc).__name__}: {exc}"[:1000], results=results, agents=agents,
            )
        finally:
            # PL44 leaves bulk work enabled; the normal governor remains the only
            # authority that may yield it to genuine higher-priority work.
            try:
                threading.Thread(target=self.refresh, name="LadduLifecycleClosureOpsRefresh", daemon=True).start()
            except Exception:
                pass

    def start_full_lifecycle(self) -> Dict[str, Any]:
        current = self.lifecycle_status()
        if str(current.get("state") or "").upper() == "RUNNING":
            return {"ok": True, "state": "ALREADY_RUNNING", "status": current, "no_op": True}
        with self._lifecycle_lock:
            if str(self._lifecycle_status.get("state") or "").upper() == "RUNNING":
                return {"ok": True, "state": "ALREADY_RUNNING", "status": dict(self._lifecycle_status), "no_op": True}
            self._lifecycle_status.update({
                "state": "STARTING", "stage": "queued", "completed": 0, "total": 8,
                "progress_pct": 0.0, "started_at": now_iso(), "completed_at": None, "last_error": None,
                "agents": {
                    "monitoring_recovery": {"state": "QUEUED", "always_on": True, "actions": []},
                    "reconciliation": {"state": "QUEUED", "always_on": True, "checks": {}},
                },
            })
        thread = threading.Thread(target=self._run_full_lifecycle, name="LadduFullLifecycleClosure", daemon=True)
        thread.start()
        return {
            "ok": True, "state": "LIFECYCLE_CLOSURE_SCHEDULED",
            "status": self.lifecycle_status(),
            "production_influence": 0.0, "broker_authority": "NONE",
        }

    def _virtual_jobs(self, *, include_selected_pipeline: bool = False) -> list[Dict[str, Any]]:
        status = dict(getattr(self.app, "status", {}) or {})
        jobs: list[Dict[str, Any]] = []
        closure = self.lifecycle_status()
        if str(closure.get("state") or "NOT_RUN").upper() != "NOT_RUN":
            jobs.append({
                "job_id": "lifecycle:closure",
                "component": "lifecycle_closure",
                "title": "End-to-end lifecycle closure",
                "state": str(closure.get("state") or "UNKNOWN").upper(),
                "stage": closure.get("stage"),
                "current_item": None,
                "completed": closure.get("completed"),
                "total": closure.get("total"),
                "progress_pct": closure.get("progress_pct"),
                "rate_per_min": None,
                "last_progress_at": closure.get("completed_at") or closure.get("started_at"),
                "waiting_on": None,
                "last_error": closure.get("last_error"),
                "allowed_actions": [],
                "safety_class": "SAFE_COMPONENT",
                "agents": dict(closure.get("agents") or {}),
                "result_summary": {
                    "walk_forward": dict((closure.get("results") or {}).get("walk_forward") or {}),
                    "reconciliation": dict((closure.get("results") or {}).get("final_reconciliation") or {}),
                    "monitoring_recovery_agent": dict((closure.get("results") or {}).get("monitoring_recovery_agent") or {}),
                    "reconciliation_agent": dict((closure.get("results") or {}).get("reconciliation_agent") or {}),
                },
            })
        deep = dict(status.get("deep_history_backfill") or {})
        if deep:
            done, total = deep.get("operational_ready") or deep.get("done"), deep.get("total")
            jobs.append({
                "job_id": "data:deep-history",
                "component": "deep_history_backfill",
                "title": "Operational history convergence",
                "state": str(deep.get("state") or "UNKNOWN").upper(),
                "stage": "exact-gap history",
                "current_item": deep.get("current_item") or next((r.get("symbol") for r in deep.get("members") or [] if r.get("state") in {"BACKFILLING", "DUE"}), None),
                "completed": done,
                "total": total,
                "progress_pct": self._pct(done, total),
                "rate_per_min": self._rate("data:deep-history", self._num(done)),
                "last_progress_at": deep.get("last_run"),
                "waiting_on": deep.get("yield_reason") or ("retry schedule" if deep.get("state") == "waiting_retry" else None),
                "rows_written": deep.get("rows_saved_this_cycle"),
                "last_error": deep.get("error"),
                "allowed_actions": ["pause_bulk", "resume_bulk"],
                "safety_class": "SAFE_COMPONENT",
            })
        modes = dict(status.get("mode_scanners") or {})
        # Intraday has two explicit supervisor authorities in R40:
        # ``intraday_scanner`` = recurrent live analysis and
        # ``intraday_coverage`` = whole-universe sweep. Do not add a third
        # virtual row with the same component name and conflicting counters.
        for desk in ("delivery",):
            root = dict(modes.get(desk) or {})
            analysis = dict(root.get("analysis") or {})
            contract = dict(root.get("progress_contract") or analysis.get("progress_contract") or {})
            done = contract.get("current_sweep_scanned")
            total = contract.get("population_count")
            jobs.append({
                "job_id": f"scanner:{desk}",
                "component": f"{desk}_scanner",
                "title": f"{desk.title()} governed scan",
                "state": str(contract.get("state") or root.get("state") or "UNKNOWN").upper(),
                "stage": analysis.get("current_stage") or "scanner",
                "current_item": analysis.get("current_symbol"),
                "completed": done,
                "total": total,
                "progress_pct": self._pct(done, total),
                "rate_per_min": self._rate(f"scanner:{desk}", self._num(done)),
                "last_progress_at": contract.get("last_progress_at") or analysis.get("last_progress_at") or root.get("last_run"),
                "waiting_on": contract.get("pause_reason"),
                "last_error": analysis.get("last_error"),
                "timeouts": analysis.get("cycle_analysis_timeouts"),
                "capacity_deferred": analysis.get("cycle_capacity_deferred"),
                "allowed_actions": ["recover_component"] if desk == "delivery" else [],
                "safety_class": "SAFE_COMPONENT",
            })
        selected = {}
        try:
            governor = getattr(self.app, "workload_governor", None)
            state = dict(governor.snapshot() or {}) if governor is not None else {}
            if state.get("selected_stock"):
                selected = {
                    "symbol": state.get("selected_stock"),
                    "mode": state.get("selected_mode") or "delivery",
                    "instrument_key": state.get("selected_instrument_key"),
                    "interval": state.get("selected_interval") or "day",
                }
        except Exception:
            selected = {}
        conveyor = dict(status.get("data_conveyor") or {})
        research = dict(conveyor.get("research") or {})
        reconciliation = dict(research.get("reconciliation") or {})
        research_desks = dict(reconciliation.get("by_desk") or {})
        business_age = self._num(research.get("business_progress_age_sec"))
        for desk in ("delivery", "intraday"):
            row = dict(research_desks.get(desk) or {})
            if not row:
                continue
            stages = dict(row.get("stages") or {})
            captured = int(stages.get("captured") or 0)
            stage_order = (
                ("population", captured > 0),
                ("features", captured > 0 and int(stages.get("feature_complete") or 0) >= captured),
                ("baseline", captured > 0 and int(stages.get("baseline_predicted") or 0) >= captured),
                ("ml", captured > 0 and int(stages.get("ml_predicted") or 0) >= captured),
                ("hybrid", captured > 0 and int(stages.get("hybrid_predicted") or 0) >= captured),
                ("paper", int(stages.get("paper_opened") or 0) > 0),
                ("settled", int(stages.get("settled") or 0) > 0),
            )
            completed_stages = sum(1 for _label, passed in stage_order if passed)
            lifecycle_state = str(row.get("state") or "UNKNOWN").upper()
            if lifecycle_state == "PAPER_ADMISSION_BLOCKED":
                ops_state = "NO_PROGRESS"
            elif lifecycle_state in {"PAPER_ADMISSION_WAITING", "PAPER_ADMISSION_NOT_SELECTED", "PAPER_MODEL_EVIDENCE_WAITING", "FEATURE_EVIDENCE_PENDING"}:
                ops_state = "EXPECTED_IDLE"
            elif lifecycle_state in {"PAPER_ADMISSION_PENDING", "PAPER_MODEL_TRAINING_BLOCKED"}:
                ops_state = "NO_PROGRESS"
            elif lifecycle_state in {"MONITORING", "SETTLEMENT_ACTIVE"}:
                ops_state = "RUNNING"
            elif lifecycle_state == "NOT_STARTED":
                ops_state = "WAITING_DEPENDENCY"
            elif "INCOMPLETE" in lifecycle_state or "PENDING" in lifecycle_state:
                ops_state = "NO_PROGRESS"
            else:
                ops_state = "RUNNING"
            jobs.append({
                "job_id": f"research:{desk}",
                "component": f"research_{desk}",
                "title": f"{desk.title()} research lifecycle",
                "state": ops_state,
                "stage": next((label for label, passed in stage_order if not passed), "settled"),
                "current_item": row.get("population_fingerprint"),
                "completed": completed_stages,
                "total": len(stage_order),
                "progress_pct": self._pct(completed_stages, len(stage_order)),
                "rate_per_min": None,
                "last_progress_at": research.get("last_business_progress_at"),
                "progress_age_sec": business_age,
                "waiting_on": row.get("next_action") or research.get("waiting_on"),
                "last_error": None,
                "allowed_actions": ["recover_component"] if ops_state == "NO_PROGRESS" else [],
                "action_component": "data_conveyor",
                "safety_class": "SAFE_COMPONENT",
                "upstream_component": "data_conveyor",
                "paper_admission": row.get("paper_admission") or {},
            })

        if selected.get("symbol") and include_selected_pipeline:
            # The selected-stock detailed checkpoint may perform storage-backed
            # reconciliation.  It must never hold the global Progress & Proof
            # projection lock.  Dedicated selected-stock actions/pages own that
            # detailed read; the operations projection stays bounded and live.
            pipeline = {}
            try:
                pipeline = dict(self.app.priority_pipeline.snapshot(symbol=str(selected.get("symbol")), mode=str(selected.get("mode") or "delivery")) or {})
            except Exception:
                pipeline = {}
            jobs.append({
                "job_id": f"selected:{selected.get('symbol')}:{selected.get('mode') or 'delivery'}",
                "component": "selected_stock_pipeline",
                "title": f"{selected.get('symbol')} selected-stock intelligence",
                "state": str(pipeline.get("state") or "WAITING").upper(),
                "stage": pipeline.get("current_stage") or selected.get("interval") or "selected stock",
                "current_item": selected.get("symbol"),
                "completed": pipeline.get("completed_stages"),
                "total": pipeline.get("total_stages"),
                "progress_pct": pipeline.get("progress_pct"),
                "last_progress_at": pipeline.get("last_progress_at"),
                "waiting_on": pipeline.get("blocker"),
                "last_error": pipeline.get("last_error"),
                "allowed_actions": ["force_history_sync", "rebuild_selected_stock", "resume_priority_pipeline"],
                "safety_class": "SAFE_COMPONENT",
                "symbol": selected.get("symbol"),
                "mode": selected.get("mode") or "delivery",
                "interval": selected.get("interval") or "day",
            })
        return jobs

    def _build_jobs_live(self) -> Dict[str, Any]:
        rows = self._supervisor_jobs() + self._virtual_jobs(include_selected_pipeline=False)
        # Make blocking and active rows visible first without losing deterministic ordering.
        priority = {"STUCK": 0, "FAILED": 1, "CIRCUIT_OPEN": 1, "NO_PROGRESS": 2, "UNINSTRUMENTED": 3, "RECOVERING": 4, "RUNNING": 5, "WAITING_DEPENDENCY": 6, "EXPECTED_IDLE": 7, "COMPLETE": 8}
        rows.sort(key=lambda row: (priority.get(str(row.get("state") or "").upper(), 6), str(row.get("title") or "")))
        counts: Dict[str, int] = {}
        for row in rows:
            state = str(row.get("state") or "UNKNOWN").upper()
            counts[state] = counts.get(state, 0) + 1
        return {
            "ok": True,
            "version": self.VERSION,
            "build": APP_VERSION,
            "time": now_iso(),
            "counts": counts,
            "jobs": rows,
            "workload_governor": getattr(self.app, "workload_governor", None).snapshot() if getattr(self.app, "workload_governor", None) else {},
            "trust": self.app.trust_state_service.snapshot() if getattr(self.app, "trust_state_service", None) is not None else {"state": "WARMING"},
            "historical_pit": self.app.historical_pit_sweep.snapshot() if getattr(self.app, "historical_pit_sweep", None) is not None else {"state": "STARTING"},
            "database_pools": {
                "operational": self.app.production_data_plane.operational.pool_health(),
                "interactive": self.app.production_data_plane.interactive.pool_health() if hasattr(self.app.production_data_plane, "interactive") else {},
                "governance": self.app.production_data_plane.governance.pool_health(),
            },
        }


    def live_summary(self) -> Dict[str, Any]:
        """Return a bounded operator snapshot from current in-memory authorities.

        This path deliberately bypasses the materialized OCC projection cache so
        an operator pressing Refresh/Copy can never receive a ten-minute-old
        proof merely because the background projection generation is stalled.
        It does not scan logs, query historical stores, or execute recovery.
        """
        payload = self._build_jobs_live()
        controller = getattr(self.app, "autonomic_controller", None)
        control = controller.snapshot(refresh=False) if controller else {}
        blockers = list(control.get("blockers") or [])
        active_states = {"FAILED", "STUCK", "NO_PROGRESS", "CIRCUIT_OPEN", "DEAD", "UNINSTRUMENTED"}
        active_blockers = [
            row for row in blockers
            if bool(row.get("actionable")) or str(row.get("state") or "").upper() in active_states
        ]
        evidence_pending = [row for row in blockers if row not in active_blockers]
        counts = dict(payload.get("counts") or {})
        if any(int(counts.get(key) or 0) > 0 for key in ("FAILED", "STUCK", "CIRCUIT_OPEN", "DEAD")):
            operations_state = "FAILED"
        elif any(int(counts.get(key) or 0) > 0 for key in ("NO_PROGRESS", "UNINSTRUMENTED", "RECOVERING")):
            operations_state = "BLOCKED"
        else:
            operations_state = "READY"
        with self._projection_lock:
            cached_age = round(max(0.0, time.monotonic() - self._projection_monotonic), 3)
            generation = self._projection_generation
        with self._lock:
            recent = [dict(row) for row in list(self._recent_actions)[-20:]]
        live = {
            **payload,
            "state": operations_state,
            "controller": {
                "state": control.get("state"),
                "cycle": control.get("cycle"),
                "primary_blocker": control.get("primary_blocker"),
                "last_action": control.get("last_action"),
                "blockers": blockers[:20],
                "active_blockers": active_blockers[:20],
                "evidence_pending": evidence_pending[:40],
                "active_blocker_count": len(active_blockers),
                "evidence_pending_count": len(evidence_pending),
            },
            "recent_actions": recent,
            "projected_at": now_iso(),
            "time": now_iso(),
            "snapshot_source": "LIVE_BOUNDED_OPERATOR_READ",
            "projection_generation": generation,
            "projection_age_sec": 0.0,
            "cached_projection_age_sec": cached_age,
        }
        live["business_signature"] = self._projection_progress_signature(live)
        return live

    def events(self, *, after_sequence: int = 0, limit: int = 200) -> Dict[str, Any]:
        controller = getattr(self.app, "autonomic_controller", None)
        controller_events = controller.events(after_sequence=after_sequence, limit=limit).get("events", []) if controller else []
        with self._lock:
            actions = list(self._recent_actions)[-limit:]
        return {"ok": True, "time": now_iso(), "events": controller_events, "operator_actions": actions}

    @staticmethod
    def _redact_log_line(line: str) -> str:
        value = str(line or "")
        patterns = (
            (r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s,;]+", r"\1<redacted>"),
            (r"(?i)(access[_-]?token|refresh[_-]?token|api[_-]?key|client[_-]?secret|password)(\s*[:=]\s*)[^\s,;]+", r"\1\2<redacted>"),
            (r"(?i)(token=)[^&\s]+", r"\1<redacted>"),
        )
        for pattern, replacement in patterns:
            value = re.sub(pattern, replacement, value)
        return value

    def _log_candidates(self) -> list[Path]:
        # Operations logs must never recursively enumerate the product/data
        # parent. Installed Parquet trees can contain tens of thousands of
        # files; walking them made OCC itself look NO_PROGRESS. Restrict the
        # projection to the canonical log root and at most one child level.
        root = Path(LOG_DIR)
        candidates: list[Path] = []
        seen: set[str] = set()
        try:
            if not root.exists():
                return []
            paths = list(root.glob("*")) + list(root.glob("*/*"))
            for path in paths:
                try:
                    if not path.is_file() or path.suffix.lower() not in {".log", ".stdout", ".stderr", ".txt", ".jsonl"}:
                        continue
                    resolved = path.resolve()
                    if root.resolve() not in resolved.parents:
                        continue
                    key = str(resolved)
                    if key in seen:
                        continue
                    seen.add(key)
                    candidates.append(path)
                except Exception:
                    continue
        except Exception:
            return []
        candidates.sort(key=lambda path: path.stat().st_mtime if path.exists() else 0.0, reverse=True)
        return candidates[:30]

    def _build_logs_live(self, *, limit: int = 1200) -> Dict[str, Any]:
        limit = max(100, min(int(limit or 1200), 4000))
        candidates = self._log_candidates()
        lines: list[str] = []
        used: list[str] = []
        for path in candidates:
            try:
                with path.open("r", encoding="utf-8", errors="replace") as handle:
                    tail = deque(handle, maxlen=max(limit * 4, 800))
                relative = path.name
                used.append(relative)
                lines.extend(f"[{relative}] {self._redact_log_line(line.rstrip())}" for line in tail)
                if len(lines) >= limit * 8:
                    break
            except Exception:
                continue
        # The in-process event authority is a valid fallback when file logging
        # has not yet materialised on a new installation.
        if not lines:
            try:
                for event in self.app.store.events(limit=max(limit, 100)):
                    lines.append(self._redact_log_line(json.dumps(event, sort_keys=True, default=str)))
                if lines:
                    used.append("runtime_event_buffer")
            except Exception:
                pass
        return {
            "ok": True, "state": "READY" if used else "NO_LOG_AUTHORITY",
            "time": now_iso(), "source_files": used,
            "lines": lines[-limit:],
            "message": None if used else "No backend log file or runtime event buffer is available.",
        }

    def refresh_inflight(self) -> bool:
        return self._refresh_lock.locked()

    def refresh(self) -> Dict[str, Any]:
        """Build the expensive operations projection off the HTTP request path."""
        if not self._refresh_lock.acquire(blocking=False):
            return self.summary()
        try:
            payload = self._build_jobs_live()
            controller = getattr(self.app, "autonomic_controller", None)
            control = controller.snapshot(refresh=False) if controller else {}
            blockers = list(control.get("blockers") or [])
            active_states = {"FAILED", "STUCK", "NO_PROGRESS", "CIRCUIT_OPEN", "DEAD", "UNINSTRUMENTED"}
            live_by_component = {
                str(row.get("component") or ""): row for row in list(payload.get("jobs") or [])
                if str(row.get("component") or "")
            }
            reconciled_blockers = []
            for blocker in blockers:
                row = dict(blocker or {})
                component = str(row.get("component") or "")
                live = dict(live_by_component.get(component) or {})
                live_state = str(live.get("state") or "").upper()
                blocker_state = str(row.get("state") or "").upper()
                if (
                    blocker_state in active_states
                    and live
                    and live_state not in active_states
                    and (
                        live_state == "EXPECTED_IDLE"
                        or float(live.get("heartbeat_age_sec") or 999999) < 30.0
                        or float(live.get("progress_age_sec") or 999999) < 120.0
                    )
                ):
                    row["state"] = "RECOVERED_IN_LIVE_PROJECTION"
                    row["actionable"] = False
                    row["safe_action"] = "OBSERVE_PROGRESS"
                    row["detail"] = (
                        f"{component} controller blocker superseded by current live state "
                        f"{live_state} (heartbeat={live.get('heartbeat_age_sec')}s, progress={live.get('progress_age_sec')}s)"
                    )
                    row["controller_state_superseded"] = blocker_state
                reconciled_blockers.append(row)
            blockers = reconciled_blockers
            active_blockers = [
                row for row in blockers
                if bool(row.get("actionable")) or str(row.get("state") or "").upper() in active_states
            ]
            evidence_pending = [row for row in blockers if row not in active_blockers]
            counts = dict(payload.get("counts") or {})
            if any(int(counts.get(key) or 0) > 0 for key in ("FAILED", "STUCK", "CIRCUIT_OPEN", "DEAD")):
                operations_state = "FAILED"
            elif any(int(counts.get(key) or 0) > 0 for key in ("NO_PROGRESS", "UNINSTRUMENTED", "RECOVERING")):
                operations_state = "BLOCKED"
            else:
                operations_state = "READY"
            from core.storage_budget_service import StorageBudgetService
            projection = {
                **payload,
                "state": operations_state,
                "storage": StorageBudgetService.snapshot(),
                "controller": {
                    "state": control.get("state"),
                    "cycle": control.get("cycle"),
                    "primary_blocker": control.get("primary_blocker"),
                    "last_action": control.get("last_action"),
                    "blockers": blockers[:20],
                    "active_blockers": active_blockers[:20],
                    "evidence_pending": evidence_pending[:40],
                    "active_blocker_count": len(active_blockers),
                    "evidence_pending_count": len(evidence_pending),
                },
                "recent_actions": list(self._recent_actions)[-20:],
                "projected_at": now_iso(),
            }
            projection["business_signature"] = self._projection_progress_signature(projection)
            with self._projection_lock:
                self._projection_generation += 1
                projection["projection_generation"] = self._projection_generation
                self._projection = projection
                self._projection_monotonic = time.monotonic()
            return self.summary()
        finally:
            self._refresh_lock.release()

    def refresh_logs(self) -> Dict[str, Any]:
        payload = self._build_logs_live()
        with self._projection_lock:
            self._log_projection = payload
            self._log_projection_monotonic = time.monotonic()
        return dict(payload)

    def jobs(self) -> Dict[str, Any]:
        with self._projection_lock:
            payload = dict(self._projection)
            payload["jobs"] = [dict(row) for row in self._projection.get("jobs") or []]
            payload["projection_age_sec"] = round(max(0.0, time.monotonic() - self._projection_monotonic), 3)
        # Jobs/database/controller are materialized in the background, but an
        # operator action must be auditable immediately after POST returns.
        # Overlay only the tiny bounded in-memory action deque; never rebuild
        # expensive operational projections on the request path.
        with self._lock:
            payload["recent_actions"] = [dict(row) for row in list(self._recent_actions)[-20:]]
        return payload

    def summary(self) -> Dict[str, Any]:
        return self.jobs()

    def logs(self, *, component: str = "", level: str = "", limit: int = 250) -> Dict[str, Any]:
        limit = max(20, min(int(limit or 250), 1000))
        # Unit/isolated service instances have no supervised projection loop;
        # preserve deterministic log-redaction tests without reintroducing file
        # scans on the production HTTP path.
        if not hasattr(self.app, "supervisor") and str(self._log_projection.get("state") or "") == "STARTING":
            self.refresh_logs()
        with self._projection_lock:
            source = dict(self._log_projection)
            source_lines = list(self._log_projection.get("lines") or [])
            age = max(0.0, time.monotonic() - self._log_projection_monotonic)
        component_q = str(component or "").strip().lower()
        level_q = str(level or "").strip().upper()
        filtered = []
        for line in source_lines:
            if component_q and component_q not in line.lower():
                continue
            if level_q and level_q not in line.upper():
                continue
            filtered.append(line)
        return {
            **source,
            "component": component_q or None,
            "level": level_q or None,
            "lines": filtered[-limit:],
            "projection_age_sec": round(age, 3),
        }

    def run(self, supervisor: Any, *, running_fn) -> None:
        """Continuously project job/log truth without blocking operator reads."""
        name = "operations_projection"
        last_logs = 0.0
        while supervisor.running and running_fn():
            supervisor.beat(name)
            try:
                with supervisor.heartbeat_guard(name):
                    payload = self.refresh()
                counts = dict(payload.get("counts") or {})
                jobs = list(payload.get("jobs") or [])
                blocked = sum(int(counts.get(state) or 0) for state in ("STUCK", "FAILED", "NO_PROGRESS", "CIRCUIT_OPEN"))
                # Publish useful OCC progress before optional log I/O.
                supervisor.progress(
                    name,
                    # Read-model health is proved by a completed projection
                    # generation.  Underlying business progress has its own
                    # immutable business_signature and may legitimately remain
                    # unchanged while OCC continues to report the stall.
                    token=f"projection:{int(payload.get('projection_generation') or 0)}",
                    stage="operations_projection",
                    completed_units=len(jobs),
                    total_units=len(jobs),
                    waiting_on=(f"{blocked} actionable/stalled job(s) visible" if blocked else None),
                    expected_idle=False,
                )
                now_mono = time.monotonic()
                if now_mono - last_logs >= 30.0:
                    with supervisor.heartbeat_guard(name):
                        self.refresh_logs()
                    last_logs = now_mono
            except Exception as exc:
                try:
                    self.app.event("ERROR", "operations_projection", "Operations projection refresh failed", {"error": str(exc)[:300]})
                except Exception:
                    pass
            for _ in range(4):
                if not supervisor.running or not running_fn():
                    return
                supervisor.beat(name)
                time.sleep(0.5)

    def _record_action(self, row: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            self._recent_actions.append(row)
            self._action_ids[str(row.get("action_id"))] = row
        try:
            writer = getattr(self.app, "control_audit_writer", None)
            if writer is not None:
                writer.submit(self.KV_KEY, list(self._recent_actions))
            else:
                # Disposable/test facades predate the isolated writer. The
                # installed runtime always supplies ControlAuditWriter.
                self.app.store.set_kv(self.KV_KEY, list(self._recent_actions))
        except Exception:
            pass
        try:
            self.app.event("INFO" if row.get("ok") else "WARN", "operations_control", "Operator action completed", row)
        except Exception:
            pass
        try:
            self.app.autonomic_controller.publish("OPERATOR_ACTION", str(row.get("component") or "operations"), row)
        except Exception:
            pass
        return row

    def execute(self, request: Mapping[str, Any]) -> Dict[str, Any]:
        action = str(request.get("action") or "").strip().lower()
        action_id = str(request.get("action_id") or "").strip()
        if not action_id:
            seed = f"{APP_VERSION}|{action}|{request.get('component')}|{request.get('symbol')}|{time.time_ns()}"
            action_id = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:24]
        with self._lock:
            if action_id in self._action_ids:
                return dict(self._action_ids[action_id], idempotent_replay=True)
        policy = ACTIONS.get(action)
        if policy is None:
            return {"ok": False, "state": "ACTION_NOT_ALLOWED", "action": action, "allowed": sorted(ACTIONS)}
        component = str(request.get("component") or "").strip()
        symbol = str(request.get("symbol") or "").strip().upper()
        mode = str(request.get("mode") or "delivery").strip().lower()
        interval = str(request.get("interval") or ("5minute" if mode == "intraday" else "day")).strip()
        reason = str(request.get("reason") or "operator_requested").strip()[:300]
        result: Dict[str, Any]
        try:
            if action == "recover_component":
                if not component:
                    raise ValueError("component is required")
                result = dict(self.app.supervisor.recover(component, reason=reason, action="OPERATOR_SAFE_RECOVERY") or {})
            elif action == "clear_circuit":
                if not component:
                    raise ValueError("component is required")
                ok = bool(self.app.supervisor.close_circuit(component))
                result = {"ok": ok, "state": "CIRCUIT_CLEARED" if ok else "UNKNOWN_COMPONENT", "component": component}
            elif action == "force_history_sync":
                if not symbol:
                    raise ValueError("symbol is required")
                self.app.workload_governor.activate_selected(symbol, mode, ttl_seconds=90)
                result = dict(self.app.market_data.schedule_priority_stock_pipeline(symbol, mode=mode, selected_interval=interval, action="operator_force_exact_gap_sync") or {})
                result.setdefault("ok", True)
            elif action == "resume_priority_pipeline":
                if not symbol:
                    raise ValueError("symbol is required")
                reconciliation = dict(self.app.priority_pipeline.reconcile(symbol=symbol, mode=mode, reason=reason) or {})
                scheduled = dict(self.app.market_data.schedule_priority_stock_pipeline(symbol, mode=mode, selected_interval=interval, action="operator_resume_checkpoint") or {})
                result = {"ok": bool(reconciliation.get("ok", True)) and bool(scheduled.get("ok", True)), "state": "RECONCILED_AND_RESUMED", "reconciliation": reconciliation, "pipeline": scheduled}
            elif action == "rebuild_selected_stock":
                if not symbol:
                    raise ValueError("symbol is required")
                self.app.workload_governor.activate_selected(symbol, mode, ttl_seconds=90)
                reconciliation = dict(self.app.priority_pipeline.reconcile(symbol=symbol, mode=mode, reason=reason) or {})
                scheduled = self.app.market_data.schedule_priority_stock_pipeline(symbol, mode=mode, selected_interval=interval, action="operator_rebuild_selected_stock")
                # Read-model refreshes are background authorities; an operator
                # action must not synchronously execute expensive projections.
                threading.Thread(target=self.app.operator_read_models.refresh, name="LadduOperatorReadModelRefresh", daemon=True).start()
                threading.Thread(target=lambda: self.app.dashboard.refresh_cards_cache("all"), name="LadduOperatorCardRefresh", daemon=True).start()
                result = {"ok": True, "state": "REBUILD_SCHEDULED", "reconciliation": reconciliation, "pipeline": scheduled}
            elif action == "rebuild_market_snapshot":
                # Index level worker is read-model safe; request a bounded scan and
                # refresh operator projections without restarting the database.
                try:
                    request_row = self.app.scan_orchestration.request_scan("delivery")
                except Exception:
                    request_row = {}
                try:
                    self.app.operator_read_models.refresh()
                    self.app.dashboard.refresh_cards_cache("all")
                except Exception:
                    pass
                result = {"ok": True, "state": "MARKET_SNAPSHOT_REFRESH_REQUESTED", "request": request_row}
            elif action == "evaluate_controller":
                result = dict(self.app.autonomic_controller.request_evaluation(allow_action=True, reason=reason) or {})
                result.setdefault("ok", True)
            elif action == "pause_bulk":
                result = {"ok": True, "state": "BULK_PAUSED", "governor": self.app.workload_governor.pause_bulk(seconds=float(request.get("seconds") or 120), reason=reason)}
            elif action == "resume_bulk":
                result = {"ok": True, "state": "BULK_RESUMED", "governor": self.app.workload_governor.resume_bulk()}
            elif action in {"advance_full_lifecycle", "run_end_to_end"}:
                result = self.start_full_lifecycle()
            elif action == "recover_all_safe_stuck":
                # Recovery is a *diagnostic predicate first*. Healthy/idle jobs are
                # never sent through a recovery playbook merely because the action
                # is allow-listed. This keeps the consolidated operator action
                # deterministic and prevents healthy scanners from turning a safe
                # no-op into HTTP 409 / false failure evidence.
                actionable_states = {
                    "NO_PROGRESS", "UNINSTRUMENTED", "STUCK", "FAILED", "DEAD",
                    "RECOVERED_WITH_ERROR", "CIRCUIT_OPEN",
                }
                candidates = []
                for job in self._supervisor_jobs():
                    if str(job.get("safety_class") or "").upper() != "SAFE_COMPONENT":
                        continue
                    if "recover_component" not in list(job.get("allowed_actions") or []):
                        continue
                    if str(job.get("state") or "").upper() not in actionable_states:
                        continue
                    candidates.append(job)
                attempts = []
                for job in candidates[:5]:
                    target = str(job.get("component") or "")
                    try:
                        recovery = dict(self.app.supervisor.recover(target, reason=reason, action="OPERATOR_RECOVER_ALL_SAFE_STUCK") or {})
                    except Exception as exc:
                        recovery = {"ok": False, "state": "RECOVERY_EXCEPTION", "error": str(exc)[:300]}
                    attempts.append({"component": target, **recovery})
                result = {
                    "ok": all(bool(row.get("ok")) for row in attempts) if attempts else True,
                    "state": "SAFE_RECOVERY_BATCH_EXECUTED" if attempts else "NO_ELIGIBLE_SAFE_COMPONENTS",
                    "attempts": attempts,
                    "no_op": not bool(attempts),
                    "eligible_count": len(candidates),
                }
            else:  # pragma: no cover - guarded by ACTIONS
                result = {"ok": False, "state": "ACTION_NOT_IMPLEMENTED"}
        except Exception as exc:
            result = {"ok": False, "state": "ACTION_FAILED", "error": f"{type(exc).__name__}: {exc}"[:500]}
        row = {
            "action_id": action_id,
            "action": action,
            "component": component or None,
            "symbol": symbol or None,
            "mode": mode,
            "interval": interval,
            "reason": reason,
            "safety_class": policy.safety,
            "requested_at": now_iso(),
            **result,
        }
        row["verified"] = bool(row.get("ok")) and not bool(row.get("no_op")) and str(row.get("state") or "").upper() not in {"ACTION_FAILED", "UNKNOWN_COMPONENT"}
        row["verification"] = "bounded action evaluated successfully; useful progress remains visible in the job token/history" if row["verified"] else "action did not obtain a verified accepted state"
        return self._record_action(row)
