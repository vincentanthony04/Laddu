"""Audit shadow-model observations and ranking authority without training.

The audit proves that models calculate for scanner/manual candidates, that
shadow observations are linkable to future outcomes, and that no unpromoted
model contribution leaks into canonical rank.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Mapping

from core.production_ranking_service import RANKING_VERSION, MODEL_LEARNING_CONTRACT_VERSION

SERVICE_VERSION = "model-learning-audit-1.0.0"


def _rows(value: Any) -> list[Dict[str, Any]]:
    if not isinstance(value, Iterable) or isinstance(value, (str, bytes, Mapping)):
        return []
    return [dict(row) for row in value if isinstance(row, Mapping)]


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class ModelLearningAuditService:
    def __init__(self, app: Any):
        self.app = app
        self.store = getattr(app, "store", None)

    def _decisions(self) -> list[Dict[str, Any]]:
        getter = getattr(self.store, "latest_decisions", None)
        if not callable(getter):
            return []
        try:
            return _rows(getter("all", limit=1000))
        except TypeError:
            try:
                return _rows(getter("all", 1000))
            except Exception:
                return []
        except Exception:
            return []

    def status(self) -> Dict[str, Any]:
        rows = [row for row in self._decisions() if row.get("ranking_version") == RANKING_VERSION]
        observations = [row for row in rows if row.get("model_score") is not None]
        complete = [
            row for row in observations
            if row.get("model_learning_observation_id")
            and isinstance(row.get("model_learning_contract"), Mapping)
            and row.get("model_learning_contract", {}).get("contract_version") == MODEL_LEARNING_CONTRACT_VERSION
        ]
        errors: list[Dict[str, Any]] = []
        by_observation: Dict[str, set[str]] = {}
        settled = 0
        for row in observations:
            oid = str(row.get("model_learning_observation_id") or "")
            input_hash = str(row.get("ranking_input_hash") or "")
            if oid:
                by_observation.setdefault(oid, set()).add(input_hash)
            authority = _float(row.get("model_ranking_authority_pct"))
            contribution = _float(row.get("model_rank_contribution"))
            influence = bool(row.get("model_influence_applied"))
            stage = str(row.get("model_ranking_stage") or "")
            if authority < 0.0 or authority > 15.000001:
                errors.append({"observation_id": oid, "error": "model authority outside 0-15% cap", "authority_pct": authority})
            if not influence and (abs(authority) > 1e-9 or abs(contribution) > 1e-6):
                errors.append({"observation_id": oid, "error": "shadow model leaked into canonical rank", "authority_pct": authority, "contribution": contribution})
            if influence and (authority <= 0.0 or stage != "GOVERNED_PRODUCTION"):
                errors.append({"observation_id": oid, "error": "influence applied without governed-production stage", "stage": stage})
            state = str(row.get("state") or row.get("status") or "").upper()
            if state in {"COMPLETED", "INVALIDATED", "SUCCESS", "FAIL", "CLOSED", "EXITED"} or row.get("closed_at"):
                settled += 1
                if not oid:
                    errors.append({"decision_id": row.get("decision_id"), "error": "settled governed decision lacks learning observation link"})
        collisions = [
            {"observation_id": oid, "ranking_input_hashes": sorted(values)}
            for oid, values in by_observation.items() if len(values) > 1
        ]
        if collisions:
            errors.extend({"error": "observation ID collision", **row} for row in collisions)
        observations_ready = bool(observations)
        passed = bool(observations_ready and len(complete) == len(observations) and not errors)
        missing = []
        if not observations_ready:
            missing.append("at least one canonical shadow-model observation")
        if observations and len(complete) != len(observations):
            missing.append("learning observation ID and contract on every model-scored decision")
        if errors:
            missing.append("zero model-authority leakage, cap or observation-link conflicts")
        return {
            "ok": passed,
            "version": SERVICE_VERSION,
            "contract_version": MODEL_LEARNING_CONTRACT_VERSION,
            "state": "PASS" if passed else "PENDING_EVIDENCE" if not observations_ready else "FAILED",
            "passed": passed,
            "governed_decisions": len(rows),
            "model_observations": len(observations),
            "contract_complete_observations": len(complete),
            "settled_linked_candidates": settled,
            "observation_collisions": collisions,
            "errors": errors[:100],
            "missing_gates": missing,
            "policy": "calculate and record always; apply one healthy governed channel with a reversible 15% cap; deterministic fallback when unavailable",
            "production_change_allowed": False,
            "evaluated_at": datetime.now(timezone.utc).isoformat(),
        }
