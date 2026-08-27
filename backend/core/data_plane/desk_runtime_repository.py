"""PostgreSQL authority for fast dual-desk candidate and lifecycle checkpoints.

Hot calculations remain in memory. PostgreSQL receives only meaningful candidate
state transitions and compact worker checkpoints, never every tick or score twitch.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Dict, Mapping

from core.production_mode_policy import require_production_mode


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value if value is not None else {}, separators=(",", ":"), sort_keys=True, default=str)


class DeskRuntimeRepository:
    VERSION = "desk-runtime-repository-1.0.0"

    def __init__(self, operational):
        self.operational = operational

    @staticmethod
    def candidate_id(row: Mapping[str, Any]) -> str:
        desk = require_production_mode(row.get("desk") or row.get("mode"))
        symbol = str(row.get("symbol") or "").upper().strip()
        strategy = str(row.get("strategy") or row.get("setup") or row.get("source") or "unspecified").lower().strip()
        raw = f"{desk}|{symbol}|{strategy}"
        return "CAND-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:28]

    def upsert_candidate(self, row: Mapping[str, Any]) -> Dict[str, Any]:
        source = dict(row or {})
        desk = require_production_mode(source.get("desk") or source.get("mode"))
        symbol = str(source.get("symbol") or "").upper().strip()
        if not symbol:
            raise ValueError("candidate symbol is required")
        strategy = str(source.get("strategy") or source.get("setup") or source.get("source") or "unspecified").lower().strip()
        candidate_id = str(source.get("candidate_id") or self.candidate_id({**source, "desk": desk, "symbol": symbol, "strategy": strategy}))
        state = str(source.get("state") or "DISCOVERED").upper()
        allowed = {"DISCOVERED","PREQUALIFIED","ENRICHING","READY_FOR_GATE","GATE_EVALUATION","RESEARCH","PROMOTED","REJECTED","EXPIRED"}
        if state not in allowed:
            state = "RESEARCH"
        stamp = str(source.get("updated_at") or _now())
        with self.operational.transaction(isolation_level="read committed", lock_timeout_ms=500, statement_timeout_ms=1500) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT state,row_version FROM trading.desk_candidates WHERE candidate_id=%s FOR UPDATE", (candidate_id,))
                previous = cur.fetchone()
                old_state = str(previous["state"]) if previous else None
                cur.execute(
                    """INSERT INTO trading.desk_candidates(
                         candidate_id,instrument_key,symbol,exchange,desk,strategy,direction,state,priority,
                         entry_price,target_price,stop_price,score,model_probability,risk_reward,data_freshness,
                         evidence_summary,missing_requirements,source_methods,next_evaluation_at,expires_at,
                         decision_id,row_version,active,created_at,updated_at)
                       VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb,%s::jsonb,%s,%s,%s,1,%s,%s,%s)
                       ON CONFLICT(candidate_id) DO UPDATE SET
                         instrument_key=COALESCE(EXCLUDED.instrument_key,trading.desk_candidates.instrument_key),
                         exchange=EXCLUDED.exchange,direction=EXCLUDED.direction,state=EXCLUDED.state,
                         priority=EXCLUDED.priority,entry_price=EXCLUDED.entry_price,target_price=EXCLUDED.target_price,
                         stop_price=EXCLUDED.stop_price,score=EXCLUDED.score,model_probability=EXCLUDED.model_probability,
                         risk_reward=EXCLUDED.risk_reward,data_freshness=EXCLUDED.data_freshness,
                         evidence_summary=EXCLUDED.evidence_summary,missing_requirements=EXCLUDED.missing_requirements,
                         source_methods=EXCLUDED.source_methods,next_evaluation_at=EXCLUDED.next_evaluation_at,
                         expires_at=EXCLUDED.expires_at,decision_id=COALESCE(EXCLUDED.decision_id,trading.desk_candidates.decision_id),
                         row_version=trading.desk_candidates.row_version+1,active=EXCLUDED.active,updated_at=EXCLUDED.updated_at""",
                    (candidate_id, source.get("instrument_key"), symbol, str(source.get("exchange") or "NSE").upper(), desk,
                     strategy, str(source.get("direction") or source.get("side") or "LONG").upper(), state,
                     float(source.get("priority") or source.get("priority_score") or 0), source.get("entry_price") or source.get("entry"),
                     source.get("target_price") or source.get("target"), source.get("stop_price") or source.get("stop_loss") or source.get("stop"),
                     source.get("score"), source.get("model_probability") or source.get("probability"), source.get("risk_reward"),
                     _json(source.get("data_freshness") or {}), _json(source.get("evidence_summary") or source.get("evidence") or {}),
                     _json(source.get("missing_requirements") or []), _json(source.get("source_methods") or [source.get("source") or strategy]),
                     source.get("next_evaluation_at"), source.get("expires_at"), source.get("decision_id"), state not in {"REJECTED","EXPIRED"}, stamp, stamp)
                )
                if old_state != state:
                    cur.execute("SELECT COALESCE(MAX(event_sequence),0)+1 AS seq FROM trading.desk_candidate_events WHERE candidate_id=%s", (candidate_id,))
                    seq = int(cur.fetchone()["seq"])
                    cur.execute(
                        """INSERT INTO trading.desk_candidate_events(candidate_id,event_sequence,event_type,from_state,to_state,reason_code,payload,occurred_at)
                           VALUES(%s,%s,%s,%s,%s,%s,%s::jsonb,%s)""",
                        (candidate_id, seq, "STATE_CHANGED" if old_state else "CANDIDATE_CREATED", old_state, state,
                         str(source.get("reason_code") or source.get("reason") or "scanner_evaluation"), _json(source.get("event_payload") or {}), stamp)
                    )
        return {"ok": True, "candidate_id": candidate_id, "desk": desk, "state": state}

    def checkpoint(self, worker_name: str, desk: str, worker_kind: str, state: str, payload: Mapping[str, Any] | None = None) -> None:
        desk = require_production_mode(desk)
        if worker_kind not in {"candidate", "lifecycle"}:
            raise ValueError("worker_kind must be candidate or lifecycle")
        stamp = _now()
        with self.operational.transaction(isolation_level="read committed", lock_timeout_ms=300, statement_timeout_ms=1000) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO trading.desk_runtime_checkpoints(worker_name,desk,worker_kind,state,payload,heartbeat_at,updated_at)
                       VALUES(%s,%s,%s,%s,%s::jsonb,%s,%s)
                       ON CONFLICT(worker_name) DO UPDATE SET desk=EXCLUDED.desk,worker_kind=EXCLUDED.worker_kind,
                       state=EXCLUDED.state,payload=EXCLUDED.payload,heartbeat_at=EXCLUDED.heartbeat_at,updated_at=EXCLUDED.updated_at""",
                    (worker_name, desk, worker_kind, state, _json(payload or {}), stamp, stamp)
                )
