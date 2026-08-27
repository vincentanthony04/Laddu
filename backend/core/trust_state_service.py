"""Customer-visible trust authority for trading surfaces.

This projection is deliberately small and cache/in-memory only.  It converts
runtime failure truth into three states a trader can act on: TRUSTED, DEGRADED,
and DO_NOT_TRUST.  It never grants trade authority; DO_NOT_TRUST explicitly
removes customer admission regardless of a stale green decision badge.
"""
from __future__ import annotations

from typing import Any, Dict, List
import time
from models import now_iso
from core.runtime_primitives import is_india_market_open


class TrustStateService:
    VERSION = "trader-trust-state-r5-healthy-cadence-1.1.0"
    RUNTIME_CRITICAL_COMPONENTS = {
        "index_levels", "intraday_scanner", "delivery_scanner",
        "decision_quote_projection", "live_market_stream",
        "operator_read_models", "product_state_envelope",
    }

    def __init__(self, app: Any) -> None:
        self.app = app

    @staticmethod
    def _runtime_failure(row: Dict[str, Any]) -> bool:
        state = str(row.get("state") or "").upper()
        return any(token in state for token in ("FAILED", "STUCK", "NO_PROGRESS", "CIRCUIT_OPEN", "DEAD"))

    def snapshot(self) -> Dict[str, Any]:
        try:
            controller = dict(self.app.autonomic_controller.snapshot(refresh=False) or {})
        except Exception:
            controller = {"state": "STARTING", "blockers": []}
        try:
            governor = dict(self.app.workload_governor.snapshot() or {})
        except Exception:
            governor = {"database_pressure": {}}
        try:
            latency = dict(self.app.http_latency_monitor.trading_snapshot() or {})
        except Exception:
            latency = {"routes": {}, "customer_read_p95_ms": None, "customer_read_samples": 0}

        pressure = dict(governor.get("database_pressure") or {})
        governance = dict(pressure.get("governance") or {})
        interactive = dict(pressure.get("interactive") or {})
        blockers = [dict(row or {}) for row in (controller.get("blockers") or [])]
        try:
            supervisor = dict(self.app.supervisor.snapshot() or {})
        except Exception:
            supervisor = {}
        def healthy_expected_idle(component: str) -> bool:
            current = dict(supervisor.get(component) or {})
            return bool(
                current.get("alive") is True
                and current.get("stale") is not True
                and (current.get("expected_idle") is True or str(current.get("state") or "").upper() == "EXPECTED_IDLE")
            )
        runtime_blockers = [
            row for row in blockers
            if self._runtime_failure(row)
            and str(row.get("component") or "") in self.RUNTIME_CRITICAL_COMPONENTS
            and not healthy_expected_idle(str(row.get("component") or ""))
        ]
        worker_health = dict((getattr(self.app, "status", {}) or {}).get("worker_health") or {})
        scanner_cadence = {}
        for component in ("intraday_scanner", "delivery_scanner"):
            loop = dict(supervisor.get(component) or {})
            cadence = dict(worker_health.get(component) or {})
            raw_state = str(loop.get("state") or cadence.get("state") or "UNKNOWN").upper()
            display_state = "SLEEPING" if raw_state == "EXPECTED_IDLE" and loop.get("alive") is True and loop.get("stale") is not True else raw_state
            scanner_cadence[component] = {
                "state": display_state,
                "healthy": bool(loop.get("alive") is True and loop.get("stale") is not True and not self._runtime_failure(loop)),
                "last_cycle_at": cadence.get("last_completed_at"),
                "next_cycle_at": cadence.get("next_run_at"),
                "seconds_to_next": cadence.get("seconds_to_next"),
                "waiting_on": loop.get("waiting_on"),
                "heartbeat_age_sec": loop.get("heartbeat_age_sec"),
            }

        reasons: List[str] = []
        severity = 0  # 0 trusted, 1 degraded, 2 do-not-trust

        if runtime_blockers:
            severity = 2
            row = runtime_blockers[0]
            reasons.append(f"{row.get('component') or 'runtime'} {str(row.get('state') or 'blocked').lower()}")

        gov_available = int(governance.get("pool_available") or 0)
        gov_size = int(governance.get("pool_size") or 0)
        gov_waiting = int(governance.get("requests_waiting") or 0)
        # requests_queued is a cumulative psycopg-pool statistic, not the
        # current queue depth.  Trust is therefore based on available/waiting,
        # never on the cumulative counter alone.
        if governance.get("recovering") or governance.get("usable") is False:
            severity = 2
            reasons.append("governance database authority unavailable/recovering")
        elif gov_size and gov_available <= 0 and gov_waiting > 0:
            severity = 2
            reasons.append(f"governance database saturated · {gov_waiting} waiting")
        elif gov_size and gov_available <= 0:
            severity = max(severity, 1)
            reasons.append("governance database has no immediately available connection")
        elif governance.get("pressured"):
            severity = max(severity, 1)
            reasons.append("governance database under pressure")

        if interactive.get("saturated") or int(interactive.get("requests_waiting") or 0) > 0:
            severity = max(severity, 1)
            reasons.append("interactive database reads are waiting")

        p95 = latency.get("customer_read_p95_ms")
        try:
            p95_value = float(p95) if p95 is not None else None
        except Exception:
            p95_value = None
        if p95_value is not None and p95_value >= 5000:
            severity = 2
            reasons.append(f"customer chart/snapshot p95 {p95_value/1000.0:.1f}s")
        elif p95_value is not None and p95_value >= 2000:
            severity = max(severity, 1)
            reasons.append(f"customer chart/snapshot p95 {p95_value/1000.0:.1f}s")

        state = ("TRUSTED", "DEGRADED", "DO_NOT_TRUST")[severity]
        if not reasons:
            reasons.append("data/read-model path current and no critical runtime blocker")
        sequence_ns = time.time_ns()
        return {
            "ok": True,
            "version": self.VERSION,
            "evaluated_at": now_iso(),
            "sequence_ns": sequence_ns,
            "sequence_us": sequence_ns // 1_000,
            "state": state,
            "decision_admission_allowed": state != "DO_NOT_TRUST",
            "market_open": bool(is_india_market_open()),
            "reason": " · ".join(reasons[:3]),
            "reasons": reasons[:8],
            "controller": {
                "state": controller.get("state"),
                "primary_blocker": controller.get("primary_blocker"),
                "runtime_blocker_count": len(runtime_blockers),
            },
            "database": {
                "governance": governance,
                "interactive": interactive,
            },
            "latency": latency,
            "scanner_cadence": scanner_cadence,
            "policy": "Healthy scheduled scanner sleep is TRUSTED cadence, not a runtime failure. TRUSTED still requires no genuine critical blocker and bounded customer-read latency; DO_NOT_TRUST suppresses customer trade admission without changing canonical stored evidence.",
        }
