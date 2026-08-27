from __future__ import annotations

"""Explicit non-destructive Research/data preservation manifest for Clean Core.

The manifest is evidence, not a new data authority. It names the retained
canonical datasets and their current high-water counts so upgrades can prove
that accumulated Research, decisions, Model Paper and learning evidence did
not disappear behind a new projection.
"""

from datetime import datetime, timezone
from typing import Any, Dict

from core.research_retention_service import ResearchRetentionService


class ResearchPreservationManifestService:
    VERSION = "clean-core-research-preservation-manifest-1.0.0"
    ALLOWED_CLASSIFICATIONS = {
        "RETAINED", "MIGRATED", "QUARANTINED", "SUPERSEDED_WITH_LINEAGE"
    }

    DATASETS = {
        "canonical_lifecycle_records": ("PostgreSQL canonical decision lifecycle", "RETAINED"),
        "canonical_open": ("PostgreSQL canonical open decisions", "RETAINED"),
        "canonical_settled": ("PostgreSQL canonical settled decisions", "RETAINED"),
        "accuracy_eligible": ("Settled decision accuracy evidence", "RETAINED"),
        "performance_eligible": ("Settled decision performance evidence", "RETAINED"),
        "outcome_learning_rows": ("Outcome/learning observations", "RETAINED"),
        "selected_signal_rows": ("Canonical selected-signal continuity", "RETAINED"),
        "model_paper_positions": ("PostgreSQL Model Paper positions", "RETAINED"),
        "model_paper_open": ("PostgreSQL open Model Paper positions", "RETAINED"),
        "model_paper_closed": ("PostgreSQL closed Model Paper positions", "RETAINED"),
        "research_ranking_populations": ("Governance Research ranking populations", "RETAINED"),
        "research_feature_snapshots": ("Governance Research feature snapshots", "RETAINED"),
        "research_predictions": ("Governance Baseline/ML/Hybrid predictions", "RETAINED"),
        "research_prediction_outcomes": ("Governance prediction outcomes", "RETAINED"),
        "research_model_paper_observations": ("Governance Model Paper observations", "RETAINED"),
        "research_selector_populations": ("Governance selector populations", "RETAINED"),
        "research_selector_population_members": ("Governance selector population members", "RETAINED"),
        "research_selector_arm_predictions": ("Governance selector arm predictions", "RETAINED"),
        "research_selector_outcomes": ("Governance selector outcomes", "RETAINED"),
        "research_training_publications": ("Governance training/publication lineage", "RETAINED"),
        "research_training_validation_evidence": ("Immutable research/capital WFA evidence", "RETAINED"),
    }

    def __init__(self, app: Any):
        self.app = app

    def build(self, retention: Dict[str, Any]) -> Dict[str, Any]:
        counts = dict(retention.get("counts") or {})
        datasets = []
        for key, (authority, classification) in self.DATASETS.items():
            datasets.append({
                "dataset": key,
                "authority": authority,
                "classification": classification,
                "count": int(counts.get(key) or 0),
            })
        invalid = [row for row in datasets if row["classification"] not in self.ALLOWED_CLASSIFICATIONS]
        return {
            "ok": bool(retention.get("ok", True)) and not invalid,
            "version": self.VERSION,
            "state": "PRESERVED" if bool(retention.get("ok", True)) and not invalid else "PRESERVATION_BLOCKED",
            "retention_state": retention.get("state"),
            "retention_content_hash": retention.get("content_hash"),
            "counts": counts,
            "datasets": datasets,
            "desk_lineage": retention.get("desk_lineage") or {},
            "regressions": retention.get("regressions") or [],
            "allowed_classifications": sorted(self.ALLOWED_CLASSIFICATIONS),
            "destructive_upgrade_allowed": False,
            "policy": "Existing Research and trading evidence is cumulative capital. Upgrade may retain, migrate, quarantine or supersede with lineage; silent delete/truncate/reset is forbidden.",
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }

    def status(self) -> Dict[str, Any]:
        retention = ResearchRetentionService(self.app).status()
        payload = self.build(retention)
        try:
            if payload.get("ok"):
                self.app.store.set_kv("clean_core:research_preservation_manifest:last", payload)
        except Exception:
            pass
        return payload
