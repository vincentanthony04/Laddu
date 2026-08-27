"""Read model and governed checkpoint for the Level-5 learning loop."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from typing import Any

from core.level5_edge_optimization_service import Level5EdgeOptimizationService
from core.management_action_effectiveness_authority import DEFAULT_MANAGEMENT_ACTION_EFFECTIVENESS_AUTHORITY
from core.signal_age_authority import DEFAULT_SIGNAL_AGE_AUTHORITY
from core.model_efficacy_drift_authority import AUTHORITY_VERSION as MODEL_EFFICACY_DRIFT_AUTHORITY_VERSION, DEFAULT_MODEL_EFFICACY_DRIFT_AUTHORITY


class Level5LearningLoopService:
    version = "level5-learning-loop-1.3.0-model-efficacy-closed-loop"

    def __init__(self, repository: Any | None, model_governance_repository: Any | None = None):
        self.repository = repository
        self.model_governance_repository = model_governance_repository

    def _active_model_efficacy(self) -> dict[str, Any]:
        governance = self.model_governance_repository
        if governance is None or not hasattr(governance, "status") or not hasattr(governance, "recent_model_efficacy"):
            return {"state": "UNAVAILABLE", "active_models": [], "review_required": 0}
        try:
            governance_status = dict(governance.status() or {})
        except Exception as exc:
            return {"state": "UNAVAILABLE", "active_models": [], "review_required": 0, "reason": f"{type(exc).__name__}: {exc}"[:240]}
        active_models = []
        seen: set[str] = set()
        for assignment in list(governance_status.get("active_champions") or []):
            row = dict(assignment or {})
            model_id = str(row.get("model_id") or "").strip()
            desk = str(row.get("desk") or "").strip().upper()
            if not model_id or desk not in {"INTRADAY", "DELIVERY"} or model_id in seen:
                continue
            seen.add(model_id)
            try:
                efficacy = dict(governance.recent_model_efficacy(model_id=model_id, desk=desk, limit_populations=60) or {})
            except Exception as exc:
                efficacy = {"state": "UNVERIFIED", "model_id": model_id, "desk": desk, "reason": f"{type(exc).__name__}: {exc}"[:240]}
            health = DEFAULT_MODEL_EFFICACY_DRIFT_AUTHORITY.evaluate(efficacy)
            active_models.append({
                "model_id": model_id, "desk": desk,
                "model_key": row.get("model_key"), "model_version": row.get("model_version"),
                "assignment_id": row.get("assignment_id"), "production_weight": row.get("production_weight"),
                "efficacy": efficacy, "health": health,
            })
        review_required = sum(1 for row in active_models if str(row.get("health", {}).get("gate") or "").upper() in {"WATCH", "WITHDRAW"})
        withdrawals = sum(1 for row in active_models if str(row.get("health", {}).get("gate") or "").upper() == "WITHDRAW")
        return {
            "state": "REVIEW_REQUIRED" if review_required else "HEALTHY" if active_models else "NO_ACTIVE_MODEL",
            "active_models": active_models, "review_required": review_required, "withdrawals": withdrawals,
            "authority_version": MODEL_EFFICACY_DRIFT_AUTHORITY_VERSION,
            "automatic_production_mutation": False,
        }

    def status(self) -> dict[str, Any]:
        if self.repository is None:
            rows = []
            lifecycle = {"total": 0, "by_type": {}, "latest": []}
        else:
            rows = self.repository.settled_learning_rows(limit=10000)
            lifecycle = self.repository.signal_lifecycle_summary(limit=50)
        optimizations = {
            desk: Level5EdgeOptimizationService.optimize(rows, mode=desk)
            for desk in ("intraday", "delivery")
        }
        management_effectiveness = DEFAULT_MANAGEMENT_ACTION_EFFECTIVENESS_AUTHORITY.aggregate(rows)
        signal_age_attribution = DEFAULT_SIGNAL_AGE_AUTHORITY.aggregate(rows)
        proposals = [value.get("champion_proposal") for value in optimizations.values() if value.get("champion_proposal")]
        model_efficacy_health = self._active_model_efficacy()
        state = "PROPOSAL_READY" if proposals else "EVIDENCE_ACCUMULATING"
        return {
            "ok": True,
            "state": state,
            "version": self.version,
            "signal_lifecycle": lifecycle,
            "edge_optimization": optimizations,
            "management_action_effectiveness": management_effectiveness,
            "signal_age_attribution": signal_age_attribution,
            "model_efficacy_health": model_efficacy_health,
            "governance": {
                "learning_findings_append_only": True,
                "rule_change_proposals_append_only": True,
                "material_change_requires_human_approval": True,
                "automatic_production_mutation": False,
                "broker_authority": "NONE",
                "product_mode": "AUTOMATIC_MODEL_PAPER_ONLY",
            },
            "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }

    def checkpoint(self) -> dict[str, Any]:
        """Persist deterministic findings/proposals without activating them."""
        status = self.status()
        if self.repository is None:
            return {**status, "persisted": False, "reason": "NO_PRODUCTION_GOVERNANCE_REPOSITORY"}
        stored: list[str] = []
        for desk, report in status["edge_optimization"].items():
            material = {"desk": desk, "optimizer_version": report.get("version"), "report": report}
            digest = hashlib.sha256(json.dumps(material, sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()
            finding_id = f"finding:{digest[:24]}"
            self.repository.append_learning_finding({
                "finding_id": finding_id,
                "finding_type": "RISK_ADJUSTED_EDGE_OPTIMIZATION",
                "mode": desk,
                "evidence_hash": digest,
                "finding": report,
                "authority_version": self.version,
            })
            stored.append(finding_id)
            champion = report.get("champion_proposal")
            if champion:
                proposal_id = f"proposal:{digest[:24]}"
                self.repository.append_rule_change_proposal({
                    "proposal_id": proposal_id,
                    "finding_id": finding_id,
                    "proposal_type": "MINIMUM_SCORE_THRESHOLD",
                    "mode": desk,
                    "proposal": champion,
                    "evidence_hash": digest,
                    "authority_version": self.version,
                })
                stored.append(proposal_id)
        age_report = status.get("signal_age_attribution") or {}
        if int(age_report.get("observations") or 0) > 0:
            age_material = {"authority_version": age_report.get("authority_version"), "age_bucket_policy_version": age_report.get("age_bucket_policy_version"), "report": age_report}
            age_digest = hashlib.sha256(json.dumps(age_material, sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()
            age_finding_id = f"finding:{age_digest[:24]}"
            self.repository.append_learning_finding({
                "finding_id": age_finding_id,
                "finding_type": "SIGNAL_AGE_ATTRIBUTION",
                "mode": None,
                "evidence_hash": age_digest,
                "finding": age_report,
                "authority_version": self.version,
            })
            stored.append(age_finding_id)
        model_health = status.get("model_efficacy_health") or {}
        for model_row in list(model_health.get("active_models") or []):
            health_material = {
                "model_id": model_row.get("model_id"), "desk": model_row.get("desk"),
                "assignment_id": model_row.get("assignment_id"), "health": model_row.get("health"),
                "efficacy": model_row.get("efficacy"),
            }
            health_digest = hashlib.sha256(json.dumps(health_material, sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()
            health_finding_id = f"finding:{health_digest[:24]}"
            self.repository.append_learning_finding({
                "finding_id": health_finding_id,
                "finding_type": "MODEL_EFFICACY_DRIFT",
                "mode": str(model_row.get("desk") or "").lower() or None,
                "evidence_hash": health_digest,
                "finding": health_material,
                "authority_version": self.version,
            })
            stored.append(health_finding_id)
            gate = str((model_row.get("health") or {}).get("gate") or "").upper()
            if gate in {"WATCH", "WITHDRAW"}:
                review_id = f"proposal:{health_digest[:24]}"
                self.repository.append_rule_change_proposal({
                    "proposal_id": review_id,
                    "finding_id": health_finding_id,
                    "proposal_type": "MODEL_AUTHORITY_REVIEW",
                    "mode": str(model_row.get("desk") or "").lower() or None,
                    "proposal": {
                        "model_id": model_row.get("model_id"),
                        "assignment_id": model_row.get("assignment_id"),
                        "health_gate": gate,
                        "recommended_action": "KEEP_ML_AUTHORITY_WITHDRAWN_PENDING_HUMAN_REVIEW" if gate == "WITHDRAW" else "HUMAN_REVIEW_BEFORE_ANY_WEIGHT_CHANGE",
                        "automatic_production_mutation": False,
                    },
                    "evidence_hash": health_digest,
                    "authority_version": self.version,
                })
                stored.append(review_id)

        management = status.get("management_action_effectiveness") or {}
        if int(management.get("actions_observed") or 0) > 0:
            management_material = {"authority_version": management.get("authority_version"), "report": management}
            management_digest = hashlib.sha256(json.dumps(management_material, sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()
            management_finding_id = f"finding:{management_digest[:24]}"
            self.repository.append_learning_finding({
                "finding_id": management_finding_id,
                "finding_type": "MANAGEMENT_ACTION_EFFECTIVENESS",
                "mode": None,
                "evidence_hash": management_digest,
                "finding": management,
                "authority_version": self.version,
            })
            stored.append(management_finding_id)
        return {**status, "persisted": True, "persisted_ids": stored}
