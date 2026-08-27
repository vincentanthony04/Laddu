from __future__ import annotations

"""PostgreSQL-first forward-evidence checkpoints and legacy import."""

from datetime import datetime, timezone
import hashlib
import json
from typing import Any, Dict, Iterable, Mapping, Optional

from .postgres import PostgresAuthority
from .model_governance_repository import ProductionModelGovernanceRepository

SERVICE_VERSION = "forward-evidence-governance-2.0.0"
_REQUIRED_ARMS = ("heuristic", "quant", "hybrid")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _sha(value: Any) -> str:
    material = value if isinstance(value, str) else _canonical(value)
    return hashlib.sha256(material.encode("utf-8", errors="replace")).hexdigest()


def _row_dict(row: Any) -> Dict[str, Any]:
    try:
        return dict(row)
    except Exception:
        return {}


def _json_object(value: Any) -> Dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    try:
        parsed = json.loads(str(value or "{}"))
        return dict(parsed) if isinstance(parsed, Mapping) else {}
    except Exception:
        return {}


class ForwardEvidenceGovernanceRepository:
    """Append-only projection and checkpoint authority."""

    def __init__(self, authority: PostgresAuthority):
        self.authority = authority
        self.governance_repository = ProductionModelGovernanceRepository(authority)

    @staticmethod
    def _local_cursor(store: Any, key: str, default: Mapping[str, Any]) -> Dict[str, Any]:
        getter = getattr(store, "get_kv", None)
        if callable(getter):
            try:
                value = getter(key, None)
                if isinstance(value, Mapping):
                    return dict(value)
            except Exception:
                pass
        return dict(default)

    @staticmethod
    def _set_local_cursor(store: Any, key: str, value: Mapping[str, Any]) -> None:
        setter = getattr(store, "set_kv", None)
        if callable(setter):
            setter(key, dict(value))

    @staticmethod
    def _local_rows(store: Any, sql: str, params: Iterable[Any] = ()) -> list[Dict[str, Any]]:
        cursor = store.conn.execute(sql, tuple(params))
        return [_row_dict(row) for row in cursor.fetchall()]

    @staticmethod
    def _assert_existing_hash(cur: Any, table: str, key_column: str, key: str, expected: str) -> None:
        # Table and key column are fixed internal constants, never user input.
        cur.execute(f"SELECT payload_sha256 FROM {table} WHERE {key_column}=%s", (key,))
        row = cur.fetchone()
        if not row or str(row["payload_sha256"]) != expected:
            raise RuntimeError(f"FORWARD_EVIDENCE_IDEMPOTENCY_CONFLICT:{table}:{key}")

    def _sync_population_batch(self, store: Any, limit: int) -> Dict[str, Any]:
        key = "forward_evidence_governance:population_cursor"
        cursor = self._local_cursor(store, key, {"observed_at": "", "fingerprint": ""})
        rows = self._local_rows(
            store,
            """SELECT pop.* FROM candidate_populations pop
               WHERE (pop.observed_at>? OR (pop.observed_at=? AND pop.population_fingerprint>?))
                 AND (SELECT COUNT(*) FROM shadow_selector_predictions sp
                       WHERE sp.population_fingerprint=pop.population_fingerprint
                         AND sp.arm IN ('heuristic','quant','hybrid')) = pop.candidate_count * 3
                 AND (SELECT COUNT(DISTINCT sp.arm) FROM shadow_selector_predictions sp
                       WHERE sp.population_fingerprint=pop.population_fingerprint
                         AND sp.arm IN ('heuristic','quant','hybrid')) = 3
               ORDER BY pop.observed_at,pop.population_fingerprint LIMIT ?""",
            (cursor.get("observed_at") or "", cursor.get("observed_at") or "", cursor.get("fingerprint") or "", max(1, int(limit))),
        )
        if not rows:
            return {"seen": 0, "inserted": 0, "members": 0, "predictions": 0, "cursor": cursor}

        inserted = members_inserted = predictions_inserted = 0
        with self.authority.transaction(isolation_level="serializable", statement_timeout_ms=20000) as conn:
            with conn.cursor() as cur:
                for population in rows:
                    fingerprint = str(population["population_fingerprint"])
                    population_payload = {
                        "population_fingerprint": fingerprint,
                        "desk": str(population["mode"]).upper(),
                        "observed_at": population["observed_at"],
                        "universe_id": population["universe_id"],
                        "dataset_fingerprint": population["dataset_fingerprint"],
                        "feature_manifest_hash": population["feature_manifest_hash"],
                        "candidate_count": int(population["candidate_count"]),
                        "policy_version": population["policy_version"],
                    }
                    population_hash = _sha(population_payload)
                    cur.execute(
                        """INSERT INTO research.selector_populations(
                               population_fingerprint,desk,observed_at,universe_id,dataset_fingerprint,
                               feature_manifest_hash,candidate_count,policy_version,payload_sha256)
                           VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)
                           ON CONFLICT(population_fingerprint) DO NOTHING""",
                        (
                            fingerprint, population_payload["desk"], population_payload["observed_at"],
                            population_payload["universe_id"], population_payload["dataset_fingerprint"],
                            population_payload["feature_manifest_hash"], population_payload["candidate_count"],
                            population_payload["policy_version"], population_hash,
                        ),
                    )
                    inserted += max(0, int(cur.rowcount or 0))
                    self._assert_existing_hash(cur, "research.selector_populations", "population_fingerprint", fingerprint, population_hash)

                    members = self._local_rows(
                        store,
                        "SELECT * FROM candidate_population_observations WHERE population_fingerprint=? ORDER BY candidate_id",
                        (fingerprint,),
                    )
                    if len(members) != int(population["candidate_count"]):
                        raise RuntimeError(f"LOCAL_POPULATION_MEMBER_COUNT_MISMATCH:{fingerprint}")
                    for member in members:
                        features = _json_object(member.get("feature_json"))
                        side = str(member.get("side") or features.get("side") or "UNKNOWN").upper()
                        if side not in {"LONG", "SHORT"}:
                            side = "UNKNOWN"
                        member_payload = {
                            "candidate_id": member["candidate_id"],
                            "population_fingerprint": fingerprint,
                            "instrument_key": member["instrument_key"],
                            "symbol": str(member["symbol"]).upper(),
                            "exchange": str(member.get("exchange") or "NSE").upper(),
                            "desk": str(member["mode"]).upper(),
                            "side": side,
                            "observed_at": member["observed_at"],
                            "feature_hash": member["feature_hash"],
                            "feature_payload": features,
                        }
                        member_hash = _sha(member_payload)
                        cur.execute(
                            """INSERT INTO research.selector_population_members(
                                   candidate_id,population_fingerprint,instrument_key,symbol,exchange,desk,side,
                                   observed_at,feature_hash,feature_payload,payload_sha256)
                               VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s)
                               ON CONFLICT(candidate_id) DO NOTHING""",
                            (
                                member_payload["candidate_id"], fingerprint, member_payload["instrument_key"],
                                member_payload["symbol"], member_payload["exchange"], member_payload["desk"],
                                side, member_payload["observed_at"], member_payload["feature_hash"],
                                _canonical(features), member_hash,
                            ),
                        )
                        members_inserted += max(0, int(cur.rowcount or 0))
                        self._assert_existing_hash(cur, "research.selector_population_members", "candidate_id", str(member["candidate_id"]), member_hash)

                    predictions = self._local_rows(
                        store,
                        """SELECT * FROM shadow_selector_predictions
                           WHERE population_fingerprint=? AND arm IN ('heuristic','quant','hybrid')
                           ORDER BY candidate_id,arm""",
                        (fingerprint,),
                    )
                    expected_predictions = int(population["candidate_count"]) * len(_REQUIRED_ARMS)
                    if predictions and len(predictions) != expected_predictions:
                        # Incomplete populations stay local until all arms exist; do not
                        # create a misleading governance population with partial predictions.
                        raise RuntimeError(f"LOCAL_THREE_ARM_PREDICTION_COUNT_MISMATCH:{fingerprint}:{len(predictions)}:{expected_predictions}")
                    for prediction in predictions:
                        prediction_material, prediction_payload = (
                            ProductionModelGovernanceRepository._legacy_prediction_material_v1(
                                fingerprint, prediction, prediction_at=str(prediction["created_at"]),
                            )
                        )
                        prediction_key = str(prediction_material["prediction_key"])
                        expected_net = prediction_material["expected_net_return_bps"]
                        prediction_hash = _sha(prediction_material)
                        cur.execute(
                            """INSERT INTO research.selector_arm_predictions(
                                   prediction_key,population_fingerprint,candidate_id,arm,model_version,
                                   score,predicted_rank,predicted_percentile,probability_positive,
                                   expected_net_return_bps,prediction_at,prediction_payload,payload_sha256)
                               VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s)
                               ON CONFLICT(prediction_key) DO NOTHING""",
                            (
                                prediction_key, fingerprint, prediction["candidate_id"], prediction["arm"],
                                prediction["model_version"], prediction["score"], prediction["rank"],
                                prediction["percentile"], prediction.get("probability_positive"), expected_net,
                                prediction["created_at"], _canonical(prediction_payload), prediction_hash,
                            ),
                        )
                        predictions_inserted += max(0, int(cur.rowcount or 0))
                        self._assert_existing_hash(cur, "research.selector_arm_predictions", "prediction_key", prediction_key, prediction_hash)

        last = rows[-1]
        next_cursor = {"observed_at": str(last["observed_at"]), "fingerprint": str(last["population_fingerprint"]), "updated_at": _now()}
        self._set_local_cursor(store, key, next_cursor)
        return {
            "seen": len(rows), "inserted": inserted, "members": members_inserted,
            "predictions": predictions_inserted, "cursor": next_cursor,
        }

    def _sync_outcome_batch(self, store: Any, limit: int) -> Dict[str, Any]:
        key = "forward_evidence_governance:outcome_cursor"
        cursor = self._local_cursor(store, key, {"settled_at": "", "outcome_key": ""})
        population_cursor = self._local_cursor(
            store,
            "forward_evidence_governance:population_cursor",
            {"observed_at": "", "fingerprint": ""},
        )
        if not population_cursor.get("observed_at"):
            return {"seen": 0, "inserted": 0, "cursor": cursor, "state": "WAITING_FOR_POPULATION_SYNC"}
        rows = self._local_rows(
            store,
            """SELECT o.*,o.candidate_id || ':' || o.horizon AS outcome_key
                 FROM selector_candidate_outcomes o
                 JOIN candidate_populations pop ON pop.population_fingerprint=o.population_fingerprint
                WHERE (o.settled_at>? OR (o.settled_at=? AND (o.candidate_id || ':' || o.horizon)>?))
                  AND (pop.observed_at<? OR (pop.observed_at=? AND pop.population_fingerprint<=?))
                ORDER BY o.settled_at,outcome_key LIMIT ?""",
            (
                cursor.get("settled_at") or "", cursor.get("settled_at") or "", cursor.get("outcome_key") or "",
                population_cursor.get("observed_at") or "", population_cursor.get("observed_at") or "",
                population_cursor.get("fingerprint") or "", max(1, int(limit)),
            ),
        )
        if not rows:
            return {"seen": 0, "inserted": 0, "cursor": cursor}
        inserted = 0
        with self.authority.transaction(isolation_level="serializable", statement_timeout_ms=20000) as conn:
            with conn.cursor() as cur:
                for outcome in rows:
                    proof = _json_object(outcome.get("proof_json"))
                    material = {
                        "outcome_key": outcome["outcome_key"],
                        "candidate_id": outcome["candidate_id"],
                        "population_fingerprint": outcome["population_fingerprint"],
                        "horizon": outcome["horizon"],
                        "observed_at": outcome["observed_at"],
                        "settled_at": outcome["settled_at"],
                        "market_regime": outcome["market_regime"],
                        "result": outcome["result"],
                        "gross_return_bps": outcome.get("gross_return_bps"),
                        "net_return_bps": outcome["net_return_bps"],
                        "actual_cost_bps": outcome.get("actual_cost_bps"),
                        "same_bar_ambiguous": bool(outcome.get("same_bar_ambiguous")),
                        "proof_payload": proof,
                        "record_hash": outcome["record_hash"],
                    }
                    payload_hash = _sha(material)
                    cur.execute(
                        """INSERT INTO research.selector_outcomes(
                               outcome_key,candidate_id,population_fingerprint,horizon,observed_at,settled_at,
                               market_regime,result,gross_return_bps,net_return_bps,actual_cost_bps,
                               same_bar_ambiguous,proof_payload,record_hash,payload_sha256)
                           VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s)
                           ON CONFLICT(outcome_key) DO NOTHING""",
                        (
                            outcome["outcome_key"], outcome["candidate_id"], outcome["population_fingerprint"],
                            outcome["horizon"], outcome["observed_at"], outcome["settled_at"],
                            outcome["market_regime"], outcome["result"], outcome.get("gross_return_bps"),
                            outcome["net_return_bps"], outcome.get("actual_cost_bps"),
                            bool(outcome.get("same_bar_ambiguous")), _canonical(proof), outcome["record_hash"], payload_hash,
                        ),
                    )
                    inserted += max(0, int(cur.rowcount or 0))
                    self._assert_existing_hash(cur, "research.selector_outcomes", "outcome_key", str(outcome["outcome_key"]), payload_hash)
        last = rows[-1]
        next_cursor = {"settled_at": str(last["settled_at"]), "outcome_key": str(last["outcome_key"]), "updated_at": _now()}
        self._set_local_cursor(store, key, next_cursor)
        return {"seen": len(rows), "inserted": inserted, "cursor": next_cursor}

    @staticmethod
    def _merge_batches(batches: list[Mapping[str, Any]]) -> Dict[str, Any]:
        if not batches:
            return {"batches": 0, "seen": 0, "inserted": 0}
        numeric_keys = ("seen", "inserted", "members", "predictions")
        merged: Dict[str, Any] = {
            "batches": len(batches),
            **{key: sum(int(batch.get(key) or 0) for batch in batches) for key in numeric_keys},
            "cursor": dict(batches[-1].get("cursor") or {}),
        }
        states = [str(batch.get("state")) for batch in batches if batch.get("state")]
        if states:
            merged["states"] = states
        return merged

    def sync_from_local_store(
        self,
        store: Any,
        *,
        population_limit: int = 50,
        outcome_limit: int = 1000,
        max_batches: int = 100,
    ) -> Dict[str, Any]:
        if hasattr(self, "governance_repository"):
            migration = self.governance_repository.legacy_research_migration_status(store)
            verified = bool(
                migration.get("count_verified") is True
                and migration.get("hash_verified") is True
                and migration.get("quarantine_verified") is True
            )
            return {
                **migration,
                "version": SERVICE_VERSION,
                "fully_drained": verified,
                "production_influence": 0.0,
                "broker_authority": "NONE",
            }
        population_batches: list[Dict[str, Any]] = []
        population_drained = False
        for _ in range(max(1, int(max_batches))):
            batch = self._sync_population_batch(store, population_limit)
            population_batches.append(batch)
            if int(batch.get("seen") or 0) == 0:
                population_drained = True
                break

        outcome_batches: list[Dict[str, Any]] = []
        outcome_drained = False
        if population_drained:
            for _ in range(max(1, int(max_batches))):
                batch = self._sync_outcome_batch(store, outcome_limit)
                outcome_batches.append(batch)
                if int(batch.get("seen") or 0) == 0:
                    outcome_drained = True
                    break

        fully_drained = population_drained and outcome_drained
        return {
            "ok": True,
            "version": SERVICE_VERSION,
            "authority": "GOVERNANCE_POSTGRESQL",
            "population_sync": self._merge_batches(population_batches),
            "outcome_sync": self._merge_batches(outcome_batches),
            "fully_drained": fully_drained,
            "state": "SYNC_COMPLETE" if fully_drained else "SYNC_BATCH_LIMIT_REACHED",
            "production_influence": 0.0,
            "broker_authority": "NONE",
        }

    def record_checkpoint(self, payload: Mapping[str, Any], *, build_version: str, policy_version: str) -> Dict[str, Any]:
        canonical_payload = _canonical(dict(payload or {}))
        with self.authority.transaction(isolation_level="serializable", statement_timeout_ms=10000) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", ("forward-maturity-checkpoint",))
                cur.execute(
                    """SELECT checkpoint_hash FROM research.forward_maturity_checkpoints
                       ORDER BY created_at DESC,checkpoint_id DESC LIMIT 1"""
                )
                previous = cur.fetchone()
                previous_hash = str(previous["checkpoint_hash"]) if previous else "GENESIS"
                evidence_cutoff = str(payload.get("evaluated_at") or payload.get("evidence_cutoff_at") or _now())
                maturity_state = str(payload.get("state") or payload.get("maturity_state") or "UNPROVEN")
                checkpoint_hash = _sha({
                    "previous_checkpoint_hash": previous_hash,
                    "build_version": build_version,
                    "policy_version": policy_version,
                    "evidence_cutoff_at": evidence_cutoff,
                    "payload": json.loads(canonical_payload),
                })
                checkpoint_id = f"forward:{checkpoint_hash[:32]}"
                cur.execute(
                    """INSERT INTO research.forward_maturity_checkpoints(
                           checkpoint_id,previous_checkpoint_hash,checkpoint_hash,build_version,policy_version,
                           maturity_state,evidence_cutoff_at,checkpoint_payload)
                       VALUES(%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
                       ON CONFLICT(checkpoint_id) DO NOTHING""",
                    (
                        checkpoint_id, None if previous_hash == "GENESIS" else previous_hash, checkpoint_hash,
                        build_version, policy_version, maturity_state, evidence_cutoff, canonical_payload,
                    ),
                )
                cur.execute(
                    "SELECT checkpoint_hash FROM research.forward_maturity_checkpoints WHERE checkpoint_id=%s",
                    (checkpoint_id,),
                )
                stored = cur.fetchone()
                if not stored or str(stored["checkpoint_hash"]) != checkpoint_hash:
                    raise RuntimeError(f"FORWARD_CHECKPOINT_IDEMPOTENCY_CONFLICT:{checkpoint_id}")
        return {
            "ok": True,
            "checkpoint_id": checkpoint_id,
            "checkpoint_hash": checkpoint_hash,
            "previous_checkpoint_hash": previous_hash,
            "authority": "GOVERNANCE_POSTGRESQL",
            "immutable": True,
        }

    def latest_checkpoint(self) -> Optional[Dict[str, Any]]:
        row = self.authority.execute(
            """SELECT checkpoint_id,previous_checkpoint_hash,checkpoint_hash,build_version,policy_version,
                      maturity_state,evidence_cutoff_at,checkpoint_payload,created_at
                 FROM research.forward_maturity_checkpoints
                ORDER BY created_at DESC,checkpoint_id DESC LIMIT 1""",
            fetch="one",
            statement_timeout_ms=1500,
        )
        return dict(row) if row else None

    def status(self) -> Dict[str, Any]:
        counts = self.authority.execute(
            """SELECT
                 (SELECT count(*) FROM research.selector_populations)::bigint AS populations,
                 (SELECT count(*) FROM research.selector_population_members)::bigint AS candidates,
                 (SELECT count(*) FROM research.selector_arm_predictions)::bigint AS predictions,
                 (SELECT count(*) FROM research.selector_outcomes)::bigint AS outcomes,
                 (SELECT count(*) FROM research.forward_maturity_checkpoints)::bigint AS checkpoints""",
            fetch="one",
            statement_timeout_ms=2500,
        ) or {}
        by_desk = self.authority.execute(
            """SELECT p.desk,count(DISTINCT p.population_fingerprint)::bigint AS populations,
                      count(DISTINCT m.candidate_id)::bigint AS candidates,
                      count(DISTINCT o.outcome_key)::bigint AS outcomes,
                      min(p.observed_at) AS first_observed_at,max(p.observed_at) AS latest_observed_at,
                      max(o.settled_at) AS latest_settled_at
                 FROM research.selector_populations p
                 LEFT JOIN research.selector_population_members m USING(population_fingerprint)
                 LEFT JOIN research.selector_outcomes o USING(population_fingerprint)
                GROUP BY p.desk ORDER BY p.desk""",
            fetch="all",
            statement_timeout_ms=2500,
        ) or []
        return {
            "ok": True,
            "version": SERVICE_VERSION,
            "authority": "GOVERNANCE_POSTGRESQL",
            "counts": {key: int(value or 0) for key, value in dict(counts).items()},
            "by_desk": {str(row["desk"]).lower(): dict(row) for row in by_desk},
            "latest_checkpoint": self.latest_checkpoint(),
            "production_influence": 0.0,
            "broker_authority": "NONE",
        }
