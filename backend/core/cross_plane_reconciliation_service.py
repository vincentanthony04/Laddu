"""Bounded reconciliation of canonical candle coverage across storage planes."""
from __future__ import annotations

from datetime import date, datetime
import hashlib
import json
from typing import Any, Dict, Mapping
from uuid import uuid4

from config import APP_VERSION
from core.db_utils import canonical_interval
from models import now_iso


def _jsonable(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _hash(value: Any) -> str:
    raw = json.dumps(_jsonable(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()


def _coverage(row: Mapping[str, Any] | None, source: str) -> Dict[str, Any]:
    row = dict(row or {})
    return {
        "source": str(row.get("source") or source),
        "count": int(row.get("count") or 0),
        "first": _jsonable(row.get("first") or row.get("first_ts")),
        "last": _jsonable(row.get("last") or row.get("last_ts")),
        "last_received_at": _jsonable(row.get("last_received_at")),
        "state": str(row.get("state") or row.get("catalog_state") or "MEASURED"),
    }


class CrossPlaneReconciliationService:
    VERSION = "cross-plane-reconciliation-1.0.0"

    def __init__(self, app: Any):
        self.app = app
        self.store = app.store
        plane = getattr(app, "production_data_plane", None)
        self.db = getattr(plane, "operational", None)

    @staticmethod
    def _kv_key(symbol: str, interval: str) -> str:
        return f"cross_plane_reconciliation:{symbol}:{interval}"

    def reconcile(self, *, symbol: str, instrument_key: str, interval: str) -> Dict[str, Any]:
        symbol = str(symbol or "").upper().strip()
        instrument_key = str(instrument_key or "").strip()
        norm = canonical_interval(interval)
        if not symbol or not instrument_key:
            raise ValueError("verified symbol and instrument key are required")

        canonical = _coverage(self.store.candle_coverage(instrument_key, norm), "canonical_merged")
        production_lake = getattr(self.store, "production_candle_repository", None)
        if production_lake is not None:
            lake = _coverage(production_lake.candle_coverage(instrument_key, norm), "parquet_catalog")
            operational = _coverage({}, "operational_not_authoritative")
        else:
            curated = getattr(self.store, "curated_market_data", None)
            lake = _coverage(curated.candle_coverage(instrument_key, norm) if curated is not None else {}, "curated_lake")
            operational = _coverage({}, "compatibility_operational")

        market = getattr(self.store, "production_market_time_series_repository", None)
        quest_rows = []
        if market is not None:
            try:
                quest_rows = list(market.recent_bars(instrument_key, norm, limit=2) or [])
            except Exception:
                quest_rows = []
        questdb = {
            "source": "questdb_recent_tail",
            "count": len(quest_rows),
            "first": _jsonable((quest_rows[0] if quest_rows else {}).get("timestamp") or (quest_rows[0] if quest_rows else {}).get("ts")),
            "last": _jsonable((quest_rows[-1] if quest_rows else {}).get("timestamp") or (quest_rows[-1] if quest_rows else {}).get("ts")),
            "state": "MEASURED" if market is not None else "NOT_ACTIVE",
        }
        runtime = getattr(self.store, "runtime_market_state", None)
        runtime_rows = []
        if runtime is not None:
            try:
                runtime_rows = list(runtime.canonical_bars(instrument_key, norm, limit=2, include_forming=True) or [])
            except Exception:
                runtime_rows = []
        runtime_tail = {
            "source": "hot_runtime_tail",
            "count": len(runtime_rows),
            "first": _jsonable((runtime_rows[0] if runtime_rows else {}).get("timestamp") or (runtime_rows[0] if runtime_rows else {}).get("ts")),
            "last": _jsonable((runtime_rows[-1] if runtime_rows else {}).get("timestamp") or (runtime_rows[-1] if runtime_rows else {}).get("ts")),
            "state": "MEASURED" if runtime is not None else "NOT_ACTIVE",
        }
        planes = {"canonical": canonical, "lake": lake, "operational": operational, "questdb": questdb, "runtime": runtime_tail}

        mismatches: list[str] = []
        repair_plan: list[str] = []
        canonical_count = int(canonical.get("count") or 0)
        durable_count = max(int(lake.get("count") or 0), int(operational.get("count") or 0), int(questdb.get("count") or 0))
        if canonical_count < int(lake.get("count") or 0):
            mismatches.append("CANONICAL_COUNT_BELOW_LAKE")
            repair_plan.append("rebuild canonical coverage catalogue from retained Parquet metadata")
        lasts = [str(row.get("last")) for row in (lake, operational, questdb, runtime_tail) if row.get("last")]
        expected_last = max(lasts) if lasts else None
        if expected_last and str(canonical.get("last") or "") < expected_last:
            mismatches.append("CANONICAL_LAST_TIMESTAMP_BEHIND_PLANE")
            repair_plan.append("reconcile the latest accepted tail into canonical coverage")
        if canonical_count > 0 and durable_count == 0:
            mismatches.append("RUNTIME_ONLY_HISTORY")
            repair_plan.append("persist accepted bars to QuestDB and immutable Parquet before qualifying mathematics or ML")
        if int(questdb.get("count") or 0) > 0 and int(lake.get("count") or 0) == 0:
            mismatches.append("PARQUET_PROJECTION_PENDING")
            repair_plan.append("project verified completed bars from the recent time-series plane into the historical lake")
        if canonical_count == 0:
            mismatches.append("NO_CANONICAL_COVERAGE")
            repair_plan.append("schedule exact-gap acquisition using provider-valid partitions")

        hard = {"CANONICAL_COUNT_BELOW_LAKE", "CANONICAL_LAST_TIMESTAMP_BEHIND_PLANE", "RUNTIME_ONLY_HISTORY"}
        state = "BLOCKED" if any(item in hard for item in mismatches) else "PARTIAL" if mismatches else "PASS"
        material = {
            "symbol": symbol, "instrument_key": instrument_key, "interval": norm,
            "state": state, "canonical_count": canonical_count, "planes": planes,
            "mismatches": mismatches, "repair_plan": repair_plan, "build_version": APP_VERSION,
        }
        evidence_hash = _hash(material)
        run_id = str(uuid4())
        payload = {
            "ok": state != "BLOCKED",
            "version": self.VERSION,
            "run_id": run_id,
            **material,
            "evidence_hash": evidence_hash,
            "captured_at": now_iso(),
            "bounded_reads": {"questdb_rows": 2, "runtime_rows": 2, "parquet": "catalog_only"},
        }
        if self.db is not None:
            row = self.db.execute(
                """INSERT INTO runtime_control.cross_plane_reconciliation_runs(
                       run_id,symbol,instrument_key,interval,state,canonical_count,planes,mismatches,
                       repair_plan,evidence_hash,build_version,captured_at)
                     VALUES(%s::uuid,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb,%s,%s,now())
                     RETURNING run_id::text,captured_at""",
                (run_id, symbol, instrument_key, norm, state, canonical_count, json.dumps(planes),
                 json.dumps(mismatches), json.dumps(repair_plan), evidence_hash, APP_VERSION),
                fetch="one", statement_timeout_ms=2500,
            ) or {}
            payload["run_id"] = str(row.get("run_id") or run_id)
            if row.get("captured_at") is not None:
                payload["captured_at"] = _jsonable(row.get("captured_at"))
        else:
            self.store.set_kv(self._kv_key(symbol, norm), payload)
        return payload

    def latest(self, *, symbol: str, interval: str) -> Dict[str, Any]:
        symbol = str(symbol or "").upper().strip()
        norm = canonical_interval(interval)
        if self.db is None:
            return dict(self.store.get_kv(self._kv_key(symbol, norm), {}) or {
                "ok": True, "version": self.VERSION, "symbol": symbol, "interval": norm,
                "state": "NOT_RUN", "mismatches": [], "repair_plan": [],
            })
        row = self.db.execute(
            """SELECT run_id::text,symbol,instrument_key,interval,state,canonical_count,planes,
                      mismatches,repair_plan,evidence_hash,build_version,captured_at
                 FROM runtime_control.cross_plane_reconciliation_runs
                WHERE symbol=%s AND interval=%s ORDER BY captured_at DESC LIMIT 1""",
            (symbol, norm), fetch="one", statement_timeout_ms=1800,
        )
        if not row:
            return {"ok": True, "version": self.VERSION, "symbol": symbol, "interval": norm, "state": "NOT_RUN", "mismatches": [], "repair_plan": []}
        payload = _jsonable(dict(row)); payload.update({"ok": payload.get("state") != "BLOCKED", "version": self.VERSION})
        return payload
