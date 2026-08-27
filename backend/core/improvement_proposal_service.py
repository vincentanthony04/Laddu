"""Persisted, human-governed improvement proposals and isolated challengers.

This service turns the read-only ImprovementReview into an auditable workflow.
It may create a research proposal and, only after explicit human approval and
an ACCEPT_FOR_CHALLENGER review, activate the already-computed hybrid model in
an isolated Model Paper lane.  Production weight and broker authority remain
zero in every state.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import threading
from typing import Any, Dict, List, Mapping, Optional
from uuid import uuid4

from core.improvement_review_service import ImprovementReviewService
from core.forward_evidence_clock_service import ForwardEvidenceClockService
from core.forward_horizon_policy import canonical_horizon, normalise_desk


SERVICE_VERSION = "improvement-proposal-workflow-1.2.0-forward-reassessment"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _hash(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8", errors="replace")).hexdigest()


class ImprovementProposalService:
    def __init__(self, store: Any):
        self.store = store
        if not hasattr(store, "write_lock"):
            store.write_lock = threading.RLock()
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        with self.store.write_lock:
            self.store.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS improvement_proposals (
                  proposal_id TEXT PRIMARY KEY,
                  mode TEXT NOT NULL,
                  horizon TEXT NOT NULL,
                  recommendation TEXT NOT NULL,
                  status TEXT NOT NULL,
                  evidence_hash TEXT NOT NULL,
                  model_version TEXT,
                  proposal_json TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  decided_at TEXT,
                  decided_by TEXT,
                  decision_reason TEXT,
                  production_influence REAL NOT NULL DEFAULT 0,
                  broker_authority TEXT NOT NULL DEFAULT 'NONE',
                  workflow_version TEXT NOT NULL,
                  research_hypothesis_id TEXT
                );
                CREATE INDEX IF NOT EXISTS ix_improvement_proposals_latest
                  ON improvement_proposals(mode,horizon,created_at);
                CREATE TABLE IF NOT EXISTS improvement_proposal_events (
                  event_id TEXT PRIMARY KEY,
                  proposal_id TEXT NOT NULL,
                  event_type TEXT NOT NULL,
                  event_json TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  FOREIGN KEY(proposal_id) REFERENCES improvement_proposals(proposal_id)
                );
                CREATE INDEX IF NOT EXISTS ix_improvement_proposal_events
                  ON improvement_proposal_events(proposal_id,created_at);
                CREATE TABLE IF NOT EXISTS improvement_challenger_activations (
                  activation_id TEXT PRIMARY KEY,
                  proposal_id TEXT NOT NULL UNIQUE,
                  mode TEXT NOT NULL,
                  horizon TEXT NOT NULL,
                  model_version TEXT NOT NULL,
                  state TEXT NOT NULL,
                  started_at TEXT NOT NULL,
                  last_reconciled_at TEXT,
                  activation_json TEXT NOT NULL,
                  production_influence REAL NOT NULL DEFAULT 0,
                  broker_authority TEXT NOT NULL DEFAULT 'NONE',
                  FOREIGN KEY(proposal_id) REFERENCES improvement_proposals(proposal_id)
                );
                CREATE TABLE IF NOT EXISTS vibe_research_hypotheses (
                  hypothesis_id TEXT PRIMARY KEY, title TEXT NOT NULL,
                  thesis TEXT NOT NULL, feature_spec_json TEXT NOT NULL,
                  status TEXT NOT NULL, generated_by TEXT NOT NULL,
                  validation_model_id TEXT, created_at TEXT NOT NULL,
                  reviewed_at TEXT, review_note TEXT
                );
                """
            )
            columns = {str(row[1]) for row in self.store.conn.execute("PRAGMA table_info(improvement_proposals)").fetchall()}
            if "research_hypothesis_id" not in columns:
                self.store.conn.execute("ALTER TABLE improvement_proposals ADD COLUMN research_hypothesis_id TEXT")
            self.store.conn.commit()

    @staticmethod
    def _governed_hybrid_model(review: Mapping[str, Any]) -> Optional[str]:
        value = str(review.get("governed_challenger_model_version") or "").strip()
        return value or None

    def _upsert_research_hypothesis(
        self, current: Mapping[str, Any], *, actor: str, reason: str, status: str
    ) -> str:
        proposal_id = str(current["proposal_id"])
        evidence_hash = str(current.get("evidence_hash") or "")
        model_version = str(current.get("model_version") or "").strip() or None
        hypothesis_id = "hypothesis:" + hashlib.sha256(
            f"{proposal_id}|{evidence_hash}".encode("utf-8")
        ).hexdigest()[:24]
        proposal_payload = dict(current.get("proposal") or {})
        review = dict(proposal_payload.get("review") or {})
        recommendation = dict(review.get("recommendation") or {})
        feature_spec = {
            "proposal_id": proposal_id,
            "evidence_hash": evidence_hash,
            "mode": current.get("mode"),
            "horizon": current.get("horizon"),
            "recommendation": recommendation,
            "model_versions": review.get("model_versions") or {},
            "governed_challenger_model_version": model_version,
            "walk_forward_version": (review.get("walk_forward") or {}).get("version"),
            "production_influence": 0.0,
            "broker_authority": "NONE",
        }
        now = _now()
        title = f"{str(current.get('mode') or '').upper()} {current.get('horizon')} governed improvement {evidence_hash[:10]}"
        thesis = str(recommendation.get("reason") or reason or "Governed research hypothesis")
        self.store.conn.execute(
            """INSERT OR IGNORE INTO vibe_research_hypotheses(
                 hypothesis_id,title,thesis,feature_spec_json,status,generated_by,
                 validation_model_id,created_at,reviewed_at,review_note
               ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (hypothesis_id, title, thesis, _json(feature_spec), status, SERVICE_VERSION,
             model_version, now, now, reason),
        )
        self.store.conn.execute(
            """UPDATE vibe_research_hypotheses
                  SET status=?,validation_model_id=?,reviewed_at=?,review_note=?
                WHERE hypothesis_id=?""",
            (status, model_version, now, reason, hypothesis_id),
        )
        self.store.conn.execute(
            "UPDATE improvement_proposals SET research_hypothesis_id=?,updated_at=? WHERE proposal_id=?",
            (hypothesis_id, now, proposal_id),
        )
        return hypothesis_id

    def _event(self, proposal_id: str, event_type: str, payload: Mapping[str, Any]) -> None:
        event_id = f"ipe:{uuid4().hex}"
        self.store.conn.execute(
            "INSERT INTO improvement_proposal_events(event_id,proposal_id,event_type,event_json,created_at) VALUES(?,?,?,?,?)",
            (event_id, proposal_id, event_type, _json(dict(payload)), _now()),
        )

    def create(self, *, mode: str, horizon: str, actor: str = "local_operator") -> Dict[str, Any]:
        mode = normalise_desk(mode)
        horizon = canonical_horizon(mode, horizon)
        actor = str(actor or "local_operator").strip() or "local_operator"
        review = ImprovementReviewService(self.store).review(mode=mode, horizon=horizon)
        proposal_id = str(review["proposal_id"])
        recommendation = str((review.get("recommendation") or {}).get("decision") or "BLOCKED")
        evidence_hash = _hash({
            "mode": mode,
            "horizon": horizon,
            "recommendation": review.get("recommendation"),
            "forward_evidence": review.get("forward_evidence"),
            "walk_forward": review.get("walk_forward"),
        })
        model_version = self._governed_hybrid_model(review)
        status = {
            "ACCEPT_FOR_RESEARCH": "PENDING_RESEARCH_APPROVAL",
            "ACCEPT_FOR_CHALLENGER": "PENDING_CHALLENGER_APPROVAL",
            "QUARANTINE": "PENDING_QUARANTINE_REVIEW",
            "REJECT": "REJECTED_BY_EVIDENCE",
            "RETAIN_CURRENT_VERSION": "RETAIN_CURRENT_VERSION",
        }.get(recommendation, "BLOCKED")
        now = _now()
        payload = {
            "review": review,
            "created_by": actor,
            "model_version": model_version,
            "authority": {
                "human_approval_required": True,
                "production_influence": 0.0,
                "broker_authority": "NONE",
                "proposal_persistence": "LOCAL_RESEARCH_WORKFLOW_PROJECTION",
                "production_governance": "GOVERNANCE_POSTGRESQL_UNCHANGED",
            },
        }
        with self.store.write_lock:
            cursor = self.store.conn.execute(
                """INSERT OR IGNORE INTO improvement_proposals(
                     proposal_id,mode,horizon,recommendation,status,evidence_hash,model_version,
                     proposal_json,created_at,updated_at,production_influence,broker_authority,workflow_version
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (proposal_id, mode, horizon, recommendation, status, evidence_hash, model_version,
                 _json(payload), now, now, 0.0, "NONE", SERVICE_VERSION),
            )
            inserted = int(cursor.rowcount or 0) == 1
            if inserted:
                self._event(proposal_id, "PROPOSAL_CREATED", {"status": status, "actor": actor, "evidence_hash": evidence_hash})
            self.store.conn.commit()
        return self.get(proposal_id)

    def get(self, proposal_id: str) -> Dict[str, Any]:
        row = self.store.conn.execute(
            "SELECT * FROM improvement_proposals WHERE proposal_id=?", (proposal_id,)
        ).fetchone()
        if not row:
            raise KeyError("proposal not found")
        item = dict(row)
        try:
            item["proposal"] = json.loads(item.pop("proposal_json") or "{}")
        except Exception:
            item["proposal"] = {}
        activation = self.store.conn.execute(
            "SELECT * FROM improvement_challenger_activations WHERE proposal_id=?", (proposal_id,)
        ).fetchone()
        item["activation"] = dict(activation) if activation else None
        item["production_influence"] = 0.0
        item["broker_authority"] = "NONE"
        item["persistence_authority"] = "LOCAL_RESEARCH_WORKFLOW_PROJECTION"
        item["production_governance"] = "GOVERNANCE_POSTGRESQL_UNCHANGED"
        item["ok"] = True
        item["version"] = SERVICE_VERSION
        return item

    def list(self, *, mode: Optional[str] = None, limit: int = 50) -> Dict[str, Any]:
        params: List[Any] = []
        where = ""
        if mode:
            where = "WHERE mode=?"
            params.append(str(mode).lower())
        params.append(max(1, min(200, int(limit))))
        rows = self.store.conn.execute(
            f"SELECT proposal_id FROM improvement_proposals {where} ORDER BY created_at DESC LIMIT ?",
            tuple(params),
        ).fetchall()
        return {
            "ok": True,
            "version": SERVICE_VERSION,
            "rows": [self.get(str(row[0])) for row in rows],
            "production_influence": 0.0,
            "broker_authority": "NONE",
            "persistence_authority": "LOCAL_RESEARCH_WORKFLOW_PROJECTION",
            "production_governance": "GOVERNANCE_POSTGRESQL_UNCHANGED",
        }

    def decide(self, *, proposal_id: str, action: str, actor: str, reason: str) -> Dict[str, Any]:
        action_key = str(action or "").upper().strip()
        if action_key not in {"APPROVE_RESEARCH", "APPROVE_CHALLENGER", "REJECT", "QUARANTINE"}:
            raise ValueError("unsupported proposal action")
        if not str(actor or "").strip() or not str(reason or "").strip():
            raise ValueError("actor and reason are required")
        current = self.get(proposal_id)
        recommendation = str(current.get("recommendation") or "")
        current_status = str(current.get("status") or "")
        if current_status in {"REJECTED", "QUARANTINED"}:
            return current
        if current_status == "CHALLENGER_ACTIVE" and action_key == "APPROVE_CHALLENGER":
            return current
        if current_status == "RESEARCH_APPROVED" and action_key == "APPROVE_RESEARCH":
            return current
        if action_key == "APPROVE_RESEARCH" and recommendation not in {"ACCEPT_FOR_RESEARCH", "ACCEPT_FOR_CHALLENGER"}:
            raise ValueError("evidence does not permit research approval")
        if action_key == "APPROVE_CHALLENGER" and recommendation != "ACCEPT_FOR_CHALLENGER":
            raise ValueError("evidence does not permit challenger approval")
        model_version = str(current.get("model_version") or "").strip()
        if action_key == "APPROVE_CHALLENGER" and not model_version:
            raise ValueError("no governed hybrid model_version is available for isolated activation")
        status = {
            "APPROVE_RESEARCH": "RESEARCH_APPROVED",
            "APPROVE_CHALLENGER": "CHALLENGER_ACTIVE",
            "REJECT": "REJECTED",
            "QUARANTINE": "QUARANTINED",
        }[action_key]
        now = _now()
        with self.store.write_lock:
            self.store.conn.execute(
                """UPDATE improvement_proposals
                      SET status=?,updated_at=?,decided_at=?,decided_by=?,decision_reason=?,
                          production_influence=0,broker_authority='NONE'
                    WHERE proposal_id=?""",
                (status, now, now, actor, reason, proposal_id),
            )
            self._event(proposal_id, action_key, {"status": status, "actor": actor, "reason": reason})
            hypothesis_id = None
            if action_key in {"APPROVE_RESEARCH", "APPROVE_CHALLENGER"}:
                hypothesis_status = "SHADOW_CHALLENGER_ACTIVE" if action_key == "APPROVE_CHALLENGER" else "APPROVED_RESEARCH"
                hypothesis_id = self._upsert_research_hypothesis(
                    current, actor=actor, reason=reason, status=hypothesis_status
                )
            if action_key == "QUARANTINE":
                self.store.conn.execute(
                    """UPDATE improvement_challenger_activations
                          SET state='QUARANTINED',last_reconciled_at=?,production_influence=0,broker_authority='NONE'
                        WHERE proposal_id=?""",
                    (now, proposal_id),
                )
            if action_key == "APPROVE_CHALLENGER":
                activation_id = f"challenger:{hashlib.sha256((proposal_id+'|'+model_version).encode()).hexdigest()[:24]}"
                activation = {
                    "proposal_id": proposal_id,
                    "mode": current["mode"],
                    "horizon": current["horizon"],
                    "model_version": model_version,
                    "research_hypothesis_id": hypothesis_id,
                    "lane": "ISOLATED_MODEL_PAPER",
                    "production_influence": 0.0,
                    "broker_authority": "NONE",
                    "human_approved_by": actor,
                    "human_approval_reason": reason,
                }
                self.store.conn.execute(
                    """INSERT OR REPLACE INTO improvement_challenger_activations(
                         activation_id,proposal_id,mode,horizon,model_version,state,started_at,
                         last_reconciled_at,activation_json,production_influence,broker_authority
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (activation_id, proposal_id, current["mode"], current["horizon"], model_version,
                     "ACTIVE_SHADOW", now, now, _json(activation), 0.0, "NONE"),
                )
            self.store.conn.commit()
        return self.get(proposal_id)

    def reconcile(self) -> Dict[str, Any]:
        clock = ForwardEvidenceClockService(self.store).status()
        rows = self.store.conn.execute(
            "SELECT * FROM improvement_challenger_activations WHERE state IN ('ACTIVE_SHADOW','QUARANTINE_REVIEW_REQUIRED')"
        ).fetchall()
        changed = 0
        now = _now()
        with self.store.write_lock:
            for raw in rows:
                row = dict(raw)
                try:
                    payload = json.loads(row.get("activation_json") or "{}")
                except Exception:
                    payload = {}
                desk = str(row.get("mode") or "")
                arm = ((clock.get("by_desk_arm") or {}).get(desk) or {}).get("hybrid") or {}
                try:
                    reassessment = ImprovementReviewService(self.store).review(
                        mode=desk, horizon=str(row.get("horizon") or "")
                    )
                except Exception as exc:
                    reassessment = {
                        "ok": False, "recommendation": {"decision": "BLOCKED", "reason": f"reassessment unavailable: {type(exc).__name__}: {exc}"[:240]},
                    }
                recommendation = str(((reassessment.get("recommendation") or {}).get("decision") or "BLOCKED"))
                review_material = {
                    "review_version": reassessment.get("version"),
                    "proposal_id": reassessment.get("proposal_id"),
                    "recommendation": reassessment.get("recommendation"),
                    "governed_challenger_model_version": reassessment.get("governed_challenger_model_version"),
                }
                review_hash = _hash(review_material)
                next_state = "QUARANTINE_REVIEW_REQUIRED" if recommendation == "QUARANTINE" else "ACTIVE_SHADOW"
                snapshot = {
                    **dict(payload),
                    "last_reconciled_at": now,
                    "forward_clock_state": clock.get("state"),
                    "settled_observation_count": int(arm.get("settled_observation_count") or 0),
                    "mean_net_return_bps": arm.get("mean_net_return_bps"),
                    "latest_research_recommendation": recommendation,
                    "latest_research_reason": (reassessment.get("recommendation") or {}).get("reason"),
                    "latest_research_evidence_hash": review_hash,
                    "complexity_contribution": (((reassessment.get("validation") or {}).get("complexity_contribution") or {}).get("hybrid_vs_mathematics")),
                    "automatic_production_mutation": False,
                    "production_influence": 0.0,
                    "broker_authority": "NONE",
                }
                self.store.conn.execute(
                    """UPDATE improvement_challenger_activations
                          SET state=?,last_reconciled_at=?,activation_json=?,production_influence=0,broker_authority='NONE'
                        WHERE activation_id=?""",
                    (next_state, now, _json(snapshot), row["activation_id"]),
                )
                if next_state == "QUARANTINE_REVIEW_REQUIRED":
                    self._event(str(row["proposal_id"]), "FORWARD_EVIDENCE_QUARANTINE_REVIEW_REQUIRED", {
                        "activation_id": row["activation_id"], "actor": "automatic-evidence-reconciliation",
                        "evidence_hash": review_hash, "production_influence": 0.0, "broker_authority": "NONE",
                    })
                changed += 1
            self.store.conn.commit()
        return {
            "ok": True,
            "version": SERVICE_VERSION,
            "active_challengers_reconciled": changed,
            "forward_clock": clock,
            "production_influence": 0.0,
            "broker_authority": "NONE",
            "persistence_authority": "LOCAL_RESEARCH_WORKFLOW_PROJECTION",
            "production_governance": "GOVERNANCE_POSTGRESQL_UNCHANGED",
        }
