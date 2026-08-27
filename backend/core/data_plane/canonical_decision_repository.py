from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from typing import Any, Callable, Dict, Mapping, Optional

from core.canonical_decision_repository import (
    ACTIVE_STATES,
    ALLOWED_STATES,
    CANONICAL_DECISION_VERSION,
    CANONICAL_EVENT_VERSION,
    FREEZE_STATES,
    TERMINAL_STATES,
    _ALLOWED_TRANSITIONS,
    CanonicalDecisionRepository,
)
from core.india_time import trading_date_ist
from core.production_mode_policy import require_production_mode
from .postgres import PostgresAuthority


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _sha(value: Any, size: int = 64) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()[:size]


class ProductionCanonicalDecisionRepository:
    """PostgreSQL authority for canonical Intraday/Delivery decisions.

    It deliberately exposes the same read/write contract as the legacy SQLite
    repository, while all mutation, event append and projection-outbox writes
    commit in one bounded PostgreSQL transaction.
    """

    production_authority = True

    def __init__(
        self, operational: PostgresAuthority, event_fn: Optional[Callable[..., None]] = None,
        read_authority: PostgresAuthority | None = None,
    ):
        self.operational = operational
        # Interactive HTTP/read models use a dedicated bounded PostgreSQL pool.
        # Mutation/serializable transaction paths remain on operational.
        self.read_authority = read_authority or operational
        self._event_fn = event_fn

    @staticmethod
    def _decode(row: Mapping[str, Any] | None) -> Dict[str, Any]:
        if not row:
            return {}
        out = dict(row)
        for key in (
            "entry_plan", "risk_plan", "candidate_snapshot", "frozen_evidence",
            "live_snapshot", "confidence", "data_lineage", "rejection_reasons",
            "latest_payload", "outcome",
        ):
            value = out.get(key)
            if isinstance(value, str):
                try:
                    out[key] = json.loads(value)
                except Exception:
                    pass
        for key, value in list(out.items()):
            if isinstance(value, datetime):
                out[key] = value.isoformat().replace("+00:00", "Z")
        return out

    @staticmethod
    def _event_key(decision_id: str, event_type: str, occurred_at: Any, payload: Mapping[str, Any]) -> str:
        return _sha({"decision_id": decision_id, "event_type": event_type, "occurred_at": str(occurred_at), "payload": dict(payload)})

    def _append_event(
        self,
        conn: Any,
        *,
        decision_id: str,
        thesis_id: str,
        event_type: str,
        from_state: str | None,
        to_state: str | None,
        reason: str,
        payload: Mapping[str, Any],
        occurred_at: Any,
    ) -> None:
        event_key = self._event_key(decision_id, event_type, occurred_at, payload)
        material = json.dumps(dict(payload), sort_keys=True, default=str)
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO trading.canonical_decision_events(
                       event_key,decision_id,thesis_id,event_type,from_state,to_state,reason,payload,occurred_at,contract_version)
                   VALUES(%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s)
                   ON CONFLICT(event_key) DO NOTHING""",
                (event_key, decision_id, thesis_id, event_type, from_state, to_state, reason, material, occurred_at, CANONICAL_EVENT_VERSION),
            )
            cur.execute(
                """INSERT INTO integration.transactional_outbox(
                       event_key,aggregate_type,aggregate_id,event_type,payload,occurred_at)
                   VALUES(%s,'canonical_decision',%s,%s,%s::jsonb,%s)
                   ON CONFLICT(event_key) DO NOTHING""",
                ("projection:" + event_key, decision_id, event_type, material, occurred_at),
            )

    def _existing(self, conn: Any, identity: Mapping[str, str]) -> Dict[str, Any] | None:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM trading.canonical_decisions WHERE thesis_key=%s AND active ORDER BY updated_at DESC LIMIT 1",
                (identity["thesis_key"],),
            )
            row = cur.fetchone()
            if row:
                return dict(row)
            if identity["mode"] == "delivery":
                cur.execute(
                    "SELECT * FROM trading.canonical_decisions WHERE symbol=%s AND mode='delivery' AND side=%s AND active ORDER BY created_at LIMIT 1",
                    (identity["symbol"], identity["side"]),
                )
                row = cur.fetchone()
                if row:
                    return dict(row)
            cur.execute(
                "SELECT * FROM trading.canonical_decisions WHERE thesis_key=%s AND trading_date=%s::date ORDER BY updated_at DESC LIMIT 1",
                (identity["thesis_key"], identity["trading_date"]),
            )
            row = cur.fetchone()
            return dict(row) if row else None

    def record(self, decision: Mapping[str, Any]) -> Dict[str, Any]:
        source = dict(decision or {})
        source["mode"] = require_production_mode(source.get("mode"))
        requested_state = CanonicalDecisionRepository.derive_state(source)
        if requested_state not in ALLOWED_STATES:
            requested_state = "WATCHING"
        identity = CanonicalDecisionRepository._identity(source)
        entry_plan = CanonicalDecisionRepository._entry_plan(source)
        risk_plan = CanonicalDecisionRepository._risk_plan(source)
        live = CanonicalDecisionRepository._live_snapshot(source)
        confidence = CanonicalDecisionRepository._confidence(source)
        lineage = CanonicalDecisionRepository._lineage(source)
        rejection_reasons = CanonicalDecisionRepository._rejection_reasons(source, requested_state)
        publication = CanonicalDecisionRepository.publication_authority(source, requested_state)
        execution = CanonicalDecisionRepository.execution_authority(source)
        stamp = str(source.get("updated_at") or source.get("decision_as_of") or _now())
        latest_payload = dict(source)

        with self.operational.transaction(
            isolation_level="serializable", lock_timeout_ms=1000,
            statement_timeout_ms=4000, idle_timeout_ms=5000,
        ) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", ("canonical:" + identity["thesis_key"],))
            existing = self._existing(conn, identity)
            if existing:
                decision_id = str(existing["decision_id"])
                thesis_id = str(existing["thesis_id"])
                current_state = str(existing["state"])
                if requested_state not in _ALLOWED_TRANSITIONS.get(current_state, {current_state}):
                    self._append_event(
                        conn, decision_id=decision_id, thesis_id=thesis_id,
                        event_type="INVALID_TRANSITION", from_state=current_state, to_state=requested_state,
                        reason="invalid canonical state transition rejected", payload={"requested_state": requested_state},
                        occurred_at=stamp,
                    )
                    return self.get(decision_id, conn=conn)
                frozen = existing.get("frozen_evidence")
                frozen_hash = existing.get("frozen_evidence_hash")
                if frozen is None and requested_state in FREEZE_STATES:
                    frozen = CanonicalDecisionRepository._freeze_payload(source)
                    frozen_hash = _sha(frozen)
                active = requested_state in ACTIVE_STATES
                activated_at = existing.get("activated_at") or (stamp if requested_state in FREEZE_STATES else None)
                closed_at = existing.get("closed_at") or (stamp if requested_state in TERMINAL_STATES else None)
                with conn.cursor() as cur:
                    cur.execute(
                        """UPDATE trading.canonical_decisions SET
                               state=%s,decision_action=%s,publication_authority=%s,execution_authority=%s,
                               entry_plan=%s::jsonb,risk_plan=%s::jsonb,
                               frozen_evidence=%s::jsonb,frozen_evidence_hash=%s,
                               live_snapshot=%s::jsonb,confidence=%s::jsonb,data_lineage=%s::jsonb,
                               rejection_reasons=%s::jsonb,latest_payload=%s::jsonb,
                               model_version=%s,policy_version=%s,pipeline_version=%s,
                               record_version=record_version+1,active=%s,updated_at=%s,
                               activated_at=%s,closed_at=%s
                         WHERE decision_id=%s""",
                        (
                            requested_state, source.get("decision"), publication, execution,
                            _canonical(entry_plan), _canonical(risk_plan),
                            _canonical(frozen) if frozen is not None else None, frozen_hash,
                            _canonical(live), _canonical(confidence), _canonical(lineage),
                            _canonical(rejection_reasons), _canonical(latest_payload),
                            source.get("model_version") or source.get("ranking_version"), source.get("policy_version"),
                            source.get("decision_pipeline_version") or source.get("pipeline_version"),
                            active, stamp, activated_at, closed_at, decision_id,
                        ),
                    )
                event_type = "STATE_CHANGED" if requested_state != current_state else ("THESIS_REINFORCED" if identity["mode"] == "delivery" else "DECISION_REFRESHED")
                self._append_event(
                    conn, decision_id=decision_id, thesis_id=thesis_id, event_type=event_type,
                    from_state=current_state, to_state=requested_state,
                    reason=str(source.get("reason") or "canonical decision updated"),
                    payload={"publication_authority": publication, "execution_authority": execution, "live_snapshot": live},
                    occurred_at=stamp,
                )
            else:
                explicit = str(source.get("signal_id") or source.get("decision_id") or "").strip()
                decision_id = explicit or ("DEC-" + _sha({"thesis_key": identity["thesis_key"], "trading_date": identity["trading_date"]}, 28))
                signal_id = explicit or decision_id
                thesis_id = identity["thesis_id"]
                candidate_snapshot = CanonicalDecisionRepository._freeze_payload(source)
                frozen = candidate_snapshot if requested_state in FREEZE_STATES else None
                frozen_hash = _sha(frozen) if frozen is not None else None
                active = requested_state in ACTIVE_STATES
                with conn.cursor() as cur:
                    cur.execute(
                        """INSERT INTO trading.canonical_decisions(
                               decision_id,thesis_id,thesis_key,signal_id,symbol,exchange,mode,side,setup_family,
                               activation_window,trading_date,state,decision_action,publication_authority,execution_authority,
                               entry_plan,risk_plan,candidate_snapshot,frozen_evidence,frozen_evidence_hash,live_snapshot,
                               confidence,data_lineage,rejection_reasons,latest_payload,outcome,model_version,policy_version,
                               pipeline_version,record_version,active,created_at,updated_at,activated_at,closed_at,contract_version)
                           VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::date,%s,%s,%s,%s,
                                  %s::jsonb,%s::jsonb,%s::jsonb,%s::jsonb,%s,%s::jsonb,
                                  %s::jsonb,%s::jsonb,%s::jsonb,%s::jsonb,NULL,%s,%s,%s,1,%s,%s,%s,%s,%s,%s)""",
                        (
                            decision_id, thesis_id, identity["thesis_key"], signal_id, identity["symbol"],
                            str(source.get("exchange") or "NSE").upper(), identity["mode"], identity["side"],
                            identity["setup_family"], identity["activation_window"], identity["trading_date"], requested_state,
                            source.get("decision"), publication, execution, _canonical(entry_plan), _canonical(risk_plan),
                            _canonical(candidate_snapshot), _canonical(frozen) if frozen is not None else None, frozen_hash,
                            _canonical(live), _canonical(confidence), _canonical(lineage), _canonical(rejection_reasons),
                            _canonical(latest_payload), source.get("model_version") or source.get("ranking_version"),
                            source.get("policy_version"), source.get("decision_pipeline_version") or source.get("pipeline_version"),
                            active, stamp, stamp, stamp if requested_state in FREEZE_STATES else None,
                            stamp if requested_state in TERMINAL_STATES else None, CANONICAL_DECISION_VERSION,
                        ),
                    )
                self._append_event(
                    conn, decision_id=decision_id, thesis_id=thesis_id, event_type="DECISION_CREATED",
                    from_state=None, to_state=requested_state,
                    reason=str(source.get("reason") or "canonical decision created"),
                    payload={"thesis_key": identity["thesis_key"], "publication_authority": publication, "execution_authority": execution},
                    occurred_at=stamp,
                )
            row = self.get(decision_id, conn=conn)

        if self._event_fn is not None:
            try:
                self._event_fn("INFO", "canonical_decision", "Canonical PostgreSQL decision recorded", {
                    "decision_id": row.get("decision_id"), "symbol": row.get("symbol"), "mode": row.get("mode"), "state": row.get("state")
                })
            except Exception:
                pass
        return row

    def get(self, decision_id: str, *, conn: Any | None = None) -> Dict[str, Any]:
        if conn is None:
            row = self.read_authority.execute(
                "SELECT * FROM trading.canonical_decisions WHERE decision_id=%s", (str(decision_id),), fetch="one",
                statement_timeout_ms=1800,
            )
        else:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM trading.canonical_decisions WHERE decision_id=%s", (str(decision_id),))
                row = cur.fetchone()
        return self._decode(row)

    @staticmethod
    def _project_latest_payload(decoded: Mapping[str, Any]) -> Dict[str, Any]:
        row = dict(decoded or {})
        latest = dict(row.get("latest_payload") or {})
        entry = dict(row.get("entry_plan") or {})
        risk = dict(row.get("risk_plan") or {})
        live = dict(row.get("live_snapshot") or {})
        confidence = dict(row.get("confidence") or {})
        outcome = dict(row.get("outcome") or {})
        latest.update({
            "decision_id": row.get("decision_id"),
            "thesis_id": row.get("thesis_id"),
            "signal_id": row.get("signal_id"),
            "symbol": row.get("symbol"),
            "exchange": row.get("exchange"),
            "mode": row.get("mode"),
            "side": row.get("side"),
            "setup_family": row.get("setup_family"),
            "canonical_state": row.get("state"),
            "publication_authority": row.get("publication_authority"),
            "execution_authority": row.get("execution_authority"),
            "entry": entry.get("entry"),
            "t1": entry.get("target_1"),
            "t2": entry.get("target_2"),
            "sl": risk.get("stop"),
            "rr": risk.get("risk_reward"),
            "ltp": live.get("ltp"),
            # Points-only signal movement and rupee Model Paper economics are
            # distinct authorities. Never project points as net P&L.
            "pnl_points": outcome.get("pnl_points", outcome.get("pnl")),
            "net_pnl": outcome.get("net_pnl"),
            "gross_pnl": outcome.get("gross_pnl"),
            "quantity": outcome.get("quantity"),
            "settlement_id": outcome.get("settlement_id"),
            "position_id": outcome.get("position_id"),
            "exit": outcome.get("exit", outcome.get("exit_price")),
            "exit_price": outcome.get("exit_price", outcome.get("exit")),
            "result": outcome.get("result", outcome.get("status")),
            "outcome": outcome.get("result", outcome.get("status")),
            "outcome_payload": outcome,
            "costs": outcome.get("costs"),
            "charges": outcome.get("charges"),
            "confidence_value": confidence.get("value"),
            "trading_date": str(row.get("trading_date") or "")[:10],
            "created_at": row.get("created_at"),
            "updated_at": row.get("updated_at"),
            "last_update": row.get("updated_at"),
            "opened_at": row.get("activated_at") or row.get("created_at"),
            "closed_at": row.get("closed_at"),
            "active": row.get("active"),
            "record_version": row.get("record_version"),
            "frozen_evidence_hash": row.get("frozen_evidence_hash"),
            "rejection_reasons": list(row.get("rejection_reasons") or []),
        })
        return latest

    def events(self, decision_id: str) -> list[Dict[str, Any]]:
        rows = self.read_authority.execute(
            "SELECT * FROM trading.canonical_decision_events WHERE decision_id=%s ORDER BY event_id",
            (str(decision_id),), fetch="all", statement_timeout_ms=1800,
        )
        return [self._decode(row) for row in rows]

    def record_outcome(self, decision_or_signal_id: str, outcome: Mapping[str, Any]) -> Dict[str, Any]:
        key = str(decision_or_signal_id or "").strip()
        if not key:
            return {}
        payload = dict(outcome or {})
        stamp = str(payload.get("closed_at") or _now())
        terminal = "INVALIDATED" if str(payload.get("status") or "").upper() in {"CANCELLED", "EXPIRED", "INVALIDATED"} else "COMPLETED"
        with self.operational.transaction(isolation_level="serializable", statement_timeout_ms=4000) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM trading.canonical_decisions WHERE decision_id=%s OR signal_id=%s ORDER BY updated_at DESC LIMIT 1 FOR UPDATE",
                    (key, key),
                )
                row = cur.fetchone()
                if not row:
                    return {}
                current = dict(row)
                cur.execute(
                    "UPDATE trading.canonical_decisions SET state=%s,active=false,outcome=%s::jsonb,updated_at=%s,closed_at=COALESCE(closed_at,%s) WHERE decision_id=%s",
                    (terminal, _canonical(payload), stamp, stamp, current["decision_id"]),
                )
            self._append_event(
                conn, decision_id=str(current["decision_id"]), thesis_id=str(current["thesis_id"]),
                event_type="OUTCOME_RECORDED", from_state=str(current["state"]), to_state=terminal,
                reason=str(payload.get("result") or payload.get("status") or "outcome recorded"),
                payload=payload, occurred_at=stamp,
            )
            return self.get(str(current["decision_id"]), conn=conn)

    def lifecycle_rows(self, mode: str = "all", limit: int = 5000) -> list[Dict[str, Any]]:
        """Return the transactionally maintained narrow lifecycle projection.

        Canonical decisions remain the source of truth. Migration 027 updates this
        scalar projection in the same PostgreSQL transaction, avoiding repeated
        foreground JSONB extraction from wide decision rows.
        """
        params: list[Any] = []
        where = ""
        if str(mode or "all").lower() != "all":
            where = "WHERE mode=%s"
            params.append(require_production_mode(mode))
        params.append(max(1, min(int(limit), 5000)))
        rows = self.read_authority.execute(
            f"""SELECT
                    decision_id,thesis_id,signal_id,symbol,exchange,mode,side,setup_family,
                    canonical_state,publication_authority,execution_authority,
                    entry,target,t2,stop,rr,ltp,exit_price,net_pnl,gross_pnl,quantity,
                    settlement_id,position_id,signal_outcome,economic_outcome,result,costs,
                    created_at,updated_at,opened_at,closed_at,active,record_version,frozen_evidence_hash
               FROM trading.canonical_decision_lifecycle {where}
              ORDER BY updated_at DESC LIMIT %s""",
            tuple(params), fetch="all", statement_timeout_ms=1800,
        ) or []
        return [self._decode(row) for row in rows]

    def latest_decisions(self, mode: str = "all", limit: int = 50) -> list[Dict[str, Any]]:
        params: list[Any] = []
        where = ""
        if str(mode or "all").lower() != "all":
            where = "WHERE mode=%s"
            params.append(require_production_mode(mode))
        params.append(max(1, min(int(limit), 5000)))
        rows = self.read_authority.execute(
            f"SELECT * FROM trading.canonical_decisions {where} ORDER BY updated_at DESC LIMIT %s",
            tuple(params), fetch="all", statement_timeout_ms=1800,
        )
        return [self._project_latest_payload(self._decode(row)) for row in rows]

    def active_decisions(self, mode: str = "all", limit: int = 500) -> list[Dict[str, Any]]:
        """Return the one authoritative open signal/position-decision stream.

        Intraday is session-scoped; Delivery remains active across sessions.
        WATCHING research rows and non-publishable scanner observations are
        intentionally excluded so a scan is never counted as a trade.
        """
        params: list[Any] = []
        clauses = [
            "active",
            "publication_authority IN ('CAPITAL','MODEL_PAPER')",
            "state IN ('PREPARED','TRIGGERED','CONFIRMED','WEAKENING')",
        ]
        requested = str(mode or "all").lower()
        if requested != "all":
            canonical = require_production_mode(requested)
            clauses.append("mode=%s")
            params.append(canonical)
            if canonical == "intraday":
                clauses.append("trading_date=CURRENT_DATE")
        else:
            clauses.append("(mode='delivery' OR trading_date=CURRENT_DATE)")
        params.append(max(1, min(int(limit), 5000)))
        rows = self.read_authority.execute(
            "SELECT * FROM trading.canonical_decisions WHERE " + " AND ".join(clauses) +
            " ORDER BY CASE WHEN mode='delivery' THEN 0 ELSE 1 END,updated_at DESC LIMIT %s",
            tuple(params), fetch="all", statement_timeout_ms=1800,
        ) or []
        return [self._project_latest_payload(self._decode(row)) for row in rows]

    def today_entries(self, mode: str = "all", trading_date: Optional[str] = None, limit: int = 100) -> list[Dict[str, Any]]:
        day = str(trading_date or trading_date_ist())[:10]
        params: list[Any] = [day]
        mode_clause = ""
        if str(mode or "all").lower() != "all":
            mode_clause = " AND mode=%s"
            params.append(require_production_mode(mode))
        params.append(max(1, min(int(limit), 1000)))
        rows = self.read_authority.execute(
            """SELECT * FROM trading.canonical_decisions
                 WHERE trading_date=%s::date
                   AND publication_authority IN ('CAPITAL','MODEL_PAPER')
                   AND state IN ('PREPARED','TRIGGERED','CONFIRMED','WEAKENING')"""
            + mode_clause + " ORDER BY updated_at DESC LIMIT %s",
            tuple(params), fetch="all", statement_timeout_ms=1800,
        )
        out = []
        for raw in rows:
            decoded = self._decode(raw)
            latest = self._project_latest_payload(decoded)
            latest.update({
                "paper_only": decoded.get("publication_authority") == "MODEL_PAPER",
                "book": "MODEL_PAPER" if decoded.get("publication_authority") == "MODEL_PAPER" else "CAPITAL",
            })
            out.append(latest)
        return out
