"""Append-only PostgreSQL evidence for v86 qualification and target proof.

Runtime KV remains the latest-state read model.  These methods add immutable,
content-addressed history when the production PostgreSQL authority is active.
Compatibility/test stores deliberately return an explicit non-production state
instead of pretending persistence occurred.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


class Level5QualificationRepository:
    def __init__(self, store: Any):
        self.store = store

    def _operational(self) -> Any | None:
        repository = getattr(self.store, "production_kv_repository", None)
        return getattr(repository, "operational", None) if repository is not None else None

    def persist_ml(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        operational = self._operational()
        if operational is None:
            return {"state": "COMPATIBILITY_KV_ONLY", "persisted": False, "payload_hash": _hash(payload)}
        desks = dict(payload.get("desks") or {})
        source = dict(payload.get("official_source_coverage") or {})
        digest = _hash({
            "build": payload.get("build"),
            "state": payload.get("state"),
            "official_source_coverage": source,
            "delivery": desks.get("delivery") or {},
            "intraday": desks.get("intraday") or {},
        })
        operational.execute(
            """
            INSERT INTO runtime_control.ml_population_qualification_runs
              (build_version,state,official_source_current,official_source_total,delivery,intraday,payload_hash,captured_at)
            VALUES (%s,%s,%s,%s,CAST(%s AS jsonb),CAST(%s AS jsonb),%s,%s)
            ON CONFLICT (build_version,payload_hash) DO NOTHING
            """,
            (
                str(payload.get("build") or ""),
                str(payload.get("state") or "UNKNOWN"),
                int(source.get("current") or 0),
                int(source.get("total") or 0),
                _canonical(desks.get("delivery") or {}),
                _canonical(desks.get("intraday") or {}),
                digest,
                payload.get("captured_at"),
            ),
        )
        return {"state": "POSTGRES_APPEND_ONLY", "persisted": True, "payload_hash": digest}

    def persist_proof(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        operational = self._operational()
        digest = _hash({
            "build": payload.get("build"),
            "state": payload.get("state"),
            "passed": payload.get("passed"),
            "gates": payload.get("gates") or {},
            "missing_gates": payload.get("missing_gates") or [],
        })
        if operational is None:
            return {"state": "COMPATIBILITY_KV_ONLY", "persisted": False, "evidence_hash": digest}
        operational.execute(
            """
            INSERT INTO runtime_control.level5_operational_proof_runs
              (build_version,state,passed,gates,missing_gates,evidence_hash,captured_at)
            VALUES (%s,%s,%s,CAST(%s AS jsonb),CAST(%s AS jsonb),%s,%s)
            ON CONFLICT (build_version,evidence_hash) DO NOTHING
            """,
            (
                str(payload.get("build") or ""),
                str(payload.get("state") or "UNKNOWN"),
                bool(payload.get("passed")),
                _canonical(payload.get("gates") or {}),
                _canonical(payload.get("missing_gates") or []),
                digest,
                payload.get("captured_at"),
            ),
        )
        return {"state": "POSTGRES_APPEND_ONLY", "persisted": True, "evidence_hash": digest}
