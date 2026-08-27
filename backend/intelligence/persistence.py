"""
ActionObject snapshot persistence.

Mirrors the exact pattern `core.evidence_engine_service.EvidenceEngineService`
already uses for `evidence_snapshots` (same hash-id-as-primary-key,
INSERT OR IGNORE, same write_lock discipline) -- a second, differently-named
table so ActionObject snapshots don't collide with evidence snapshots, but
no new persistence mechanism is introduced.

This module owns only storage. Building the payload is
`intelligence.action_object.build_action_objects()`'s job; this file just
writes/reads what it produced.
"""
from __future__ import annotations

import hashlib
import json
import threading
from typing import Any, Dict, List, Optional


def _ensure_schema(conn: Any) -> None:
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS action_object_snapshots (
      snapshot_id TEXT PRIMARY KEY, as_of TEXT NOT NULL, contract_version TEXT NOT NULL,
      payload_json TEXT NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );
    CREATE INDEX IF NOT EXISTS ix_action_object_snapshots_asof ON action_object_snapshots(as_of);
    """)
    conn.commit()


def persist_action_objects(store: Any, payload: Dict[str, Any]) -> Dict[str, Any]:
    """store: app.store (needs .conn and .write_lock, same as EvidenceEngineService).
    payload: the dict returned by routes_get.r_action_objects (or anything
    with "as_of"/"contract_version" keys plus the action_objects list).
    Returns payload with a "snapshot_id" key added, same contract as
    EvidenceEngineService._persist(). No-ops (returns payload unchanged) if
    store is None -- callers must not crash when persistence isn't wired up
    (e.g. in unit tests using a lightweight double)."""
    if store is None:
        return payload
    if not hasattr(store, "write_lock"):
        store.write_lock = threading.Lock()
    conn = store.conn
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    snapshot_id = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]
    with store.write_lock:
        _ensure_schema(conn)
        conn.execute(
            "INSERT OR IGNORE INTO action_object_snapshots(snapshot_id,as_of,contract_version,payload_json) VALUES(?,?,?,?)",
            (snapshot_id, payload.get("as_of"), payload.get("contract_version") or "action-object-v1", canonical),
        )
        conn.commit()
    payload["snapshot_id"] = snapshot_id
    return payload


def action_object_history(store: Any, limit: int = 20) -> Dict[str, Any]:
    if store is None:
        return {"ok": True, "snapshots": []}
    _ensure_schema(store.conn)
    rows = store.conn.execute(
        "SELECT snapshot_id,as_of,contract_version,payload_json FROM action_object_snapshots ORDER BY as_of DESC LIMIT ?",
        (max(1, min(int(limit), 200)),),
    ).fetchall()
    return {
        "ok": True,
        "snapshots": [
            {"snapshot_id": r[0], "as_of": r[1], "contract_version": r[2], "payload": json.loads(r[3])}
            for r in rows
        ],
    }
