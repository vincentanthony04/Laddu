from __future__ import annotations

"""Separate PostgreSQL authority for frozen predictions and model lifecycle."""

from datetime import datetime, timezone
import hashlib
import json
from typing import Any, Iterable, Mapping
from uuid import UUID, uuid4

from core.quant_v68.evaluation import evaluate_prediction_rows, evaluate_regime_strata, permutation_null_alpha_test
from core.quant_v68.promotion_gate import PromotionGate
from .postgres import PostgresAuthority
from core.strict_json import strict_json_dumps


def _uuid(value: Any | None = None) -> str:
    if value is None or str(value).strip() == "":
        return str(uuid4())
    return str(UUID(str(value)))


def _json(value: Any) -> str:
    return strict_json_dumps(value if value is not None else {}, sort_keys=True, separators=(",", ":"))


def _sha(value: Any) -> str:
    material = value if isinstance(value, str) else _json(value)
    return hashlib.sha256(material.encode("utf-8", errors="replace")).hexdigest()


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


class ProductionModelGovernanceRepository:
    SERVICE_VERSION = "postgres-model-governance-1.1.0-regime-calibration-drift"

    def __init__(self, authority: PostgresAuthority):
        self.authority = authority

    @staticmethod
    def _assert_payload_hash(cur: Any, table: str, key_column: str, key: str, expected: str) -> None:
        # Table and column names are fixed internal constants.
        cur.execute(f"SELECT payload_sha256 FROM {table} WHERE {key_column}=%s", (key,))
        row = cur.fetchone()
        if not row or str(row["payload_sha256"]) != expected:
            raise RuntimeError(f"MODEL_GOVERNANCE_IDEMPOTENCY_CONFLICT:{table}:{key}")

    @staticmethod
    def _snapshot_without_hash(value: Mapping[str, Any] | None) -> dict[str, Any]:
        payload = dict(value or {})
        payload.pop("snapshot_hash", None)
        return payload

    @staticmethod
    def _canonical_timestamp(value: Any) -> str:
        if isinstance(value, datetime):
            parsed = value
        else:
            try:
                parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
            except (TypeError, ValueError):
                return str(value or "")
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")

    @classmethod
    def _legacy_snapshot_hash_only_compatible(
        cls, existing: Mapping[str, Any] | None, incoming: Mapping[str, Any] | None,
    ) -> bool:
        """Accept only the PL29 NaN/Infinity -> JSONB-null hash-only mismatch.

        Existing governed rows are never rewritten. Compatibility is granted
        only when every persisted snapshot field is semantically identical
        after removing the derived snapshot_hash itself. Any genuine content,
        time, identity, lineage, cost or feature difference still fails closed.
        """
        left = cls._snapshot_without_hash(existing)
        right = cls._snapshot_without_hash(incoming)
        return bool(left) and _json(left) == _json(right)

    @staticmethod
    def _member_payload(member: Mapping[str, Any], population: Mapping[str, Any]) -> dict[str, Any]:
        row = dict(member)
        features = _json_object(row.get("feature_json") or row.get("features") or row.get("feature_snapshot"))
        quant_snapshot = row.get("governance_feature_snapshot")
        if bool(row.get("_legacy_feature_payload")):
            # Preserve the exact v1 projection hash so a database already
            # imported by the former async copier verifies idempotently.
            feature_payload = features
        else:
            feature_payload = {
                "features": features,
                "candidate_identity_version": str(row.get("candidate_identity_version") or "candidate-identity-v1"),
                "cost_authority_id": str(row.get("cost_authority_id") or ""),
                "feature_manifest_hash": str(population.get("feature_manifest_hash") or ""),
                "dataset_fingerprint": str(population.get("dataset_fingerprint") or ""),
            }
            if isinstance(quant_snapshot, Mapping):
                feature_payload["quant_snapshot"] = dict(quant_snapshot)
        side = str(row.get("side") or features.get("side") or "UNKNOWN").upper()
        if side not in {"LONG", "SHORT"}:
            side = "UNKNOWN"
        return {
            "candidate_id": str(row["candidate_id"]),
            "population_fingerprint": str(population["population_fingerprint"]),
            "instrument_key": str(row["instrument_key"]),
            "symbol": str(row["symbol"]).upper(),
            "exchange": str(row.get("exchange") or "").upper(),
            "desk": str(row.get("mode") or population.get("mode") or population.get("desk")).upper(),
            "side": side,
            "observed_at": row["observed_at"],
            "feature_hash": str(row["feature_hash"]),
            "feature_payload": feature_payload,
        }

    def record_legacy_research_quarantine(
        self,
        *,
        entity_type: str,
        legacy_key: str,
        reason: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Persist invalid legacy research evidence without granting canonical authority."""
        kind = str(entity_type or "").upper().strip()
        key = str(legacy_key or "").strip()
        why = str(reason or "").upper().strip()
        if kind not in {"POPULATION", "OUTCOME"} or not key or not why:
            raise ValueError("legacy research quarantine requires entity_type, legacy_key and reason")
        material = {
            "entity_type": kind,
            "legacy_key": key,
            "reason": why,
            "payload": dict(payload or {}),
        }
        payload_hash = _sha(material)
        quarantine_key = f"{kind}:{key}"
        with self.authority.transaction(isolation_level="serializable", statement_timeout_ms=10000) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO research.legacy_research_quarantine(
                           quarantine_key,entity_type,legacy_key,reason,payload,payload_sha256)
                       VALUES(%s,%s,%s,%s,%s::jsonb,%s)
                       ON CONFLICT(quarantine_key) DO NOTHING""",
                    (quarantine_key, kind, key, why, _json(material["payload"]), payload_hash),
                )
                self._assert_payload_hash(
                    cur, "research.legacy_research_quarantine", "quarantine_key",
                    quarantine_key, payload_hash,
                )
        return {
            "ok": True, "quarantine_key": quarantine_key, "entity_type": kind,
            "legacy_key": key, "reason": why, "payload_sha256": payload_hash,
            "authority": "GOVERNANCE_POSTGRESQL_QUARANTINE",
        }

    @staticmethod
    def _legacy_population_quarantine_reason(
        population: Mapping[str, Any],
        members: Iterable[Mapping[str, Any]],
        predictions: Iterable[Mapping[str, Any]],
    ) -> str | None:
        member_rows = [dict(row) for row in members]
        prediction_rows = [dict(row) for row in predictions]
        declared = int(population.get("candidate_count") or 0)
        if declared <= 0:
            return "EMPTY_OR_NON_POSITIVE_POPULATION"
        if declared != len(member_rows):
            return "POPULATION_MEMBER_COUNT_MISMATCH"
        if str(population.get("mode") or "").lower() not in {"intraday", "delivery"}:
            return "UNSUPPORTED_POPULATION_MODE"
        if any(not str(population.get(key) or "").strip() for key in (
            "universe_id", "dataset_fingerprint", "feature_manifest_hash",
        )):
            return "MISSING_POPULATION_AUTHORITY_IDENTITY"
        if any(
            not str(member.get(key) or "").strip()
            for member in member_rows
            for key in ("candidate_id", "symbol", "exchange", "instrument_key", "feature_hash")
        ):
            return "INVALID_POPULATION_MEMBER_IDENTITY"
        if prediction_rows:
            expected = declared * 3
            arms_by_candidate: dict[str, set[str]] = {}
            for row in prediction_rows:
                arms_by_candidate.setdefault(str(row.get("candidate_id") or ""), set()).add(str(row.get("arm") or ""))
            if (
                len(prediction_rows) != expected
                or len(arms_by_candidate) != declared
                or any(arms != {"heuristic", "quant", "hybrid"} for arms in arms_by_candidate.values())
            ):
                return "INCOMPLETE_THREE_ARM_PREDICTIONS"
        return None

    def record_selector_population(
        self,
        record: Mapping[str, Any],
        members: Iterable[Mapping[str, Any]],
    ) -> dict[str, Any]:
        """Atomically insert-or-verify one population and all feature snapshots."""
        row = dict(record)
        population = {
            "population_fingerprint": str(row["population_fingerprint"]),
            "desk": str(row.get("desk") or row.get("mode")).upper(),
            "observed_at": row["observed_at"],
            "universe_id": str(row["universe_id"]),
            "dataset_fingerprint": str(row["dataset_fingerprint"]),
            "feature_manifest_hash": str(row["feature_manifest_hash"]),
            "candidate_count": int(row["candidate_count"]),
            "policy_version": str(row["policy_version"]),
        }
        member_rows = [self._member_payload(member, {**row, **population}) for member in members]
        if population["desk"] not in {"INTRADAY", "DELIVERY"}:
            raise ValueError("selector population desk must be INTRADAY or DELIVERY")
        if not population["universe_id"] or not population["dataset_fingerprint"] or not population["feature_manifest_hash"]:
            raise ValueError("selector population requires universe, dataset and feature-manifest authority")
        if population["candidate_count"] <= 0 or population["candidate_count"] != len(member_rows):
            raise ValueError("selector population member count must be positive and exact")
        if any(not member["exchange"] for member in member_rows):
            raise ValueError("selector feature snapshot requires an explicit exchange")
        population_hash = _sha(population)
        inserted_population = inserted_members = 0
        with self.authority.transaction(isolation_level="serializable", statement_timeout_ms=20000) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (f"selector-population:{population['population_fingerprint']}",))
                cur.execute(
                    """INSERT INTO research.selector_populations(
                           population_fingerprint,desk,observed_at,universe_id,dataset_fingerprint,
                           feature_manifest_hash,candidate_count,policy_version,payload_sha256)
                       VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT(population_fingerprint) DO NOTHING""",
                    (
                        population["population_fingerprint"], population["desk"], population["observed_at"],
                        population["universe_id"], population["dataset_fingerprint"],
                        population["feature_manifest_hash"], population["candidate_count"],
                        population["policy_version"], population_hash,
                    ),
                )
                inserted_population += max(0, int(cur.rowcount or 0))
                self._assert_payload_hash(
                    cur, "research.selector_populations", "population_fingerprint",
                    population["population_fingerprint"], population_hash,
                )
                for member in member_rows:
                    member_hash = _sha(member)
                    cur.execute(
                        """INSERT INTO research.selector_population_members(
                               candidate_id,population_fingerprint,instrument_key,symbol,exchange,desk,side,
                               observed_at,feature_hash,feature_payload,payload_sha256)
                           VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s)
                           ON CONFLICT(candidate_id) DO NOTHING""",
                        (
                            member["candidate_id"], member["population_fingerprint"], member["instrument_key"],
                            member["symbol"], member["exchange"], member["desk"], member["side"],
                            member["observed_at"], member["feature_hash"], _json(member["feature_payload"]),
                            member_hash,
                        ),
                    )
                    inserted_members += max(0, int(cur.rowcount or 0))
                    cur.execute(
                        """SELECT population_fingerprint,instrument_key,symbol,exchange,desk,side,
                                  observed_at,feature_hash,feature_payload,payload_sha256
                             FROM research.selector_population_members WHERE candidate_id=%s""",
                        (member["candidate_id"],),
                    )
                    existing_member = cur.fetchone()
                    if not existing_member:
                        raise RuntimeError(
                            f"MODEL_GOVERNANCE_IDEMPOTENCY_CONFLICT:research.selector_population_members:{member['candidate_id']}"
                        )
                    if str(existing_member["payload_sha256"]) != member_hash:
                        immutable_same = all([
                            str(existing_member.get("population_fingerprint")) == member["population_fingerprint"],
                            str(existing_member.get("instrument_key")) == member["instrument_key"],
                            str(existing_member.get("symbol")) == member["symbol"],
                            str(existing_member.get("exchange")) == member["exchange"],
                            str(existing_member.get("desk")) == member["desk"],
                            str(existing_member.get("side")) == member["side"],
                            self._canonical_timestamp(existing_member.get("observed_at"))
                            == self._canonical_timestamp(member["observed_at"]),
                            str(existing_member.get("feature_hash")) == member["feature_hash"],
                        ])
                        existing_envelope = _json_object(existing_member.get("feature_payload"))
                        incoming_envelope = _json_object(member.get("feature_payload"))
                        existing_quant = _json_object(existing_envelope.get("quant_snapshot"))
                        incoming_quant = _json_object(incoming_envelope.get("quant_snapshot"))
                        existing_envelope.pop("quant_snapshot", None)
                        incoming_envelope.pop("quant_snapshot", None)
                        envelope_same = _json(existing_envelope) == _json(incoming_envelope)
                        if not (
                            immutable_same
                            and envelope_same
                            and self._legacy_snapshot_hash_only_compatible(existing_quant, incoming_quant)
                        ):
                            raise RuntimeError(
                                f"MODEL_GOVERNANCE_IDEMPOTENCY_CONFLICT:research.selector_population_members:{member['candidate_id']}"
                            )
        return {
            "ok": True,
            "inserted": bool(inserted_population),
            "population_fingerprint": population["population_fingerprint"],
            "population_payload_sha256": population_hash,
            "member_payload_sha256": _sha(sorted(_sha(member) for member in member_rows)),
            "members": len(member_rows),
            "new_members": inserted_members,
            "authority": "GOVERNANCE_POSTGRESQL",
        }

    def selector_population_members(self, population_fingerprint: str) -> list[dict[str, Any]]:
        rows = self.authority.execute(
            """SELECT m.*,p.universe_id,p.dataset_fingerprint,p.feature_manifest_hash,p.policy_version
                 FROM research.selector_population_members m
                 JOIN research.selector_populations p USING(population_fingerprint)
                WHERE m.population_fingerprint=%s ORDER BY m.symbol""",
            (str(population_fingerprint),), fetch="all", statement_timeout_ms=5000,
        ) or []
        result: list[dict[str, Any]] = []
        for raw in rows:
            row = dict(raw)
            envelope = _json_object(row.get("feature_payload"))
            features = (
                _json_object(envelope.get("features"))
                if "features" in envelope else envelope
            )
            quant_snapshot = _json_object(envelope.get("quant_snapshot"))
            item = dict(features)
            item.update({
                "candidate_id": row.get("candidate_id"),
                "population_fingerprint": row.get("population_fingerprint"),
                "symbol": row.get("symbol"),
                "exchange": row.get("exchange"),
                "instrument_key": row.get("instrument_key"),
                "mode": str(row.get("desk") or "").lower(),
                "side": row.get("side"),
                "observed_at": quant_snapshot.get("decision_ts") or str(row.get("observed_at")),
                "identity_verified": bool(features.get("identity_verified")),
                "production_status": features.get("status"),
                "production_decision": features.get("decision"),
                "feature_snapshot": features,
                "governance_feature_snapshot": quant_snapshot,
                "feature_snapshot_state": quant_snapshot.get("snapshot_state"),
                "feature_lineage_state": quant_snapshot.get("lineage_state"),
                "feature_hash": row.get("feature_hash"),
                "candidate_identity_version": quant_snapshot.get("candidate_identity_version") or envelope.get("candidate_identity_version"),
                "cost_authority_id": quant_snapshot.get("cost_authority_id") or envelope.get("cost_authority_id"),
                "universe_id": row.get("universe_id"),
                "dataset_fingerprint": row.get("dataset_fingerprint"),
                "feature_manifest_hash": row.get("feature_manifest_hash"),
            })
            result.append(item)
        return result

    def selector_member(self, candidate_id: str) -> dict[str, Any] | None:
        row = self.authority.execute(
            """SELECT m.*,p.universe_id,p.dataset_fingerprint,p.feature_manifest_hash,p.policy_version
                 FROM research.selector_population_members m
                 JOIN research.selector_populations p USING(population_fingerprint)
                WHERE m.candidate_id=%s""",
            (str(candidate_id),), fetch="one", statement_timeout_ms=2500,
        )
        if not row:
            return None
        records = self.selector_population_members(str(row["population_fingerprint"]))
        return next((record for record in records if str(record["candidate_id"]) == str(candidate_id)), None)

    def record_selector_feature_snapshot(self, snapshot: Mapping[str, Any]) -> dict[str, Any]:
        """Verify the snapshot frozen atomically with its population member."""
        payload = dict(snapshot)
        row = self.authority.execute(
            "SELECT feature_payload FROM research.selector_population_members WHERE candidate_id=%s",
            (str(payload["candidate_id"]),), fetch="one", statement_timeout_ms=2500,
        )
        if not row:
            raise RuntimeError("SELECTOR_FEATURE_SNAPSHOT_MEMBER_MISSING")
        envelope = _json_object(row.get("feature_payload"))
        stored = _json_object(envelope.get("quant_snapshot"))
        exact_hash = bool(stored) and str(stored.get("snapshot_hash")) == str(payload.get("snapshot_hash"))
        legacy_hash_only = (
            bool(stored)
            and not exact_hash
            and self._legacy_snapshot_hash_only_compatible(stored, payload)
        )
        if not (exact_hash or legacy_hash_only):
            raise RuntimeError("SELECTOR_FEATURE_SNAPSHOT_HASH_CONFLICT")
        return {
            "ok": True, "inserted": False, **stored,
            "snapshot_hash_compatibility": (
                "EXACT" if exact_hash else "LEGACY_PRE_JSONB_NONFINITE_NORMALIZATION"
            ),
            "authority": "GOVERNANCE_POSTGRESQL",
        }

    def selector_feature_snapshot(self, candidate_id: str) -> dict[str, Any] | None:
        row = self.authority.execute(
            "SELECT feature_payload FROM research.selector_population_members WHERE candidate_id=%s",
            (str(candidate_id),), fetch="one", statement_timeout_ms=2500,
        )
        if not row:
            return None
        return _json_object(_json_object(row.get("feature_payload")).get("quant_snapshot")) or None


    @staticmethod
    def _legacy_prediction_material_v1(
        population_fingerprint: str, row: Mapping[str, Any], *, prediction_at: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Reproduce the retired forward-evidence legacy prediction hash exactly."""
        prediction_payload = _json_object(row.get("prediction_json"))
        key = f"{population_fingerprint}:{row['candidate_id']}:{row['arm']}"
        material = {
            "prediction_key": key,
            "population_fingerprint": str(population_fingerprint),
            "candidate_id": row["candidate_id"],
            "arm": row["arm"],
            "model_version": row.get("model_version"),
            "score": row.get("score"),
            "rank": row.get("rank"),
            "percentile": row.get("percentile"),
            "probability_positive": row.get("probability_positive"),
            "expected_net_return_bps": row.get("expected_net_return"),
            "prediction_at": row.get("created_at") or prediction_at,
            "prediction_payload": prediction_payload,
        }
        return material, prediction_payload

    def record_selector_predictions(
        self,
        population_fingerprint: str,
        predictions: Iterable[Mapping[str, Any]],
        *,
        prediction_at: str,
    ) -> dict[str, Any]:
        rows = [dict(item) for item in predictions]
        population = self.authority.execute(
            "SELECT candidate_count FROM research.selector_populations WHERE population_fingerprint=%s",
            (str(population_fingerprint),), fetch="one", statement_timeout_ms=2500,
        )
        if not population:
            raise RuntimeError("SELECTOR_PREDICTION_POPULATION_MISSING")
        expected = int(population["candidate_count"]) * 3
        if len(rows) != expected:
            raise ValueError(f"three-arm prediction count mismatch: {len(rows)} != {expected}")
        arms_by_candidate: dict[str, set[str]] = {}
        for row in rows:
            arm = str(row.get("arm") or "")
            if arm not in {"heuristic", "quant", "hybrid"}:
                raise ValueError(f"unsupported authoritative selector arm: {arm}")
            arms_by_candidate.setdefault(str(row.get("candidate_id") or ""), set()).add(arm)
        unique_keys = {(str(row.get("candidate_id") or ""), str(row.get("arm") or "")) for row in rows}
        if (
            len(arms_by_candidate) != int(population["candidate_count"])
            or len(unique_keys) != len(rows)
            or any(arms != {"heuristic", "quant", "hybrid"} for arms in arms_by_candidate.values())
        ):
            raise ValueError("every candidate requires all three authoritative selector arms")
        inserted = 0
        hashes: list[str] = []
        with self.authority.transaction(isolation_level="serializable", statement_timeout_ms=20000) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (f"selector-predictions:{population_fingerprint}",))
                for row in rows:
                    key = f"{population_fingerprint}:{row['candidate_id']}:{row['arm']}"
                    if bool(row.get("_legacy_prediction_payload_v1")):
                        # The retired forward-evidence copier published legacy SQLite
                        # predictions before ProductionModelGovernanceRepository became
                        # the sole writer. Reproduce that exact v1 material so an
                        # already-published immutable row verifies idempotently instead
                        # of being misclassified as a governance conflict.
                        material, stored_payload = self._legacy_prediction_material_v1(
                            str(population_fingerprint), row, prediction_at=prediction_at,
                        )
                    else:
                        material = {
                            "prediction_key": key,
                            "population_fingerprint": str(population_fingerprint),
                            "candidate_id": str(row["candidate_id"]),
                            "arm": str(row["arm"]),
                            "model_version": str(row.get("model_version") or ""),
                            "score": row.get("score"),
                            "rank": int(row["rank"]),
                            "percentile": row.get("percentile"),
                            "probability_positive": row.get("probability_positive"),
                            "expected_net_return_bps": row.get("expected_net_return"),
                            "prediction_at": prediction_at,
                            "prediction_payload": row,
                        }
                        stored_payload = row
                    payload_hash = _sha(material)
                    cur.execute(
                        """INSERT INTO research.selector_arm_predictions(
                               prediction_key,population_fingerprint,candidate_id,arm,model_version,
                               score,predicted_rank,predicted_percentile,probability_positive,
                               expected_net_return_bps,prediction_at,prediction_payload,payload_sha256)
                           VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s)
                           ON CONFLICT(prediction_key) DO NOTHING""",
                        (
                            key, str(population_fingerprint), material["candidate_id"], material["arm"],
                            material["model_version"], material["score"], material["rank"],
                            material["percentile"], material["probability_positive"],
                            material["expected_net_return_bps"], material["prediction_at"], _json(stored_payload), payload_hash,
                        ),
                    )
                    inserted += max(0, int(cur.rowcount or 0))
                    self._assert_payload_hash(cur, "research.selector_arm_predictions", "prediction_key", key, payload_hash)
                    hashes.append(payload_hash)
                cur.execute(
                    "SELECT count(*)::bigint AS count FROM research.selector_arm_predictions WHERE population_fingerprint=%s",
                    (str(population_fingerprint),),
                )
                stored = cur.fetchone()
                if not stored or int(stored["count"] or 0) != expected:
                    raise RuntimeError("SELECTOR_THREE_ARM_PREDICTION_COUNT_VERIFICATION_FAILED")
        return {"ok": True, "inserted": inserted, "predictions": len(rows), "payload_sha256": _sha(sorted(hashes)), "authority": "GOVERNANCE_POSTGRESQL"}

    def selector_predictions(self, population_fingerprint: str) -> list[dict[str, Any]]:
        rows = self.authority.execute(
            """SELECT * FROM research.selector_arm_predictions
                WHERE population_fingerprint=%s ORDER BY arm,predicted_rank,prediction_key""",
            (str(population_fingerprint),), fetch="all", statement_timeout_ms=5000,
        ) or []
        result = []
        for raw in rows:
            row = dict(raw)
            payload = _json_object(row.get("prediction_payload"))
            payload.update({
                "population_fingerprint": row.get("population_fingerprint"),
                "candidate_id": row.get("candidate_id"), "arm": row.get("arm"),
                "score": row.get("score"), "rank": row.get("predicted_rank"),
                "percentile": row.get("predicted_percentile"),
                "created_at": str(row.get("prediction_at")),
            })
            result.append(payload)
        return result

    def latest_selector_population(self, mode: str | None = None) -> dict[str, Any] | None:
        params: tuple[Any, ...] = ()
        where = ""
        if mode:
            where = "WHERE desk=%s"
            params = (str(mode).upper(),)
        row = self.authority.execute(
            f"""SELECT population_fingerprint,observed_at FROM research.selector_populations
                  {where} ORDER BY observed_at DESC,population_fingerprint DESC LIMIT 1""",
            params, fetch="one", statement_timeout_ms=2500,
        )
        return dict(row) if row else None

    def latest_selector_challenger_model(self, *, mode: str, horizon: str) -> dict[str, Any] | None:
        horizon_key = str(horizon).lower()
        digits = "".join(character for character in horizon_key if character.isdigit())
        horizon_value = int(digits or (1 if horizon_key == "eod" else 0))
        if horizon_value <= 0:
            return None
        row = self.authority.execute(
            """SELECT model_payload,validation_payload,model_key,model_version,lifecycle_state
                 FROM research.training_publications
                WHERE desk=%s AND horizon_value=%s AND lifecycle_state='SHADOW'
                ORDER BY created_at DESC LIMIT 1""",
            (
                str(mode).upper(),
                horizon_value,
            ),
            fetch="one", statement_timeout_ms=2500,
        )
        if not row:
            return None
        model = _json_object(row.get("model_payload"))
        # Only the exact legacy-compatible artifact shape may be evaluated by
        # this selector. Other governed model families remain unavailable here.
        if not isinstance(model.get("artifact"), Mapping) or not model.get("feature_names"):
            return None
        model.update({
            "model_id": str(row.get("model_key") or ""),
            "model_version": str(row.get("model_version") or ""),
            "mode": str(mode).lower(), "horizon": str(horizon).lower(),
            "validation": _json_object(row.get("validation_payload")),
            "state": "SHADOW_MODEL_ELIGIBLE",
        })
        return model

    def record_selector_outcome(self, record: Mapping[str, Any]) -> dict[str, Any]:
        row = dict(record)
        row["outcome_key"] = str(row.get("outcome_key") or f"{row['candidate_id']}:{row['horizon']}")
        material = {
            "outcome_key": row["outcome_key"],
            "candidate_id": str(row["candidate_id"]),
            "population_fingerprint": str(row["population_fingerprint"]),
            "horizon": str(row["horizon"]).lower(),
            "observed_at": row["observed_at"],
            "settled_at": row["settled_at"],
            "market_regime": str(row.get("market_regime") or "UNKNOWN").upper(),
            "result": str(row["result"]).upper(),
            "gross_return_bps": row.get("gross_return_bps"),
            "net_return_bps": row["net_return_bps"],
            "actual_cost_bps": row.get("actual_cost_bps"),
            "same_bar_ambiguous": bool(row.get("same_bar_ambiguous")),
            "proof_payload": _json_object(row.get("proof_payload") or row.get("proof")),
            "record_hash": str(row["record_hash"]),
        }
        payload_hash = _sha(material)
        inserted = 0
        with self.authority.transaction(isolation_level="serializable", statement_timeout_ms=10000) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (f"selector-outcome:{material['outcome_key']}",))
                cur.execute(
                    """INSERT INTO research.selector_outcomes(
                           outcome_key,candidate_id,population_fingerprint,horizon,observed_at,settled_at,
                           market_regime,result,gross_return_bps,net_return_bps,actual_cost_bps,
                           same_bar_ambiguous,proof_payload,record_hash,payload_sha256)
                       VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s)
                       ON CONFLICT(outcome_key) DO NOTHING""",
                    (
                        material["outcome_key"], material["candidate_id"], material["population_fingerprint"],
                        material["horizon"], material["observed_at"], material["settled_at"],
                        material["market_regime"], material["result"], material["gross_return_bps"],
                        material["net_return_bps"], material["actual_cost_bps"],
                        material["same_bar_ambiguous"], _json(material["proof_payload"]),
                        material["record_hash"], payload_hash,
                    ),
                )
                inserted = max(0, int(cur.rowcount or 0))
                self._assert_payload_hash(cur, "research.selector_outcomes", "outcome_key", material["outcome_key"], payload_hash)
        return {"ok": True, "inserted": bool(inserted), **material, "payload_sha256": payload_hash, "authority": "GOVERNANCE_POSTGRESQL"}

    def selector_outcome(self, candidate_id: str, horizon: str) -> dict[str, Any] | None:
        row = self.authority.execute(
            "SELECT * FROM research.selector_outcomes WHERE candidate_id=%s AND horizon=%s",
            (str(candidate_id), str(horizon).lower()), fetch="one", statement_timeout_ms=2500,
        )
        return dict(row) if row else None

    def record_selector_label(self, record: Mapping[str, Any]) -> dict[str, Any]:
        """Verify the fixed-horizon label embedded in the authoritative outcome proof."""
        label = dict(record)
        outcome = self.selector_outcome(str(label["candidate_id"]), str(label["horizon"]))
        if not outcome:
            raise RuntimeError("SELECTOR_LABEL_REQUIRES_AUTHORITATIVE_OUTCOME")
        proof = _json_object(outcome.get("proof_payload"))
        expected = {
            "fixed_horizon_settled_at": str(label.get("settled_at") or ""),
            "fixed_horizon_result": str(label.get("result") or "").upper(),
            "fixed_horizon_gross_return_bps": label.get("gross_return_bps"),
            "fixed_horizon_net_return_bps": label.get("net_return_bps"),
        }
        for key, value in expected.items():
            stored = proof.get(key)
            if key.endswith("_bps"):
                if stored is None or float(stored) != float(value):
                    raise RuntimeError(f"SELECTOR_LABEL_PROOF_CONFLICT:{key}")
            elif str(stored or "") != str(value):
                raise RuntimeError(f"SELECTOR_LABEL_PROOF_CONFLICT:{key}")
        return {
            "ok": True, "inserted": False, **label,
            "authority": "GOVERNANCE_POSTGRESQL",
            "outcome_payload_sha256": outcome.get("payload_sha256"),
        }

    def selector_candidates_for_settlement(self, *, symbol: str, mode: str) -> list[dict[str, Any]]:
        rows = self.authority.execute(
            """SELECT DISTINCT m.* FROM research.selector_population_members m
                 JOIN research.selector_arm_predictions p ON p.candidate_id=m.candidate_id
                WHERE m.symbol=%s AND m.desk=%s ORDER BY m.observed_at""",
            (str(symbol).upper(), str(mode).upper()), fetch="all", statement_timeout_ms=5000,
        ) or []
        result: list[dict[str, Any]] = []
        for raw in rows:
            row = dict(raw)
            envelope = _json_object(row.get("feature_payload"))
            snapshot = _json_object(envelope.get("quant_snapshot"))
            features = _json_object(envelope.get("features"))
            result.append({
                "candidate_id": row["candidate_id"], "population_fingerprint": row["population_fingerprint"],
                "symbol": row["symbol"], "mode": str(row["desk"]).lower(), "side": row["side"],
                "observed_at": str(row["observed_at"]), "feature_json": _json(features),
                "cost_assumption_json": _json(snapshot.get("cost_assumptions")),
            })
        return result

    def selector_joined_rows(self, *, mode: str, horizon: str) -> list[dict[str, Any]]:
        rows = self.authority.execute(
            """SELECT p.arm,p.model_version,p.candidate_id,p.population_fingerprint,m.symbol,
                      lower(m.desk) AS mode,p.score,p.predicted_rank AS rank,
                      p.predicted_percentile AS percentile,p.probability_positive,
                      p.expected_net_return_bps AS expected_net_return,p.prediction_at,
                      o.population_fingerprint AS outcome_population_fingerprint,o.horizon,o.observed_at,
                      o.settled_at,o.market_regime,o.result,o.gross_return_bps,o.net_return_bps,
                      o.same_bar_ambiguous,'SAME_BAR_STOP_FIRST_PRIMARY' AS primary_ambiguity_policy,
                      o.actual_cost_bps
                 FROM research.selector_arm_predictions p
                 JOIN research.selector_population_members m ON m.candidate_id=p.candidate_id
                  AND m.population_fingerprint=p.population_fingerprint
                 JOIN research.selector_outcomes o ON o.candidate_id=p.candidate_id
                  AND o.population_fingerprint=p.population_fingerprint
                WHERE m.desk=%s AND o.horizon=%s
                ORDER BY p.arm,o.observed_at,p.predicted_rank,m.symbol""",
            (str(mode).upper(), str(horizon).lower()), fetch="all", statement_timeout_ms=10000,
        ) or []
        return [dict(row) for row in rows]

    def selector_attribution_rows(
        self, *, mode: str | None = None, horizon: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return immutable Research rows with the dimensions needed by AC-069.

        This is a governance-PostgreSQL read only.  It deliberately returns
        frozen candidate-origin fields from ``feature_payload`` rather than
        reconstructing promotion lineage from symbol/time heuristics.
        """
        clauses: list[str] = []
        params: list[Any] = []
        if mode:
            clauses.append("m.desk=%s")
            params.append(str(mode).upper())
        if horizon:
            clauses.append("o.horizon=%s")
            params.append(str(horizon).lower())
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        rows = self.authority.execute(
            f"""SELECT p.arm,p.model_version,p.candidate_id,p.population_fingerprint,
                       p.score,p.predicted_rank,p.predicted_percentile,p.probability_positive,
                       p.expected_net_return_bps,p.prediction_at,
                       m.symbol,m.exchange,m.instrument_key,lower(m.desk) AS mode,m.side,
                       m.observed_at AS candidate_observed_at,m.feature_payload,m.feature_hash,
                       pop.feature_manifest_hash,pop.dataset_fingerprint,pop.policy_version AS population_policy_version,
                       o.horizon,o.observed_at,o.settled_at,o.market_regime,o.result,
                       o.gross_return_bps,o.net_return_bps,o.actual_cost_bps,o.same_bar_ambiguous,
                       o.record_hash AS outcome_record_hash,o.payload_sha256 AS outcome_payload_sha256
                  FROM research.selector_arm_predictions p
                  JOIN research.selector_population_members m
                    ON m.candidate_id=p.candidate_id
                   AND m.population_fingerprint=p.population_fingerprint
                  JOIN research.selector_populations pop
                    ON pop.population_fingerprint=p.population_fingerprint
                  JOIN research.selector_outcomes o
                    ON o.candidate_id=p.candidate_id
                   AND o.population_fingerprint=p.population_fingerprint
                  {where}
                 ORDER BY o.observed_at,p.population_fingerprint,p.arm,p.predicted_rank,m.symbol""",
            tuple(params), fetch="all", statement_timeout_ms=15000,
        ) or []
        result: list[dict[str, Any]] = []
        for raw in rows:
            item = dict(raw)
            envelope = _json_object(item.pop("feature_payload", None))
            features = _json_object(envelope.get("features")) if "features" in envelope else dict(envelope)
            item["sector"] = str(
                features.get("sector") or features.get("sector_label") or features.get("sector_key") or "UNKNOWN"
            )
            item["generated_at"] = (
                features.get("generated_at") or features.get("decision_generated_at") or features.get("created_at")
            )
            item["planned_entry"] = features.get("planned_entry")
            item["planned_stop"] = features.get("planned_sl") if features.get("planned_sl") is not None else features.get("planned_stop")
            item["planned_target"] = features.get("planned_t1") if features.get("planned_t1") is not None else features.get("planned_target")
            for key in (
                "origin_decision_id", "origin_signal_id", "origin_production_status",
                "origin_production_decision", "origin_qualification_blocker", "origin_lineage_version",
            ):
                item[key] = features.get(key)
            for key in ("origin_rejection_reasons", "origin_promotion_blocked_by"):
                value = features.get(key)
                item[key] = (
                    [str(entry) for entry in value if entry]
                    if isinstance(value, (list, tuple, set))
                    else ([str(value)] if value else [])
                )
            result.append(item)
        return result

    def quant_training_rows(self, *, mode: str, horizon: str) -> list[dict[str, Any]]:
        rows = self.selector_joined_rows(mode=mode, horizon=horizon)
        result: list[dict[str, Any]] = []
        seen: set[str] = set()
        for row in rows:
            candidate_id = str(row["candidate_id"])
            if candidate_id in seen:
                continue
            seen.add(candidate_id)
            snapshot = self.selector_feature_snapshot(candidate_id)
            outcome = self.selector_outcome(candidate_id, horizon)
            if not snapshot or not outcome or snapshot.get("snapshot_state") != "COMPLETE" or snapshot.get("lineage_state") != "VERIFIED":
                continue
            proof = _json_object(outcome.get("proof_payload"))
            result.append({
                **snapshot,
                "horizon": str(horizon).lower(), "label_regime": outcome.get("market_regime"),
                "result": outcome.get("result"),
                "strategy_net_return_bps": outcome.get("net_return_bps"),
                "net_return_bps": proof.get("fixed_horizon_net_return_bps", outcome.get("net_return_bps")),
                "settled_at": str(outcome.get("settled_at")), "record_hash": outcome.get("record_hash"),
            })
        return result


    def quant_training_evidence_status(self, *, mode: str, horizon: str) -> dict[str, Any]:
        """Bounded exact-horizon ML evidence depth without materialising feature JSON.

        This is the GET/readiness path for calibrated-model status. Heavy feature
        materialisation and fitting are owned by the governed training cycle, not
        by an HTTP status request.
        """
        desk = str(mode or "").upper().strip()
        horizon_key = str(horizon or "").lower().strip()
        row = self.authority.execute(
            """SELECT count(DISTINCT m.candidate_id)::bigint AS observations,
                      count(DISTINCT o.observed_at::date)::bigint AS trading_days,
                      count(DISTINCT NULLIF(o.market_regime,''))::bigint AS regimes,
                      min(o.observed_at::date)::text AS first_date,
                      max(o.observed_at::date)::text AS last_date
                 FROM research.selector_outcomes o
                 JOIN research.selector_population_members m
                   ON m.candidate_id=o.candidate_id
                  AND m.population_fingerprint=o.population_fingerprint
                WHERE m.desk=%s AND lower(o.horizon)=%s
                  AND m.feature_payload->'quant_snapshot'->>'snapshot_state'='COMPLETE'
                  AND m.feature_payload->'quant_snapshot'->>'lineage_state'='VERIFIED'""",
            (desk, horizon_key), fetch="one", statement_timeout_ms=15000, pool_timeout_seconds=10.0,
        ) or {}
        return {
            "observations": int(row.get("observations") or 0),
            "trading_days": int(row.get("trading_days") or 0),
            "regimes": int(row.get("regimes") or 0),
            "first_date": row.get("first_date"),
            "last_date": row.get("last_date"),
            "mode": desk.lower(),
            "horizon": horizon_key,
            "query_profile": "PL18_BOUNDED_EXACT_HORIZON_AGGREGATE",
        }

    def selector_evidence_status(self, mode: str | None = None) -> dict[str, Any]:
        """Return bounded governance evidence depth without materialising JSON payloads.

        R25 loaded every immutable member payload into Python merely to count
        snapshots/dates/coverage. Under live scanner load that diagnostic query
        could consume the same statement budget as capital WFA and time out.
        R26 performs the aggregation in PostgreSQL and transfers one scalar row
        per evidence family.
        """
        params: tuple[Any, ...] = ()
        member_where = label_where = ""
        if mode:
            params = (str(mode).upper(),)
            member_where = "WHERE desk=%s"
            label_where = "WHERE m.desk=%s"
        snapshot = self.authority.execute(
            f"""SELECT count(*)::bigint AS snapshots,
                       count(*) FILTER (
                         WHERE feature_payload->'quant_snapshot'->>'snapshot_state'='COMPLETE'
                       )::bigint AS complete_snapshots,
                       COALESCE(avg(
                         NULLIF(feature_payload->'quant_snapshot'->>'compact_feature_coverage','')::double precision
                       ),0)::double precision AS average_compact_feature_coverage,
                       count(DISTINCT left(feature_payload->'quant_snapshot'->>'decision_ts',10))::bigint AS snapshot_days,
                       min(left(feature_payload->'quant_snapshot'->>'decision_ts',10)) AS snapshot_first_date,
                       max(left(feature_payload->'quant_snapshot'->>'decision_ts',10)) AS snapshot_last_date,
                       count(DISTINCT NULLIF(feature_payload->'quant_snapshot'->>'regime_tag',''))::bigint AS snapshot_regimes
                  FROM research.selector_population_members
                  {member_where}""",
            params, fetch="one", statement_timeout_ms=15000, pool_timeout_seconds=10.0,
        ) or {}
        labels = self.authority.execute(
            f"""SELECT count(*)::bigint AS labels,
                       count(DISTINCT o.observed_at::date)::bigint AS label_days,
                       min(o.observed_at::date)::text AS label_first_date,
                       max(o.observed_at::date)::text AS label_last_date,
                       count(DISTINCT NULLIF(o.market_regime,''))::bigint AS label_regimes
                  FROM research.selector_outcomes o
                  JOIN research.selector_population_members m
                    ON m.candidate_id=o.candidate_id
                   AND m.population_fingerprint=o.population_fingerprint
                  {label_where}""",
            params, fetch="one", statement_timeout_ms=15000, pool_timeout_seconds=10.0,
        ) or {}
        return {
            "snapshots": int(snapshot.get("snapshots") or 0),
            "complete_snapshots": int(snapshot.get("complete_snapshots") or 0),
            "average_compact_feature_coverage": float(snapshot.get("average_compact_feature_coverage") or 0.0),
            "snapshot_days": int(snapshot.get("snapshot_days") or 0),
            "snapshot_first_date": snapshot.get("snapshot_first_date"),
            "snapshot_last_date": snapshot.get("snapshot_last_date"),
            "snapshot_regimes": int(snapshot.get("snapshot_regimes") or 0),
            "labels": int(labels.get("labels") or 0),
            "label_days": int(labels.get("label_days") or 0),
            "label_first_date": labels.get("label_first_date"),
            "label_last_date": labels.get("label_last_date"),
            "label_regimes": int(labels.get("label_regimes") or 0),
            "query_profile": "R26_BOUNDED_SQL_AGGREGATE",
        }

    @staticmethod
    def _legacy_research_source_manifest(conn: Any) -> tuple[dict[str, Any], str]:
        """Build a bounded logical identity for the retired SQLite research source.

        Production no longer writes these tables. Counts + max rowid provide a
        stable append-only census without hashing a potentially multi-GB SQLite
        file on every boot. The full row hashes are still verified during the
        one-time migration itself.
        """
        tables = (
            "candidate_populations", "candidate_population_observations",
            "quant_feature_snapshots", "shadow_selector_predictions",
            "selector_candidate_outcomes",
        )
        census: dict[str, Any] = {}
        for name in tables:
            exists = bool(conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
            ).fetchone())
            if not exists:
                census[name] = {"present": False, "rows": 0, "max_rowid": 0}
                continue
            row = conn.execute(
                f'SELECT COUNT(*) AS rows, COALESCE(MAX(rowid),0) AS max_rowid FROM "{name}"'
            ).fetchone()
            census[name] = {
                "present": True,
                "rows": int(row["rows"] if hasattr(row, "keys") else row[0]),
                "max_rowid": int(row["max_rowid"] if hasattr(row, "keys") else row[1]),
            }
        manifest = {"contract": "legacy-research-source-census-1.0.0", "tables": census}
        return manifest, _sha(manifest)

    def legacy_research_migration_status(self, store: Any) -> dict[str, Any]:
        """Return a bounded checkpoint status; never perform the migration here."""
        conn = getattr(store, "conn", None)
        if conn is None or not callable(getattr(conn, "execute", None)):
            return {
                "ok": False, "state": "LEGACY_RESEARCH_MIGRATION_SOURCE_UNAVAILABLE",
                "count_verified": False, "hash_verified": False, "quarantine_verified": False,
                "authority": "GOVERNANCE_POSTGRESQL",
            }
        manifest, manifest_hash = self._legacy_research_source_manifest(conn)
        pop = manifest["tables"]["candidate_populations"]
        if not pop["present"] or int(pop["rows"]) == 0:
            return {
                "ok": True, "state": "NO_LEGACY_RESEARCH_ROWS",
                "count_verified": True, "hash_verified": True, "quarantine_verified": True,
                "source_manifest": manifest, "source_manifest_sha256": manifest_hash,
                "expected": {"populations": 0, "members": 0, "predictions": 0, "outcomes": 0},
                "verified": {"populations": 0, "members": 0, "predictions": 0, "outcomes": 0},
                "quarantine": {"entities": 0, "populations": 0, "outcomes": 0},
                "authority": "GOVERNANCE_POSTGRESQL",
            }
        reader = getattr(self.authority, "execute", None)
        if not callable(reader):
            return {
                "ok": False, "state": "LEGACY_RESEARCH_MIGRATION_REQUIRED",
                "count_verified": False, "hash_verified": False, "quarantine_verified": False,
                "source_manifest": manifest, "source_manifest_sha256": manifest_hash,
                "authority": "GOVERNANCE_POSTGRESQL",
            }
        try:
            row = reader(
                """SELECT source_manifest,expected_counts,verified_counts,quarantine,payload_sha256,completed_at
                     FROM research.legacy_research_migration_checkpoints WHERE checkpoint_key=%s""",
                (manifest_hash,), fetch="one", statement_timeout_ms=2500,
            ) or None
        except Exception:
            row = None
        if not row:
            return {
                "ok": False, "state": "LEGACY_RESEARCH_MIGRATION_REQUIRED",
                "count_verified": False, "hash_verified": False, "quarantine_verified": False,
                "source_manifest": manifest, "source_manifest_sha256": manifest_hash,
                "authority": "GOVERNANCE_POSTGRESQL",
            }
        expected = _json_object(row.get("expected_counts"))
        verified = _json_object(row.get("verified_counts"))
        quarantine = _json_object(row.get("quarantine"))
        checkpoint_material = {
            "source_manifest_sha256": manifest_hash,
            "source_manifest": manifest,
            "expected": expected,
            "verified": verified,
            "quarantine": quarantine,
        }
        if str(row.get("payload_sha256") or "") != _sha(checkpoint_material):
            raise RuntimeError("LEGACY_RESEARCH_MIGRATION_CHECKPOINT_HASH_CONFLICT")
        return {
            "ok": True, "state": "LEGACY_RESEARCH_MIGRATION_CHECKPOINT_VERIFIED",
            "count_verified": expected == verified,
            "hash_verified": True, "quarantine_verified": True,
            "source_manifest": manifest, "source_manifest_sha256": manifest_hash,
            "expected": expected, "verified": verified, "quarantine": quarantine,
            "completed_at": row.get("completed_at"),
            "authority": "GOVERNANCE_POSTGRESQL",
        }

    def _record_legacy_research_migration_checkpoint(
        self, *, source_manifest: Mapping[str, Any], source_manifest_sha256: str,
        expected: Mapping[str, Any], verified: Mapping[str, Any], quarantine: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        transaction = getattr(self.authority, "transaction", None)
        if not callable(transaction):
            return None
        material = {
            "source_manifest_sha256": str(source_manifest_sha256),
            "source_manifest": dict(source_manifest),
            "expected": dict(expected), "verified": dict(verified), "quarantine": dict(quarantine),
        }
        payload_hash = _sha(material)
        try:
            with transaction(isolation_level="serializable", statement_timeout_ms=10000) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """INSERT INTO research.legacy_research_migration_checkpoints(
                               checkpoint_key,source_manifest_sha256,source_manifest,
                               expected_counts,verified_counts,quarantine,payload_sha256)
                           VALUES(%s,%s,%s::jsonb,%s::jsonb,%s::jsonb,%s::jsonb,%s)
                           ON CONFLICT(checkpoint_key) DO NOTHING""",
                        (
                            str(source_manifest_sha256), str(source_manifest_sha256), _json(source_manifest),
                            _json(expected), _json(verified), _json(quarantine), payload_hash,
                        ),
                    )
                    self._assert_payload_hash(
                        cur, "research.legacy_research_migration_checkpoints", "checkpoint_key",
                        str(source_manifest_sha256), payload_hash,
                    )
        except Exception as exc:
            # Unit/disposable repositories created without the candidate-5 DDL
            # keep deterministic migration behaviour; production has the table
            # as a schema prerequisite and must fail closed.
            if self.__class__ is ProductionModelGovernanceRepository and type(self.authority).__name__ == "PostgresAuthority":
                raise
            return None
        return {"checkpoint_key": str(source_manifest_sha256), "payload_sha256": payload_hash}

    def migrate_legacy_research_store(self, store: Any) -> dict[str, Any]:
        """Run the one-time legacy migration outside normal runtime startup."""
        checkpoint = self.legacy_research_migration_status(store)
        if checkpoint.get("ok") is True and checkpoint.get("state") in {
            "NO_LEGACY_RESEARCH_ROWS", "LEGACY_RESEARCH_MIGRATION_CHECKPOINT_VERIFIED",
        }:
            return checkpoint
        conn = getattr(store, "conn", None)
        if conn is None or not callable(getattr(conn, "execute", None)):
            raise RuntimeError("LEGACY_RESEARCH_MIGRATION_SOURCE_UNAVAILABLE")

        def has_table(name: str) -> bool:
            return bool(conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
            ).fetchone())

        empty_counts = {"populations": 0, "members": 0, "predictions": 0, "outcomes": 0}
        if not has_table("candidate_populations"):
            return {
                "ok": True, "state": "NO_LEGACY_RESEARCH_ROWS", "authority": "GOVERNANCE_POSTGRESQL",
                "expected": dict(empty_counts), "verified": dict(empty_counts),
                "count_verified": True, "hash_verified": True, "quarantine_verified": True,
                "quarantine": {"entities": 0, "populations": 0, "outcomes": 0, "payload_sha256": _sha([])},
            }

        populations = [dict(row) for row in conn.execute(
            "SELECT * FROM candidate_populations ORDER BY observed_at,population_fingerprint"
        ).fetchall()]
        expected_counts = dict(empty_counts)
        expected_hashes: dict[str, list[str]] = {key: [] for key in expected_counts}
        quarantine_hashes: list[str] = []
        quarantine_counts = {"entities": 0, "populations": 0, "outcomes": 0}
        has_snapshots = has_table("quant_feature_snapshots")
        has_predictions = has_table("shadow_selector_predictions")
        has_outcomes = has_table("selector_candidate_outcomes")
        all_outcomes = [dict(row) for row in conn.execute(
            "SELECT * FROM selector_candidate_outcomes ORDER BY settled_at,candidate_id,horizon"
        ).fetchall()] if has_outcomes else []
        outcomes_by_population: dict[str, list[dict[str, Any]]] = {}
        for outcome in all_outcomes:
            outcomes_by_population.setdefault(str(outcome.get("population_fingerprint") or ""), []).append(outcome)

        migrated_fingerprints: list[str] = []
        migrated_candidate_ids: set[str] = set()
        legacy_population_fingerprints = {str(row.get("population_fingerprint") or "") for row in populations}

        for population in populations:
            fingerprint = str(population["population_fingerprint"])
            members = [dict(row) for row in conn.execute(
                "SELECT * FROM candidate_population_observations WHERE population_fingerprint=? ORDER BY candidate_id",
                (fingerprint,),
            ).fetchall()]
            predictions = [dict(row) for row in conn.execute(
                """SELECT * FROM shadow_selector_predictions
                    WHERE population_fingerprint=? AND arm IN ('heuristic','quant','hybrid')
                    ORDER BY candidate_id,arm""",
                (fingerprint,),
            ).fetchall()] if has_predictions else []
            population_outcomes = outcomes_by_population.get(fingerprint, [])
            quarantine_reason = self._legacy_population_quarantine_reason(population, members, predictions)
            if quarantine_reason:
                result = self.record_legacy_research_quarantine(
                    entity_type="POPULATION", legacy_key=fingerprint, reason=quarantine_reason,
                    payload={
                        "population": population, "members": members,
                        "predictions": predictions, "outcomes": population_outcomes,
                    },
                )
                quarantine_counts["entities"] += 1
                quarantine_counts["populations"] += 1
                quarantine_hashes.append(str(result["payload_sha256"]))
                continue

            for member in members:
                member["_legacy_feature_payload"] = True
                if not has_snapshots:
                    continue
                snapshot = conn.execute(
                    "SELECT * FROM quant_feature_snapshots WHERE candidate_id=?", (member["candidate_id"],)
                ).fetchone()
                if not snapshot:
                    continue
                raw = dict(snapshot)
                raw["features"] = _json_object(raw.pop("feature_json", None))
                raw["cost_assumptions"] = _json_object(raw.pop("cost_assumption_json", None))
                for source, target in (("missing_features_json", "missing_features"), ("lineage_missing_json", "lineage_missing")):
                    try:
                        raw[target] = json.loads(str(raw.pop(source, "[]") or "[]"))
                    except (TypeError, ValueError, json.JSONDecodeError):
                        raw[target] = []
                raw["freshness_eligible_for_training"] = (
                    str(raw.get("freshness_state") or "")
                    in ({"LIVE", "FRESH"} if str(raw.get("mode")) == "intraday" else {"LIVE", "FRESH", "CLOSED_MARKET", "VERIFIED_CLOSE"})
                )
                member["governance_feature_snapshot"] = raw

            record = {
                "population_fingerprint": fingerprint,
                "mode": population["mode"], "observed_at": population["observed_at"],
                "universe_id": population["universe_id"],
                "dataset_fingerprint": population["dataset_fingerprint"],
                "feature_manifest_hash": population["feature_manifest_hash"],
                "candidate_count": int(population["candidate_count"]),
                "policy_version": population["policy_version"],
            }
            result = self.record_selector_population(record, members)
            migrated_fingerprints.append(fingerprint)
            migrated_candidate_ids.update(str(member["candidate_id"]) for member in members)
            expected_counts["populations"] += 1
            expected_counts["members"] += len(members)
            expected_hashes["populations"].append(str(result["population_payload_sha256"]))
            expected_hashes["members"].append(str(result["member_payload_sha256"]))

            if predictions:
                for prediction in predictions:
                    # Preserve the exact retired forward-evidence v1 prediction
                    # material. Do not merge prediction_json into the source row:
                    # the old copier hashed top-level SQLite columns plus the parsed
                    # prediction_json object as a separate payload.
                    prediction["_legacy_prediction_payload_v1"] = True
                prediction_result = self.record_selector_predictions(
                    fingerprint, predictions, prediction_at=str(predictions[0]["created_at"]),
                )
                expected_counts["predictions"] += len(predictions)
                expected_hashes["predictions"].append(str(prediction_result["payload_sha256"]))

        for outcome in all_outcomes:
            fingerprint = str(outcome.get("population_fingerprint") or "")
            candidate_id = str(outcome.get("candidate_id") or "")
            if fingerprint not in migrated_fingerprints or candidate_id not in migrated_candidate_ids:
                # Outcomes belonging to a quarantined population are already
                # retained in that population envelope; only true orphans need
                # their own quarantine row.
                if fingerprint not in legacy_population_fingerprints:
                    legacy_key = f"{candidate_id}:{outcome.get('horizon') or ''}"
                    result = self.record_legacy_research_quarantine(
                        entity_type="OUTCOME", legacy_key=legacy_key,
                        reason="ORPHAN_OUTCOME_WITHOUT_CANONICAL_POPULATION", payload=outcome,
                    )
                    quarantine_counts["entities"] += 1
                    quarantine_counts["outcomes"] += 1
                    quarantine_hashes.append(str(result["payload_sha256"]))
                continue
            outcome["proof_payload"] = _json_object(outcome.get("proof_json"))
            result = self.record_selector_outcome(outcome)
            expected_counts["outcomes"] += 1
            expected_hashes["outcomes"].append(str(result["payload_sha256"]))

        if not migrated_fingerprints:
            verified_counts = dict(expected_counts)
            hash_verified = True
        else:
            verified = self.authority.execute(
                """SELECT
                     (SELECT count(*) FROM research.selector_populations WHERE population_fingerprint=ANY(%s))::bigint AS populations,
                     (SELECT count(*) FROM research.selector_population_members WHERE population_fingerprint=ANY(%s))::bigint AS members,
                     (SELECT count(*) FROM research.selector_arm_predictions WHERE population_fingerprint=ANY(%s))::bigint AS predictions,
                     (SELECT count(*) FROM research.selector_outcomes WHERE population_fingerprint=ANY(%s))::bigint AS outcomes""",
                (migrated_fingerprints, migrated_fingerprints, migrated_fingerprints, migrated_fingerprints),
                fetch="one", statement_timeout_ms=10000,
            ) or {}
            verified_counts = {key: int(verified.get(key) or 0) for key in expected_counts}
            hash_verified = all(expected_hashes[key] or expected_counts[key] == 0 for key in expected_counts)
        count_verified = verified_counts == expected_counts
        quarantine_verified = quarantine_counts["entities"] == len(quarantine_hashes)
        if not count_verified or not hash_verified or not quarantine_verified:
            raise RuntimeError(
                "LEGACY_RESEARCH_MIGRATION_VERIFICATION_FAILED:"
                f"{expected_counts}:{verified_counts}:{quarantine_counts}"
            )
        state = "LEGACY_RESEARCH_MIGRATED_AND_VERIFIED"
        if quarantine_counts["entities"]:
            state = "LEGACY_RESEARCH_MIGRATED_WITH_QUARANTINE_VERIFIED"
        quarantine = {
            **quarantine_counts,
            "payload_sha256": _sha(sorted(quarantine_hashes)),
            "policy": "INVALID_LEGACY_ROWS_RETAINED_NOT_PROMOTED_TO_CANONICAL_AUTHORITY",
        }
        source_manifest, source_manifest_hash = self._legacy_research_source_manifest(conn)
        checkpoint_result = self._record_legacy_research_migration_checkpoint(
            source_manifest=source_manifest, source_manifest_sha256=source_manifest_hash,
            expected=expected_counts, verified=verified_counts, quarantine=quarantine,
        )
        return {
            "ok": True, "state": state, "authority": "GOVERNANCE_POSTGRESQL",
            "expected": expected_counts, "verified": verified_counts,
            "count_verified": True, "hash_verified": True, "quarantine_verified": True,
            "content_manifest_sha256": _sha({key: sorted(values) for key, values in expected_hashes.items()}),
            "source_manifest": source_manifest, "source_manifest_sha256": source_manifest_hash,
            "checkpoint": checkpoint_result,
            "quarantine": quarantine,
        }

    def publish_training_bundle(self, bundle: Mapping[str, Any]) -> dict[str, Any]:
        """Persist one immutable shadow-training publication in governance PostgreSQL.

        Offline research never writes production assignments.  Backtest-approved
        candidates receive evaluation-paper weight only; production authority is
        granted later through the canonical experiment/promotion/assignment path.
        """
        payload = dict(bundle or {})
        model = dict(payload.get("model") or {})
        predictions = [dict(row or {}) for row in (payload.get("predictions") or [])]
        factor_decay = [dict(row or {}) for row in (payload.get("factor_decay") or [])]
        validation = dict(payload.get("validation") or {})
        capital_validation = dict(payload.get("capital_validation") or {})
        publication_id = str(payload["publication_id"])
        model_key = str(model.get("model_id") or model.get("model_key") or "").strip()
        if not model_key:
            raise ValueError("training publication model key is required")
        desk = str(model.get("desk") or (predictions[0].get("mode") if predictions else "delivery")).upper()
        if desk not in {"INTRADAY", "DELIVERY"}:
            raise ValueError("training publication desk must be INTRADAY or DELIVERY")
        horizon_value = int(model.get("horizon_days") or model.get("horizon_value") or 0)
        if horizon_value <= 0:
            raise ValueError("training publication horizon is required")
        source = str(model.get("training_data_source") or payload.get("training_data_source") or "PARQUET_DUCKDB").upper()
        if source != "PARQUET_DUCKDB":
            raise ValueError("new training publications require PARQUET_DUCKDB authority")
        canonical_payload = _json(payload)
        import hashlib
        payload_sha256 = hashlib.sha256(canonical_payload.encode("utf-8")).hexdigest()
        evaluation_weight = min(0.15, max(0.0, float(model.get("evaluation_paper_weight") or 0.0)))
        lifecycle = str(model.get("lifecycle_state") or "SHADOW").upper()
        if lifecycle not in {"SHADOW", "REJECTED", "RETIRED"}:
            lifecycle = "SHADOW"
        inserted_predictions = 0
        inserted_factors = 0
        with self.authority.transaction(isolation_level="serializable", statement_timeout_ms=15000) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO research.training_publications(
                           publication_id,model_key,model_version,desk,horizon_value,horizon_unit,
                           lifecycle_state,evaluation_paper_weight,production_weight,feature_schema_hash,
                           dataset_fingerprint,training_data_source,validation_state,validation_payload,
                           model_payload,payload_sha256,trained_through,artifact_uri)
                       VALUES(%s,%s,%s,%s,%s,'TRADING_DAY',%s,%s,0,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s,%s,%s)
                       ON CONFLICT(publication_id) DO NOTHING""",
                    (
                        publication_id, model_key, str(model.get("model_version") or "0"), desk,
                        horizon_value, lifecycle, evaluation_weight,
                        str(model.get("feature_manifest_hash") or model.get("feature_schema_hash") or ""),
                        str(model.get("dataset_fingerprint") or ""), source,
                        str(validation.get("status") or "UNVALIDATED").upper(), _json(validation),
                        _json(model), payload_sha256, model.get("trained_through"), model.get("artifact_uri"),
                    ),
                )
                cur.execute(
                    "SELECT payload_sha256 FROM research.training_publications WHERE publication_id=%s",
                    (publication_id,),
                )
                existing = cur.fetchone()
                if not existing or str(existing["payload_sha256"]) != payload_sha256:
                    raise RuntimeError("TRAINING_PUBLICATION_IDEMPOTENCY_CONFLICT")
                validation_profiles = []
                for profile, evidence in (("research", validation), ("capital", capital_validation)):
                    if not evidence or not evidence.get("approval_id") or not evidence.get("model_id"):
                        continue
                    evidence = dict(evidence)
                    evidence["validation_profile"] = str(evidence.get("validation_profile") or profile).lower()
                    if evidence["validation_profile"] != profile:
                        evidence["validation_profile"] = profile
                    material = {
                        "publication_id": publication_id,
                        "model_key": model_key,
                        "validation_profile": profile,
                        "approval_id": str(evidence["approval_id"]),
                        "status": str(evidence.get("status") or "REJECTED").upper(),
                        "lifecycle_state": str(evidence.get("lifecycle_state") or "SHADOW").upper(),
                        "validated_at": str(evidence.get("validated_at") or datetime.now(timezone.utc).isoformat()),
                        "payload": evidence,
                    }
                    validation_evidence_id = _sha(material)
                    validation_payload_hash = _sha(evidence)
                    cur.execute(
                        """INSERT INTO research.training_validation_evidence(
                               validation_evidence_id,publication_id,model_key,validation_profile,approval_id,
                               status,lifecycle_state,validated_at,payload_json,payload_sha256)
                           VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s)
                           ON CONFLICT(validation_evidence_id) DO NOTHING""",
                        (
                            validation_evidence_id, publication_id, model_key, profile,
                            str(evidence["approval_id"]), material["status"], material["lifecycle_state"],
                            material["validated_at"], _json(evidence), validation_payload_hash,
                        ),
                    )
                    cur.execute(
                        "SELECT payload_sha256 FROM research.training_validation_evidence WHERE validation_evidence_id=%s",
                        (validation_evidence_id,),
                    )
                    persisted_validation = cur.fetchone()
                    if not persisted_validation or str(persisted_validation["payload_sha256"]) != validation_payload_hash:
                        raise RuntimeError("TRAINING_VALIDATION_EVIDENCE_IDEMPOTENCY_CONFLICT")
                    validation_profiles.append(profile)
                for prediction in predictions:
                    cur.execute(
                        """INSERT INTO research.shadow_predictions(
                               prediction_id,publication_id,model_key,instrument_key,symbol,desk,as_of,
                               horizon_value,horizon_unit,predicted_rank,expected_excess_return,
                               calibrated_confidence,feature_schema_hash,dataset_fingerprint,payload_json)
                           VALUES(%s,%s,%s,%s,%s,%s,%s,%s,'TRADING_DAY',%s,%s,%s,%s,%s,%s::jsonb)
                           ON CONFLICT(prediction_id) DO NOTHING""",
                        (
                            str(prediction["prediction_id"]), publication_id, model_key,
                            prediction.get("instrument_key"), str(prediction.get("symbol") or "").upper(),
                            str(prediction.get("mode") or desk).upper(), prediction["as_of"],
                            int(prediction.get("horizon_days") or horizon_value),
                            float(prediction["rank_score"]), prediction.get("expected_excess_return"),
                            float(prediction["confidence"]), str(prediction["feature_manifest_hash"]),
                            str(prediction["dataset_fingerprint"]), _json(prediction),
                        ),
                    )
                    inserted_predictions += max(0, int(cur.rowcount or 0))
                for report in factor_decay:
                    factor_name = str(report.get("factor_name") or report.get("factor_id") or "").strip()
                    if not factor_name:
                        continue
                    cur.execute(
                        """INSERT INTO research.factor_decay_observations(
                               observation_id,publication_id,factor_name,measured_at,status,payload_json)
                           VALUES(%s,%s,%s,%s,%s,%s::jsonb)
                           ON CONFLICT(publication_id,factor_name,measured_at) DO NOTHING""",
                        (
                            str(uuid4()), publication_id, factor_name,
                            str(report.get("measured_at") or datetime.now(timezone.utc).isoformat()),
                            str(report.get("status") or "INSUFFICIENT_DATA").upper(), _json(report),
                        ),
                    )
                    inserted_factors += max(0, int(cur.rowcount or 0))
                cur.execute(
                    """INSERT INTO research.training_publication_events(
                           event_id,publication_id,event_type,payload_json)
                       VALUES(%s,%s,'TRAINING_BUNDLE_PUBLISHED',%s::jsonb)""",
                    (str(uuid4()), publication_id, _json({
                        "model_key": model_key,
                        "prediction_count": len(predictions),
                        "factor_report_count": len(factor_decay),
                        "production_weight": 0.0,
                        "evaluation_paper_weight": evaluation_weight,
                    })),
                )
        return {
            "ok": True,
            "state": "POSTGRES_SHADOW_PUBLICATION_COMMITTED",
            "publication_id": publication_id,
            "model_id": model_key,
            "lifecycle_state": lifecycle,
            "evaluation_paper_weight": evaluation_weight,
            "production_weight": 0.0,
            "predictions": len(predictions),
            "new_predictions": inserted_predictions,
            "factor_reports": len(factor_decay),
            "new_factor_reports": inserted_factors,
            "validation_profiles": validation_profiles,
            "capital_validation_persisted": "capital" in validation_profiles,
            "authority": "GOVERNANCE_POSTGRESQL",
        }


    def training_validation_evidence(self, *, model_key: str = "", profile: str = "", limit: int = 20) -> dict[str, Any]:
        """Read immutable trainer/WFA evidence from governance PostgreSQL.

        Historical capital WFA is intentionally separate from prospective selector
        populations/outcomes.  This read never interprets forward selector depth as
        historical backtest depth.
        """
        where = []
        params: list[Any] = []
        if model_key:
            where.append("model_key=%s"); params.append(str(model_key))
        if profile:
            where.append("validation_profile=%s"); params.append(str(profile).lower())
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        params.append(max(1, min(100, int(limit))))
        rows = self.authority.execute(
            f"""SELECT validation_evidence_id,publication_id,model_key,validation_profile,approval_id,
                       status,lifecycle_state,validated_at,payload_json,payload_sha256,created_at
                  FROM research.training_validation_evidence{clause}
                 ORDER BY validated_at DESC,created_at DESC LIMIT %s""",
            tuple(params), fetch="all", statement_timeout_ms=5000, pool_timeout_seconds=5.0,
        ) or []
        evidence = []
        for raw in rows:
            row = dict(raw)
            payload = _json_object(row.get("payload_json"))
            payload.update({
                "validation_evidence_id": row.get("validation_evidence_id"),
                "publication_id": row.get("publication_id"),
                "model_key": row.get("model_key"),
                "validation_profile": row.get("validation_profile"),
                "approval_id": row.get("approval_id"),
                "status": row.get("status"),
                "lifecycle_state": row.get("lifecycle_state"),
                "validated_at": str(row.get("validated_at")),
                "authority": "GOVERNANCE_POSTGRESQL",
            })
            evidence.append(payload)
        return {
            "ok": True,
            "authority": "GOVERNANCE_POSTGRESQL",
            "model_key": str(model_key),
            "profile": str(profile).lower(),
            "evidence": evidence,
        }

    def training_publication_status(self) -> dict[str, Any]:
        counts = self.authority.execute(
            """SELECT
                   (SELECT count(*) FROM research.training_publications) AS publications,
                   (SELECT count(*) FROM research.shadow_predictions) AS shadow_predictions,
                   (SELECT count(*) FROM research.shadow_predictions WHERE settled_outcome_id IS NULL) AS unsettled_predictions""",
            fetch="one",
        ) or {}
        latest = self.authority.execute(
            """SELECT publication_id,model_key,desk,lifecycle_state,evaluation_paper_weight,
                      production_weight,training_data_source,validation_state,validation_payload,
                      model_payload,created_at
                 FROM research.training_publications ORDER BY created_at DESC LIMIT 1""",
            fetch="one",
        )
        latest_rows = self.authority.execute(
            """SELECT DISTINCT ON (desk)
                      publication_id,model_key,desk,lifecycle_state,evaluation_paper_weight,
                      production_weight,training_data_source,validation_state,validation_payload,
                      model_payload,created_at
                 FROM research.training_publications
                ORDER BY desk,created_at DESC""",
            fetch="all",
        ) or []
        latest_by_desk = {
            str(row.get("desk") or "").lower(): dict(row)
            for row in latest_rows if row.get("desk")
        }
        return {
            "ok": True,
            "state": "READY",
            "authority": "GOVERNANCE_POSTGRESQL",
            "publications": int(counts.get("publications") or 0),
            "shadow_predictions": int(counts.get("shadow_predictions") or 0),
            "unsettled_predictions": int(counts.get("unsettled_predictions") or 0),
            "latest": dict(latest) if latest else None,
            "latest_by_desk": latest_by_desk,
            "production_weight_policy": "GOVERNED_ACTIVE_CAP_15_WITH_DETERMINISTIC_FALLBACK",
        }

    def register_model(self, record: Mapping[str, Any]) -> str:
        row = dict(record)
        model_id = _uuid(row.get("model_id"))
        self.authority.execute(
            """INSERT INTO model_registry.models(
                   model_id,model_key,model_version,desk,setup_family,horizon_value,horizon_unit,
                   model_type,artifact_uri,artifact_sha256,feature_schema_hash,label_definition_version,
                   training_data_manifest_uri,training_window_start,training_window_end,code_revision)
               VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT(model_key,model_version) DO NOTHING""",
            (
                model_id, row["model_key"], row["model_version"], str(row["desk"]).upper(),
                row["setup_family"], int(row["horizon_value"]), str(row["horizon_unit"]).upper(),
                row["model_type"], row["artifact_uri"], row["artifact_sha256"],
                row["feature_schema_hash"], row["label_definition_version"],
                row["training_data_manifest_uri"], row["training_window_start"],
                row["training_window_end"], row["code_revision"],
            ),
        )
        existing = self.authority.execute(
            "SELECT model_id FROM model_registry.models WHERE model_key=%s AND model_version=%s",
            (row["model_key"], row["model_version"]), fetch="one",
        )
        return str(existing["model_id"])

    def create_population(self, record: Mapping[str, Any]) -> str:
        row = dict(record)
        population_id = _uuid(row.get("population_id"))
        self.authority.execute(
            """INSERT INTO research.ranking_populations(
                   population_id,desk,setup_family,as_of,universe_revision,population_definition,
                   member_count,population_sha256)
               VALUES(%s,%s,%s,%s,%s,%s::jsonb,%s,%s)
               ON CONFLICT(population_sha256) DO NOTHING""",
            (population_id, str(row["desk"]).upper(), row["setup_family"], row["as_of"],
             row["universe_revision"], _json(row["population_definition"]), int(row["member_count"]),
             row["population_sha256"]),
        )
        existing = self.authority.execute(
            "SELECT population_id FROM research.ranking_populations WHERE population_sha256=%s",
            (row["population_sha256"],), fetch="one",
        )
        return str(existing["population_id"])

    def create_feature_snapshot(self, record: Mapping[str, Any]) -> str:
        row = dict(record)
        snapshot_id = _uuid(row.get("feature_snapshot_id"))
        self.authority.execute(
            """INSERT INTO research.feature_snapshots(
                   feature_snapshot_id,population_id,as_of,data_cutoff_at,feature_schema_hash,
                   source_manifest_uri,source_manifest_sha256,market_data_quality)
               VALUES(%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT(feature_snapshot_id) DO NOTHING""",
            (snapshot_id, row["population_id"], row["as_of"], row["data_cutoff_at"],
             row["feature_schema_hash"], row["source_manifest_uri"], row["source_manifest_sha256"],
             str(row["market_data_quality"]).upper()),
        )
        return snapshot_id

    def freeze_prediction(self, record: Mapping[str, Any]) -> str:
        row = dict(record)
        prediction_id = _uuid(row.get("prediction_id"))
        columns = (
            "prediction_id,prediction_key,model_id,population_id,feature_snapshot_id,instrument_key,"
            "as_of,data_cutoff_at,cost_model_version,return_basis,effective_sample_size,"
            "net_return_standard_error,uncertainty_method,calibration_model_id,predicted_rank,"
            "predicted_percentile,target_before_stop_probability,stop_before_target_probability,"
            "neither_probability,calibrated_confidence,observation_price,"
            "target_price,stop_price,horizon_end_at,label_parameters,return_q05,return_q50,return_q95,"
            "mae_q50,mfe_q50,expected_time_to_target,expected_time_to_stop,uncertainty_lower,"
            "uncertainty_upper,regime_observation_id"
        )
        values = (
            prediction_id, row["prediction_key"], row["model_id"], row["population_id"],
            row["feature_snapshot_id"], row["instrument_key"], row["as_of"], row["data_cutoff_at"],
            row["cost_model_version"], str(row["return_basis"]).upper(),
            int(row["effective_sample_size"]), row["net_return_standard_error"],
            str(row["uncertainty_method"]).upper(), row.get("calibration_model_id"), row.get("predicted_rank"),
            row.get("predicted_percentile"), row.get("target_before_stop_probability"),
            row.get("stop_before_target_probability"), row.get("neither_probability"),
            row.get("calibrated_confidence"), row.get("observation_price"), row.get("target_price"),
            row.get("stop_price"), row.get("horizon_end_at"), _json(row.get("label_parameters")),
            row.get("return_q05"), row.get("return_q50"), row.get("return_q95"), row.get("mae_q50"),
            row.get("mfe_q50"), row.get("expected_time_to_target"), row.get("expected_time_to_stop"),
            row.get("uncertainty_lower"), row.get("uncertainty_upper"), row.get("regime_observation_id"),
        )
        # label_parameters is the twenty-fifth inserted value (index 24); cast it
        # explicitly so both psycopg text and native JSON adapters are safe.
        placeholder_items = ["%s"] * len(values)
        placeholder_items[24] = "%s::jsonb"
        placeholders = ",".join(placeholder_items)
        self.authority.execute(
            f"INSERT INTO research.predictions({columns}) VALUES({placeholders}) ON CONFLICT(prediction_key) DO NOTHING",
            values,
        )
        existing = self.authority.execute(
            "SELECT prediction_id FROM research.predictions WHERE prediction_key=%s",
            (row["prediction_key"],), fetch="one",
        )
        return str(existing["prediction_id"])

    def settle_prediction(self, record: Mapping[str, Any]) -> str:
        row = dict(record)
        outcome_id = _uuid(row.get("outcome_id"))
        self.authority.execute(
            """INSERT INTO research.prediction_outcomes(
                   outcome_id,prediction_id,outcome_class,realised_return_gross,realised_return_net,
                   mae,mfe,time_to_target_seconds,time_to_stop_seconds,holding_seconds,slippage_bps,
                   costs,exit_reason,outcome_quality,label_definition_version,settled_at)
               VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s)
               ON CONFLICT(prediction_id) DO NOTHING""",
            (outcome_id, row["prediction_id"], str(row["outcome_class"]).upper(),
             row.get("realised_return_gross"), row.get("realised_return_net"), row.get("mae"),
             row.get("mfe"), row.get("time_to_target_seconds"), row.get("time_to_stop_seconds"),
             int(row["holding_seconds"]), row.get("slippage_bps"), _json(row.get("costs")),
             row["exit_reason"], str(row["outcome_quality"]).upper(), row["label_definition_version"],
             row["settled_at"]),
        )
        return outcome_id

    def create_experiment(self, record: Mapping[str, Any], prediction_ids: Iterable[str]) -> str:
        row = dict(record)
        experiment_id = _uuid(row.get("experiment_id"))
        ids = sorted({_uuid(value) for value in prediction_ids})
        if not ids:
            raise ValueError("experiment requires frozen prediction IDs")
        with self.authority.transaction(isolation_level="serializable", statement_timeout_ms=10000) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO research.experiments(
                           experiment_id,experiment_key,model_id,population_manifest_uri,
                           population_manifest_sha256,validation_method,cost_model_version,
                           multiple_testing_method,periods_per_year,top_fraction,requested_production_weight,status,started_at,completed_at,lineage_complete,
                           leakage_checks_passed,point_in_time_universe_passed,survivorship_control_passed,
                           corporate_action_control_passed,multiple_testing_passed,baseline_comparison_passed,
                           cost_model_verified,seed_stability_passed,ablation_passed,
                           forward_days,forward_samples,evidence_manifest)
                       VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
                       ON CONFLICT(experiment_key) DO NOTHING""",
                    (experiment_id, row["experiment_key"], row["model_id"], row["population_manifest_uri"],
                     row["population_manifest_sha256"], str(row["validation_method"]).upper(),
                     row["cost_model_version"], row["multiple_testing_method"],
                     float(row["periods_per_year"]), float(row.get("top_fraction") or 0.20),
                     min(0.15, max(0.000001, float(row.get("requested_production_weight") or 0.10))),
                     str(row.get("status") or "RUNNING").upper(), row["started_at"], row.get("completed_at"),
                     bool(row.get("lineage_complete")), bool(row.get("leakage_checks_passed")),
                     bool(row.get("point_in_time_universe_passed")), bool(row.get("survivorship_control_passed")),
                     bool(row.get("corporate_action_control_passed")), bool(row.get("multiple_testing_passed")),
                     bool(row.get("baseline_comparison_passed")), bool(row.get("cost_model_verified")),
                     bool(row.get("seed_stability_passed")), bool(row.get("ablation_passed")), int(row.get("forward_days") or 0),
                     int(row.get("forward_samples") or 0), _json(row.get("evidence_manifest"))),
                )
                cur.execute("SELECT experiment_id FROM research.experiments WHERE experiment_key=%s", (row["experiment_key"],))
                experiment_id = str(cur.fetchone()["experiment_id"])
                cur.executemany(
                    "INSERT INTO research.experiment_predictions(experiment_id,prediction_id) VALUES(%s,%s) ON CONFLICT DO NOTHING",
                    [(experiment_id, value) for value in ids],
                )
        return experiment_id

    def recent_model_efficacy(
        self, *, model_id: str, desk: str, limit_populations: int = 60
    ) -> dict[str, Any]:
        """Measure matured cross-sectional efficacy and drift for one governed model.

        Only VERIFIED settled outcomes are used.  The returned aggregate covers
        the most recent ``limit_populations`` immutable ranking populations; an
        equally sized prior window is read only for drift comparison.  Regime
        metrics and calibration drift therefore use information that had fully
        matured before this health query.  This method is read-only and cannot
        retrain, reweight or promote a model.
        """
        desk_key = str(desk or "").strip().upper()
        if desk_key not in {"INTRADAY", "DELIVERY"}:
            raise ValueError("desk must be INTRADAY or DELIVERY")
        population_limit = max(3, min(int(limit_populations), 250))
        comparison_limit = min(population_limit * 2, 500)
        rows = self.authority.execute(
            """WITH recent_populations AS (
                   SELECT p.population_id,MAX(rp.as_of) AS population_as_of
                     FROM research.predictions p
                     JOIN research.ranking_populations rp ON rp.population_id=p.population_id
                     JOIN research.prediction_outcomes o ON o.prediction_id=p.prediction_id
                    WHERE p.model_id=%s AND rp.desk=%s AND o.outcome_quality='VERIFIED'
                    GROUP BY p.population_id
                    ORDER BY MAX(rp.as_of) DESC
                    LIMIT %s
               )
               SELECT p.population_id,recent.population_as_of,p.predicted_rank,p.predicted_percentile,
                      p.target_before_stop_probability,o.outcome_class,o.realised_return_net,
                      o.settled_at,COALESCE(r.chosen_label,'UNKNOWN') AS regime_label,
                      0::numeric AS turnover,0::numeric AS capacity_inr
                 FROM recent_populations recent
                 JOIN research.predictions p ON p.population_id=recent.population_id
                 JOIN research.prediction_outcomes o ON o.prediction_id=p.prediction_id
                 LEFT JOIN research.regime_observations r ON r.regime_observation_id=p.regime_observation_id
                WHERE p.model_id=%s AND o.outcome_quality='VERIFIED'
                ORDER BY recent.population_as_of DESC,p.population_id,p.predicted_rank,p.prediction_id""",
            (str(model_id), desk_key, comparison_limit, str(model_id)),
            fetch="all", statement_timeout_ms=10000,
        ) or []
        all_data = [dict(row) for row in rows]
        if not all_data:
            return {
                "state": "INSUFFICIENT_EVIDENCE", "model_id": str(model_id),
                "desk": desk_key, "sample_size": 0, "population_count": 0,
                "rank_ic": None, "ndcg": None, "net_expectancy": None,
                "calibration_error": None, "regime_metrics": [],
                "window_drift": {"state": "INSUFFICIENT_PRIOR_WINDOW"},
            }

        ordered_populations: list[str] = []
        for row in all_data:
            population_id = str(row.get("population_id") or "")
            if population_id and population_id not in ordered_populations:
                ordered_populations.append(population_id)
        recent_ids = set(ordered_populations[:population_limit])
        prior_ids = set(ordered_populations[population_limit: population_limit * 2])
        data = [row for row in all_data if str(row.get("population_id") or "") in recent_ids]
        prior_data = [row for row in all_data if str(row.get("population_id") or "") in prior_ids]
        if not data:
            return {
                "state": "INSUFFICIENT_EVIDENCE", "model_id": str(model_id),
                "desk": desk_key, "sample_size": 0, "population_count": 0,
                "rank_ic": None, "ndcg": None, "net_expectancy": None,
                "calibration_error": None, "regime_metrics": [],
                "window_drift": {"state": "INSUFFICIENT_RECENT_WINDOW"},
            }

        metric = evaluate_prediction_rows(data, regime_label="ALL", top_fraction=0.20)
        settled = [str(row.get("settled_at") or "") for row in data if row.get("settled_at")]
        regime_metrics: list[dict[str, Any]] = []
        labels = sorted({str(row.get("regime_label") or "UNKNOWN").upper() for row in data})
        for label in labels:
            members = [row for row in data if str(row.get("regime_label") or "UNKNOWN").upper() == label]
            if not members:
                continue
            regime_metrics.append(evaluate_prediction_rows(members, regime_label=label, top_fraction=0.20).as_dict())

        if prior_data:
            prior_metric = evaluate_prediction_rows(prior_data, regime_label="ALL", top_fraction=0.20)
            window_drift = {
                "state": "MEASURED",
                "recent_population_count": metric.population_count,
                "prior_population_count": prior_metric.population_count,
                "recent": {
                    "rank_ic": metric.rank_ic, "ndcg": metric.ndcg,
                    "calibration_error": metric.calibration_error, "net_expectancy": metric.net_expectancy,
                },
                "prior": {
                    "rank_ic": prior_metric.rank_ic, "ndcg": prior_metric.ndcg,
                    "calibration_error": prior_metric.calibration_error, "net_expectancy": prior_metric.net_expectancy,
                },
                "delta_rank_ic": metric.rank_ic - prior_metric.rank_ic,
                "delta_ndcg": metric.ndcg - prior_metric.ndcg,
                "delta_calibration_error": metric.calibration_error - prior_metric.calibration_error,
                "delta_net_expectancy": metric.net_expectancy - prior_metric.net_expectancy,
                "interpretation": "recent minus prior; positive calibration-error delta is deterioration",
            }
        else:
            window_drift = {
                "state": "INSUFFICIENT_PRIOR_WINDOW",
                "recent_population_count": metric.population_count,
                "prior_population_count": 0,
            }

        return {
            "state": "MEASURED",
            "model_id": str(model_id),
            "desk": desk_key,
            "sample_size": metric.sample_size,
            "population_count": metric.population_count,
            "rank_ic": metric.rank_ic,
            "ndcg": metric.ndcg,
            "net_expectancy": metric.net_expectancy,
            "calibration_error": metric.calibration_error,
            "regime_metrics": regime_metrics,
            "window_drift": window_drift,
            "latest_settled_at": max(settled) if settled else None,
            "population_limit": population_limit,
            "comparison_population_limit": comparison_limit,
            "authority": "GOVERNANCE_POSTGRES_VERIFIED_SETTLED_OUTCOMES",
        }

    def _settled_rows(self, experiment_id: str) -> list[dict[str, Any]]:
        rows = self.authority.execute(
            """SELECT p.population_id,p.predicted_rank,p.predicted_percentile,p.target_before_stop_probability,
                      o.outcome_class,o.realised_return_net,
                      COALESCE(r.chosen_label,'UNKNOWN') AS regime_label,
                      0::numeric AS turnover,0::numeric AS capacity_inr
                 FROM research.experiment_predictions ep
                 JOIN research.predictions p ON p.prediction_id=ep.prediction_id
                 JOIN research.prediction_outcomes o ON o.prediction_id=p.prediction_id
                 LEFT JOIN research.regime_observations r ON r.regime_observation_id=p.regime_observation_id
                WHERE ep.experiment_id=%s AND o.outcome_quality='VERIFIED'""",
            (experiment_id,), fetch="all", statement_timeout_ms=10000,
        )
        return [dict(row) for row in rows]

    @staticmethod
    def _all_metric(metrics: Iterable[Mapping[str, Any]]) -> dict[str, Any] | None:
        for row in metrics:
            item = dict(row)
            if (str(item.get("regime_label") or "").upper() == "ALL"
                    and str(item.get("liquidity_band") or "ALL").upper() == "ALL"
                    and str(item.get("market_cap_band") or "ALL").upper() == "ALL"):
                return item
        return None

    def _active_scope_assignment(self, model: Mapping[str, Any]) -> dict[str, Any] | None:
        row = self.authority.execute(
            """SELECT a.*,m.model_key,m.model_version,pd.experiment_id
                 FROM deployment.assignments a
                 JOIN model_registry.models m ON m.model_id=a.model_id
                 JOIN deployment.promotion_decisions pd ON pd.promotion_decision_id=a.promotion_decision_id
                WHERE a.desk=%s AND a.setup_family=%s AND a.horizon_value=%s AND a.horizon_unit=%s
                  AND a.role='CHAMPION' AND a.effective_from<=clock_timestamp()
                  AND (a.effective_to IS NULL OR a.effective_to>clock_timestamp())
                ORDER BY a.effective_from DESC LIMIT 1""",
            (model["desk"], model["setup_family"], model["horizon_value"], model["horizon_unit"]),
            fetch="one", statement_timeout_ms=1500,
        )
        return dict(row) if row else None

    def _experiment_all_metric(self, experiment_id: str) -> dict[str, Any] | None:
        row = self.authority.execute(
            """SELECT * FROM research.experiment_metrics
                WHERE experiment_id=%s AND regime_label='ALL'
                  AND liquidity_band='ALL' AND market_cap_band='ALL'
                ORDER BY computed_at DESC LIMIT 1""",
            (experiment_id,), fetch="one", statement_timeout_ms=1500,
        )
        return dict(row) if row else None

    @staticmethod
    def _challenger_beats_champion(challenger: Mapping[str, Any], champion: Mapping[str, Any]) -> tuple[bool, list[str]]:
        blockers: list[str] = []
        # Net-return fields are decimals. Require at least five basis points of
        # incremental lower-confidence expectancy and no regression in ranking.
        if float(challenger.get("lower_confidence_net_expectancy") or 0.0) <= float(champion.get("lower_confidence_net_expectancy") or 0.0) + 0.0005:
            blockers.append("CHAMPION_INCREMENTAL_LCB_NOT_BEATEN")
        if float(challenger.get("rank_ic") or 0.0) < float(champion.get("rank_ic") or 0.0):
            blockers.append("CHAMPION_RANK_IC_NOT_BEATEN")
        if float(challenger.get("ndcg") or 0.0) < float(champion.get("ndcg") or 0.0):
            blockers.append("CHAMPION_NDCG_NOT_BEATEN")
        if abs(float(challenger.get("max_drawdown") or 1.0)) > abs(float(champion.get("max_drawdown") or 1.0)) + 0.02:
            blockers.append("CHAMPION_DRAWDOWN_WORSE")
        if abs(float(challenger.get("cvar_95") or 1.0)) > abs(float(champion.get("cvar_95") or 1.0)) + 0.01:
            blockers.append("CHAMPION_CVAR_WORSE")
        return not blockers, blockers

    def evaluate_experiment(self, experiment_id: str, *, promotion_gate: PromotionGate | None = None) -> dict[str, Any]:
        experiment = self.authority.execute(
            "SELECT * FROM research.experiments WHERE experiment_id=%s", (experiment_id,), fetch="one"
        )
        if not experiment:
            raise KeyError(f"experiment not found: {experiment_id}")
        rows = self._settled_rows(experiment_id)
        counts = self.authority.execute(
            """SELECT count(*)::bigint AS expected,
                      count(o.outcome_id) FILTER (WHERE o.outcome_quality='VERIFIED')::bigint AS verified,
                      count(o.outcome_id) FILTER (WHERE o.outcome_quality IN ('PARTIAL','REJECTED'))::bigint AS invalid
                 FROM research.experiment_predictions ep
                 LEFT JOIN research.prediction_outcomes o ON o.prediction_id=ep.prediction_id
                WHERE ep.experiment_id=%s""",
            (experiment_id,), fetch="one", statement_timeout_ms=5000,
        ) or {}
        expected = int(counts.get("expected") or 0)
        verified = int(counts.get("verified") or 0)
        invalid = int(counts.get("invalid") or 0)
        if expected <= 0 or verified + invalid < expected:
            return {"ok": False, "state": "WAITING_FOR_ALL_OUTCOMES", "experiment_id": experiment_id,
                    "expected": expected, "verified": verified, "invalid": invalid}
        metrics = evaluate_regime_strata(
            rows,
            periods_per_year=float(experiment["periods_per_year"]),
            top_fraction=float(experiment["top_fraction"]),
        )
        if not metrics:
            return {"ok": False, "state": "WAITING_FOR_SETTLED_OUTCOMES", "experiment_id": experiment_id}
        with self.authority.transaction(isolation_level="serializable", statement_timeout_ms=10000) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (f"model-experiment:{experiment_id}",))
                for metric in metrics:
                    m = metric.as_dict()
                    cur.execute(
                        """INSERT INTO research.experiment_metrics(
                               metric_id,experiment_id,regime_label,liquidity_band,market_cap_band,
                               sample_size,population_count,rank_ic,ndcg,brier_score,calibration_error,net_expectancy,
                               sharpe,sortino,max_drawdown,cvar_95,turnover,capacity_inr,
                               lower_confidence_net_expectancy)
                           VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                           ON CONFLICT(experiment_id,fold_key,regime_label,liquidity_band,market_cap_band)
                           DO UPDATE SET sample_size=EXCLUDED.sample_size,population_count=EXCLUDED.population_count,rank_ic=EXCLUDED.rank_ic,
                               ndcg=EXCLUDED.ndcg,brier_score=EXCLUDED.brier_score,
                               calibration_error=EXCLUDED.calibration_error,net_expectancy=EXCLUDED.net_expectancy,
                               sharpe=EXCLUDED.sharpe,sortino=EXCLUDED.sortino,max_drawdown=EXCLUDED.max_drawdown,
                               cvar_95=EXCLUDED.cvar_95,turnover=EXCLUDED.turnover,capacity_inr=EXCLUDED.capacity_inr,
                               lower_confidence_net_expectancy=EXCLUDED.lower_confidence_net_expectancy,
                               computed_at=clock_timestamp()""",
                        (str(uuid4()), experiment_id, m["regime_label"], m["liquidity_band"], m["market_cap_band"],
                         m["sample_size"], m["population_count"], m["rank_ic"], m["ndcg"], m["brier_score"], m["calibration_error"],
                         m["net_expectancy"], m["sharpe"], m["sortino"], m["max_drawdown"], m["cvar_95"],
                         m["turnover"], m["capacity_inr"], m["lower_confidence_net_expectancy"]),
                    )
                cur.execute(
                    "UPDATE research.experiments SET status='COMPLETED',completed_at=COALESCE(completed_at,clock_timestamp()),forward_samples=%s WHERE experiment_id=%s",
                    (len(rows), experiment_id),
                )
        null_alpha = permutation_null_alpha_test(
            rows,
            top_fraction=float(experiment["top_fraction"]),
            permutations=500,
            alpha=0.05,
            seed_material=f"{experiment_id}:{experiment.get('model_id')}:{experiment.get('decision_key')}",
        )
        gate = (promotion_gate or PromotionGate()).evaluate(
            [metric.as_dict() for metric in metrics],
            validation_method=str(experiment["validation_method"]),
            forward_days=int(experiment.get("forward_days") or 0),
            forward_samples=len(rows),
            lineage_complete=bool(experiment.get("lineage_complete")),
            leakage_checks_passed=bool(experiment.get("leakage_checks_passed")),
            point_in_time_universe_passed=bool(experiment.get("point_in_time_universe_passed")),
            survivorship_control_passed=bool(experiment.get("survivorship_control_passed")),
            corporate_action_control_passed=bool(experiment.get("corporate_action_control_passed")),
            multiple_testing_passed=bool(experiment.get("multiple_testing_passed")),
            baseline_comparison_passed=bool(experiment.get("baseline_comparison_passed")),
            cost_model_verified=bool(experiment.get("cost_model_verified")),
            seed_stability_passed=bool(experiment.get("seed_stability_passed")),
            ablation_passed=bool(experiment.get("ablation_passed")),
            null_alpha_test_passed=bool(null_alpha.get("passed")),
        )
        gate = {**gate, "null_alpha_falsification": null_alpha}
        metric_rows = [metric.as_dict() for metric in metrics]
        model = self.authority.execute(
            "SELECT * FROM model_registry.models WHERE model_id=%s",
            (experiment["model_id"],), fetch="one", statement_timeout_ms=1500,
        )
        if not model:
            raise RuntimeError("MODEL_REGISTRY_ROW_MISSING")
        model = dict(model)
        current = self._active_scope_assignment(model)
        final_decision = str(gate["decision"])
        comparison_blockers: list[str] = []
        if gate.get("eligible"):
            if current is None or str(current.get("model_id")) == str(model["model_id"]):
                final_decision = "PROMOTED_CHAMPION"
            else:
                challenger_all = self._all_metric(metric_rows)
                champion_all = self._experiment_all_metric(str(current["experiment_id"]))
                if challenger_all is None or champion_all is None:
                    comparison_blockers.append("CHAMPION_COMPARISON_EVIDENCE_MISSING")
                    final_decision = "PROMOTED_CHALLENGER"
                else:
                    wins, comparison_blockers = self._challenger_beats_champion(challenger_all, champion_all)
                    final_decision = "PROMOTED_CHAMPION" if wins else "PROMOTED_CHALLENGER"
        gate = {**gate, "decision": final_decision, "champion_comparison_blockers": comparison_blockers}
        decision_key = f"{experiment_id}:{final_decision}:regime-stratified-promotion-gate-v68"
        decision_id = str(uuid4())
        assignment_id: str | None = None
        scope_key = f"{model['desk']}:{model['setup_family']}:{model['horizon_value']}:{model['horizon_unit']}"
        with self.authority.transaction(isolation_level="serializable", statement_timeout_ms=10000) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (f"model-assignment:{scope_key}",))
                cur.execute(
                    """INSERT INTO deployment.promotion_decisions(
                           promotion_decision_id,decision_key,model_id,experiment_id,decision,
                           promotion_rule_version,gate_results,reason,decided_by)
                       VALUES(%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s)
                       ON CONFLICT(decision_key) DO NOTHING""",
                    (decision_id, decision_key, experiment["model_id"], experiment_id, final_decision,
                     "regime-stratified-promotion-gate-v68", _json(gate),
                     "; ".join(gate["blockers"] + comparison_blockers) if (gate["blockers"] or comparison_blockers) else "all promotion and champion-comparison gates passed",
                     "automatic-governance-cycle-v68"),
                )
                cur.execute("SELECT promotion_decision_id FROM deployment.promotion_decisions WHERE decision_key=%s", (decision_key,))
                decision_id = str(cur.fetchone()["promotion_decision_id"])
                if final_decision == "PROMOTED_CHAMPION":
                    cur.execute(
                        """SELECT assignment_id,model_id FROM deployment.assignments
                            WHERE desk=%s AND setup_family=%s AND horizon_value=%s AND horizon_unit=%s
                              AND role='CHAMPION' AND effective_to IS NULL
                            FOR UPDATE""",
                        (model["desk"], model["setup_family"], model["horizon_value"], model["horizon_unit"]),
                    )
                    previous = cur.fetchone()
                    if previous and str(previous["model_id"]) == str(model["model_id"]):
                        assignment_id = str(previous["assignment_id"])
                    else:
                        if previous:
                            cur.execute(
                                "UPDATE deployment.assignments SET effective_to=clock_timestamp() WHERE assignment_id=%s",
                                (previous["assignment_id"],),
                            )
                        assignment_id = str(uuid4())
                        cur.execute(
                            """INSERT INTO deployment.assignments(
                                   assignment_id,model_id,desk,setup_family,horizon_value,horizon_unit,role,
                                   production_weight,effective_from,rollback_assignment_id,promotion_decision_id)
                               VALUES(%s,%s,%s,%s,%s,%s,'CHAMPION',%s,clock_timestamp(),%s,%s)""",
                            (assignment_id, model["model_id"], model["desk"], model["setup_family"],
                             model["horizon_value"], model["horizon_unit"],
                             min(0.15, float(experiment.get("requested_production_weight") or 0.10)),
                             previous["assignment_id"] if previous else None, decision_id),
                        )
        return {
            "ok": True,
            "state": "EVALUATED",
            "experiment_id": experiment_id,
            "settled_samples": len(rows),
            "metrics": metric_rows,
            "promotion": gate,
            "promotion_decision_id": decision_id,
            "assignment_id": assignment_id,
        }

    def evaluate_ready_experiments(self, limit: int = 20) -> list[dict[str, Any]]:
        experiments = self.authority.execute(
            """SELECT e.experiment_id
                 FROM research.experiments e
                WHERE e.status IN ('RUNNING','COMPLETED')
                  AND EXISTS(SELECT 1 FROM research.experiment_predictions ep WHERE ep.experiment_id=e.experiment_id)
                  AND NOT EXISTS(SELECT 1 FROM research.experiment_predictions ep
                                 LEFT JOIN research.prediction_outcomes o ON o.prediction_id=ep.prediction_id
                                WHERE ep.experiment_id=e.experiment_id AND o.outcome_id IS NULL)
                ORDER BY e.started_at LIMIT %s""",
            (max(1, min(int(limit), 100)),), fetch="all", statement_timeout_ms=10000,
        )
        return [self.evaluate_experiment(str(row["experiment_id"])) for row in experiments]

    def active_assignment_for_model(
        self,
        *,
        model_key: str,
        desk: str,
        model_version: str | None = None,
        horizon_value: int | None = None,
        horizon_unit: str | None = None,
    ) -> dict[str, Any] | None:
        """Return the exact active champion assignment or ``None``.

        This is the sole production-weight authority. Legacy SQLite lifecycle
        labels are intentionally ignored by this query.
        """
        clauses = [
            "m.model_key=%s",
            "m.desk=%s",
            "a.role='CHAMPION'",
            "a.effective_from<=clock_timestamp()",
            "(a.effective_to IS NULL OR a.effective_to>clock_timestamp())",
            "a.model_id=m.model_id",
            "a.desk=m.desk",
            "a.setup_family=m.setup_family",
            "a.horizon_value=m.horizon_value",
            "a.horizon_unit=m.horizon_unit",
        ]
        params: list[Any] = [str(model_key), str(desk).upper()]
        if model_version:
            clauses.append("m.model_version=%s")
            params.append(str(model_version))
        if horizon_value is not None:
            clauses.append("a.horizon_value=%s")
            params.append(int(horizon_value))
        if horizon_unit:
            clauses.append("a.horizon_unit=%s")
            params.append(str(horizon_unit).upper())
        row = self.authority.execute(
            f"""SELECT a.assignment_id,a.model_id,a.desk,a.setup_family,a.horizon_value,a.horizon_unit,
                       a.role,a.production_weight,a.effective_from,a.effective_to,a.promotion_decision_id,
                       m.model_key,m.model_version,m.model_type,m.artifact_uri,m.artifact_sha256,
                       m.feature_schema_hash,m.label_definition_version
                  FROM deployment.assignments a
                  JOIN model_registry.models m ON m.model_id=a.model_id
                 WHERE {' AND '.join(clauses)}
                 ORDER BY a.effective_from DESC LIMIT 1""",
            tuple(params),
            fetch="one",
            statement_timeout_ms=1500,
        )
        return dict(row) if row else None

    def latest_shadow_prediction(
        self,
        *,
        instrument_key: str,
        desk: str,
        symbol: str | None = None,
        as_of: datetime | None = None,
    ) -> dict[str, Any] | None:
        """Read the latest frozen shadow score without production authority.

        This is intentionally separate from ``latest_active_prediction``.  It
        lets scanners calculate and record model ranking evidence from the first
        governed publication while applying active influence only from a healthy champion assignment, capped at 15%.
        """
        when = as_of or datetime.now(timezone.utc)
        row = self.authority.execute(
            """SELECT sp.*,tp.lifecycle_state,tp.evaluation_paper_weight,
                      tp.production_weight,tp.validation_state,tp.created_at AS publication_created_at
                 FROM research.shadow_predictions sp
                 JOIN research.training_publications tp ON tp.publication_id=sp.publication_id
                WHERE sp.desk=%s AND sp.as_of<=%s
                  AND (sp.instrument_key=%s OR (sp.instrument_key IS NULL AND sp.symbol=%s))
                ORDER BY CASE WHEN sp.instrument_key=%s THEN 0 ELSE 1 END,
                         sp.as_of DESC,sp.created_at DESC LIMIT 1""",
            (str(desk).upper(), when, str(instrument_key), str(symbol or "").upper(), str(instrument_key)),
            fetch="one",
            statement_timeout_ms=1500,
        )
        return dict(row) if row else None

    def latest_active_prediction(
        self,
        *,
        instrument_key: str,
        desk: str,
        as_of: datetime | None = None,
    ) -> dict[str, Any] | None:
        """Read one frozen prediction backed by an active champion assignment."""
        when = as_of or datetime.now(timezone.utc)
        row = self.authority.execute(
            """SELECT p.*,a.assignment_id,a.production_weight,a.setup_family,a.horizon_value,a.horizon_unit,
                      a.promotion_decision_id,m.model_key,m.model_version,m.model_type,m.artifact_sha256,m.feature_schema_hash,
                      m.label_definition_version
                 FROM research.predictions p
                 JOIN model_registry.models m ON m.model_id=p.model_id
                 JOIN deployment.assignments a ON a.model_id=m.model_id
                WHERE p.instrument_key=%s AND m.desk=%s AND a.desk=m.desk
                  AND a.setup_family=m.setup_family
                  AND a.horizon_value=m.horizon_value AND a.horizon_unit=m.horizon_unit
                  AND a.role='CHAMPION' AND a.production_weight>0
                  AND a.effective_from<=%s AND (a.effective_to IS NULL OR a.effective_to>%s)
                  AND p.as_of<=%s AND p.data_cutoff_at<=p.as_of
                ORDER BY p.as_of DESC,p.frozen_at DESC LIMIT 1""",
            (str(instrument_key), str(desk).upper(), when, when, when),
            fetch="one",
            statement_timeout_ms=1500,
        )
        return dict(row) if row else None


    def selector_replay_rows(self, *, desk: str, horizon: str) -> list[dict[str, Any]]:
        """Read immutable selector evidence through the offline WFA query profile.

        R25 fetched the full member/proof JSON for every three-arm observation
        and sorted it in PostgreSQL. On a live system this crossed the 15-second
        interactive statement budget. R26 pushes desk/horizon filtering first,
        returns only the immutable fields consumed by WFA, removes the redundant
        database sort (Python already groups/sorts deterministically), and grants
        this explicitly-offline research read a bounded 120-second statement
        budget. Production/interactive read budgets are unchanged.
        """
        rows = self.authority.execute(
            """WITH desk_populations AS (
                   SELECT population_fingerprint,universe_id,dataset_fingerprint,feature_manifest_hash
                     FROM research.selector_populations
                    WHERE desk=%s
               ),
               settled AS (
                   SELECT candidate_id,population_fingerprint,observed_at,settled_at,market_regime,
                          net_return_bps,actual_cost_bps,same_bar_ambiguous,
                          proof_payload->>'cost_version' AS cost_version,
                          proof_payload->>'settlement_version' AS settlement_version,
                          COALESCE(proof_payload->>'primary_ambiguity_policy','') AS primary_ambiguity_policy
                     FROM research.selector_outcomes
                    WHERE horizon=%s
               )
               SELECT p.arm,p.model_version,p.population_fingerprint,p.candidate_id,p.prediction_key,
                      p.payload_sha256 AS prediction_payload_sha256,
                      m.symbol,lower(m.desk) AS mode,p.score,p.predicted_rank AS rank,
                      p.predicted_percentile AS percentile,p.prediction_at,
                      o.observed_at,o.settled_at,o.market_regime,o.net_return_bps,o.actual_cost_bps,
                      jsonb_build_object(
                        'cost_version',o.cost_version,
                        'settlement_version',o.settlement_version,
                        'primary_ambiguity_policy',o.primary_ambiguity_policy
                      ) AS proof_json,
                      o.same_bar_ambiguous,o.primary_ambiguity_policy,
                      jsonb_build_object(
                        'liquidity_score',COALESCE(m.feature_payload->'features'->'liquidity_score',m.feature_payload->'liquidity_score'),
                        'corporate_action_adjusted',COALESCE(m.feature_payload->'features'->'corporate_action_adjusted',m.feature_payload->'corporate_action_adjusted'),
                        'survivorship_bias_controlled',COALESCE(m.feature_payload->'features'->'survivorship_bias_controlled',m.feature_payload->'survivorship_bias_controlled'),
                        'feature_as_of',COALESCE(m.feature_payload->'features'->'feature_as_of',m.feature_payload->'feature_as_of'),
                        'universe_as_of',COALESCE(m.feature_payload->'features'->'universe_as_of',m.feature_payload->'universe_as_of'),
                        'fundamental_as_of',COALESCE(m.feature_payload->'features'->'fundamental_as_of',m.feature_payload->'fundamental_as_of')
                      ) AS feature_json,
                      m.feature_hash,pop.universe_id,pop.dataset_fingerprint,pop.feature_manifest_hash
                 FROM desk_populations pop
                 JOIN research.selector_arm_predictions p
                   ON p.population_fingerprint=pop.population_fingerprint
                 JOIN research.selector_population_members m
                   ON m.population_fingerprint=p.population_fingerprint
                  AND m.candidate_id=p.candidate_id
                 JOIN settled o
                   ON o.population_fingerprint=p.population_fingerprint
                  AND o.candidate_id=p.candidate_id""",
            (str(desk).upper(), str(horizon).lower()), fetch="all",
            statement_timeout_ms=120000, pool_timeout_seconds=30.0,
        ) or []
        return [dict(row) for row in rows]

    def status(self, desk: str | None = None) -> dict[str, Any]:
        params: list[Any] = []
        where = ""
        if desk:
            where = "WHERE m.desk=%s"
            params.append(str(desk).upper())
        assignments = self.authority.execute(
            f"""SELECT a.assignment_id,a.model_id,a.role,a.production_weight,a.effective_from,a.effective_to,
                       a.desk,a.setup_family,a.horizon_value,a.horizon_unit,
                       m.model_key,m.model_version,m.model_type,m.artifact_sha256
                  FROM deployment.assignments a
                  JOIN model_registry.models m ON m.model_id=a.model_id
                  {where}
                 ORDER BY a.desk,a.setup_family,a.horizon_value,a.effective_from DESC""",
            tuple(params), fetch="all", statement_timeout_ms=2500,
        )
        counts = self.authority.execute(
            """SELECT (SELECT count(*) FROM model_registry.models)::bigint AS models,
                      (SELECT count(*) FROM research.predictions)::bigint AS frozen_predictions,
                      (SELECT count(*) FROM research.prediction_outcomes)::bigint AS settled_outcomes,
                      (SELECT count(*) FROM research.experiments)::bigint AS experiments,
                      (SELECT count(*) FROM research.experiment_metrics)::bigint AS metrics,
                      (SELECT count(*) FROM deployment.promotion_decisions)::bigint AS decisions""",
            fetch="one", statement_timeout_ms=2500,
        ) or {}
        now = datetime.now(timezone.utc)
        rows = [dict(row) for row in assignments]
        active = [row for row in rows if str(row.get("role")) == "CHAMPION"
                  and row.get("effective_from") <= now
                  and (row.get("effective_to") is None or row.get("effective_to") > now)]
        return {
            "ok": True,
            "service_version": self.SERVICE_VERSION,
            "authority": "SEPARATE_GOVERNANCE_POSTGRES",
            "counts": {key: int(value or 0) for key, value in dict(counts).items()},
            "active_champions": active,
            "assignments": rows,
            "production_rule": "Only an effective CHAMPION assignment may contribute non-zero production weight.",
        }
