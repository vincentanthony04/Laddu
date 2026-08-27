"""Event-driven job controller and Level-5 maturity optimiser.

This control plane observes useful progress, not just process liveness.  It
maps runtime/data/evidence blockers to deterministic, allow-listed playbooks,
executes at most one bounded action per evaluation cycle, and verifies that the
action changed a measurable state before marking recovery successful.

It never changes trading mathematics, risk limits, model weights, positions,
canonical ledger events or database schemas.
"""
from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import threading
import time
from typing import Any, Callable, Dict, Iterable, Mapping

from config import APP_VERSION
from models import now_iso


@dataclass(frozen=True)
class ControlEvent:
    event_id: str
    sequence: int
    event_type: str
    component: str
    occurred_at: str
    detail: Dict[str, Any]
    build: str = APP_VERSION


class ControlEventBus:
    """Small bounded event stream with replay by sequence."""

    def __init__(self, *, capacity: int = 2000):
        self._events: deque[ControlEvent] = deque(maxlen=max(100, int(capacity)))
        self._sequence = 0
        self._lock = threading.RLock()

    def publish(self, event_type: str, component: str, detail: Mapping[str, Any] | None = None) -> Dict[str, Any]:
        with self._lock:
            self._sequence += 1
            occurred_at = datetime.now(timezone.utc).isoformat()
            seed = f"{APP_VERSION}|{self._sequence}|{event_type}|{component}|{occurred_at}"
            row = ControlEvent(
                event_id=hashlib.sha256(seed.encode("utf-8")).hexdigest()[:24],
                sequence=self._sequence,
                event_type=str(event_type or "EVENT").upper(),
                component=str(component or "unknown"),
                occurred_at=occurred_at,
                detail=dict(detail or {}),
            )
            self._events.append(row)
            return asdict(row)

    def events(self, *, after_sequence: int = 0, limit: int = 200) -> list[Dict[str, Any]]:
        with self._lock:
            rows = [asdict(row) for row in self._events if row.sequence > int(after_sequence or 0)]
        return rows[-max(1, min(1000, int(limit or 200))):]

    def restore(self, rows: Iterable[Mapping[str, Any]]) -> None:
        """Restore the append-only audit tail after a service restart."""
        with self._lock:
            for raw in rows or []:
                try:
                    row = ControlEvent(
                        event_id=str(raw.get("event_id") or ""),
                        sequence=int(raw.get("sequence") or 0),
                        event_type=str(raw.get("event_type") or "EVENT"),
                        component=str(raw.get("component") or "unknown"),
                        occurred_at=str(raw.get("occurred_at") or now_iso()),
                        detail=dict(raw.get("detail") or {}),
                        build=str(raw.get("build") or APP_VERSION),
                    )
                    if row.event_id and row.sequence > 0:
                        self._events.append(row)
                        self._sequence = max(self._sequence, row.sequence)
                except Exception:
                    continue

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "version": "control-event-bus-1.0.0",
                "sequence": self._sequence,
                "retained": len(self._events),
                "capacity": self._events.maxlen,
                "latest": asdict(self._events[-1]) if self._events else None,
            }


