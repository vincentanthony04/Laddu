"""Read-only truth projection for historical PIT -> ML -> WFA evidence closure."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

from config import DATA_DIR, ML_DELIVERY_TRAIN_MIN_DAYS, BUILD_MARKER
from core.ai_governance_service import AIGovernanceService
from core.walk_forward_validation_service import CAPITAL_PROFILE, WalkForwardValidationService
from core.research_catalogue_evidence_service import ResearchCatalogueEvidenceService

VERSION = "evidence-pipeline-status-1.4.0-pl25-catalogue-activation"


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        return dict(value) if isinstance(value, dict) else {}
    except Exception:
        return {}


class EvidencePipelineStatusService:
    def __init__(self, app: Any):
        self.app = app
        self.store = app.store

    def status(self) -> Dict[str, Any]:
        data_dir = Path(DATA_DIR)
        latest_training = _read_json(data_dir / "manifests" / "latest-training-run.json")
        latest_check = _read_json(data_dir / "manifests" / "latest-training-check.json")
        pit = {}
        service = getattr(self.app, "historical_pit_enrichment", None)
        if service is not None and callable(getattr(service, "snapshot", None)):
            try:
                pit = dict(service.snapshot() or {})
            except Exception:
                pit = {}
        if not pit:
            pit = _read_json(data_dir / "manifests" / "historical-pit-runtime.json")
        catalogue_manifest = _read_json(data_dir / "manifests" / "market-lake.json")

        ai = AIGovernanceService(self.store).status()
        models = []
        validator = WalkForwardValidationService(self.store)
        for model in list(ai.get("models") or []):
            model_id = str(model.get("model_id") or "")
            capital = None
            if model_id:
                repository = (getattr(self.store, "production_model_governance_read_repository", None)
                              or getattr(self.store, "production_model_governance_repository", None))
                if repository is not None and callable(getattr(repository, "training_validation_evidence", None)):
                    try:
                        evidence = repository.training_validation_evidence(model_key=model_id, profile=CAPITAL_PROFILE, limit=1).get("evidence") or []
                        capital = evidence[0] if evidence else None
                    except Exception:
                        capital = None
                if capital is None:
                    try:
                        approvals = validator.status(model_id=model_id, profile=CAPITAL_PROFILE).get("approvals") or []
                        capital = approvals[0] if approvals else None
                    except Exception as exc:
                        capital = {"status": "UNAVAILABLE", "error": str(exc)[:240]}
            models.append({
                "model_id": model_id,
                "model_version": model.get("model_version"),
                "framework": model.get("framework"),
                "lifecycle_state": model.get("lifecycle_state"),
                "production_weight": float(model.get("production_weight") or 0.0),
                "trained_through": model.get("trained_through"),
                "capital_walk_forward": capital,
            })

        repo = (getattr(self.store, "production_model_governance_read_repository", None)
                or getattr(self.store, "production_model_governance_repository", None))
        selector_depth = {}
        if repo is not None and callable(getattr(repo, "quant_training_evidence_status", None)):
            for desk, horizon in (("intraday", "30m"), ("delivery", "10d")):
                try:
                    selector_depth[desk] = dict(repo.quant_training_evidence_status(mode=desk, horizon=horizon) or {})
                except Exception as exc:
                    selector_depth[desk] = {"state": "UNAVAILABLE", "error": str(exc)[:240]}

        retained_training_artifact = bool(latest_training.get("ok") is True and latest_training.get("state") not in (None, "", "TRAINING_NOT_REQUIRED"))
        current_cycle_completed = bool(pit.get("last_success_at"))
        publication_replay = dict(pit.get("publication_replay") or {})
        catalogue_refresh = dict(pit.get("catalogue_refresh") or {})
        outbox_drained = int(publication_replay.get("remaining") or 0) == 0 and str(publication_replay.get("state") or "").upper() == "OUTBOX_DRAINED"
        runtime_catalogue_ready = str(catalogue_refresh.get("state") or "").upper() in {"RESEARCH_CATALOG_CURRENT", "RESEARCH_CATALOG_REFRESHED"}
        catalogue_evidence = ResearchCatalogueEvidenceService.probe(data_dir=data_dir, min_dates=ML_DELIVERY_TRAIN_MIN_DAYS)
        persisted_catalogue_ready = bool(catalogue_evidence.get("ready"))
        catalogue_ready = runtime_catalogue_ready or persisted_catalogue_ready
        any_model = bool(models)
        any_capital_evaluated = any(
            isinstance(item.get("capital_walk_forward"), dict)
            and item["capital_walk_forward"].get("status") in {"APPROVED", "REJECTED"}
            for item in models
        )
        return {
            "ok": True,
            "version": VERSION,
            "build_marker": BUILD_MARKER,
            "historical_pit": pit,
            "latest_training_run": latest_training,
            "latest_training_check": latest_check,
            "forward_selector_evidence_depth": selector_depth,
            "selector_training_depth": selector_depth,
            "selector_depth_semantics": "PROSPECTIVE_FORWARD_SELECTOR_ONLY_NOT_HISTORICAL_WFA",
            "catalogue_evidence": catalogue_evidence,
            "catalogue_manifest": {
                "state": catalogue_manifest.get("state"),
                "version": catalogue_manifest.get("version"),
                "last_run": catalogue_manifest.get("last_run"),
                "training_panel": catalogue_manifest.get("research_training_panel"),
                "authority": "PERSISTED_MARKET_LAKE_MANIFEST",
            },
            "models": models,
            "gates": {
                "historical_training_completed": current_cycle_completed,
                "retained_training_artifact_exists": retained_training_artifact,
                "publication_outbox_drained": outbox_drained,
                "research_catalogue_ready": catalogue_ready,
                "research_catalogue_ready_from_persisted_authority": persisted_catalogue_ready,
                "governed_model_exists": any_model,
                "capital_walk_forward_result_persisted": any_capital_evaluated,
                "production_ml_influence_zero_until_qualified": all(float(item.get("production_weight") or 0.0) == 0.0 for item in models),
            },
            "level5_claim_allowed": False,
            "alpha_claim_allowed": False,
            "policy": "Historical capital WFA and prospective selector evidence are separate authorities. A persisted DuckDB panel may activate WFA only after direct read-only row/depth proof. Capital WFA may PASS or FAIL statistically; forward selector depth is never backfilled from history. No historical result grants broker authority.",
            "broker_authority": "NONE",
        }
