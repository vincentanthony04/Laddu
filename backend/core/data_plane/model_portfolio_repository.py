from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import threading
from typing import Any, Iterator, Mapping, Sequence

from .postgres import PostgresAuthority
from core.final_excursion_attribution_authority import DEFAULT_FINAL_EXCURSION_ATTRIBUTION_AUTHORITY
from core.management_action_effectiveness_authority import DEFAULT_MANAGEMENT_ACTION_EFFECTIVENESS_AUTHORITY
from core.signal_age_authority import DEFAULT_SIGNAL_AGE_AUTHORITY


_ALLOWED_UPDATE_FIELDS = {
    "status", "last_price", "exit_price", "gross_pnl", "total_cost", "net_pnl",
    "hit_status", "action", "exit_reason", "economic_outcome", "signal_outcome",
    "closed_at", "updated_at", "managed_stop", "high_watermark", "low_watermark",
    "data_failure", "quantity", "notional", "reserved_cost",
    "current_managed_risk", "secured_profit", "managed_risk_state",
    "last_market_observation_at", "last_market_observation_sequence", "gap_recovery_state",
}


class ProductionModelPortfolioRepository:
    """PostgreSQL authority for the governed Model Paper lifecycle.

    No method falls back to SQLite. Research observations use the independent
    governance PostgreSQL service so candidate-volume cannot contend with live
    position/risk transactions.
    """

    def __init__(
        self, operational: PostgresAuthority, governance: PostgresAuthority,
        read_authority: PostgresAuthority | None = None,
    ):
        self.operational = operational
        self.governance = governance
        # Read-heavy HTTP/performance/lifecycle projections use their own bounded
        # pool.  Any read inside an admission/update transaction still uses the
        # transaction connection for serializable/row-lock consistency.
        self.read_authority = read_authority or operational
        self._local = threading.local()

    def _execute(
        self,
        authority: PostgresAuthority,
        sql: str,
        params: Sequence[Any] | Mapping[str, Any] | None = None,
        *,
        fetch: str = "none",
    ) -> Any:
        conn = getattr(self._local, "operational_conn", None) if authority is self.operational else None
        if conn is None:
            return authority.execute(sql, params, fetch=fetch)
        with conn.cursor() as cur:
            cur.execute(sql, params)
            if fetch == "one":
                return cur.fetchone()
            if fetch == "all":
                return cur.fetchall()
            return cur.rowcount

    def _read_execute(
        self, sql: str, params: Sequence[Any] | Mapping[str, Any] | None = None, *, fetch: str = "all",
    ) -> Any:
        conn = getattr(self._local, "operational_conn", None)
        if conn is not None:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                if fetch == "one":
                    return cur.fetchone()
                if fetch == "all":
                    return cur.fetchall()
                return cur.rowcount
        return self.read_authority.execute(sql, params, fetch=fetch, statement_timeout_ms=1800)

    @contextmanager
    def admission_guard(self) -> Iterator[None]:
        """Serialize shared-capital admission across processes.

        The advisory transaction lock and SERIALIZABLE isolation ensure that
        duplicate checks, open-capital reads, sizing and insertion observe one
        consistent operational snapshot. The lock is released by PostgreSQL on
        commit, rollback, process termination or connection loss.
        """
        existing = getattr(self._local, "operational_conn", None)
        if existing is not None:
            yield
            return
        with self.operational.transaction(
            isolation_level="serializable",
            lock_timeout_ms=1000,
            statement_timeout_ms=4000,
            idle_timeout_ms=5000,
        ) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", ("project_laddu_model_paper_admission",))
            self._local.operational_conn = conn
            try:
                yield
            finally:
                self._local.operational_conn = None


    @contextmanager
    def _operational_write(self) -> Iterator[None]:
        existing = getattr(self._local, "operational_conn", None)
        if existing is not None:
            yield
            return
        with self.operational.transaction(
            lock_timeout_ms=1000,
            statement_timeout_ms=4000,
            idle_timeout_ms=5000,
        ) as conn:
            self._local.operational_conn = conn
            try:
                yield
            finally:
                self._local.operational_conn = None

    @staticmethod
    def _event_key(position_id: str, event_type: str, updated_at: Any, payload: Mapping[str, Any]) -> str:
        raw = json.dumps(
            {"position_id": position_id, "event_type": event_type, "updated_at": str(updated_at), "payload": dict(payload)},
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _append_outbox(self, *, position_id: str, event_type: str, updated_at: Any, payload: Mapping[str, Any]) -> None:
        event_key = self._event_key(position_id, event_type, updated_at, payload)
        self._execute(
            self.operational,
            """INSERT INTO integration.transactional_outbox(
                   event_key,aggregate_type,aggregate_id,event_type,payload,occurred_at)
               VALUES(%s,'model_paper_position',%s,%s,%s::jsonb,%s)
               ON CONFLICT(event_key) DO NOTHING""",
            (
                event_key,
                position_id,
                event_type,
                json.dumps(dict(payload), sort_keys=True, default=str),
                updated_at or datetime.now(timezone.utc),
            ),
        )

    @staticmethod
    def _normalise(row: Mapping[str, Any] | None) -> dict[str, Any] | None:
        if row is None:
            return None
        out = dict(row)
        if "payload" in out:
            payload = out.pop("payload", None)
            out["payload_json"] = json.dumps(payload or {}, sort_keys=True, default=str)
        if "data_failure" in out:
            out["data_failure"] = 1 if out["data_failure"] else 0
        for key, value in list(out.items()):
            if isinstance(value, datetime):
                out[key] = value.isoformat().replace("+00:00", "Z")
        return out

    def research_observation(self, record: Mapping[str, Any]) -> None:
        payload = dict(record)
        self._execute(self.governance, 
            """INSERT INTO research.model_paper_observations(
                   observation_id,source_signal_id,symbol,mode,disposition,observed_price,occurred_at,payload)
               VALUES(%(observation_id)s,%(source_signal_id)s,%(symbol)s,%(mode)s,%(disposition)s,
                      %(observed_price)s,%(occurred_at)s,%(payload)s::jsonb)
               ON CONFLICT(observation_id) DO UPDATE SET
                   observed_price=EXCLUDED.observed_price,
                   occurred_at=EXCLUDED.occurred_at,
                   payload=EXCLUDED.payload""",
            {
                **payload,
                "payload": json.dumps(payload.get("payload") or {}, sort_keys=True, default=str),
            },
        )

    def find_by_signal(self, signal_id: str) -> dict[str, Any] | None:
        row = self._read_execute(
            "SELECT * FROM trading.model_paper_positions WHERE source_signal_id=%s LIMIT 1",
            (signal_id,), fetch="one",
        )
        return self._normalise(row)

    def research_final_lineage(
        self, *, decision_ids: Sequence[str] = (), signal_ids: Sequence[str] = (),
    ) -> dict[str, Any]:
        """Batch immutable Research origin IDs into canonical Final lineage.

        No symbol/time matching is permitted.  Missing IDs remain unlinked so
        historical Research cannot be optimistically labelled as promoted or
        admitted merely because the same symbol traded later.
        """
        decisions = sorted({str(value).strip() for value in decision_ids if str(value).strip()})
        signals = sorted({str(value).strip() for value in signal_ids if str(value).strip()})
        refs = sorted(set(decisions) | set(signals))
        if not refs:
            return {"decisions": [], "positions": [], "authority": "OPERATIONAL_POSTGRESQL_EXACT_ID_ONLY"}
        decision_rows = self._read_execute(
            """SELECT decision_id,signal_id,state,decision_action,publication_authority,
                      execution_authority,rejection_reasons,model_version,policy_version,updated_at
                 FROM trading.canonical_decisions
                WHERE decision_id=ANY(%s) OR signal_id=ANY(%s)""",
            (decisions or ["__NONE__"], signals or ["__NONE__"]), fetch="all",
        ) or []
        position_rows = self._read_execute(
            """SELECT position_id,source_signal_id,decision_id,status,opened_at,closed_at,
                      signal_outcome,economic_outcome,net_pnl,model_version,policy_version
                 FROM trading.model_paper_positions
                WHERE source_signal_id=ANY(%s) OR decision_id=ANY(%s)""",
            (refs, decisions or ["__NONE__"]), fetch="all",
        ) or []
        return {
            "decisions": [self._normalise(row) or {} for row in decision_rows],
            "positions": [self._normalise(row) or {} for row in position_rows],
            "authority": "OPERATIONAL_POSTGRESQL_EXACT_ID_ONLY",
        }

    def settled_final_economics(
        self, *, closed_since: datetime | None = None, mode: str | None = None,
        position_ids: Sequence[str] | None = None, limit: int = 100000,
    ) -> list[dict[str, Any]]:
        """Canonical realized Final economics for AC-070.

        ``position_ids=None`` means the complete canonical Final book for the
        requested period/desk.  An explicit empty sequence means no linked
        Research positions and therefore returns an empty result.  This avoids
        the dangerous empty-list-as-no-filter ambiguity.
        """
        if position_ids is not None:
            ids = sorted({str(value).strip() for value in position_ids if str(value).strip()})
            if not ids:
                return []
        else:
            ids = None
        clauses = ["p.status='CLOSED'", "p.closed_at IS NOT NULL", "p.net_pnl IS NOT NULL"]
        params: list[Any] = []
        if closed_since is not None:
            clauses.append("p.closed_at >= %s")
            params.append(closed_since)
        if mode:
            clauses.append("p.mode=%s")
            params.append(str(mode).lower())
        if ids is not None:
            clauses.append("p.position_id=ANY(%s)")
            params.append(ids)
        params.append(max(1, min(int(limit), 100000)))
        rows = self._read_execute(
            f"""SELECT p.position_id,p.source_signal_id,p.decision_id,p.symbol,p.exchange,p.mode,p.side,p.status,
                       p.quantity,p.original_entry,p.original_stop,p.entry_price,p.exit_price,p.high_watermark,p.low_watermark,
                       p.gross_pnl,p.total_cost,p.net_pnl,p.exit_reason,p.economic_outcome,p.signal_outcome,
                       p.opened_at,p.closed_at,p.updated_at,p.model_version,p.policy_version,p.feature_manifest_hash
                  FROM trading.model_paper_positions p
                 WHERE {' AND '.join(clauses)}
                 ORDER BY p.closed_at ASC,p.position_id ASC
                 LIMIT %s""",
            tuple(params), fetch="all",
        ) or []
        out: list[dict[str, Any]] = []
        for raw in rows:
            item = self._normalise(raw) or {}
            item = DEFAULT_SIGNAL_AGE_AUTHORITY.enrich(item, at=item.get("closed_at") or item.get("updated_at"))
            out.append(DEFAULT_FINAL_EXCURSION_ATTRIBUTION_AUTHORITY.enrich(item))
        return out

    def find_open_by_symbol(self, symbol: str) -> dict[str, Any] | None:
        row = self._read_execute(
            "SELECT * FROM trading.model_paper_positions WHERE status='OPEN' AND symbol=%s LIMIT 1",
            (symbol,), fetch="one",
        )
        return self._normalise(row)

    def latest_by_symbol(self, symbol: str) -> dict[str, Any] | None:
        row = self._read_execute(
            "SELECT * FROM trading.model_paper_positions WHERE symbol=%s ORDER BY updated_at DESC LIMIT 1",
            (symbol,), fetch="one",
        )
        return self._normalise(row)

    def operator_stop(self) -> bool:
        row = self._read_execute(
            "SELECT operator_stop FROM risk.control_state WHERE singleton_id=1", fetch="one"
        )
        if not row:
            raise RuntimeError("RISK_CONTROL_STATE_MISSING")
        return bool(row["operator_stop"])

    def insert_position(self, record: Mapping[str, Any]) -> bool:
        original = dict(record)
        row = dict(original)
        payload_obj = self._payload_mapping(row.get("payload"))
        row.setdefault("decision_id", payload_obj.get("decision_id"))
        row.setdefault("instrument_key", payload_obj.get("instrument_key") or payload_obj.get("provider_instrument_key"))
        row.setdefault("generated_at", payload_obj.get("generated_at") or payload_obj.get("decision_generated_at") or payload_obj.get("created_at"))
        row.setdefault("model_version", payload_obj.get("model_version"))
        row.setdefault("policy_version", payload_obj.get("policy_version") or payload_obj.get("model_policy"))
        row.setdefault("evidence_snapshot_id", payload_obj.get("evidence_snapshot_id") or payload_obj.get("canonical_snapshot_id"))
        row.setdefault("evidence_hash", payload_obj.get("evidence_hash") or payload_obj.get("evidence_snapshot_hash") or payload_obj.get("canonical_snapshot_hash"))
        row.setdefault("feature_manifest_hash", payload_obj.get("feature_manifest_hash"))
        age = DEFAULT_SIGNAL_AGE_AUTHORITY.measure(
            generated_at=row.get("generated_at"),
            opened_at=row.get("opened_at"),
            at=row.get("opened_at"),
            mode=row.get("mode"),
        )
        row.setdefault("decision_delay_seconds", age.get("decision_delay_seconds"))
        row.setdefault("last_market_observation_at", row.get("opened_at"))
        row.setdefault("last_market_observation_sequence", None)
        row.setdefault("gap_recovery_state", "OPENED")
        row.setdefault("current_managed_risk", row.get("open_risk"))
        row.setdefault("secured_profit", 0.0)
        row.setdefault("managed_risk_state", "ORIGINAL_RISK")
        execution_model = row.get("execution_model") if isinstance(row.get("execution_model"), Mapping) else payload_obj.get("execution_model")
        execution_model = dict(execution_model or {}) if isinstance(execution_model, Mapping) else {}
        row.setdefault("execution_model_version", execution_model.get("execution_model_version"))
        row.setdefault("execution_model_contract_hash", execution_model.get("contract_hash"))
        row.setdefault("execution_calibration_state", execution_model.get("calibration_state"))
        row.setdefault("execution_calibration_snapshot_hash", execution_model.get("calibration_snapshot_hash"))
        row["execution_model"] = json.dumps(execution_model, sort_keys=True, default=str) if execution_model else None
        row["payload"] = json.dumps(payload_obj, sort_keys=True, default=str)
        with self._operational_write():
            count = self._execute(
                self.operational,
                """INSERT INTO trading.model_paper_positions(
                       position_id,source_signal_id,decision_id,symbol,instrument_key,exchange,bse_group,mode,side,status,quantity,
                       original_entry,original_target,original_stop,managed_stop,entry_price,last_price,
                       notional,reserved_cost,open_risk,current_managed_risk,secured_profit,managed_risk_state,high_watermark,low_watermark,hit_status,action,
                       generated_at,opened_at,updated_at,cost_version,model_version,policy_version,
                       evidence_snapshot_id,evidence_hash,feature_manifest_hash,decision_delay_seconds,
                       last_market_observation_at,last_market_observation_sequence,gap_recovery_state,
                       execution_model_version,execution_model_contract_hash,execution_calibration_state,execution_calibration_snapshot_hash,execution_model,payload)
                   VALUES(%(position_id)s,%(source_signal_id)s,%(decision_id)s,%(symbol)s,%(instrument_key)s,%(exchange)s,%(bse_group)s,%(mode)s,%(side)s,
                       %(status)s,%(quantity)s,%(original_entry)s,%(original_target)s,%(original_stop)s,
                       %(managed_stop)s,%(entry_price)s,%(last_price)s,%(notional)s,%(reserved_cost)s,
                       %(open_risk)s,%(current_managed_risk)s,%(secured_profit)s,%(managed_risk_state)s,%(high_watermark)s,%(low_watermark)s,%(hit_status)s,%(action)s,
                       %(generated_at)s,%(opened_at)s,%(updated_at)s,%(cost_version)s,%(model_version)s,%(policy_version)s,
                       %(evidence_snapshot_id)s,%(evidence_hash)s,%(feature_manifest_hash)s,%(decision_delay_seconds)s,
                       %(last_market_observation_at)s,%(last_market_observation_sequence)s,%(gap_recovery_state)s,
                       %(execution_model_version)s,%(execution_model_contract_hash)s,%(execution_calibration_state)s,%(execution_calibration_snapshot_hash)s,%(execution_model)s::jsonb,%(payload)s::jsonb)
                   ON CONFLICT DO NOTHING""",
                row,
            )
            if count == 1:
                self._append_outbox(
                    position_id=str(row["position_id"]),
                    event_type="POSITION_INSERTED",
                    updated_at=row.get("updated_at"),
                    payload=original,
                )
        return count == 1

    def update_position(
        self,
        position_id: str,
        fields: Mapping[str, Any],
        *,
        expected_row_version: int | None = None,
    ) -> bool:
        updates = {key: value for key, value in dict(fields).items() if key in _ALLOWED_UPDATE_FIELDS}
        if not updates:
            return False
        assignments = ",".join(f"{key}=%({key})s" for key in updates)
        with self._operational_write():
            current = self._execute(
                self.operational,
                "SELECT * FROM trading.model_paper_positions WHERE position_id=%s FOR UPDATE",
                (position_id,),
                fetch="one",
            )
            if not current:
                return False
            actual_version = int(current.get("row_version") or 0)
            if expected_row_version is not None and actual_version != int(expected_row_version):
                raise RuntimeError(
                    f"POSITION_VERSION_CONFLICT:{position_id}:expected={int(expected_row_version)}:actual={actual_version}"
                )
            params = {
                **updates,
                "position_id": position_id,
                "expected_row_version": actual_version,
            }
            count = self._execute(
                self.operational,
                f"UPDATE trading.model_paper_positions SET {assignments}, row_version=row_version+1 "
                "WHERE position_id=%(position_id)s AND row_version=%(expected_row_version)s",
                params,
            )
            if count != 1:
                raise RuntimeError(f"POSITION_CONCURRENT_UPDATE:{position_id}")
            refreshed = self._execute(
                self.operational,
                "SELECT * FROM trading.model_paper_positions WHERE position_id=%s",
                (position_id,),
                fetch="one",
            ) or {}
            self._append_outbox(
                position_id=position_id,
                event_type="POSITION_UPDATED",
                updated_at=updates.get("updated_at") or refreshed.get("updated_at"),
                payload=dict(refreshed),
            )
        return True

    def list_positions(self, status: str | None = None) -> list[dict[str, Any]]:
        if status:
            rows = self._read_execute(
                "SELECT * FROM trading.model_paper_positions WHERE status=%s ORDER BY opened_at DESC",
                (status,), fetch="all",
            )
        else:
            rows = self._read_execute(
                "SELECT * FROM trading.model_paper_positions ORDER BY opened_at DESC", fetch="all"
            )
        return [self._normalise(row) or {} for row in rows]

    def current_lifecycle_attribution(self, source_signal_ids: Sequence[str]) -> dict[str, dict[str, Any]]:
        """Return current Model-Paper lifecycle attribution by exact source signal ID.

        This is a read-model join only.  It never matches by symbol/time and never
        reconstructs thesis state from price.  The position row remains the current
        management truth; REASSESSED/MANAGED details come only from the append-only
        PostgreSQL lifecycle ledger.
        """
        signal_ids = sorted({str(value).strip() for value in (source_signal_ids or ()) if str(value).strip()})
        if not signal_ids:
            return {}
        rows = self._read_execute(
            """SELECT p.position_id,p.source_signal_id,p.decision_id,p.status,p.action AS position_action,
                      p.hit_status,p.opened_at,p.updated_at,p.closed_at,
                      r.thesis_state AS current_thesis_state,r.occurred_at AS latest_reassessment_at,
                      r.authority_version AS reassessment_authority_version,
                      r.payload#>'{thesis_reassessment,reasons}' AS reassessment_reasons,
                      r.payload#>>'{thesis_reassessment,validation_scope}' AS reassessment_validation_scope,
                      r.payload#>>'{thesis_reassessment,authority_version}' AS reassessment_policy_version,
                      r.evidence_hash AS reassessment_evidence_hash,
                      m.occurred_at AS latest_management_at,
                      COALESCE(m.payload->>'action',p.action) AS latest_management_action,
                      COALESCE(m.payload#>'{thesis_reassessment,reasons}',r.payload#>'{thesis_reassessment,reasons}') AS management_reasons,
                      m.payload->>'hit_status' AS latest_management_hit_status,
                      m.authority_version AS management_authority_version
                 FROM trading.model_paper_positions p
                 LEFT JOIN LATERAL (
                     SELECT e.* FROM trading.signal_lifecycle_events e
                      WHERE e.position_id=p.position_id AND e.event_type='REASSESSED'
                      ORDER BY e.occurred_at DESC,e.event_id DESC LIMIT 1
                 ) r ON true
                 LEFT JOIN LATERAL (
                     SELECT e.* FROM trading.signal_lifecycle_events e
                      WHERE e.position_id=p.position_id AND e.event_type='MANAGED'
                      ORDER BY e.occurred_at DESC,e.event_id DESC LIMIT 1
                 ) m ON true
                WHERE p.source_signal_id=ANY(%s)
                ORDER BY p.updated_at DESC,p.position_id""",
            (signal_ids,), fetch="all",
        ) or []
        out: dict[str, dict[str, Any]] = {}
        for raw in rows:
            item = dict(raw)
            signal_id = str(item.get("source_signal_id") or "").strip()
            if not signal_id or signal_id in out:
                continue
            for key in ("reassessment_reasons", "management_reasons"):
                value = item.get(key)
                if isinstance(value, str):
                    try:
                        parsed = json.loads(value)
                        item[key] = parsed if isinstance(parsed, list) else []
                    except Exception:
                        item[key] = []
                elif not isinstance(value, list):
                    item[key] = []
            for key, value in list(item.items()):
                if isinstance(value, datetime):
                    item[key] = value.isoformat().replace("+00:00", "Z")
            out[signal_id] = item
        return out

    def list_open_ordered(self, mode: str | None = None) -> list[dict[str, Any]]:
        if mode:
            rows = self._read_execute(
                "SELECT * FROM trading.model_paper_positions WHERE status='OPEN' AND mode=%s ORDER BY opened_at",
                (str(mode).lower(),), fetch="all",
            )
        else:
            rows = self._read_execute(
                "SELECT * FROM trading.model_paper_positions WHERE status='OPEN' ORDER BY opened_at",
                fetch="all",
            )
        return [self._normalise(row) or {} for row in rows]

    def settlement_lineage_candidates(self, limit: int = 100) -> list[dict[str, Any]]:
        """Closed positions whose canonical DecisionRecord projection is absent.

        This is the only recovery input for execution outcomes.  It deliberately
        does not inspect candles, quotes or legacy signal-ledger tables.
        """
        rows = self._read_execute(
            """SELECT p.*
                 FROM trading.model_paper_positions p
                WHERE p.status='CLOSED'
                  AND NOT EXISTS (
                      SELECT 1
                        FROM trading.canonical_decisions c
                       WHERE (c.decision_id=p.source_signal_id OR c.signal_id=p.source_signal_id)
                         AND COALESCE(c.outcome->>'settlement_id','')=p.position_id
                  )
                ORDER BY p.updated_at ASC
                LIMIT %s""",
            (max(1, min(int(limit), 500)),), fetch="all",
        ) or []
        return [self._normalise(row) or {} for row in rows]


    @staticmethod
    def _lifecycle_event_key(record: Mapping[str, Any]) -> str:
        event_type = str(record.get("event_type") or "").upper()
        occurred = record.get("occurred_at")
        try:
            parsed = occurred if isinstance(occurred, datetime) else datetime.fromisoformat(str(occurred).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            seconds = int(parsed.timestamp())
            bucket_size = 300 if str(record.get("mode") or "").lower() == "intraday" else 1800
            payload = dict(record.get("payload") or {})
            # Material state/action transitions preserve their exact second.
            # Repeated unchanged observations are sampled into a desk-specific
            # bucket to bound ledger volume without erasing a real transition.
            material_transition = bool(payload.get("material_transition"))
            bucket = seconds if material_transition or event_type not in {"REASSESSED", "MANAGED"} else seconds // bucket_size
        except Exception:
            bucket = str(occurred)
        payload = dict(record.get("payload") or {})
        material = {
            "signal_id": str(record.get("signal_id") or ""),
            "position_id": str(record.get("position_id") or ""),
            "event_type": event_type,
            "bucket": bucket,
            "thesis_state": str(record.get("thesis_state") or ""),
            "action": str(payload.get("action") or payload.get("exit_reason") or ""),
        }
        return hashlib.sha256(json.dumps(material, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()

    def _append_lifecycle_intent(self, record: Mapping[str, Any]) -> None:
        event = dict(record or {})
        lifecycle_event_key = self._lifecycle_event_key(event)
        payload = {
            "lifecycle_event_key": lifecycle_event_key,
            "event": event,
            "replay_authority": "POSTGRESQL_TRANSACTIONAL_OUTBOX",
        }
        self._execute(
            self.operational,
            """INSERT INTO integration.transactional_outbox(
                   event_key,aggregate_type,aggregate_id,event_type,payload,occurred_at)
               VALUES(%s,'signal_lifecycle',%s,'SIGNAL_LIFECYCLE_INTENT',%s::jsonb,%s)
               ON CONFLICT(event_key) DO NOTHING""",
            (
                "signal-lifecycle-intent:" + lifecycle_event_key,
                str(event.get("signal_id") or event.get("position_id") or event.get("decision_id") or "unknown"),
                json.dumps(payload, sort_keys=True, default=str),
                event.get("occurred_at") or datetime.now(timezone.utc),
            ),
        )

    def update_position_with_lifecycle(
        self,
        position_id: str,
        fields: Mapping[str, Any],
        lifecycle_events: Sequence[Mapping[str, Any]],
        *,
        expected_row_version: int | None = None,
    ) -> bool:
        """Commit position truth and replayable lifecycle intent atomically.

        The lifecycle projection itself remains best-effort so stop/target/time
        exits can never be blocked by evidence-table trouble.  The exact intent
        is in the transactional outbox before commit and is therefore replayable.
        """
        with self._operational_write():
            changed = self.update_position(
                position_id, fields, expected_row_version=expected_row_version
            )
            if changed:
                for event in lifecycle_events or ():
                    self._append_lifecycle_intent(event)
            return changed

    def append_signal_lifecycle_event(self, record: Mapping[str, Any]) -> bool:
        row = dict(record or {})
        row["event_key"] = self._lifecycle_event_key(row)
        payload_obj = self._payload_mapping(row.get("payload"))
        age = self._payload_mapping(payload_obj.get("signal_age"))
        row.setdefault("generated_at", payload_obj.get("generated_at") or payload_obj.get("decision_generated_at"))
        row.setdefault("opened_at", payload_obj.get("opened_at"))
        measured_age = DEFAULT_SIGNAL_AGE_AUTHORITY.measure(
            generated_at=row.get("generated_at"), opened_at=row.get("opened_at"),
            at=row.get("occurred_at"), mode=row.get("mode") or payload_obj.get("mode"),
        )
        row.setdefault("model_version", payload_obj.get("model_version"))
        row.setdefault("policy_version", payload_obj.get("policy_version"))
        row.setdefault("evidence_hash", payload_obj.get("evidence_hash") or payload_obj.get("canonical_snapshot_hash"))
        row.setdefault("generation_age_seconds", age.get("generation_age_seconds", measured_age.get("generation_age_seconds")))
        row.setdefault("open_age_seconds", age.get("open_age_seconds", measured_age.get("open_age_seconds")))
        row.setdefault("decision_delay_seconds", age.get("decision_delay_seconds", measured_age.get("decision_delay_seconds")))
        row.setdefault("generation_age_bucket", age.get("generation_age_bucket", measured_age.get("generation_age_bucket")))
        row.setdefault("open_age_bucket", age.get("open_age_bucket", measured_age.get("open_age_bucket")))
        row.setdefault("decision_delay_bucket", age.get("decision_delay_bucket", measured_age.get("decision_delay_bucket")))
        row.setdefault("age_attribution_state", age.get("age_attribution_state", measured_age.get("age_attribution_state")))
        row.setdefault("age_bucket_policy_version", age.get("age_bucket_policy_version", measured_age.get("age_bucket_policy_version")))
        row["payload"] = json.dumps(payload_obj, sort_keys=True, default=str)
        count = self._execute(
            self.operational,
            """INSERT INTO trading.signal_lifecycle_events(
                   event_key,signal_id,position_id,decision_id,event_type,thesis_state,occurred_at,authority_version,
                   generated_at,opened_at,model_version,policy_version,evidence_hash,generation_age_seconds,open_age_seconds,
                   decision_delay_seconds,generation_age_bucket,open_age_bucket,decision_delay_bucket,age_attribution_state,age_bucket_policy_version,payload)
               VALUES(%(event_key)s,%(signal_id)s,%(position_id)s,%(decision_id)s,%(event_type)s,%(thesis_state)s,
                      %(occurred_at)s,'level5-signal-lifecycle-1.2.0',%(generated_at)s,%(opened_at)s,%(model_version)s,%(policy_version)s,
                      %(evidence_hash)s,%(generation_age_seconds)s,%(open_age_seconds)s,%(decision_delay_seconds)s,
                      %(generation_age_bucket)s,%(open_age_bucket)s,%(decision_delay_bucket)s,%(age_attribution_state)s,%(age_bucket_policy_version)s,%(payload)s::jsonb)
               ON CONFLICT(event_key) DO NOTHING""",
            row,
        )
        return count == 1

    @staticmethod
    def _payload_mapping(value: Any) -> dict[str, Any]:
        if isinstance(value, Mapping):
            return dict(value)
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
                return dict(parsed) if isinstance(parsed, Mapping) else {}
            except Exception:
                return {}
        return {}

    def lifecycle_replay_candidates(self, limit: int = 200) -> list[dict[str, Any]]:
        """Return exact missed intents plus reconstructable baseline events.

        REASSESSED/MANAGED must come from the transactional intent written with
        the position mutation. GENERATED/OPENED/SETTLED can additionally be
        reconstructed from their durable canonical/position authorities.
        """
        cap = max(1, min(int(limit), 500))
        events: list[dict[str, Any]] = []
        intents = self._read_execute(
            """SELECT o.payload
                 FROM integration.transactional_outbox o
                WHERE o.event_type='SIGNAL_LIFECYCLE_INTENT'
                  AND NOT EXISTS (
                      SELECT 1 FROM trading.signal_lifecycle_events e
                       WHERE e.event_key=COALESCE(o.payload->>'lifecycle_event_key','')
                  )
                ORDER BY o.outbox_id ASC
                LIMIT %s""",
            (cap,), fetch="all",
        ) or []
        for raw in intents:
            envelope = self._payload_mapping(dict(raw).get("payload"))
            event = self._payload_mapping(envelope.get("event"))
            if event:
                events.append(event)
        remaining = cap - len(events)
        if remaining <= 0:
            return events

        generated = self._read_execute(
            """SELECT c.signal_id,c.decision_id,c.symbol,c.mode,c.side,c.created_at,c.model_version,
                      c.policy_version,c.pipeline_version,c.latest_payload
                 FROM trading.canonical_decisions c
                WHERE NOT EXISTS (
                      SELECT 1 FROM trading.signal_lifecycle_events e
                       WHERE e.signal_id=c.signal_id AND e.event_type='GENERATED'
                )
                ORDER BY c.created_at ASC LIMIT %s""",
            (remaining,), fetch="all",
        ) or []
        for raw in generated:
            row = dict(raw)
            payload = self._payload_mapping(row.get("latest_payload"))
            events.append({
                "signal_id": row.get("signal_id"), "decision_id": row.get("decision_id"),
                "position_id": None, "event_type": "GENERATED", "thesis_state": "VALID",
                "occurred_at": row.get("created_at"), "mode": row.get("mode"),
                "payload": {
                    "symbol": row.get("symbol"), "mode": row.get("mode"), "side": row.get("side"),
                    "generated_at": payload.get("generated_at") or row.get("created_at"),
                    "model_version": row.get("model_version"), "policy_version": row.get("policy_version"),
                    "pipeline_version": row.get("pipeline_version"),
                    "reconstructed_from": "trading.canonical_decisions",
                    "material_transition": True,
                },
            })
            if len(events) >= cap:
                return events

        remaining = cap - len(events)
        positions = self._read_execute(
            """SELECT p.* FROM trading.model_paper_positions p
                WHERE NOT EXISTS (
                      SELECT 1 FROM trading.signal_lifecycle_events e
                       WHERE e.position_id=p.position_id AND e.event_type='OPENED'
                )
                ORDER BY p.opened_at ASC LIMIT %s""",
            (remaining,), fetch="all",
        ) or []
        for raw in positions:
            row = dict(raw)
            events.append({
                "signal_id": row.get("source_signal_id"), "decision_id": row.get("decision_id") or row.get("source_signal_id"),
                "position_id": row.get("position_id"), "event_type": "OPENED", "thesis_state": "VALID",
                "occurred_at": row.get("opened_at"), "mode": row.get("mode"),
                "payload": {
                    "symbol": row.get("symbol"), "mode": row.get("mode"), "side": row.get("side"),
                    "entry_price": row.get("entry_price"), "target": row.get("original_target"),
                    "stop": row.get("original_stop"), "quantity": row.get("quantity"),
                    "generated_at": row.get("generated_at"), "opened_at": row.get("opened_at"),
                    "model_version": row.get("model_version"), "policy_version": row.get("policy_version"),
                    "evidence_hash": row.get("evidence_hash"),
                    "reconstructed_from": "trading.model_paper_positions",
                    "material_transition": True,
                },
            })
            if len(events) >= cap:
                return events

        remaining = cap - len(events)
        closed = self._read_execute(
            """SELECT p.* FROM trading.model_paper_positions p
                WHERE p.status='CLOSED'
                  AND NOT EXISTS (
                      SELECT 1 FROM trading.signal_lifecycle_events e
                       WHERE e.position_id=p.position_id AND e.event_type='SETTLED'
                  )
                ORDER BY p.closed_at ASC NULLS LAST,p.updated_at ASC LIMIT %s""",
            (remaining,), fetch="all",
        ) or []
        for raw in closed:
            row = dict(raw)
            events.append({
                "signal_id": row.get("source_signal_id"), "decision_id": row.get("decision_id") or row.get("source_signal_id"),
                "position_id": row.get("position_id"), "event_type": "SETTLED",
                "thesis_state": "INVALIDATED" if str(row.get("signal_outcome") or "").upper() == "FAILURE" else None,
                "occurred_at": row.get("closed_at") or row.get("updated_at"), "mode": row.get("mode"),
                "payload": {
                    "symbol": row.get("symbol"), "exit_price": row.get("exit_price"),
                    "exit_reason": row.get("exit_reason"), "gross_pnl": row.get("gross_pnl"),
                    "total_cost": row.get("total_cost"), "net_pnl": row.get("net_pnl"),
                    "economic_outcome": row.get("economic_outcome"), "signal_outcome": row.get("signal_outcome"),
                    "generated_at": row.get("generated_at"), "opened_at": row.get("opened_at"),
                    "model_version": row.get("model_version"), "policy_version": row.get("policy_version"),
                    "evidence_hash": row.get("evidence_hash"),
                    "reconstructed_from": "trading.model_paper_positions",
                    "material_transition": True,
                },
            })
        return events[:cap]

    def signal_lifecycle_summary(self, limit: int = 50) -> dict[str, Any]:
        totals = self._read_execute(
            "SELECT event_type,COUNT(*) AS count FROM trading.signal_lifecycle_events GROUP BY event_type ORDER BY event_type",
            fetch="all",
        ) or []
        latest = self._read_execute(
            """SELECT event_key,signal_id,position_id,decision_id,event_type,thesis_state,occurred_at,authority_version,
                      generated_at,opened_at,model_version,policy_version,evidence_hash,generation_age_seconds,open_age_seconds,
                      decision_delay_seconds,generation_age_bucket,open_age_bucket,decision_delay_bucket,age_attribution_state,age_bucket_policy_version,payload
                 FROM trading.signal_lifecycle_events ORDER BY occurred_at DESC,event_id DESC LIMIT %s""",
            (max(1, min(int(limit), 500)),), fetch="all",
        ) or []
        normalised = []
        for raw in latest:
            item = dict(raw)
            for key, value in list(item.items()):
                if isinstance(value, datetime):
                    item[key] = value.isoformat().replace("+00:00", "Z")
            normalised.append(item)
        by_type = {str(row.get("event_type")): int(row.get("count") or 0) for row in totals}
        return {"total": sum(by_type.values()), "by_type": by_type, "latest": normalised, "authority": "POSTGRESQL_APPEND_ONLY_SIGNAL_LIFECYCLE"}

    def settled_learning_rows(self, limit: int = 10000) -> list[dict[str, Any]]:
        rows = self._read_execute(
            """SELECT p.position_id,p.source_signal_id,p.symbol,p.mode,p.side,p.quantity,p.entry_price,p.original_entry,
                      p.original_stop,p.original_target,p.managed_stop,p.high_watermark,p.low_watermark,p.exit_price,
                      p.gross_pnl,p.total_cost,p.net_pnl,p.exit_reason,p.economic_outcome,p.signal_outcome,p.hit_status,p.action,
                      p.generated_at,p.opened_at,p.closed_at,p.updated_at,p.decision_id AS position_decision_id,
                      p.model_version AS position_model_version,p.policy_version AS position_policy_version,
                      p.evidence_snapshot_id,p.evidence_hash,p.feature_manifest_hash,
                      c.decision_id,c.latest_payload,c.candidate_snapshot,c.model_version,c.policy_version,c.pipeline_version,
                      COALESCE(l.lifecycle_action_path,'[]'::jsonb) AS lifecycle_action_path
                 FROM trading.model_paper_positions p
                 LEFT JOIN LATERAL (
                     SELECT c.* FROM trading.canonical_decisions c
                      WHERE c.decision_id=p.source_signal_id OR c.signal_id=p.source_signal_id
                      ORDER BY c.updated_at DESC LIMIT 1
                 ) c ON true
                 LEFT JOIN LATERAL (
                     SELECT jsonb_agg(
                                jsonb_build_object(
                                    'event_type',e.event_type,
                                    'thesis_state',e.thesis_state,
                                    'occurred_at',e.occurred_at,
                                    'action',COALESCE(e.payload->>'action',e.payload->>'exit_reason'),
                                    'reason',COALESCE(e.payload#>>'{thesis_reassessment,reason}',e.payload->>'exit_reason'),
                                    'price',COALESCE(e.payload->>'price',e.payload->>'exit_price',e.payload->>'entry_price'),
                                    'managed_stop',e.payload->>'managed_stop',
                                    'hit_status',e.payload->>'hit_status',
                                    'signal_age',e.payload->'signal_age',
                                    'generation_age_seconds',e.generation_age_seconds,
                                    'open_age_seconds',e.open_age_seconds,
                                    'model_version',e.model_version,
                                    'policy_version',e.policy_version,
                                    'evidence_hash',e.evidence_hash,
                                    'full_thesis_validated',e.payload#>'{thesis_reassessment,full_thesis_validated}',
                                    'regime',COALESCE(
                                        e.payload#>>'{thesis_reassessment,current_thesis_evidence,domains,market_sector,market,direction}',
                                        e.payload#>>'{thesis_reassessment,current_thesis_evidence,domains,market_sector,sector,direction}'
                                    )
                                ) ORDER BY e.occurred_at,e.event_id
                            ) AS lifecycle_action_path
                       FROM trading.signal_lifecycle_events e
                      WHERE e.position_id=p.position_id
                        AND e.event_type IN ('OPENED','REASSESSED','MANAGED','SETTLED')
                 ) l ON true
                WHERE p.status='CLOSED' AND p.closed_at IS NOT NULL AND p.net_pnl IS NOT NULL
                ORDER BY p.closed_at ASC LIMIT %s""",
            (max(1, min(int(limit), 100000)),), fetch="all",
        ) or []
        out = []
        for raw in rows:
            item = dict(raw)
            path = item.get("lifecycle_action_path")
            if isinstance(path, str):
                try:
                    parsed = json.loads(path)
                    item["lifecycle_action_path"] = parsed if isinstance(parsed, list) else []
                except Exception:
                    item["lifecycle_action_path"] = []
            for key, value in list(item.items()):
                if isinstance(value, datetime):
                    item[key] = value.isoformat().replace("+00:00", "Z")
            item = DEFAULT_SIGNAL_AGE_AUTHORITY.enrich(item, at=item.get("closed_at") or item.get("updated_at"))
            item = DEFAULT_FINAL_EXCURSION_ATTRIBUTION_AUTHORITY.enrich(item)
            out.append(DEFAULT_MANAGEMENT_ACTION_EFFECTIVENESS_AUTHORITY.enrich(item))
        return out

    def append_learning_finding(self, record: Mapping[str, Any]) -> bool:
        row = dict(record or {})
        row["finding"] = json.dumps(dict(row.get("finding") or {}), sort_keys=True, default=str)
        count = self._execute(
            self.governance,
            """INSERT INTO research.learning_findings(
                   finding_id,finding_type,mode,evidence_hash,finding,authority_version)
               VALUES(%(finding_id)s,%(finding_type)s,%(mode)s,%(evidence_hash)s,%(finding)s::jsonb,%(authority_version)s)
               ON CONFLICT(finding_id) DO NOTHING""", row,
        )
        return count == 1

    def append_rule_change_proposal(self, record: Mapping[str, Any]) -> bool:
        row = dict(record or {})
        row["proposal"] = json.dumps(dict(row.get("proposal") or {}), sort_keys=True, default=str)
        count = self._execute(
            self.governance,
            """INSERT INTO research.rule_change_proposals(
                   proposal_id,finding_id,proposal_type,mode,proposal,evidence_hash,authority_version,
                   human_approval_required,approval_state,production_applied)
               VALUES(%(proposal_id)s,%(finding_id)s,%(proposal_type)s,%(mode)s,%(proposal)s::jsonb,%(evidence_hash)s,%(authority_version)s,
                      true,'PENDING',false) ON CONFLICT(proposal_id) DO NOTHING""", row,
        )
        return count == 1

    def research_rows(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self._execute(self.governance, 
            """SELECT observation_id AS research_id,source_signal_id,symbol,mode,disposition,
                      observed_price,occurred_at,payload::text AS payload_json
                 FROM research.model_paper_observations ORDER BY occurred_at DESC LIMIT %s""",
            (max(1, min(int(limit), 1000)),), fetch="all",
        )
        return [self._normalise(row) or {} for row in rows]