class AutonomicControlPlane:
    VERSION = "autonomic-control-plane-2.0.0-safe-recovery-priority"
    KV_KEY = "autonomic_control_plane:last"
    EVENT_KV_KEY = "autonomic_control_plane:events:v2"
    SAFE_ACTION_COOLDOWN_SEC = 45.0
    ACTION_FAILURE_LIMIT = 3
    ACTION_VERIFY_DEADLINE_SEC = 120.0

    GATE_WEIGHTS = {
        "operational_data_coverage": 100,
        "priority_pipeline_recovered": 98,
        "exact_build_browser_workflows": 94,
        "market_session_soak": 92,
        "official_nse_data_complete": 90,
        "intraday_full_cycle": 88,
        "delivery_full_cycle": 88,
        "canonical_ranking_reconciliation": 84,
        "decision_surface_reconciliation": 84,
        "delivery_ml_population_qualified": 60,
        "intraday_ml_population_qualified": 60,
        "forward_post_cost_authority": 40,
    }

    def __init__(self, app: Any, event_bus: ControlEventBus | None = None, audit_writer: Any | None = None):
        self.app = app
        self.event_bus = event_bus or ControlEventBus()
        self.audit_writer = audit_writer
        self._lock = threading.RLock()
        self._last_snapshot: Dict[str, Any] = {}
        self._last_action_at: Dict[str, float] = {}
        self._action_attempts: Dict[str, int] = {}
        self._action_successes: Dict[str, int] = {}
        self._last_component_signature: Dict[str, str] = {}
        self._last_data_event: Dict[str, Any] = {}
        self._cycle = 0
        self._action_failures: Dict[str, int] = {}
        self._circuits: Dict[str, Dict[str, Any]] = {}
        self._pending_actions: Dict[str, Dict[str, Any]] = {}
        # Recovery execution is isolated from the controller evaluation loop.
        # A provider/database recovery playbook may itself stall; it must never
        # make the authority that detects failures stop progressing.
        self._dispatch_lock = threading.RLock()
        self._inflight_dispatch: Dict[str, Dict[str, Any]] = {}
        self._dispatch_limit = 2
        self._dispatch_timeout_sec = 180.0
        # Controller evaluation itself is scheduled by a tiny watchdog loop.
        # No maturity/database/recovery call is allowed to hold the supervised
        # controller heartbeat. A timed-out generation is fenced; a bounded
        # replacement may start while the orphaned daemon thread is retained
        # only for post-mortem visibility. Action keys remain idempotent.
        self._evaluation_lock = threading.RLock()
        self._evaluation_generation = 0
        self._evaluation_thread: threading.Thread | None = None
        self._evaluation_started_monotonic = 0.0
        self._evaluation_started_at: str | None = None
        self._evaluation_timeout_sec = 5.0
        self._retired_evaluations: deque[Dict[str, Any]] = deque(maxlen=2)
        self._evaluation_last_error: str | None = None
        try:
            retained = self.app.store.get_kv(self.EVENT_KV_KEY, []) or []
            if isinstance(retained, list):
                self.event_bus.restore(retained)
        except Exception:
            pass

    # --------------------------------------------------------------- events
    def publish(self, event_type: str, component: str, detail: Mapping[str, Any] | None = None) -> Dict[str, Any]:
        row = self.event_bus.publish(event_type, component, detail)
        try:
            self.app.event("INFO", "autonomic_controller", event_type, dict(detail or {}))
        except Exception:
            pass
        try:
            if self.audit_writer is not None:
                self.audit_writer.submit(self.EVENT_KV_KEY, self.event_bus.events(limit=500))
        except Exception:
            pass
        return row

    def on_data_stored(self, *, instrument_key: str, interval: str, rows: int, reason: str, missing_from: str, missing_to: str) -> None:
        detail = {
            "instrument_key": instrument_key,
            "interval": interval,
            "rows": int(rows or 0),
            "reason": reason,
            "missing_from": missing_from,
            "missing_to": missing_to,
        }
        self._last_data_event = {**detail, "at": now_iso()}
        self.publish("DATA_PERSISTED", "market_data", detail)
        self.publish("DERIVED_STATE_INVALIDATED", "mtf_and_levels", {
            "instrument_key": instrument_key,
            "source_interval": interval,
            "reason": "new canonical bars persisted",
        })
        selected = self._selected_stock()
        if str(selected.get("instrument_key") or "").strip() == str(instrument_key or "").strip() and selected.get("symbol"):
            self.publish("DEPENDENCY_RESUME_SCHEDULED", "selected_stock_pipeline", {
                "symbol": selected.get("symbol"),
                "mode": selected.get("mode") or "delivery",
                "instrument_key": instrument_key,
                "reason": "canonical data persisted",
            })

            def resume_dependants() -> None:
                try:
                    try:
                        self.app.priority_pipeline.reconcile(
                            symbol=str(selected.get("symbol")),
                            mode=str(selected.get("mode") or "delivery"),
                            reason="canonical_data_persisted",
                        )
                    except Exception as exc:
                        self.publish("PIPELINE_RECONCILE_FAILED", "selected_stock_pipeline", {
                            "symbol": selected.get("symbol"), "error": str(exc)[:240],
                        })
                    self.app.market_data.schedule_priority_stock_pipeline(
                        str(selected.get("symbol")),
                        mode=str(selected.get("mode") or "delivery"),
                        selected_interval=str(selected.get("interval") or "day"),
                        action="data_ready_continuation",
                    )
                    self.app.operator_read_models.refresh()
                    self.app.dashboard.refresh_cards_cache("all")
                    self.publish("DEPENDENCY_RESUMED", "selected_stock_pipeline", {
                        "symbol": selected.get("symbol"), "instrument_key": instrument_key,
                    })
                except Exception as exc:
                    self.publish("DEPENDENCY_RESUME_FAILED", "selected_stock_pipeline", {
                        "symbol": selected.get("symbol"), "instrument_key": instrument_key,
                        "error": str(exc)[:300],
                    })

            threading.Thread(
                target=resume_dependants,
                name=f"LadduDataReady-{str(selected.get('symbol'))[:20]}",
                daemon=True,
            ).start()

    # -------------------------------------------------------------- diagnosis
    @staticmethod
    def _signature(row: Mapping[str, Any]) -> str:
        material = {
            key: row.get(key)
            for key in (
                "state", "stage", "current_item", "completed_units", "total_units",
                "restart_count", "recovery_count", "last_error", "progress_token",
            )
        }
        return hashlib.sha256(json.dumps(material, sort_keys=True, default=str).encode("utf-8")).hexdigest()

    @staticmethod
    def _business_progress_signature(row: Mapping[str, Any]) -> str:
        """Hash only measurable business progress for recovery verification.

        Recovery counters, lifecycle labels and restart metadata are deliberately
        excluded: accepting a recovery must never verify itself. A recovery is
        successful only after the worker changes its immutable progress token,
        current business item, completed count or governed total.
        """
        material = {
            key: row.get(key)
            for key in ("progress_token", "current_item", "completed_units", "total_units")
        }
        return hashlib.sha256(json.dumps(material, sort_keys=True, default=str).encode("utf-8")).hexdigest()

    def _component_rows(self) -> list[Dict[str, Any]]:
        snapshot = self.app.supervisor.snapshot()
        rows = []
        for name, value in sorted(snapshot.items()):
            row = {"component": name, **dict(value or {})}
            signature = self._signature(row)
            prior = self._last_component_signature.get(name)
            if prior and prior != signature:
                self.publish("JOB_PROGRESS", name, {
                    "state": row.get("state"), "stage": row.get("stage"),
                    "completed_units": row.get("completed_units"), "total_units": row.get("total_units"),
                })
            self._last_component_signature[name] = signature
            rows.append(row)
        return rows

    def _historical_state(self) -> Dict[str, Any]:
        # The controller consumes the continuously published backfill projection.
        # It never scans physical candle catalogues on its own control thread.
        status = dict(getattr(self.app, "status", {}) or {})
        queue = dict(status.get("deep_history_backfill") or {})
        return {
            "queue": queue,
            "timeframes": [],
            "operational_ready": int(queue.get("operational_ready") or queue.get("done") or 0),
            "total": int(queue.get("total") or 0),
            "remaining_operational": int(queue.get("remaining_operational") or 0),
            "research_ready": int(queue.get("research_ready") or 0),
            "deep_enriched": int(queue.get("deep_enriched") or 0),
            "source": "background_backfill_projection",
        }

    def _maturity(self) -> tuple[Dict[str, Any], Dict[str, Any]]:
        # Heavy product/Level-5 evaluation is isolated on maturity_projection.
        # The controller must remain capable of recovering other components even
        # when evidence generation itself is slow or unavailable.
        projection = getattr(self.app, "maturity_projection", None)
        if projection is None:
            return (
                {"ok": False, "maturity_level": 0, "missing_level4_gates": ["maturity_projection_unavailable"]},
                {"ok": False, "passed": False, "state": "UNAVAILABLE", "missing_gates": ["maturity_projection_unavailable"], "gates": {}},
            )
        payload = dict(projection.snapshot() or {})
        product = dict(payload.get("product") or {})
        proof = dict(payload.get("proof") or {})
        product.setdefault("maturity_level", 0)
        proof.setdefault("passed", False)
        return product, proof

    def _selected_stock(self) -> Dict[str, Any]:
        governor = getattr(self.app, "workload_governor", None)
        if governor is not None:
            try:
                state = dict(governor.snapshot() or {})
                symbol = str(state.get("selected_stock") or "").upper().strip()
                if symbol:
                    return {
                        "symbol": symbol,
                        "mode": str(state.get("selected_mode") or "delivery"),
                        "instrument_key": state.get("selected_instrument_key"),
                        "interval": state.get("selected_interval") or ("5minute" if str(state.get("selected_mode")) == "intraday" else "day"),
                    }
            except Exception:
                pass
        return {}

    def _blockers(
        self,
        *,
        components: Iterable[Mapping[str, Any]],
        product: Mapping[str, Any],
        proof: Mapping[str, Any],
        historical: Mapping[str, Any],
    ) -> list[Dict[str, Any]]:
        blockers: list[Dict[str, Any]] = []
        governor = getattr(self.app, "workload_governor", None)
        governor_state = governor.snapshot() if governor is not None else {}
        db_pressure = dict(governor_state.get("database_pressure") or {})
        if db_pressure.get("saturated") or int(db_pressure.get("requests_waiting") or 0) >= 2:
            blockers.append({
                "key": "operational_database_pool_pressure",
                "state": "BLOCKED",
                "detail": f"operational pool available={db_pressure.get('pool_available')} waiting={db_pressure.get('requests_waiting')} queued={db_pressure.get('requests_queued')}",
                "component": "deep_history_backfill",
                "safe_action": "YIELD_BACKGROUND_WORK",
                "score": 118,
            })
        try:
            delivery = dict((self.app.status.get("mode_scanners") or {}).get("delivery") or {})
            analysis = dict(delivery.get("analysis") or {})
            # Controller control-thread purity: consume the frozen universe
            # projection already published by startup/scanner workers. Never
            # issue PostgreSQL reads while evaluating recovery blockers.
            universe_status = dict((getattr(self.app, "status", {}) or {}).get("universe_authority") or {})
            snapshot = dict((universe_status.get("snapshots") or {}).get("delivery") or {})
            expected = int(snapshot.get("population_count") or 0)
            observed = int(analysis.get("universe_size") or 0)
            if expected > 0 and observed > 0 and observed != expected:
                blockers.append({
                    "key": "delivery_population_authority_mismatch",
                    "state": "BLOCKED",
                    "detail": f"Delivery scanner population {observed} does not match immutable authority {expected}",
                    "component": "delivery_scanner",
                    "safe_action": "RECONCILE_DELIVERY_POPULATION",
                    "score": 116,
                })
        except Exception:
            pass
        component_rows = [dict(row or {}) for row in components]
        recoverable_concrete = []
        for row in component_rows:
            state = str(row.get("state") or "").upper()
            recovery_available = bool(row.get("recovery_available"))
            safety_class = str(row.get("safety_class") or "SAFE_COMPONENT").upper()
            progress_age = float(row.get("progress_age_sec") or 0.0)
            heartbeat_age = float(row.get("heartbeat_age_sec") or 0.0)
            actionable = recovery_available and safety_class == "SAFE_COMPONENT" and state in {
                "DEAD", "STUCK", "NO_PROGRESS", "UNINSTRUMENTED", "FAILED"
            }
            # UNINSTRUMENTED is actionable only after it is materially overdue.  This
            # prevents startup noise from consuming every controller cycle.
            if state == "UNINSTRUMENTED" and max(progress_age, heartbeat_age) < 120.0:
                actionable = False
            if actionable:
                recoverable_concrete.append(str(row.get("component") or ""))
            if state in {"DEAD", "STUCK", "NO_PROGRESS", "UNINSTRUMENTED", "FAILED", "CIRCUIT_OPEN"}:
                if safety_class in {"RISK_AUTHORITY", "LEDGER_AUTHORITY", "DATABASE_AUTHORITY"}:
                    severity = 122
                elif actionable:
                    severity = {
                        "FAILED": 132,
                        "DEAD": 130,
                        "STUCK": 128,
                        "NO_PROGRESS": 124,
                        "UNINSTRUMENTED": 116,
                    }.get(state, 112)
                else:
                    severity = 78
                blockers.append({
                    "key": f"worker:{row.get('component')}",
                    "state": state,
                    "detail": f"{row.get('component')} {state.lower()} · heartbeat {row.get('heartbeat_age_sec')}s · progress {row.get('progress_age_sec')}s",
                    "component": row.get("component"),
                    "safe_action": "RECOVER_COMPONENT" if actionable else "ESCALATE",
                    "actionable": actionable,
                    "score": severity,
                })

        remaining = int(historical.get("remaining_operational") or 0)
        if remaining > 0:
            selected = self._selected_stock()
            selected_symbol = str(selected.get("symbol") or "").strip().upper()
            deep_row = next((row for row in component_rows if row.get("component") == "deep_history_backfill"), {})
            deep_state = str(deep_row.get("state") or "").upper()
            deep_advancing = deep_state in {"RUNNING", "EXPECTED_IDLE"} and bool(deep_row.get("progress_token"))
            actionable = bool(selected_symbol) and not recoverable_concrete and not deep_advancing
            blockers.append({
                "key": "operational_data_coverage",
                "state": "IN_PROGRESS" if deep_advancing else "BLOCKED",
                "detail": f"{remaining} governed instruments still lack operational candle coverage",
                "component": "deep_history_backfill",
                "safe_action": "PRIORITISE_EXACT_GAPS" if actionable else "OBSERVE_PROGRESS",
                "actionable": actionable,
                # Broad maturity work must never outrank a concrete recoverable job.
                "score": 72 + min(12, remaining / 100),
            })
        for gate in list(proof.get("missing_gates") or []):
            blockers.append({
                "key": gate,
                "state": "PENDING_EVIDENCE",
                "detail": gate.replace("_", " "),
                "component": self._gate_component(gate),
                "safe_action": self._gate_action(gate),
                "score": self.GATE_WEIGHTS.get(gate, 50),
            })
        for gate in list(product.get("missing_level4_gates") or []):
            if any(row.get("key") == gate for row in blockers):
                continue
            blockers.append({
                "key": gate,
                "state": "PENDING_EVIDENCE",
                "detail": gate.replace("_", " "),
                "component": self._gate_component(gate),
                "safe_action": self._gate_action(gate),
                "score": self.GATE_WEIGHTS.get(gate, 55),
            })
        return sorted(blockers, key=lambda row: (-float(row.get("score") or 0), str(row.get("key"))))

    @staticmethod
    def _gate_component(gate: str) -> str:
        gate = str(gate or "")
        if "pipeline" in gate:
            return "priority_pipeline_recovery"
        if "data" in gate or "population" in gate:
            return "data_conveyor"
        if "intraday" in gate:
            return "intraday_scanner"
        if "delivery" in gate:
            return "delivery_scanner"
        if "browser" in gate:
            return "operator_read_models"
        if "decision" in gate or "ranking" in gate:
            return "operator_read_models"
        return "autonomic_controller"

    @staticmethod
    def _gate_action(gate: str) -> str:
        gate = str(gate or "")
        if gate == "priority_pipeline_recovered":
            return "RECOVER_STALE_PRIORITY_PIPELINES"
        if "data" in gate or "population" in gate:
            return "RUN_DATA_CONVEYOR"
        if "intraday" in gate or "delivery" in gate:
            return "RECOVER_SCANNER_CAPACITY"
        if "decision" in gate or "ranking" in gate:
            return "REBUILD_READ_MODELS"
        return "EVIDENCE_REQUIRED"

    # --------------------------------------------------------------- actions
    def _cooldown_ready(self, key: str) -> bool:
        return time.monotonic() - float(self._last_action_at.get(key) or 0) >= self.SAFE_ACTION_COOLDOWN_SEC

    def _record_action(self, key: str, result: Mapping[str, Any]) -> None:
        self._last_action_at[key] = time.monotonic()
        self._action_attempts[key] = self._action_attempts.get(key, 0) + 1
        if result.get("ok") and result.get("verified", True):
            self._action_successes[key] = self._action_successes.get(key, 0) + 1
            self._action_failures[key] = 0
            self._circuits.pop(key, None)
        elif result.get("accepted") and not result.get("verified"):
            # Accepted asynchronous work is neither success nor failure until a
            # later cycle verifies a changed progress signature.
            return
        else:
            failures = self._action_failures.get(key, 0) + 1
            self._action_failures[key] = failures
            if failures >= self.ACTION_FAILURE_LIMIT:
                self._circuits[key] = {"opened_at": now_iso(), "failures": failures, "last_result": dict(result)}

    def _verify_pending_actions(self, components: Iterable[Mapping[str, Any]]) -> list[Dict[str, Any]]:
        now_mono = time.monotonic()
        signatures = {str(row.get("component") or ""): self._business_progress_signature(row) for row in components}
        resolved: list[Dict[str, Any]] = []
        for key, pending in list(self._pending_actions.items()):
            component = str(pending.get("component") or "")
            before = str(pending.get("before_signature") or "")
            current = signatures.get(component, "")
            age = now_mono - float(pending.get("accepted_monotonic") or now_mono)
            if current and before and current != before:
                result = {
                    "ok": True,
                    "accepted": True,
                    "verified": True,
                    "state": "RECOVERY_PROGRESS_VERIFIED",
                    "action": pending.get("action"),
                    "blocker": pending.get("blocker"),
                    "component": component,
                    "verification_age_sec": round(age, 2),
                }
                self._action_successes[key] = self._action_successes.get(key, 0) + 1
                self._action_failures[key] = 0
                self._circuits.pop(key, None)
                self._pending_actions.pop(key, None)
                self.publish("CONTROLLER_ACTION_VERIFIED", component or "controller", result)
                resolved.append(result)
            elif age >= self.ACTION_VERIFY_DEADLINE_SEC:
                failures = self._action_failures.get(key, 0) + 1
                self._action_failures[key] = failures
                result = {
                    "ok": False,
                    "accepted": True,
                    "verified": False,
                    "state": "RECOVERY_PROGRESS_NOT_VERIFIED",
                    "action": pending.get("action"),
                    "blocker": pending.get("blocker"),
                    "component": component,
                    "verification_age_sec": round(age, 2),
                }
                if failures >= self.ACTION_FAILURE_LIMIT:
                    self._circuits[key] = {"opened_at": now_iso(), "failures": failures, "last_result": result}
                self._pending_actions.pop(key, None)
                self.publish("CONTROLLER_ACTION_FAILED_VERIFICATION", component or "controller", result)
                resolved.append(result)
        return resolved

    def _execute_action_sync(self, blocker: Mapping[str, Any]) -> Dict[str, Any]:
        action = str(blocker.get("safe_action") or "EVIDENCE_REQUIRED")
        key = f"{blocker.get('key')}:{action}"
        if key in self._circuits:
            return {"ok": False, "verified": False, "state": "CIRCUIT_OPEN", "action": action, "key": key, "circuit": self._circuits[key]}
        if key in self._pending_actions:
            return {"ok": True, "accepted": True, "verified": False, "state": "ACTION_ALREADY_PENDING_VERIFICATION", "action": action, "key": key}
        if not self._cooldown_ready(key):
            return {"ok": False, "verified": False, "state": "COOLDOWN", "action": action, "key": key}
        before_rows = {row.get("component"): self._business_progress_signature(row) for row in self._component_rows()}
        selected = self._selected_stock()
        symbol = str(selected.get("symbol") or "").upper().strip()
        mode = str(selected.get("mode") or "delivery").lower()
        try:
            if action == "RECOVER_COMPONENT":
                result = self.app.supervisor.recover(
                    str(blocker.get("component") or ""),
                    reason=str(blocker.get("detail") or blocker.get("key")),
                    action="LEVEL5_BLOCKER_RECOVERY",
                )
            elif action == "RECOVER_STALE_PRIORITY_PIPELINES":
                result = dict(self.app.priority_pipeline.recover_stale() or {})
                checked = int(result.get("checked") or 0)
                blocked_count = int(result.get("blocked") or 0)
                recovered = int(result.get("recovered") or 0)
                result["ok"] = blocked_count == 0
                if checked == 0 and blocked_count == 0 and recovered == 0:
                    result.update({
                        "verified": True, "accepted": False,
                        "state": "NO_STALE_PRIORITY_JOBS",
                        "already_healthy": True,
                    })
            elif action == "PRIORITISE_EXACT_GAPS":
                if not symbol:
                    result = {"ok": False, "state": "NO_SELECTED_STOCK", "action": action}
                else:
                    result = dict(self.app.market_data.schedule_priority_stock_pipeline(
                        symbol, mode=mode if mode in {"delivery", "intraday"} else "delivery",
                        selected_interval=str(selected.get("interval") or "day"),
                        action="repair_gaps",
                    ) or {})
            elif action == "RUN_DATA_CONVEYOR":
                result = self.app.supervisor.recover(
                    "data_conveyor", reason=str(blocker.get("key")), action="BOUNDED_DATA_CONVEYOR_CYCLE"
                )
            elif action == "RECOVER_SCANNER_CAPACITY":
                component = str(blocker.get("component") or "delivery_scanner")
                result = self.app.supervisor.recover(
                    component, reason=str(blocker.get("key")), action="ROTATE_STALE_ANALYSIS_GENERATION"
                )
            elif action == "REBUILD_READ_MODELS":
                result = self.app.supervisor.recover(
                    "operator_read_models", reason=str(blocker.get("key")), action="REPLAY_AND_REFRESH_READ_MODELS"
                )
            elif action == "YIELD_BACKGROUND_WORK":
                governor = getattr(self.app, "workload_governor", None)
                if governor is None:
                    result = {"ok": False, "state": "GOVERNOR_UNAVAILABLE"}
                else:
                    state = governor.pause_bulk(seconds=120, reason=str(blocker.get("detail") or blocker.get("key")))
                    result = {"ok": True, "state": "BACKGROUND_YIELD_ACTIVE", "governor": state}
            elif action == "RECONCILE_DELIVERY_POPULATION":
                result = self.app.supervisor.recover(
                    "delivery_scanner", reason=str(blocker.get("detail") or blocker.get("key")), action="REBUILD_IMMUTABLE_DELIVERY_POPULATION"
                )
            else:
                result = {"ok": False, "state": "EVIDENCE_REQUIRED", "action": action}
        except Exception as exc:
            result = {"ok": False, "state": "ACTION_EXCEPTION", "action": action, "error": str(exc)[:300]}
        result = {"action": action, "blocker": blocker.get("key"), **dict(result or {})}
        component = str(blocker.get("component") or "")
        after_rows = {row.get("component"): self._business_progress_signature(row) for row in self._component_rows()}
        if result.get("ok"):
            if "verified" not in result:
                if component and component in before_rows and component in after_rows:
                    result["verified"] = before_rows[component] != after_rows[component] or str(result.get("state") or "").upper() in {"RESTARTED", "RECOVERED", "QUEUED", "RUNNING", "COMPLETE"}
                else:
                    result["verified"] = str(result.get("state") or "").upper() not in {"FAILED", "BLOCKED", "ACTION_EXCEPTION"}
            if not result["verified"]:
                result["state"] = "ACTION_ACCEPTED_PENDING_VERIFICATION"
                result["accepted"] = True
                result["ok"] = True
                self._pending_actions[key] = {
                    "component": component,
                    "before_signature": before_rows.get(component, ""),
                    "accepted_monotonic": time.monotonic(),
                    "accepted_at": now_iso(),
                    "action": action,
                    "blocker": blocker.get("key"),
                }
        else:
            result["verified"] = False
        self._record_action(key, result)
        self.publish("CONTROLLER_ACTION", str(blocker.get("component") or "controller"), result)
        return result

    def _dispatch_snapshot(self) -> Dict[str, Any]:
        now = time.monotonic()
        with self._dispatch_lock:
            rows = {
                key: {
                    **{k: v for k, v in row.items() if k != "thread"},
                    "age_sec": round(max(0.0, now - float(row.get("started_monotonic") or now)), 2),
                    "alive": bool(row.get("thread") and row["thread"].is_alive()),
                }
                for key, row in self._inflight_dispatch.items()
            }
        return {"limit": self._dispatch_limit, "inflight": rows, "count": len(rows)}

    def _reconcile_dispatch_timeouts(self) -> list[Dict[str, Any]]:
        now = time.monotonic()
        timed_out: list[Dict[str, Any]] = []
        with self._dispatch_lock:
            rows = list(self._inflight_dispatch.items())
        for key, row in rows:
            age = now - float(row.get("started_monotonic") or now)
            thread = row.get("thread")
            if age < self._dispatch_timeout_sec or not (thread and thread.is_alive()):
                continue
            if not row.get("timeout_reported"):
                row["timeout_reported"] = True
                self._circuits[key] = {
                    "opened_at": now_iso(), "failures": self.ACTION_FAILURE_LIMIT,
                    "last_result": {"state": "RECOVERY_DISPATCH_TIMEOUT", "age_sec": round(age, 2)},
                }
                detail = {
                    "key": key, "action": row.get("action"), "component": row.get("component"),
                    "age_sec": round(age, 2), "state": "RECOVERY_DISPATCH_TIMEOUT",
                    "note": "Recovery worker remains isolated; controller evaluation continues fail-closed.",
                }
                self.publish("RECOVERY_DISPATCH_TIMEOUT", str(row.get("component") or "controller"), detail)
                timed_out.append(detail)
        return timed_out

    def _run_action(self, blocker: Mapping[str, Any]) -> Dict[str, Any]:
        """Dispatch one safe playbook without blocking controller evaluation."""
        action = str(blocker.get("safe_action") or "EVIDENCE_REQUIRED")
        key = f"{blocker.get('key')}:{action}"
        if key in self._circuits:
            return {"ok": False, "verified": False, "state": "CIRCUIT_OPEN", "action": action, "key": key, "circuit": self._circuits[key]}
        if key in self._pending_actions:
            return {"ok": True, "accepted": True, "verified": False, "state": "ACTION_PENDING_VERIFICATION", "action": action, "key": key}
        if not self._cooldown_ready(key):
            return {"ok": True, "accepted": False, "verified": False, "state": "COOLDOWN", "action": action, "key": key}
        with self._dispatch_lock:
            existing = self._inflight_dispatch.get(key)
            if existing and existing.get("thread") and existing["thread"].is_alive():
                return {"ok": True, "accepted": True, "verified": False, "state": "ACTION_ALREADY_DISPATCHED", "action": action, "key": key}
            alive_count = sum(1 for row in self._inflight_dispatch.values() if row.get("thread") and row["thread"].is_alive())
            if alive_count >= self._dispatch_limit:
                return {"ok": False, "accepted": False, "verified": False, "state": "RECOVERY_DISPATCH_CAPACITY", "action": action, "key": key}

            def work() -> None:
                try:
                    self._execute_action_sync(dict(blocker))
                except Exception as exc:
                    result = {"ok": False, "verified": False, "state": "RECOVERY_DISPATCH_EXCEPTION", "action": action, "key": key, "error": str(exc)[:300]}
                    self._record_action(key, result)
                    self.publish("CONTROLLER_ACTION_DISPATCH_FAILED", str(blocker.get("component") or "controller"), result)
                finally:
                    with self._dispatch_lock:
                        self._inflight_dispatch.pop(key, None)

            thread = threading.Thread(target=work, name=f"LadduRecovery-{hashlib.sha256(key.encode()).hexdigest()[:8]}", daemon=True)
            self._inflight_dispatch[key] = {
                "thread": thread, "started_monotonic": time.monotonic(), "started_at": now_iso(),
                "action": action, "component": str(blocker.get("component") or ""), "blocker": blocker.get("key"),
                "timeout_reported": False,
            }
            thread.start()
        detail = {"ok": True, "accepted": True, "verified": False, "state": "ACTION_DISPATCHED", "action": action, "key": key, "component": blocker.get("component")}
        self.publish("CONTROLLER_ACTION_DISPATCHED", str(blocker.get("component") or "controller"), detail)
        return detail

    # -------------------------------------------------------------- lifecycle
    def evaluate(self, *, allow_action: bool = True, _generation: int | None = None) -> Dict[str, Any]:
        started = time.monotonic()
        components = self._component_rows()
        product, proof = self._maturity()
        historical = self._historical_state()
        dispatch_timeouts = self._reconcile_dispatch_timeouts()
        verified_actions = self._verify_pending_actions(components)
        blockers = self._blockers(components=components, product=product, proof=proof, historical=historical)
        primary = blockers[0] if blockers else None
        action_target = next((
            row for row in blockers
            if bool(row.get("actionable", str(row.get("safe_action")) not in {"EVIDENCE_REQUIRED", "ESCALATE", "OBSERVE_PROGRESS"}))
            and str(row.get("safe_action")) not in {"EVIDENCE_REQUIRED", "ESCALATE", "OBSERVE_PROGRESS"}
        ), None)
        action_result = None
        if allow_action and action_target:
            action_result = self._run_action(action_target)
        elif allow_action and blockers:
            self.publish("CONTROLLER_NO_SAFE_ACTION", "autonomic_controller", {
                "primary_blocker": primary,
                "reason": "No allow-listed actionable blocker is currently eligible",
            })
        self._cycle += 1
        maturity_level = int(product.get("maturity_level") or 0)
        level5_ready = bool(
            maturity_level == 5
            and product.get("level5_ready") is True
            and proof.get("passed") is True
        )
        payload = {
            "ok": True,
            "version": self.VERSION,
            "build": APP_VERSION,
            "state": "LEVEL5_CERTIFIED" if level5_ready else "RECOVERING" if action_result and action_result.get("ok") else "BLOCKED" if blockers else "MONITORING",
            "cycle": self._cycle,
            "maturity_level": maturity_level,
            "maturity_max": 5,
            "level5_ready": level5_ready,
            "primary_blocker": primary,
            "action_target": action_target,
            "blockers": blockers[:40],
            "components": components,
            "historical_data": historical,
            "last_data_event": self._last_data_event or None,
            "last_action": action_result,
            "actions": {
                "attempts": dict(self._action_attempts),
                "successes": dict(self._action_successes),
                "failures": dict(self._action_failures),
                "circuits": dict(self._circuits),
                "pending_verification": dict(self._pending_actions),
                "verified_this_cycle": verified_actions,
                "dispatch": self._dispatch_snapshot(),
                "dispatch_timeouts_this_cycle": dispatch_timeouts,
                "policy": "one bounded allow-listed action per cycle; concrete recoverable jobs outrank broad maturity work; accepted actions remain pending until measurable progress verifies them; trading/risk/ledger/model authority remains fail-closed",
            },
            "event_bus": self.event_bus.snapshot(),
            "production_change_allowed": False,
            "broker_authority": "NONE",
            "evaluated_at": now_iso(),
            "elapsed_ms": round((time.monotonic() - started) * 1000.0, 2),
        }
        publish = True
        if _generation is not None:
            with self._evaluation_lock:
                publish = int(_generation) == int(self._evaluation_generation)
        if publish:
            with self._lock:
                self._last_snapshot = payload
            try:
                if self.audit_writer is not None:
                    self.audit_writer.submit(self.KV_KEY, payload)
            except Exception:
                pass
        return payload

    def _evaluation_state(self) -> Dict[str, Any]:
        with self._evaluation_lock:
            thread = self._evaluation_thread
            alive = bool(thread and thread.is_alive())
            age = time.monotonic() - self._evaluation_started_monotonic if alive and self._evaluation_started_monotonic else 0.0
            retired = [
                {key: value for key, value in row.items() if key != "thread"}
                | {"alive": bool(row.get("thread") and row["thread"].is_alive())}
                for row in list(self._retired_evaluations)
            ]
            return {
                "generation": self._evaluation_generation,
                "alive": alive,
                "age_sec": round(age, 3),
                "started_at": self._evaluation_started_at,
                "timeout_sec": self._evaluation_timeout_sec,
                "retired": retired,
                "last_error": self._evaluation_last_error,
            }

    def request_evaluation(self, *, allow_action: bool = True, reason: str = "scheduled") -> Dict[str, Any]:
        """Request a controller cycle without ever blocking the caller.

        This is the only entry point used by HTTP/operator surfaces and the
        supervised scheduler.  A wedged evaluation cannot make Operations or
        the controller heartbeat disappear.
        """
        now_mono = time.monotonic()
        with self._evaluation_lock:
            current = self._evaluation_thread
            if current is not None and current.is_alive():
                age = now_mono - self._evaluation_started_monotonic
                if age < self._evaluation_timeout_sec:
                    return {"ok": True, "accepted": False, "state": "EVALUATION_IN_PROGRESS", **self._evaluation_state()}
                # Fence the old generation. At most two still-alive retired
                # generations are tolerated; beyond that fail closed instead
                # of creating unbounded Python threads.
                retired_alive = sum(1 for row in self._retired_evaluations if row.get("thread") and row["thread"].is_alive())
                if retired_alive >= self._retired_evaluations.maxlen:
                    return {"ok": False, "accepted": False, "state": "EVALUATION_CIRCUIT_OPEN", **self._evaluation_state()}
                self._retired_evaluations.append({
                    "thread": current, "generation": self._evaluation_generation,
                    "started_at": self._evaluation_started_at, "timed_out_at": now_iso(),
                    "age_sec": round(age, 3), "reason": reason,
                })
                self._evaluation_generation += 1
                self._evaluation_thread = None
                self._evaluation_started_monotonic = 0.0
                self._evaluation_started_at = None
                try:
                    self.publish("CONTROLLER_EVALUATION_TIMEOUT", "autonomic_controller", {
                        "generation": self._evaluation_generation - 1, "age_sec": round(age, 3),
                        "note": "timed-out evaluation fenced; scheduler remains responsive",
                    })
                except Exception:
                    pass

            generation = self._evaluation_generation + 1
            self._evaluation_generation = generation
            self._evaluation_started_monotonic = time.monotonic()
            self._evaluation_started_at = now_iso()
            self._evaluation_last_error = None

            def work() -> None:
                try:
                    self.evaluate(allow_action=allow_action, _generation=generation)
                except Exception as exc:
                    with self._evaluation_lock:
                        if generation == self._evaluation_generation:
                            self._evaluation_last_error = f"{type(exc).__name__}: {exc}"[:400]
                    try:
                        self.publish("CONTROLLER_EVALUATION_FAILED", "autonomic_controller", {
                            "generation": generation, "error": str(exc)[:400],
                        })
                    except Exception:
                        pass

            thread = threading.Thread(target=work, name=f"LadduControllerEval-{generation}", daemon=True)
            self._evaluation_thread = thread
            thread.start()
        return {"ok": True, "accepted": True, "state": "EVALUATION_DISPATCHED", **self._evaluation_state()}

    def snapshot(self, *, refresh: bool = False) -> Dict[str, Any]:
        with self._lock:
            current = dict(self._last_snapshot)
        if refresh or not current:
            self.request_evaluation(allow_action=False, reason="snapshot_refresh")
        if current:
            current["evaluation_watchdog"] = self._evaluation_state()
            return current
        return {
            "ok": True, "version": self.VERSION, "build": APP_VERSION,
            "state": "STARTING", "cycle": self._cycle, "blockers": [],
            "primary_blocker": None, "evaluation_watchdog": self._evaluation_state(),
            "message": "Controller projection is warming; this read never executes maturity or recovery work inline.",
        }

    def events(self, *, after_sequence: int = 0, limit: int = 200) -> Dict[str, Any]:
        return {
            "ok": True,
            "version": self.VERSION,
            "build": APP_VERSION,
            "event_bus": self.event_bus.snapshot(),
            "events": self.event_bus.events(after_sequence=after_sequence, limit=limit),
        }

    def loop(self, sup=None, *, running_fn: Callable[[], bool]) -> None:
        """Non-blocking controller scheduler/watchdog.

        The scheduler never executes evaluation or recovery inline. Its own
        heartbeat therefore remains an independent authority even when a
        generation is fenced as timed out.
        """
        name = "autonomic_controller"
        next_cycle = 0.0
        while running_fn() and (sup is None or sup.running):
            now_mono = time.monotonic()
            if sup is not None:
                sup.beat(name)
            if now_mono >= next_cycle:
                request = self.request_evaluation(allow_action=True, reason="supervised_cycle")
                next_cycle = now_mono + 30.0
                if sup is not None:
                    snapshot = self.snapshot(refresh=False)
                    watch = dict(snapshot.get("evaluation_watchdog") or self._evaluation_state())
                    state = str(request.get("state") or "MONITORING")
                    sup.progress(
                        name,
                        token=f"cycle:{self._cycle}|gen:{watch.get('generation')}|{state}",
                        stage="controller_watchdog" if watch.get("alive") else "monitoring",
                        current_item=(snapshot.get("primary_blocker") or {}).get("key") or state,
                        completed_units=int(snapshot.get("maturity_level") or 0),
                        total_units=5,
                    )
            time.sleep(0.5)
