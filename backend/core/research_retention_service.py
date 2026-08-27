"""Non-destructive research and decision evidence continuity projection."""
from __future__ import annotations

import hashlib
import json
from typing import Any, Dict

from core.decision_lifecycle_read_model_service import DecisionLifecycleReadModelService
from core.research_lifecycle_reconciliation_service import ResearchLifecycleReconciliationService
from models import now_iso


class ResearchRetentionService:
    VERSION = "research-retention-2.1.0-batched-plane-counts"

    def __init__(self, app: Any):
        self.app = app

    @staticmethod
    def _safe_count(fn, default: int = 0) -> int:
        try:
            value = fn()
            if isinstance(value, (list, tuple, set, dict)):
                return len(value)
            return int(value or 0)
        except Exception:
            return default

    def _pg_counts(self, plane: str, specs: Dict[str, tuple[str, str]]) -> Dict[str, int]:
        """Read one PostgreSQL plane in one bounded statement, not N queries.

        v119 executed each retained-count check independently. Under pool
        pressure that multiplied a 5s statement/connect timeout across more
        than a dozen counts and made /api/research-retention take >140s.
        """
        result = {key: 0 for key in specs}
        try:
            authority = getattr(getattr(self.app, "production_data_plane", None), plane, None)
            if authority is None or not specs:
                return result
            parts = []
            for key, (table, where) in specs.items():
                safe_key = str(key).replace("'", "")
                parts.append(f"SELECT '{safe_key}' AS k, COUNT(*)::bigint AS n FROM {table} {where}")
            rows = authority.execute(" UNION ALL ".join(parts), fetch="all") or []
            for row in rows:
                key = str((row or {}).get("k") or "")
                if key in result:
                    result[key] = int((row or {}).get("n") or 0)
        except Exception:
            pass
        return result

    def status(self) -> Dict[str, Any]:
        lifecycle = DecisionLifecycleReadModelService(self.app).status(limit=5000)
        try:
            research = ResearchLifecycleReconciliationService(self.app.store).status()
        except Exception as exc:
            research = {"ok": False, "state": "UNAVAILABLE", "error": str(exc)[:300], "by_desk": {}}
        operational_counts = self._pg_counts("operational", {
            "model_paper_positions": ("trading.model_paper_positions", ""),
            "model_paper_open": ("trading.model_paper_positions", "WHERE status='OPEN'"),
            "model_paper_closed": ("trading.model_paper_positions", "WHERE status='CLOSED'"),
        })
        governance_counts = self._pg_counts("governance", {
            "research_ranking_populations": ("research.ranking_populations", ""),
            "research_feature_snapshots": ("research.feature_snapshots", ""),
            "research_predictions": ("research.predictions", ""),
            "research_prediction_outcomes": ("research.prediction_outcomes", ""),
            "research_model_paper_observations": ("research.model_paper_observations", ""),
            "research_selector_populations": ("research.selector_populations", ""),
            "research_selector_population_members": ("research.selector_population_members", ""),
            "research_selector_arm_predictions": ("research.selector_arm_predictions", ""),
            "research_selector_outcomes": ("research.selector_outcomes", ""),
            "research_training_publications": ("research.training_publications", ""),
            "research_training_validation_evidence": ("research.training_validation_evidence", ""),
        })
        counts = {
            "canonical_lifecycle_records": int((lifecycle.get("overall") or {}).get("records") or 0),
            "canonical_open": int((lifecycle.get("overall") or {}).get("open") or 0),
            "canonical_settled": int((lifecycle.get("overall") or {}).get("settled") or 0),
            "accuracy_eligible": int((lifecycle.get("overall") or {}).get("accuracy_eligible") or 0),
            "performance_eligible": int((lifecycle.get("overall") or {}).get("performance_eligible") or 0),
            "outcome_learning_rows": self._safe_count(lambda: self.app.store.outcome_learning_rows(5000)),
            "selected_signal_rows": self._safe_count(lambda: self.app.store.selected_signals("all", 5000)),
            **operational_counts,
            **governance_counts,
        }
        desk_lineage = {}
        for desk, row in dict(research.get("by_desk") or {}).items():
            desk_lineage[desk] = dict(row.get("stages") or {})
        material = {"counts": counts, "desk_lineage": desk_lineage}
        content_hash = hashlib.sha256(json.dumps(material, sort_keys=True, default=str).encode("utf-8")).hexdigest()
        prior = {}
        try:
            prior = dict(self.app.store.get_kv("research_retention:last", {}) or {})
        except Exception:
            prior = {}
        prior_counts = dict(prior.get("counts") or {})
        regressions = []
        for key, previous in prior_counts.items():
            current = counts.get(key)
            if current is not None and int(previous or 0) > int(current or 0):
                regressions.append({"key": key, "previous": int(previous or 0), "current": int(current or 0)})
        payload = {
            "ok": not regressions,
            "version": self.VERSION,
            "state": "RETAINED_AND_PROJECTED" if not regressions else "RETENTION_REGRESSION_DETECTED",
            "counts": counts,
            "research_lifecycle_state": research.get("state"),
            "desk_lineage": desk_lineage,
            "content_hash": content_hash,
            "previous_content_hash": prior.get("content_hash"),
            "regressions": regressions,
            "policy": "No destructive research cleanup; retained rows are reconciled, migrated, quarantined or explicitly superseded with counts and hashes.",
            "evaluated_at": now_iso(),
        }
        try:
            # Never overwrite the retained high-water mark with a regression.
            if not regressions:
                self.app.store.set_kv("research_retention:last", payload)
        except Exception:
            pass
        return payload
