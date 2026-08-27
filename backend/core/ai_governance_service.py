"""Governed AI inference and Vibe research registry.

Installing Qlib or Vibe is never treated as predictive evidence.  Only a
point-in-time prediction from a registered model whose exact validation
approval is still present may influence the production rank.
"""
from __future__ import annotations

import threading

from datetime import datetime, timezone
import hashlib
import json
import math
from typing import Any, Dict, Optional
from core.factor_authority_service import FactorAuthorityService
from core.model_challenger_governance_service import ModelChallengerGovernanceService
from core.production_mode_policy import require_production_mode


GOVERNANCE_VERSION = "ai-governance-3.0.0-active-capped-hybrid"
DEFAULT_PRODUCTION_WEIGHT = 0.10
MAX_PRODUCTION_WEIGHT = 0.15


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse(value: Any) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _finite(value: Any) -> Optional[float]:
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


class AIGovernanceService:
    def __init__(self, store: Any = None):
        self.store = store
        # v60.14 P0 fix: write_lock may be absent on lightweight test doubles
        # that stand in for Store -- fall back to a private lock so this
        # service is never unsynchronized either way.
        if store is not None and not hasattr(store, "write_lock"):
            store.write_lock = threading.Lock()
        self.factor_authority = None
        if store is not None:
            with store.write_lock:
                self._ensure_schema(store.conn)
            self.factor_authority = FactorAuthorityService(store)

    @staticmethod
    def _ensure_schema(conn: Any) -> None:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS ai_model_registry (
          model_id TEXT PRIMARY KEY, model_version TEXT NOT NULL,
          framework TEXT NOT NULL, horizon_days INTEGER NOT NULL,
          lifecycle_state TEXT NOT NULL, approval_id TEXT,
          production_weight REAL NOT NULL DEFAULT 0,
          feature_manifest_hash TEXT NOT NULL, dataset_fingerprint TEXT NOT NULL,
          trained_through TEXT NOT NULL, artifact_uri TEXT,
          metadata_json TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS ai_predictions (
          prediction_id TEXT PRIMARY KEY, model_id TEXT NOT NULL,
          symbol TEXT NOT NULL, mode TEXT NOT NULL, as_of TEXT NOT NULL,
          horizon_days INTEGER NOT NULL, rank_score REAL NOT NULL,
          expected_excess_return REAL, confidence REAL NOT NULL,
          feature_manifest_hash TEXT NOT NULL, dataset_fingerprint TEXT NOT NULL,
          payload_json TEXT NOT NULL, created_at TEXT NOT NULL,
          FOREIGN KEY(model_id) REFERENCES ai_model_registry(model_id)
        );
        CREATE INDEX IF NOT EXISTS ix_ai_prediction_lookup
          ON ai_predictions(symbol, mode, as_of DESC);
        CREATE TABLE IF NOT EXISTS vibe_research_hypotheses (
          hypothesis_id TEXT PRIMARY KEY, title TEXT NOT NULL,
          thesis TEXT NOT NULL, feature_spec_json TEXT NOT NULL,
          status TEXT NOT NULL, generated_by TEXT NOT NULL,
          validation_model_id TEXT, created_at TEXT NOT NULL,
          reviewed_at TEXT, review_note TEXT
        );
        CREATE TABLE IF NOT EXISTS ai_audit_events (
          event_id TEXT PRIMARY KEY, event_type TEXT NOT NULL,
          model_id TEXT, symbol TEXT, occurred_at TEXT NOT NULL,
          payload_json TEXT NOT NULL
        );
        """)
        conn.commit()

    def register_model(self, record: Dict[str, Any]) -> Dict[str, Any]:
        """Register metadata only; this does not approve or activate a model."""
        required = ("model_id", "model_version", "framework", "horizon_days",
                    "feature_manifest_hash", "dataset_fingerprint", "trained_through")
        missing = [k for k in required if not record.get(k)]
        if missing:
            raise ValueError("missing model metadata: " + ", ".join(missing))
        state = str(record.get("lifecycle_state") or "EXPERIMENTAL").upper()
        if state not in ("EXPERIMENTAL", "SHADOW", "APPROVED", "RETIRED"):
            raise ValueError("invalid lifecycle_state")
        weight = min(MAX_PRODUCTION_WEIGHT, max(0.0, float(record.get("production_weight") or 0.0)))
        if state != "APPROVED":
            weight = 0.0
        payload = dict(record, lifecycle_state=state, production_weight=weight)
        with self.store.write_lock:
            self.store.conn.execute("""INSERT OR REPLACE INTO ai_model_registry
              (model_id,model_version,framework,horizon_days,lifecycle_state,approval_id,
               production_weight,feature_manifest_hash,dataset_fingerprint,trained_through,
               artifact_uri,metadata_json,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
              (payload["model_id"], payload["model_version"], payload["framework"],
               int(payload["horizon_days"]), state, payload.get("approval_id"), weight,
               payload["feature_manifest_hash"], payload["dataset_fingerprint"],
               payload["trained_through"], payload.get("artifact_uri"),
               json.dumps(payload, sort_keys=True), _now()))
            self.store.conn.commit()
        return payload

    def record_prediction(self, prediction: Dict[str, Any]) -> Dict[str, Any]:
        model_id = str(prediction.get("model_id") or "")
        model = self._model(model_id)
        if not model:
            raise ValueError("model is not registered")
        rank = _finite(prediction.get("rank_score"))
        confidence = _finite(prediction.get("confidence"))
        if rank is None or not 0 <= rank <= 100 or confidence is None or not 0 <= confidence <= 1:
            raise ValueError("rank_score must be 0..100 and confidence 0..1")
        for key in ("feature_manifest_hash", "dataset_fingerprint"):
            if prediction.get(key) != model.get(key):
                raise ValueError(f"prediction {key} does not match registered model")
        basis = {k: prediction.get(k) for k in sorted(prediction)}
        pid = hashlib.sha256(json.dumps(basis, sort_keys=True, default=str).encode()).hexdigest()[:24]
        as_of = str(prediction.get("as_of") or _now())
        mode = require_production_mode(prediction.get("mode"))
        payload = dict(prediction, prediction_id=pid, as_of=as_of, mode=mode)
        with self.store.write_lock:
            self.store.conn.execute("""INSERT OR REPLACE INTO ai_predictions
              (prediction_id,model_id,symbol,mode,as_of,horizon_days,rank_score,
               expected_excess_return,confidence,feature_manifest_hash,dataset_fingerprint,
               payload_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
              (pid, model_id, str(prediction.get("symbol") or "").upper(),
               mode, as_of,
               int(prediction.get("horizon_days") or model["horizon_days"]), rank,
               _finite(prediction.get("expected_excess_return")), confidence,
               prediction["feature_manifest_hash"], prediction["dataset_fingerprint"],
               json.dumps(payload, sort_keys=True), _now()))
            self.store.conn.commit()
        return payload

    def shadow_signal(self, symbol: str, mode: str, *, instrument_key: str | None = None) -> Dict[str, Any]:
        """Return the latest governed model calculation with zero rank authority.

        Shadow evidence is consumed by Today Entries, normal scanners and the
        reassessment scanner for measurement and ordering diagnostics.  It never
        changes the canonical score until the separate production assignment is
        active.
        """
        desk = require_production_mode(mode)
        if self.store is None:
            return {"available": False, "eligible": False, "state": "NO_STORE", "weight": 0.0}
        if bool(getattr(self.store, "production_model_governance_required", False)):
            repository = getattr(self.store, "production_model_governance_repository", None)
            if repository is None:
                return {"available": False, "eligible": False, "state": "SHADOW_AUTHORITY_UNAVAILABLE", "weight": 0.0}
            try:
                # Shadow publication may legitimately precede full canonical-key
                # enrichment. Prefer instrument identity when present, while the
                # repository's bounded symbol fallback preserves calculation-only
                # evidence at zero authority. Production inference still requires
                # the canonical instrument key below.
                row = repository.latest_shadow_prediction(
                    instrument_key=str(instrument_key or ""), desk=desk, symbol=str(symbol).upper(),
                )
            except Exception as exc:
                return {"available": False, "eligible": False, "state": "SHADOW_QUERY_FAILED", "reason": str(exc)[:200], "weight": 0.0}
            if not row:
                return {"available": False, "eligible": False, "state": "NO_SHADOW_PREDICTION", "weight": 0.0}
            score = _finite(row.get("predicted_rank"))
            confidence = _finite(row.get("calibrated_confidence"))
            if score is None:
                return {"available": False, "eligible": False, "state": "SHADOW_SCORE_MISSING", "weight": 0.0}
            # Shadow publications store rank_score on a 0..100 scale.
            if 0.0 <= score <= 1.0:
                score *= 100.0
            return {
                "available": True, "eligible": False, "state": "POSTGRES_SHADOW_INFERENCE",
                "model_id": row.get("model_key"), "publication_id": row.get("publication_id"),
                "rank_score": max(0.0, min(100.0, float(score))),
                "confidence": max(0.0, min(1.0, float(confidence if confidence is not None else 0.0))),
                "expected_excess_return": row.get("expected_excess_return"),
                "evaluation_weight": float(row.get("evaluation_paper_weight") or 0.0),
                "weight": 0.0, "production_weight": 0.0,
                "lifecycle_state": row.get("lifecycle_state") or "SHADOW",
                "validation_state": row.get("validation_state"),
                "as_of": str(row.get("as_of") or ""),
                "authority": "GOVERNANCE_POSTGRESQL_SHADOW",
                "reason": "Model calculation recorded as shadow evidence; a separate healthy governed champion assignment may contribute up to 15%.",
            }
        row = self.store.conn.execute(
            """SELECT p.*,m.lifecycle_state,m.model_version,m.framework
                 FROM ai_predictions p JOIN ai_model_registry m ON m.model_id=p.model_id
                WHERE p.symbol=? AND p.mode=? ORDER BY p.as_of DESC LIMIT 1""",
            (str(symbol).upper(), desk),
        ).fetchone()
        if not row:
            return {"available": False, "eligible": False, "state": "NO_SHADOW_PREDICTION", "weight": 0.0}
        item = dict(row)
        score = _finite(item.get("rank_score"))
        if score is None:
            return {"available": False, "eligible": False, "state": "SHADOW_SCORE_MISSING", "weight": 0.0}
        return {
            "available": True, "eligible": False, "state": "LEGACY_SHADOW_INFERENCE",
            "model_id": item.get("model_id"), "model_version": item.get("model_version"),
            "framework": item.get("framework"), "rank_score": float(score),
            "confidence": float(item.get("confidence") or 0.0),
            "expected_excess_return": item.get("expected_excess_return"),
            "weight": 0.0, "production_weight": 0.0,
            "lifecycle_state": item.get("lifecycle_state") or "SHADOW",
            "as_of": item.get("as_of"), "authority": "LEGACY_RESEARCH_SHADOW",
            "reason": "Model calculation recorded as shadow evidence; a separate healthy governed champion assignment may contribute up to 15%.",
        }

    def production_signal(self, symbol: str, mode: str, *, instrument_key: str | None = None) -> Dict[str, Any]:
        if self.store is None:
            return self._blocked("NO_STORE", "AI registry is unavailable")
        if bool(getattr(self.store, "production_model_governance_required", False)):
            repository = getattr(self.store, "production_model_governance_repository", None)
            if repository is None:
                return self._blocked("GOVERNANCE_AUTHORITY_MISSING", "Separate PostgreSQL model-governance authority is unavailable")
            if not str(instrument_key or "").strip():
                return self._blocked("INSTRUMENT_KEY_MISSING", "Production inference requires canonical instrument identity")
            try:
                governed = repository.latest_active_prediction(
                    instrument_key=str(instrument_key),
                    desk=require_production_mode(mode),
                )
            except Exception as exc:
                return self._blocked("GOVERNANCE_QUERY_FAILED", f"Model-governance query failed: {exc}")
            if not governed:
                return self._blocked("GOVERNED_PREDICTION_MISSING", "No fresh frozen prediction from an effective champion assignment")
            percentile = _finite(governed.get("predicted_percentile"))
            confidence = _finite(governed.get("calibrated_confidence"))
            weight = _finite(governed.get("production_weight"))
            stamp = _parse(governed.get("as_of"))
            age = (datetime.now(timezone.utc) - stamp).total_seconds() if stamp else 10**12
            max_age = 1800 if str(mode).lower() == "intraday" else 36 * 3600
            if age < -60 or age > max_age:
                return self._blocked(
                    "STALE_GOVERNED_PREDICTION",
                    f"Champion prediction age {age:.0f}s is outside the {max_age}s desk window",
                    governed,
                )
            if percentile is None or not 0 <= percentile <= 1:
                return self._blocked("RANK_CONTRACT_MISSING", "Governed prediction lacks a valid cross-sectional percentile")
            if confidence is None or not 0 <= confidence <= 1:
                return self._blocked("CALIBRATION_CONTRACT_MISSING", "Governed prediction lacks calibrated model reliability")
            if weight is None or not 0 < weight <= MAX_PRODUCTION_WEIGHT:
                return self._blocked("ASSIGNMENT_WEIGHT_INVALID", "Champion assignment weight is missing or exceeds the production cap")
            return {
                "eligible": True,
                "state": "POSTGRES_CHAMPION_INFERENCE",
                "model_id": str(governed.get("model_id")),
                "model_key": governed.get("model_key"),
                "model_version": governed.get("model_version"),
                "framework": governed.get("model_type"),
                "approval_id": str(governed.get("promotion_decision_id")),
                "assignment_id": str(governed.get("assignment_id")),
                "prediction_id": str(governed.get("prediction_id")),
                "rank_score": float(percentile) * 100.0,
                "confidence": float(confidence),
                "expected_excess_return": governed.get("return_q50"),
                "weight": float(weight),
                "as_of": str(governed.get("as_of")),
                "prediction_contract": {
                    key: governed.get(key) for key in (
                        "instrument_key", "as_of", "data_cutoff_at", "cost_model_version",
                        "return_basis", "effective_sample_size", "net_return_standard_error",
                        "uncertainty_method", "predicted_rank", "predicted_percentile",
                        "target_before_stop_probability", "stop_before_target_probability",
                        "neither_probability", "calibrated_confidence", "observation_price",
                        "target_price", "stop_price", "horizon_end_at", "label_parameters",
                        "return_q05", "return_q50", "return_q95", "mae_q50", "mfe_q50",
                        "expected_time_to_target", "expected_time_to_stop",
                        "uncertainty_lower", "uncertainty_upper", "regime_observation_id",
                    )
                },
                "factor_authority": {"eligible": True, "state": "GOVERNANCE_POSTGRES_LINEAGE"},
                "governance_version": GOVERNANCE_VERSION,
                "authority": "SEPARATE_GOVERNANCE_POSTGRES",
            }
        row = self.store.conn.execute("""SELECT p.*,m.lifecycle_state,m.approval_id,
          m.production_weight,m.model_version,m.framework,m.trained_through,
          m.metadata_json,
          m.feature_manifest_hash AS model_feature_hash,
          m.dataset_fingerprint AS model_dataset_hash
          FROM ai_predictions p JOIN ai_model_registry m ON m.model_id=p.model_id
          WHERE p.symbol=? AND p.mode=? ORDER BY p.as_of DESC LIMIT 1""",
          (str(symbol).upper(), str(mode).lower())).fetchone()
        if not row:
            return self._blocked("NO_PREDICTION", "No point-in-time AI prediction for this stock and mode")
        item = dict(row)
        if item["lifecycle_state"] != "APPROVED":
            return self._blocked("MODEL_NOT_APPROVED", "Model remains experimental/shadow", item)
        if not self._approval_valid(item["model_id"], item.get("approval_id")):
            return self._blocked("APPROVAL_MISSING", "Exact walk-forward approval is absent or revoked", item)
        if item["feature_manifest_hash"] != item["model_feature_hash"] or item["dataset_fingerprint"] != item["model_dataset_hash"]:
            return self._blocked("LINEAGE_MISMATCH", "Prediction lineage differs from the approved model", item)
        stamp = _parse(item.get("as_of")); age = (datetime.now(timezone.utc) - stamp).total_seconds() if stamp else 10**12
        max_age = 1800 if str(mode).lower() == "intraday" else 36 * 3600
        if age < -60 or age > max_age:
            return self._blocked("STALE_PREDICTION", "AI prediction is outside its freshness window", item)
        factor_authority = self.factor_authority.authorize_model(item)
        if not factor_authority.get("eligible"):
            blocked = self._blocked(factor_authority["state"], factor_authority["reason"], item)
            blocked["factor_authority"] = factor_authority
            return blocked
        weight = min(MAX_PRODUCTION_WEIGHT, max(0.0, float(item.get("production_weight") or DEFAULT_PRODUCTION_WEIGHT)))
        return {"eligible": True, "state": "APPROVED_INFERENCE", "model_id": item["model_id"],
                "model_version": item["model_version"], "framework": item["framework"],
                "approval_id": item["approval_id"], "prediction_id": item["prediction_id"],
                "rank_score": float(item["rank_score"]), "confidence": float(item["confidence"]),
                "expected_excess_return": item.get("expected_excess_return"), "weight": weight,
                "as_of": item["as_of"], "factor_authority": factor_authority,
                "governance_version": GOVERNANCE_VERSION}

    def status(self) -> Dict[str, Any]:
        if self.store is None:
            return {"ok": False, "governance_version": GOVERNANCE_VERSION, "models": []}
        models = [dict(r) for r in self.store.conn.execute("SELECT model_id,model_version,framework,horizon_days,lifecycle_state,approval_id,production_weight,trained_through,updated_at,metadata_json FROM ai_model_registry ORDER BY updated_at DESC").fetchall()]
        authority_service = self.factor_authority
        challenger_governance = ModelChallengerGovernanceService()
        for model in models:
            model["factor_authority"] = authority_service.authorize_model(model)
            try:
                metadata = json.loads(model.get("metadata_json") or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                metadata = {}
            model["challenger_governance"] = challenger_governance.assess(metadata or model, metadata.get("evidence") or {})
            model.pop("metadata_json", None)
        hypotheses = self.store.conn.execute("SELECT status,COUNT(*) n FROM vibe_research_hypotheses GROUP BY status").fetchall()
        try:
            daily = self.store.conn.execute("SELECT COUNT(DISTINCT substr(ts,1,10)),COUNT(DISTINCT instrument_key) FROM candles WHERE lower(interval) IN ('1d','day','1day')").fetchone()
            delivery = self.store.conn.execute("SELECT COUNT(DISTINCT trade_date),COUNT(DISTINCT symbol) FROM delivery_data").fetchone()
            coverage = {"daily_dates": int(daily[0] or 0), "daily_symbols": int(daily[1] or 0),
                        "delivery_dates": int(delivery[0] or 0), "delivery_symbols": int(delivery[1] or 0),
                        "required_daily_dates": 315, "required_delivery_dates": 252}
            coverage["training_ready"] = coverage["daily_dates"] >= 315 and coverage["delivery_dates"] >= 252 and coverage["daily_symbols"] >= 25
        except Exception:
            coverage = {"training_ready": False, "state": "coverage_unavailable"}
        return {"ok": True, "governance_version": GOVERNANCE_VERSION,
                "policy": "Qlib/Vibe influence production only through fresh predictions from exactly approved point-in-time models.",
                "max_production_weight": MAX_PRODUCTION_WEIGHT, "models": models,
                "vibe_hypotheses": {r[0]: r[1] for r in hypotheses}, "training_coverage": coverage,
                "factor_authority": authority_service.status(),
                "challenger_model_policy": challenger_governance.status()}

    def _model(self, model_id: str) -> Optional[Dict[str, Any]]:
        row = self.store.conn.execute("SELECT * FROM ai_model_registry WHERE model_id=?", (model_id,)).fetchone()
        return dict(row) if row else None

    def _approval_valid(self, model_id: str, approval_id: Any) -> bool:
        if not approval_id:
            return False
        try:
            row = self.store.conn.execute("SELECT 1 FROM validation_approvals WHERE approval_id=? AND model_id=? AND status='APPROVED'", (approval_id, model_id)).fetchone()
            return bool(row)
        except Exception:
            return False

    @staticmethod
    def _blocked(state: str, reason: str, item: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return {"eligible": False, "state": state, "reason": reason,
                "model_id": (item or {}).get("model_id"), "weight": 0.0,
                "governance_version": GOVERNANCE_VERSION}
