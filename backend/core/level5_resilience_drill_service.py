"""Non-destructive deterministic resilience drill for current-build evidence."""
from __future__ import annotations

from typing import Any, Dict

from config import APP_VERSION, BROKER_ORDER_EXECUTION_ENABLED, PRODUCT_MODE
from core.canonical_evidence_snapshot_service import CanonicalEvidenceSnapshotService
from core.cross_plane_reconciliation_service import CrossPlaneReconciliationService
from core.priority_pipeline_service import PriorityPipelineService
from models import now_iso


class _MemoryStore:
    def __init__(self):
        self.rows: Dict[str, Any] = {}
        self.production_candle_repository = None
        self.production_market_time_series_repository = None
        self.runtime_market_state = self
        self.curated_market_data = None

    def set_kv(self, key, value):
        self.rows[key] = value

    def get_kv(self, key, default=None):
        return self.rows.get(key, default)

    def candle_coverage(self, *_):
        return {"count": 2, "first": "2026-08-04T09:15:00+05:30", "last": "2026-08-04T09:18:00+05:30", "source": "runtime_only"}

    def canonical_bars(self, *_args, **_kwargs):
        return [
            {"timestamp": "2026-08-04T09:15:00+05:30"},
            {"timestamp": "2026-08-04T09:18:00+05:30"},
        ]


class _MemoryApp:
    def __init__(self):
        self.store = _MemoryStore()
        self.production_data_plane = None
        self.events = []

    def event(self, level, module, message, detail=None):
        self.events.append({"level": level, "module": module, "message": message, "detail": detail or {}})


class Level5ResilienceDrillService:
    VERSION = "level5-resilience-drill-1.0.0"

    def __init__(self, app: Any):
        self.app = app

    def run(self) -> Dict[str, Any]:
        fixture = _MemoryApp()
        pipeline = PriorityPipelineService(fixture)
        queued = pipeline.queue(symbol="TCS", instrument_key="NSE_EQ|TEST", mode="delivery", action="priority_sync")
        recovered = pipeline.recover_payload(queued, stale=True, max_recoveries=3)
        pipeline_pass = recovered.get("state") == "RUNNING" and any(row.get("state") == "QUEUED" for row in recovered.get("stages") or [])

        snapshots = CanonicalEvidenceSnapshotService(fixture)
        captured = snapshots.capture(
            symbol="TCS", instrument_key="NSE_EQ|TEST", mode="delivery",
            components={
                "identity": {"state": "READY"}, "coverage": {"state": "READY"},
                "timeframes": {"state": "READY"}, "mathematics": {"state": "READY"},
                "features": {"state": "READY"}, "inference": {"state": "NOT_REQUIRED"},
                "risk": {"state": "READY"}, "decision": {"state": "READY"},
            },
        )
        valid = snapshots.verify(captured)
        tampered = dict(captured); tampered["components"] = {**dict(captured.get("components") or {}), "decision": {"state": "READY", "decision": "ALTERED"}}
        tamper_check = snapshots.verify(tampered)
        snapshot_pass = valid.get("ok") is True and tamper_check.get("tampered") is True

        reconciliation = CrossPlaneReconciliationService(fixture).reconcile(
            symbol="TCS", instrument_key="NSE_EQ|TEST", interval="3minute"
        )
        reconciliation_pass = reconciliation.get("state") == "BLOCKED" and "RUNTIME_ONLY_HISTORY" in (reconciliation.get("mismatches") or [])
        safety_pass = PRODUCT_MODE == "AUTOMATIC_MODEL_PAPER_ONLY" and BROKER_ORDER_EXECUTION_ENABLED is False

        checks = [
            {"key": "stale_lease_requeued", "ok": pipeline_pass, "detail": recovered.get("state")},
            {"key": "snapshot_tamper_detected", "ok": snapshot_pass, "detail": tamper_check},
            {"key": "runtime_only_history_blocked", "ok": reconciliation_pass, "detail": reconciliation.get("mismatches")},
            {"key": "broker_authority_none", "ok": safety_pass, "detail": {"product_mode": PRODUCT_MODE, "broker_execution": BROKER_ORDER_EXECUTION_ENABLED}},
        ]
        passed = all(row["ok"] for row in checks)
        result = {
            "ok": passed,
            "passed": passed,
            "state": "PASS" if passed else "FAILED",
            "version": self.VERSION,
            "build": APP_VERSION,
            "captured_at": now_iso(),
            "checks": checks,
            "non_destructive": True,
            "production_change_allowed": False,
            "broker_authority": "NONE",
        }
        try:
            self.app.store.set_kv("level5_resilience_drill:last", result)
        except Exception as exc:
            result.update({"ok": False, "passed": False, "state": "PERSIST_FAILED", "error": str(exc)[:240]})
        return result
