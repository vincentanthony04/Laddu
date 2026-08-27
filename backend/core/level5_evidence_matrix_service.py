"""Failure-first Level 5 evidence matrix.

Scores are navigation aids only. A failed hard gate caps maturity regardless
of the average, so UI quality or infrastructure cannot mask missing forward
alpha, recovery or decision-integrity evidence.
"""
from __future__ import annotations

from typing import Any, Dict, Mapping

from config import APP_VERSION, BROKER_ORDER_EXECUTION_ENABLED, PRODUCT_MODE
from core.decision_surface_reconciliation_service import DecisionSurfaceReconciliationService
from core.model_learning_audit_service import ModelLearningAuditService
from core.operational_evidence_integrity_service import OperationalEvidenceIntegrityService
from core.level5_operational_proof_service import Level5OperationalProofService
from core.product_maturity_service import ProductMaturityService
from models import now_iso


def _map(value: Any) -> Dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


class Level5EvidenceMatrixService:
    VERSION = "level5-evidence-matrix-1.1.0-materialized-read"

    def __init__(self, app: Any):
        self.app = app

    @staticmethod
    def _domain(key: str, label: str, passed: bool, score: int, detail: str, *, hard_gate: bool = True, state: str | None = None, evidence: Any = None) -> Dict[str, Any]:
        return {
            "key": key,
            "label": label,
            "state": state or ("PASS" if passed else "PENDING_EVIDENCE"),
            "passed": bool(passed),
            "score": max(0, min(100, int(score))),
            "hard_gate": bool(hard_gate),
            "detail": detail,
            "evidence": evidence,
        }


    @classmethod
    def materialized(cls, app: Any) -> Dict[str, Any]:
        """Build the operator matrix only from immutable background snapshots.

        The former HTTP route recomputed ProductMaturity, decision surfaces,
        learning audit, evidence integrity, forward maturity, operational proof
        and recovery synchronously. On the installed target that exceeded 30s.
        This projection deliberately consumes the canonical Product State and
        maturity worker snapshots only; heavy qualification remains owned by
        their background authorities.
        """
        try:
            envelope = dict(app.product_state_envelope.snapshot() or {})
        except Exception:
            envelope = {}
        try:
            projected = dict(app.maturity_projection.snapshot() or {})
        except Exception:
            projected = {}
        maturity = _map(envelope.get("maturity"))
        operations = _map(envelope.get("operations"))
        sources = _map(envelope.get("sources"))
        proof = _map(projected.get("proof"))
        forward = _map(projected.get("forward_maturity"))
        level = int(maturity.get("level") or 0)
        evidence_score = float(maturity.get("evidence_score") or 0.0)
        missing = list(maturity.get("missing_gates") or proof.get("missing_gates") or [])
        op_counts = _map(operations.get("counts"))
        op_failures = sum(int(op_counts.get(key) or 0) for key in ("FAILED", "STUCK", "CIRCUIT_OPEN", "NO_PROGRESS", "UNINSTRUMENTED"))
        data_ready = all(_map(sources.get(key)).get("available") is not False for key in ("runtime", "research", "maturity"))
        forward_ready = bool(forward.get("level5_ready") or maturity.get("level5_ready"))
        target_ready = bool(proof.get("passed"))
        safety_ready = bool(PRODUCT_MODE == "AUTOMATIC_MODEL_PAPER_ONLY" and BROKER_ORDER_EXECUTION_ENABLED is False)
        domains = [
            cls._domain("data_truth", "Data truth & reconciliation", data_ready, 100 if data_ready else 25, "Canonical Product State source authorities must remain available.", evidence=sources),
            cls._domain("runtime", "Runtime & useful progression", op_failures == 0, 100 if op_failures == 0 else 25, "OCC must report no failed, stuck, no-progress or uninstrumented critical work.", evidence={"counts": op_counts, "primary_blocker": operations.get("primary_blocker")}),
            cls._domain("forward_alpha", "Forward post-cost alpha", forward_ready, 100 if forward_ready else 15, "Both desks require governed same-population settled forward evidence.", evidence=forward),
            cls._domain("target_proof", "Installed target proof continuity", target_ready, 100 if target_ready else 35, "Installed-target proof must belong to the exact build and remain current.", evidence=proof),
            cls._domain("safety", "Safety & execution boundary", safety_ready, 100 if safety_ready else 0, "Model Paper only; broker execution authority remains NONE.", evidence={"product_mode": PRODUCT_MODE, "broker_order_execution": BROKER_ORDER_EXECUTION_ENABLED}),
        ]
        failed_hard = [row["key"] for row in domains if row.get("hard_gate") and not row.get("passed")]
        return {
            "ok": bool(envelope),
            "version": cls.VERSION,
            "build": APP_VERSION,
            "maturity_level": level,
            "evidence_score": evidence_score,
            "level5_ready": bool(level >= 5 and not failed_hard and forward_ready and target_ready),
            "state": str(maturity.get("state") or projected.get("state") or "WARMING"),
            "failed_hard_gates": failed_hard or missing,
            "domains": domains,
            "source": "MATERIALIZED_PRODUCT_STATE_AND_MATURITY_PROJECTION",
            "projection_age_sec": projected.get("projection_age_sec"),
            "score_policy": "NAVIGATION_ONLY_HARD_GATES_CANNOT_BE_AVERAGED_AWAY",
            "captured_at": envelope.get("generated_at") or projected.get("projected_at") or now_iso(),
        }

    def status(self) -> Dict[str, Any]:
        try:
            maturity = ProductMaturityService(self.app).status()
        except Exception as exc:
            maturity = {"ok": False, "state": "UNAVAILABLE", "error": str(exc)[:240]}
        try:
            surfaces = DecisionSurfaceReconciliationService(self.app).status()
        except Exception as exc:
            surfaces = {"passed": False, "state": "UNAVAILABLE", "error": str(exc)[:240]}
        try:
            learning = ModelLearningAuditService(self.app).status()
        except Exception as exc:
            learning = {"passed": False, "state": "UNAVAILABLE", "error": str(exc)[:240]}
        try:
            integrity = OperationalEvidenceIntegrityService(self.app).status()
        except Exception as exc:
            integrity = {"passed": False, "state": "UNAVAILABLE", "error": str(exc)[:240]}
        try:
            forward = self.app.level5_forward_maturity.status()
        except Exception as exc:
            forward = {"level5_ready": False, "state": "UNAVAILABLE", "error": str(exc)[:240]}
        try:
            operational_proof = Level5OperationalProofService(self.app).status()
        except Exception as exc:
            operational_proof = {"passed": False, "state": "UNAVAILABLE", "error": str(exc)[:240]}
        try:
            recovery = self.app.priority_pipeline.recovery_status()
        except Exception as exc:
            recovery = {"ok": False, "state": "UNAVAILABLE", "stale": 0, "blocked": 0, "error": str(exc)[:240]}
        try:
            supervisor = self.app.supervisor.snapshot()
        except Exception:
            supervisor = {}
        started_workers = [row for row in supervisor.values() if row.get("started")]
        worker_failures = [row for row in started_workers if not row.get("alive") or row.get("stale")]
        store = getattr(self.app, "store", None)
        drill = {}
        try:
            drill = _map(store.get_kv("level5_resilience_drill:last", {}) if store is not None else {})
        except Exception:
            drill = {}
        browser = {}
        try:
            browser = _map(store.get_kv("level4_browser_proof:last", {}) if store is not None else {})
        except Exception:
            browser = {}

        readiness = _map(maturity.get("readiness"))
        operational = str(readiness.get("product_state") or readiness.get("truth_level") or "").upper() == "OPERATIONAL"
        scanner = _map(maturity.get("scanner"))
        scanner_pass = bool(scanner) and all(_map(scanner.get(desk)).get("passed") is True for desk in ("intraday", "delivery"))
        data_pass = bool(operational and integrity.get("passed") is True)
        recovery_pass = bool(recovery.get("stale") in (0, None) and not worker_failures and drill.get("passed") is True and drill.get("build") == APP_VERSION)
        ui_pass = bool(browser.get("passed") is True and browser.get("build") == APP_VERSION)
        safety_pass = bool(PRODUCT_MODE == "AUTOMATIC_MODEL_PAPER_ONLY" and BROKER_ORDER_EXECUTION_ENABLED is False)
        forward_pass = bool(forward.get("level5_ready") is True)

        domains = [
            self._domain("data_truth", "Data truth & reconciliation", data_pass, 100 if data_pass else 45 if operational else 20, "Canonical storage/readiness evidence must reconcile across durable planes.", evidence={"operational": operational, "integrity": integrity}),
            self._domain("runtime", "Runtime & full-universe operation", operational and scanner_pass, 100 if operational and scanner_pass else 55 if operational else 20, "Both desks require a completed market-session cycle with terminal accounting.", evidence={"readiness": readiness, "scanner": scanner}),
            self._domain("decision", "Canonical decision continuity", surfaces.get("passed") is True, 100 if surfaces.get("passed") is True else 45, "Workspace, Stock Intelligence, Model Paper and Ledger must share one decision/evidence identity.", evidence=surfaces),
            self._domain("learning", "Learning integrity", learning.get("passed") is True, 100 if learning.get("passed") is True else 40, "Feature, prediction, outcome and model lineage must remain point-in-time and reproducible.", evidence=learning),
            self._domain("forward_alpha", "Forward post-cost alpha", forward_pass, 100 if forward_pass else 15, "Both desks need immutable same-population Baseline/ML/Hybrid evidence across regimes.", evidence=forward),
            self._domain("recovery", "Recovery & fault drills", recovery_pass, 100 if recovery_pass else 35 if recovery.get("stale") in (0, None) else 10, "Stale jobs, worker loss and retained-state recovery require current-build drill evidence.", evidence={"pipeline": recovery, "worker_failures": len(worker_failures), "drill": drill}),
            self._domain("terminal", "Professional terminal proof", ui_pass, 100 if ui_pass else 45, "Current-build browser proof must pass all customer workflows with zero uncaught errors.", evidence=browser),
            self._domain("target_proof", "Installed target proof continuity", operational_proof.get("passed") is True, 100 if operational_proof.get("passed") is True else 35, "Browser, market-session soak, recovery, data authority, ML population and forward evidence must belong to the exact installed build.", evidence=operational_proof),
            self._domain("safety", "Safety & execution boundary", safety_pass, 100 if safety_pass else 0, "Delivery/Intraday Model Paper only; broker execution authority remains NONE.", evidence={"product_mode": PRODUCT_MODE, "broker_order_execution": BROKER_ORDER_EXECUTION_ENABLED}),
        ]
        score = round(sum(row["score"] for row in domains) / len(domains), 1)
        failed_hard = [row["key"] for row in domains if row["hard_gate"] and not row["passed"]]
        # No average can certify a level. Forward alpha and resilience are
        # mandatory for Level 5; missing target operation caps the state below 4.
        if not safety_pass or not data_pass:
            level = 2
        elif not operational or not scanner_pass or not ui_pass:
            level = 3
        elif not recovery_pass or not surfaces.get("passed") or not learning.get("passed") or operational_proof.get("passed") is not True:
            level = 4
        elif forward_pass:
            level = 5
        else:
            level = 4
        return {
            "ok": True,
            "version": self.VERSION,
            "build": APP_VERSION,
            "maturity_level": level,
            "evidence_score": score,
            "level5_ready": level == 5 and not failed_hard,
            "state": "LEVEL_5_PROVEN" if level == 5 and not failed_hard else f"LEVEL_{level}_EVIDENCE_INCOMPLETE",
            "failed_hard_gates": failed_hard,
            "domains": domains,
            "score_policy": "NAVIGATION_ONLY_HARD_GATES_CANNOT_BE_AVERAGED_AWAY",
            "captured_at": now_iso(),
        }
