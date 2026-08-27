"""Authoritative publication boundary for offline AI training.

Offline trainers work only against Parquet/DuckDB and private scratch state.
They publish one compact bundle to the live service.  In production the bundle
is committed atomically to governance PostgreSQL first; the bounded SQLite
surface is refreshed afterwards as a rebuildable compatibility projection.
Backtest evidence can grant evaluation-paper weight, never live production
weight or broker authority.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Dict, Iterable, List

from core.ai_governance_service import AIGovernanceService, MAX_PRODUCTION_WEIGHT
from core.factors.factor_store import ensure_factor_tables
from core.production_mode_policy import require_production_mode
from core.strict_json import json_safe, strict_json_dumps
from core.walk_forward_validation_service import AUTHORITY_VERSION, WalkForwardValidationService


PUBLICATION_VERSION = "ai-training-publication-2.2.0-pl29-source-authority-contract"
CANONICAL_TRAINING_DATA_SOURCE = "PARQUET_DUCKDB"
LEGACY_MATERIALIZED_PIPELINE_SOURCE = "R46_MATERIALIZED_RESEARCH_PANEL_TO_INCREMENTAL_FEATURE_STORE"



def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _finite(value: Any):
    try:
        parsed = float(value)
        return parsed if math.isfinite(parsed) else None
    except (TypeError, ValueError):
        return None


class AITrainingPublicationService:
    def __init__(self, store: Any):
        self.store = store

    @staticmethod
    def _normalise_model(record: Dict[str, Any]) -> Dict[str, Any]:
        required = (
            "model_id", "model_version", "framework", "horizon_days",
            "feature_manifest_hash", "dataset_fingerprint", "trained_through",
        )
        missing = [key for key in required if not record.get(key)]
        if missing:
            raise ValueError("missing model metadata: " + ", ".join(missing))
        requested_state = str(record.get("lifecycle_state") or "SHADOW").upper()
        if requested_state not in ("EXPERIMENTAL", "SHADOW", "APPROVED", "REJECTED", "RETIRED"):
            raise ValueError("invalid lifecycle_state")
        raw_source = str(record.get("training_data_source") or CANONICAL_TRAINING_DATA_SOURCE).upper()
        pipeline_source = str(record.get("training_pipeline_source") or "").upper()
        if raw_source == LEGACY_MATERIALIZED_PIPELINE_SOURCE:
            # PL28 wrote a truthful pipeline-lineage token into the canonical authority field.
            # Canonicalize only this exact known lineage so its durable outbox can replay;
            # preserve the original lineage separately and reject every other unknown source.
            pipeline_source = raw_source
            raw_source = CANONICAL_TRAINING_DATA_SOURCE
        elif raw_source != CANONICAL_TRAINING_DATA_SOURCE:
            raise ValueError(f"unsupported training data source authority: {raw_source}")
        record = dict(record)
        record["training_data_source"] = CANONICAL_TRAINING_DATA_SOURCE
        if pipeline_source:
            record["training_pipeline_source"] = pipeline_source
        requested_weight = min(MAX_PRODUCTION_WEIGHT, max(0.0, float(record.get("production_weight") or 0.0)))
        evaluation_weight = record.get("evaluation_paper_weight")
        if evaluation_weight is None and requested_state == "APPROVED":
            evaluation_weight = requested_weight
        evaluation_weight = min(MAX_PRODUCTION_WEIGHT, max(0.0, float(evaluation_weight or 0.0)))
        # Offline/backtest publication is always shadow evidence. Only the
        # PostgreSQL forward-paper promotion path may create an assignment.
        state = requested_state if requested_state in ("REJECTED", "RETIRED") else "SHADOW"
        return dict(
            record,
            requested_lifecycle_state=requested_state,
            lifecycle_state=state,
            evaluation_paper_weight=evaluation_weight,
            production_weight=0.0,
            broker_authority="NONE",
            promotion_state="PENDING_FORWARD_PAPER" if state == "SHADOW" else state,
        )

    @staticmethod
    def _normalise_predictions(model: Dict[str, Any], predictions: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
        output: List[Dict[str, Any]] = []
        for raw in predictions or ():
            prediction = dict(raw or {})
            if str(prediction.get("model_id") or "") != str(model["model_id"]):
                raise ValueError("prediction model_id does not match publication model")
            rank = _finite(prediction.get("rank_score"))
            confidence = _finite(prediction.get("confidence"))
            if rank is None or not 0 <= rank <= 100 or confidence is None or not 0 <= confidence <= 1:
                raise ValueError("rank_score must be 0..100 and confidence 0..1")
            for key in ("feature_manifest_hash", "dataset_fingerprint"):
                if prediction.get(key) != model.get(key):
                    raise ValueError(f"prediction {key} does not match publication model")
            mode = require_production_mode(prediction.get("mode"))
            as_of = str(prediction.get("as_of") or _now())
            basis = {key: prediction.get(key) for key in sorted(prediction)}
            prediction_id = hashlib.sha256(
                json.dumps(basis, sort_keys=True, default=str).encode("utf-8")
            ).hexdigest()[:24]
            output.append(dict(
                prediction,
                prediction_id=prediction_id,
                mode=mode,
                as_of=as_of,
                rank_score=rank,
                confidence=confidence,
            ))
        return output

    def publish(self, bundle: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(bundle, dict):
            raise ValueError("training publication bundle must be an object")
        # PostgreSQL jsonb is strict JSON: legacy research bundles may contain Python NaN/Inf.
        # Normalise at the authoritative boundary so both old durable outbox files and new
        # HTTP publications are accepted without inventing numeric evidence.
        bundle = dict(json_safe(bundle) or {})
        model = self._normalise_model(dict(bundle.get("model") or {}))
        predictions = self._normalise_predictions(model, bundle.get("predictions") or [])
        publication_id = str(bundle.get("publication_id") or hashlib.sha256(
            json.dumps({
                "model_id": model["model_id"],
                "dataset_fingerprint": model["dataset_fingerprint"],
                "trained_through": model["trained_through"],
                "prediction_count": len(predictions),
            }, sort_keys=True).encode("utf-8")
        ).hexdigest()[:24])
        normalised_bundle = {
            **dict(bundle),
            "publication_id": publication_id,
            "publication_version": PUBLICATION_VERSION,
            "model": model,
            "predictions": predictions,
            "factor_decay": [dict(item or {}) for item in (bundle.get("factor_decay") or [])],
            "factor_registry": [dict(item or {}) for item in (bundle.get("factor_registry") or [])],
            "factor_redundancy": dict(bundle.get("factor_redundancy") or {}),
            "validation": dict(bundle.get("validation") or {}),
            "capital_validation": dict(bundle.get("capital_validation") or {}),
            "training_data_source": str(
                model.get("training_data_source") or bundle.get("training_data_source") or "PARQUET_DUCKDB"
            ).upper(),
        }
        repository = getattr(self.store, "production_model_governance_repository", None)
        governance_required = bool(getattr(self.store, "production_model_governance_required", False))
        if governance_required and repository is None:
            raise RuntimeError("GOVERNANCE_POSTGRES_PUBLICATION_AUTHORITY_MISSING")
        if repository is None:
            projection = self._publish_compatibility_projection(normalised_bundle)
            return {**projection, "state": "PUBLISHED", "compatibility_only": True}

        authority = repository.publish_training_bundle(normalised_bundle)
        try:
            projection = self._publish_compatibility_projection(normalised_bundle)
        except Exception as exc:
            projection = {
                "ok": False,
                "state": "COMPATIBILITY_PROJECTION_DEGRADED",
                "error": f"{type(exc).__name__}: {exc}"[:400],
                "rebuildable": True,
            }
        return {
            **authority,
            "ok": True,
            "publication_version": PUBLICATION_VERSION,
            "compatibility_projection": projection,
            "authority_commit_precedes_projection": True,
            "broker_authority": "NONE",
        }

    def _publish_compatibility_projection(self, bundle: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(bundle, dict):
            raise ValueError("training publication bundle must be an object")
        bundle = dict(json_safe(bundle) or {})
        model = self._normalise_model(dict(bundle.get("model") or {}))
        predictions = self._normalise_predictions(model, bundle.get("predictions") or [])
        validation = dict(bundle.get("validation") or {})
        capital_validation = dict(bundle.get("capital_validation") or {})
        factor_decay = [dict(item or {}) for item in (bundle.get("factor_decay") or [])]
        factor_registry = [dict(item or {}) for item in (bundle.get("factor_registry") or [])]
        publication_id = str(bundle.get("publication_id") or hashlib.sha256(
            json.dumps({
                "model_id": model["model_id"],
                "dataset_fingerprint": model["dataset_fingerprint"],
                "trained_through": model["trained_through"],
                "prediction_count": len(predictions),
            }, sort_keys=True).encode("utf-8")
        ).hexdigest()[:24])

        with self.store.write_lock:
            conn = self.store.conn
            AIGovernanceService._ensure_schema(conn)
            WalkForwardValidationService._ensure_schema(conn)
            ensure_factor_tables(conn)
            conn.execute("BEGIN IMMEDIATE")
            try:
                # PL26: publish measured local NSE IC/IR + redundancy evidence into
                # the rebuildable compatibility registry. Offline publication can
                # never assert formula identity or production influence. Existing
                # separately-verified formula/production fields are preserved.
                for factor in factor_registry:
                    factor_name = str(factor.get("factor_name") or "").strip()
                    if not factor_name:
                        continue
                    conn.execute(
                        """INSERT INTO factor_registry
                           (factor_name,family,ic_score,ir_score,status,last_validated,
                            redundancy_status,canonical_factor_name,redundancy_correlation,
                            dedup_version,dedup_measured_at,formula_class,formula_verification_hash,
                            empirical_qualification_hash,production_influence)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,'UNVERIFIED',NULL,?,0)
                           ON CONFLICT(factor_name) DO UPDATE SET
                             family=excluded.family,ic_score=excluded.ic_score,ir_score=excluded.ir_score,
                             status=excluded.status,last_validated=excluded.last_validated,
                             redundancy_status=excluded.redundancy_status,
                             canonical_factor_name=excluded.canonical_factor_name,
                             redundancy_correlation=excluded.redundancy_correlation,
                             dedup_version=excluded.dedup_version,dedup_measured_at=excluded.dedup_measured_at,
                             empirical_qualification_hash=excluded.empirical_qualification_hash""",
                        (
                            factor_name, str(factor.get("family") or "model_feature"),
                            _finite(factor.get("ic_score")), _finite(factor.get("ir_score")),
                            str(factor.get("status") or "insufficient_data").lower(),
                            str(factor.get("last_validated") or _now()),
                            str(factor.get("redundancy_status") or "UNMEASURED").upper(),
                            factor.get("canonical_factor_name"), _finite(factor.get("redundancy_correlation")),
                            factor.get("dedup_version"), factor.get("dedup_measured_at"),
                            factor.get("empirical_qualification_hash"),
                        ),
                    )

                for report in factor_decay:
                    factor_name = str(report.get("factor_name") or report.get("factor_id") or "").strip()
                    if not factor_name:
                        continue
                    conn.execute(
                        """INSERT INTO factor_decay_history
                           (factor_name,measured_at,status,baseline_dates,recent_dates,
                            baseline_ic,recent_ic,ic_change,recent_hit_rate,reason)
                           VALUES(?,?,?,?,?,?,?,?,?,?)""",
                        (
                            factor_name, str(report.get("measured_at") or _now()),
                            str(report.get("status") or "insufficient_data"),
                            int(report.get("baseline_dates") or 0), int(report.get("recent_dates") or 0),
                            _finite(report.get("baseline_ic")), _finite(report.get("recent_ic")),
                            _finite(report.get("ic_change")), _finite(report.get("recent_hit_rate")),
                            str(report.get("reason") or "offline training publication"),
                        ),
                    )

                # Persist both research-profile and capital-profile WFA evidence.
                # The isolated trainer deletes its scratch DB after publication; dropping
                # capital_validation here made genuine capital WFA disappear from the live
                # validation authority even though the trainer had completed it.
                for validation_payload in (validation, capital_validation):
                    if validation_payload.get("approval_id") and validation_payload.get("model_id"):
                        conn.execute(
                            """INSERT OR REPLACE INTO validation_approvals
                               (approval_id,model_id,authority_version,status,lifecycle_state,
                                validated_at,payload_json) VALUES(?,?,?,?,?,?,?)""",
                            (
                                validation_payload["approval_id"], validation_payload["model_id"],
                                str(validation_payload.get("authority_version") or AUTHORITY_VERSION),
                                str(validation_payload.get("status") or "REJECTED"),
                                str(validation_payload.get("lifecycle_state") or "SHADOW"),
                                str(validation_payload.get("validated_at") or _now()),
                                strict_json_dumps(validation_payload, sort_keys=True),
                            ),
                        )

                conn.execute(
                    """INSERT OR REPLACE INTO ai_model_registry
                       (model_id,model_version,framework,horizon_days,lifecycle_state,approval_id,
                        production_weight,feature_manifest_hash,dataset_fingerprint,trained_through,
                        artifact_uri,metadata_json,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        model["model_id"], model["model_version"], model["framework"],
                        int(model["horizon_days"]), model["lifecycle_state"], model.get("approval_id"),
                        float(model["production_weight"]), model["feature_manifest_hash"],
                        model["dataset_fingerprint"], model["trained_through"], model.get("artifact_uri"),
                        strict_json_dumps(model, sort_keys=True), _now(),
                    ),
                )

                rows = []
                for prediction in predictions:
                    rows.append((
                        prediction["prediction_id"], model["model_id"],
                        str(prediction.get("symbol") or "").upper(), prediction["mode"],
                        prediction["as_of"], int(prediction.get("horizon_days") or model["horizon_days"]),
                        float(prediction["rank_score"]), _finite(prediction.get("expected_excess_return")),
                        float(prediction["confidence"]), prediction["feature_manifest_hash"],
                        prediction["dataset_fingerprint"],
                        strict_json_dumps(prediction, sort_keys=True), _now(),
                    ))
                if rows:
                    conn.executemany(
                        """INSERT OR REPLACE INTO ai_predictions
                           (prediction_id,model_id,symbol,mode,as_of,horizon_days,rank_score,
                            expected_excess_return,confidence,feature_manifest_hash,dataset_fingerprint,
                            payload_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        rows,
                    )

                audit_payload = {
                    "publication_id": publication_id,
                    "publication_version": PUBLICATION_VERSION,
                    "model_id": model["model_id"],
                    "prediction_count": len(predictions),
                    "factor_report_count": len(factor_decay),
                    "factor_registry_count": len(factor_registry),
                    "published_at": _now(),
                }
                audit_id = hashlib.sha256(json.dumps(audit_payload, sort_keys=True).encode("utf-8")).hexdigest()[:24]
                conn.execute(
                    """INSERT OR REPLACE INTO ai_audit_events
                       (event_id,event_type,model_id,symbol,occurred_at,payload_json)
                       VALUES(?,?,?,?,?,?)""",
                    (audit_id, "TRAINING_BUNDLE_PUBLISHED", model["model_id"], None,
                     audit_payload["published_at"], json.dumps(audit_payload, sort_keys=True)),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

        return {
            "ok": True,
            "state": "COMPATIBILITY_PROJECTION_UPDATED",
            "publication_id": publication_id,
            "model_id": model["model_id"],
            "lifecycle_state": model["lifecycle_state"],
            "predictions": len(predictions),
            "factor_reports": len(factor_decay),
            "factor_registry_rows": len(factor_registry),
            "publication_version": PUBLICATION_VERSION,
            "authority": "SQLITE_COMPATIBILITY_PROJECTION",
        }

    def publish_pending(self, directory: Path, *, limit: int = 20) -> Dict[str, Any]:
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        published, failed = [], []
        for path in sorted(directory.glob("*.json"))[:max(1, int(limit))]:
            work = path.with_suffix(path.suffix + ".publishing")
            try:
                path.replace(work)
                bundle = json.loads(work.read_text(encoding="utf-8"))
                published.append(self.publish(bundle))
                work.unlink(missing_ok=True)
            except Exception as exc:
                failed.append({"file": str(path), "error": str(exc)})
                try:
                    if work.exists():
                        work.replace(path)
                except OSError:
                    pass
        return {"ok": not failed, "published": published, "failed": failed}
