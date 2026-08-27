"""Durable priority-stock pipeline authority with lease recovery.

One resumable job exists per (desk, symbol). Stages never become READY from a
heartbeat alone. Progress, evidence snapshots and stale-lease recovery are
persisted so browser/service restarts cannot hide or duplicate work.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import threading
import time
from typing import Any, Dict, Iterable
from uuid import uuid4

from models import now_iso


STAGES = (
    (1, "identity", "Identity"),
    (2, "coverage", "Exact-gap coverage"),
    (3, "corporate_actions", "Corporate actions"),
    (4, "timeframes", "Canonical timeframes"),
    (5, "mathematics", "Mathematics"),
    (6, "features", "Feature snapshot"),
    (7, "inference", "Model inference"),
    (8, "risk", "Risk admission"),
    (9, "publication", "Decision publication"),
    (10, "outcome", "Outcome / training population"),
)


class PriorityPipelineService:
    VERSION = "priority-pipeline-authority-3.3.0-auto-stale-reconciliation"
    DEFAULT_LEASE_SECONDS = 180
    DEFAULT_STALE_SECONDS = 240
    MAX_RECOVERIES = 3

    def __init__(self, app: Any):
        self.app = app
        self.store = app.store
        plane = getattr(app, "production_data_plane", None)
        self.write_db = getattr(plane, "operational", None)
        self.read_db = getattr(plane, "interactive", None) or self.write_db
        # Compatibility alias for mutation paths. Reads are explicitly routed
        # through read_db below so selected-stock/Operations status cannot queue
        # behind scanner and bulk write transactions.
        self.db = self.write_db
        # Operator HTTP requests persist the priority job first, then dispatch
        # exact-gap inspection asynchronously. One in-process dispatch per
        # symbol/desk is enough because the underlying historical jobs are
        # themselves exact-gap single-flight/coalesced.
        self._dispatch_lock = threading.RLock()
        self._dispatching: set[str] = set()
        # One automatic reconciliation per exhausted (desk,symbol,stage) in this
        # runtime. This releases stale-lease poison without creating an infinite
        # recovery loop when the underlying physical dependency is still absent.
        self._auto_reconciled_exhausted: set[str] = set()

    @staticmethod
    def _mode(value: str) -> str:
        mode = str(value or "delivery").lower().strip()
        if mode not in {"intraday", "delivery"}:
            raise ValueError("priority pipeline supports Intraday and Delivery only")
        return mode

    @staticmethod
    def _key(symbol: str, mode: str) -> str:
        return f"{mode}:{str(symbol or '').upper().strip()}"

    @staticmethod
    def _normalise_state(value: str) -> str:
        value = str(value or "WAITING").upper().strip().replace(" ", "_")
        return value if value in {"WAITING", "QUEUED", "RUNNING", "READY", "BLOCKED", "FAILED", "NOT_REQUIRED"} else "WAITING"

    def _kv_key(self, symbol: str, mode: str) -> str:
        return f"priority_pipeline:{self._key(symbol, mode)}"

    def _persist_fallback(self, payload: Dict[str, Any]) -> None:
        self.store.set_kv(self._kv_key(payload["symbol"], payload["mode"]), payload)

    def queue(self, *, symbol: str, instrument_key: str, mode: str, action: str, lease_owner: str = "priority_request") -> Dict[str, Any]:
        symbol = str(symbol or "").upper().strip()
        instrument_key = str(instrument_key or "").strip()
        mode = self._mode(mode)
        if not symbol or not instrument_key:
            raise ValueError("verified identity is required")
        job_id = str(uuid4())
        job_key = self._key(symbol, mode)
        if self.db is not None:
            row = self.db.execute(
                """INSERT INTO runtime_control.priority_pipeline_jobs(
                       job_id,job_key,symbol,instrument_key,mode,action,state,current_stage,
                       completed_stages,total_stages,progress_pct,started_at,updated_at,
                       last_progress_at,lease_owner,lease_expires_at,attempt_count)
                     VALUES(%s::uuid,%s,%s,%s,%s,%s,'RUNNING','coverage',1,%s,10,now(),now(),
                            now(),%s,now()+(%s||' seconds')::interval,1)
                     ON CONFLICT(job_key) DO UPDATE SET
                       instrument_key=EXCLUDED.instrument_key,
                       action=EXCLUDED.action,updated_at=now(),
                       attempt_count=runtime_control.priority_pipeline_jobs.attempt_count+1
                     RETURNING job_id::text""",
                (job_id, job_key, symbol, instrument_key, mode, str(action or "priority_sync"), len(STAGES), lease_owner, self.DEFAULT_LEASE_SECONDS),
                fetch="one", statement_timeout_ms=2500,
            )
            job_id = str((row or {}).get("job_id") or job_id)
            for order, key, label in STAGES:
                state = "READY" if key == "identity" else "RUNNING" if key == "coverage" else "WAITING"
                detail = "Verified instrument identity" if key == "identity" else "Inspecting local coverage and exact missing ranges" if key == "coverage" else "Waiting for prerequisite evidence"
                self.db.execute(
                    """INSERT INTO runtime_control.priority_pipeline_stages(
                           job_id,stage_order,stage_key,state,detail,evidence,started_at,updated_at,completed_at,
                           last_progress_at,lease_owner,lease_expires_at,attempt_count)
                         VALUES(%s::uuid,%s,%s,%s,%s,'{}'::jsonb,
                                CASE WHEN %s IN ('READY','RUNNING') THEN now() ELSE NULL END,now(),
                                CASE WHEN %s='READY' THEN now() ELSE NULL END,
                                CASE WHEN %s IN ('READY','RUNNING') THEN now() ELSE NULL END,
                                CASE WHEN %s='RUNNING' THEN %s ELSE NULL END,
                                CASE WHEN %s='RUNNING' THEN now()+(%s||' seconds')::interval ELSE NULL END,
                                CASE WHEN %s='RUNNING' THEN 1 ELSE 0 END)
                         ON CONFLICT(job_id,stage_key) DO NOTHING""",
                    (job_id, order, key, state, detail, state, state, state, state, lease_owner, state, self.DEFAULT_LEASE_SECONDS, state),
                    statement_timeout_ms=2500,
                )
            # A repeated selected-stock request is an idempotent ensure, not a
            # destructive pipeline reset. Only stale-lease failure states are
            # reconciled here; READY evidence remains intact.
            self.reconcile(symbol=symbol, mode=mode, reason="priority_request_reconcile")
        else:
            now = now_iso()
            payload = {
                "ok": True, "version": self.VERSION, "job_id": job_id, "job_key": job_key,
                "symbol": symbol, "instrument_key": instrument_key, "mode": mode,
                "action": action, "state": "RUNNING", "current_stage": "coverage",
                "progress_pct": 10.0, "updated_at": now, "last_progress_at": now,
                "lease_owner": lease_owner, "attempt_count": 1, "recovery_count": 0,
                "stages": [
                    {"stage_order": order, "stage_key": key, "label": label,
                     "state": "READY" if key == "identity" else "RUNNING" if key == "coverage" else "WAITING",
                     "detail": "Verified instrument identity" if key == "identity" else "Inspecting local coverage" if key == "coverage" else "Waiting for prerequisite evidence",
                     "last_progress_at": now if key in {"identity", "coverage"} else None,
                     "lease_owner": lease_owner if key == "coverage" else None,
                     "attempt_count": 1 if key == "coverage" else 0,
                     "recovery_count": 0}
                    for order, key, label in STAGES
                ],
            }
            self._persist_fallback(payload)
        return self.snapshot(symbol=symbol, mode=mode)

    def dispatch_history_schedule(self, *, symbol: str, mode: str, selected_interval: str = "day",
                                  action: str = "priority_sync") -> Dict[str, Any]:
        """Dispatch selected-stock exact-gap inspection off the HTTP thread.

        ``queue()`` is the durable acceptance boundary. This method only starts
        the bounded worker that inspects/schedules the canonical base intervals.
        A recovered stale coverage lease is re-dispatched by ``recovery_loop``.
        """
        symbol = str(symbol or "").upper().strip()
        mode = self._mode(mode)
        key = self._key(symbol, mode)
        with self._dispatch_lock:
            if key in self._dispatching:
                return {"ok": True, "accepted": True, "state": "COALESCED", "background": True, "job_key": key}
            self._dispatching.add(key)

        def work() -> None:
            try:
                scheduler = getattr(getattr(self.app, "market_data", None), "schedule_priority_stock_pipeline", None)
                if not callable(scheduler):
                    raise RuntimeError("selected-stock priority scheduler unavailable")
                scheduled = dict(scheduler(
                    symbol, mode=mode, selected_interval=selected_interval, action=action,
                ) or {})
                # Coverage workers are asynchronous. This dedicated priority
                # orchestrator may wait without holding an HTTP request, then
                # advances the stages that previously had no production owner.
                deadline = time.monotonic() + 90.0
                latest = self.snapshot(symbol=symbol, mode=mode)
                while time.monotonic() < deadline:
                    coverage = next((row for row in latest.get("stages") or [] if row.get("stage_key") == "coverage"), {})
                    if str(coverage.get("state") or "").upper() in {"READY", "BLOCKED", "FAILED"}:
                        break
                    time.sleep(1.0)
                    latest = self.snapshot(symbol=symbol, mode=mode)
                coverage = next((row for row in latest.get("stages") or [] if row.get("stage_key") == "coverage"), {})
                if str(coverage.get("state") or "").upper() == "READY":
                    identity_key = str(latest.get("instrument_key") or "")
                    latest = self.observe_corporate_action_coverage(
                        symbol=symbol, mode=mode, instrument_key=identity_key
                    )
                    corporate = next((row for row in latest.get("stages") or [] if row.get("stage_key") == "corporate_actions"), {})
                    if str(corporate.get("state") or "").upper() == "READY":
                        composer = getattr(self.app, "symbol_market_intelligence", None)
                        if callable(composer):
                            payload = dict(composer(symbol, mode=mode, refresh=False) or {})
                            latest = self.observe_intelligence(symbol=symbol, mode=mode, payload=payload)
                self.app.event("INFO", "priority_pipeline", "Background priority coverage scheduling completed", {
                    "symbol": symbol, "mode": mode, "action": action,
                    "state": scheduled.get("state"),
                    "pipeline_state": latest.get("state"),
                    "scheduled_intervals": int(scheduled.get("scheduled_intervals") or 0),
                    "current_or_coalesced_intervals": int(scheduled.get("current_or_coalesced_intervals") or 0),
                })
            except Exception as exc:
                try:
                    self.update_stage(
                        symbol=symbol, mode=mode, stage_key="coverage", state="FAILED",
                        detail=f"Priority coverage scheduling failed: {str(exc)[:180]}",
                        evidence={"action": action, "selected_interval": selected_interval},
                    )
                except Exception:
                    pass
                try:
                    self.app.event("ERROR", "priority_pipeline", "Background priority coverage scheduling failed", {
                        "symbol": symbol, "mode": mode, "action": action, "error": str(exc)[:240],
                    })
                except Exception:
                    pass
            finally:
                with self._dispatch_lock:
                    self._dispatching.discard(key)

        threading.Thread(target=work, name=f"LadduPriority-{mode}-{symbol[:16]}", daemon=True).start()
        return {"ok": True, "accepted": True, "state": "QUEUED", "background": True, "job_key": key}

    def _stage_rows(self, job_id: str) -> list[dict[str, Any]]:
        rows = self.read_db.execute(
            """SELECT stage_order,stage_key,state,completed_units,total_units,throughput_per_sec,
                      eta_low_seconds,eta_high_seconds,detail,evidence,started_at,updated_at,completed_at,
                      last_progress_at,lease_owner,lease_expires_at,attempt_count,recovery_count,last_error
                 FROM runtime_control.priority_pipeline_stages
                WHERE job_id=%s::uuid ORDER BY stage_order""",
            (job_id,), fetch="all", statement_timeout_ms=1800,
        ) or []
        labels = {key: label for _, key, label in STAGES}
        return [{**dict(row), "label": labels.get(str(row.get("stage_key")), str(row.get("stage_key")))} for row in rows]

    def snapshot(self, *, symbol: str, mode: str) -> Dict[str, Any]:
        symbol = str(symbol or "").upper().strip()
        mode = self._mode(mode)
        if self.read_db is None:
            payload = dict(self.store.get_kv(self._kv_key(symbol, mode), {}) or {})
            if payload:
                return payload
            return {"ok": True, "version": self.VERSION, "symbol": symbol, "mode": mode, "state": "NOT_STARTED", "progress_pct": 0.0, "stages": []}
        row = self.read_db.execute(
            """SELECT job_id::text,job_key,symbol,instrument_key,mode,action,state,current_stage,
                      completed_stages,total_stages,progress_pct,eta_low_seconds,eta_high_seconds,
                      blocker,created_at,started_at,updated_at,completed_at,last_progress_at,
                      lease_owner,lease_expires_at,attempt_count,recovery_count,last_error
                 FROM runtime_control.priority_pipeline_jobs
                WHERE symbol=%s AND mode=%s ORDER BY updated_at DESC LIMIT 1""",
            (symbol, mode), fetch="one", statement_timeout_ms=1800,
        )
        if not row:
            return {"ok": True, "version": self.VERSION, "symbol": symbol, "mode": mode, "state": "NOT_STARTED", "progress_pct": 0.0, "stages": []}
        payload = dict(row)
        payload.update({"ok": True, "version": self.VERSION, "stages": self._stage_rows(payload["job_id"])})
        return payload

    def heartbeat(self, *, symbol: str, mode: str, stage_key: str, lease_owner: str, lease_seconds: int | None = None) -> Dict[str, Any]:
        symbol = str(symbol or "").upper().strip(); mode = self._mode(mode)
        snapshot = self.snapshot(symbol=symbol, mode=mode)
        if snapshot.get("state") == "NOT_STARTED":
            return snapshot
        lease = max(30, int(lease_seconds or self.DEFAULT_LEASE_SECONDS))
        if self.db is None:
            now = now_iso()
            for row in snapshot.get("stages") or []:
                if row.get("stage_key") == stage_key and row.get("state") in {"RUNNING", "QUEUED"}:
                    row.update({"lease_owner": lease_owner, "updated_at": now})
            snapshot.update({"lease_owner": lease_owner, "updated_at": now})
            self._persist_fallback(snapshot)
            return snapshot
        self.db.execute(
            """UPDATE runtime_control.priority_pipeline_stages
                  SET lease_owner=%s,lease_expires_at=now()+(%s||' seconds')::interval,updated_at=now()
                WHERE job_id=%s::uuid AND stage_key=%s AND state IN ('RUNNING','QUEUED')""",
            (lease_owner, lease, snapshot["job_id"], stage_key), statement_timeout_ms=1800,
        )
        self.db.execute(
            """UPDATE runtime_control.priority_pipeline_jobs
                  SET lease_owner=%s,lease_expires_at=now()+(%s||' seconds')::interval,updated_at=now()
                WHERE job_id=%s::uuid""",
            (lease_owner, lease, snapshot["job_id"]), statement_timeout_ms=1800,
        )
        return self.snapshot(symbol=symbol, mode=mode)

    def update_stage(
        self, *, symbol: str, mode: str, stage_key: str, state: str,
        detail: str = "", completed_units: int | None = None, total_units: int | None = None,
        evidence: Dict[str, Any] | None = None, lease_owner: str = "pipeline_observer",
    ) -> Dict[str, Any]:
        symbol = str(symbol or "").upper().strip(); mode = self._mode(mode)
        state = self._normalise_state(state)
        snapshot = self.snapshot(symbol=symbol, mode=mode)
        if snapshot.get("state") == "NOT_STARTED":
            return snapshot
        if self.db is None:
            stages = list(snapshot.get("stages") or [])
            for row in stages:
                if row.get("stage_key") == stage_key:
                    prior = (row.get("state"), row.get("completed_units"), row.get("total_units"))
                    current = (state, completed_units, total_units)
                    row.update({"state": state, "detail": detail or row.get("detail"), "completed_units": completed_units, "total_units": total_units, "evidence": evidence or {}, "updated_at": now_iso(), "lease_owner": lease_owner if state in {"RUNNING", "QUEUED"} else None})
                    if prior != current:
                        row["last_progress_at"] = now_iso()
            ready = sum(1 for row in stages if row.get("state") in {"READY", "NOT_REQUIRED"})
            failed = next((row for row in stages if row.get("state") in {"FAILED", "BLOCKED"}), None)
            snapshot.update({"stages": stages, "completed_stages": ready, "progress_pct": round(ready * 100 / len(STAGES), 1), "current_stage": next((row.get("stage_key") for row in stages if row.get("state") in {"RUNNING", "QUEUED", "WAITING"}), None), "state": "BLOCKED" if failed else "READY" if ready == len(STAGES) else "RUNNING", "blocker": (failed or {}).get("detail"), "updated_at": now_iso(), "last_progress_at": now_iso()})
            self._persist_fallback(snapshot)
            return snapshot
        job_id = snapshot["job_id"]
        throughput = None
        eta_low = eta_high = None
        stage_before = next((row for row in snapshot.get("stages") or [] if row.get("stage_key") == stage_key), {})
        progress_changed = (
            str(stage_before.get("state") or "") != state
            or stage_before.get("completed_units") != completed_units
            or stage_before.get("total_units") != total_units
        )
        if completed_units is not None and total_units is not None and int(total_units) > int(completed_units):
            started_at = stage_before.get("started_at")
            if isinstance(started_at, datetime):
                elapsed = max(0.001, (datetime.now(timezone.utc) - started_at.astimezone(timezone.utc)).total_seconds())
                throughput = max(0.0, float(completed_units) / elapsed)
                if throughput > 0:
                    remaining = max(0, int(total_units) - int(completed_units))
                    estimate = remaining / throughput
                    eta_low = max(1, int(estimate * 0.8))
                    eta_high = max(eta_low, int(estimate * 1.35))
        self.db.execute(
            """UPDATE runtime_control.priority_pipeline_stages SET
                    state=%s,detail=COALESCE(NULLIF(%s,''),detail),completed_units=%s,total_units=%s,
                    throughput_per_sec=COALESCE(%s,throughput_per_sec),
                    eta_low_seconds=%s,eta_high_seconds=%s,evidence=%s::jsonb,
                    started_at=COALESCE(started_at,CASE WHEN %s IN ('RUNNING','READY') THEN now() ELSE NULL END),
                    updated_at=now(),completed_at=CASE WHEN %s IN ('READY','NOT_REQUIRED') THEN now() ELSE NULL END,
                    last_progress_at=CASE WHEN %s THEN now() ELSE COALESCE(last_progress_at,now()) END,
                    lease_owner=CASE WHEN %s IN ('RUNNING','QUEUED') THEN %s ELSE NULL END,
                    lease_expires_at=CASE WHEN %s IN ('RUNNING','QUEUED') THEN now()+(%s||' seconds')::interval ELSE NULL END,
                    last_error=CASE WHEN %s='FAILED' THEN COALESCE(NULLIF(%s,''),last_error) ELSE NULL END
                  WHERE job_id=%s::uuid AND stage_key=%s""",
            (state, detail, completed_units, total_units, throughput, eta_low, eta_high,
             json.dumps(evidence or {}), state, state, progress_changed, state, lease_owner,
             state, self.DEFAULT_LEASE_SECONDS, state, detail, job_id, stage_key),
            statement_timeout_ms=2500,
        )
        rows = self._stage_rows(job_id)
        completed = sum(1 for row in rows if row.get("state") in {"READY", "NOT_REQUIRED"})
        failed = next((row for row in rows if row.get("state") in {"FAILED", "BLOCKED"}), None)
        current = next((row for row in rows if row.get("state") in {"RUNNING", "QUEUED"}), None) or next((row for row in rows if row.get("state") == "WAITING"), None)
        job_state = "BLOCKED" if failed else "READY" if completed == len(STAGES) else "RUNNING"
        self.db.execute(
            """UPDATE runtime_control.priority_pipeline_jobs SET state=%s,current_stage=%s,
                    completed_stages=%s,progress_pct=%s,eta_low_seconds=%s,eta_high_seconds=%s,
                    blocker=%s,updated_at=now(),last_progress_at=CASE WHEN %s THEN now() ELSE COALESCE(last_progress_at,now()) END,
                    lease_owner=%s,lease_expires_at=%s,last_error=%s,
                    completed_at=CASE WHEN %s='READY' THEN now() ELSE NULL END
                  WHERE job_id=%s::uuid""",
            (job_state, current.get("stage_key") if current else None, completed,
             round(completed * 100.0 / len(STAGES), 1), (current or {}).get("eta_low_seconds"),
             (current or {}).get("eta_high_seconds"), (failed or {}).get("detail"), progress_changed,
             (current or {}).get("lease_owner"), (current or {}).get("lease_expires_at"),
             (failed or {}).get("last_error"), job_state, job_id),
            statement_timeout_ms=2500,
        )
        return self.snapshot(symbol=symbol, mode=mode)

    def reconcile(self, *, symbol: str, mode: str, reason: str = "evidence_reconcile") -> Dict[str, Any]:
        """Release stale-lease poison without leasing every downstream stage.

        v119 could move every exhausted WAITING/BLOCKED stage back to QUEUED in
        one statement.  Because QUEUED owns a lease, prerequisites and all of
        their dependants then expired together and repeatedly exhausted recovery
        budgets.  Reconciliation now activates only the earliest exhausted
        stage; later exhausted stages become plain WAITING with no lease.
        """
        symbol = str(symbol or "").upper().strip()
        mode = self._mode(mode)
        snapshot = self.snapshot(symbol=symbol, mode=mode)
        if snapshot.get("state") == "NOT_STARTED" or self.write_db is None:
            return snapshot
        job_id = str(snapshot.get("job_id") or "")
        if not job_id:
            return snapshot
        stages = list(snapshot.get("stages") or [])
        exhausted = [
            row for row in stages
            if str(row.get("last_error") or "") == "STALE_LEASE_RECOVERY_LIMIT"
            and str(row.get("state") or "").upper() in {"WAITING", "BLOCKED", "QUEUED", "RUNNING"}
        ]
        exhausted.sort(key=lambda row: int(row.get("stage_order") or 999))
        changed = 0
        activated = None
        if exhausted:
            activated = str(exhausted[0].get("stage_key") or "")
            changed += int(self.write_db.execute(
                """UPDATE runtime_control.priority_pipeline_stages
                      SET state='QUEUED',detail=%s,lease_owner=NULL,lease_expires_at=NULL,
                          recovery_count=0,updated_at=now(),last_progress_at=now(),last_error=NULL
                    WHERE job_id=%s::uuid AND stage_key=%s""",
                (f"Reconciled stale lease · {str(reason or 'evidence_reconcile')[:180]}", job_id, activated),
                statement_timeout_ms=1800,
            ) or 0)
            later = [str(row.get("stage_key") or "") for row in exhausted[1:] if row.get("stage_key")]
            if later:
                changed += int(self.write_db.execute(
                    """UPDATE runtime_control.priority_pipeline_stages
                          SET state='WAITING',detail='Waiting for prerequisite evidence after stale-lease reconciliation',
                              lease_owner=NULL,lease_expires_at=NULL,recovery_count=0,
                              updated_at=now(),last_error=NULL
                        WHERE job_id=%s::uuid AND stage_key = ANY(%s)""",
                    (job_id, later), statement_timeout_ms=1800,
                ) or 0)
        rows = self._stage_rows(job_id)
        completed = sum(1 for row in rows if row.get("state") in {"READY", "NOT_REQUIRED"})
        failed = next((row for row in rows if row.get("state") in {"FAILED", "BLOCKED"}), None)
        current = next((row for row in rows if row.get("state") in {"RUNNING", "QUEUED"}), None) or next((row for row in rows if row.get("state") == "WAITING"), None)
        job_state = "BLOCKED" if failed else "READY" if completed == len(STAGES) else "RUNNING"
        self.write_db.execute(
            """UPDATE runtime_control.priority_pipeline_jobs
                  SET state=%s,current_stage=%s,completed_stages=%s,progress_pct=%s,
                      blocker=%s,last_error=%s,lease_owner=NULL,lease_expires_at=NULL,
                      recovery_count=CASE WHEN %s>0 THEN 0 ELSE recovery_count END,
                      updated_at=now(),last_progress_at=CASE WHEN %s>0 THEN now() ELSE last_progress_at END
                WHERE job_id=%s::uuid""",
            (job_state, (current or {}).get("stage_key"), completed, round(completed * 100.0 / len(STAGES), 1),
             (failed or {}).get("detail"), (failed or {}).get("last_error"), changed, changed, job_id),
            statement_timeout_ms=1800,
        )
        payload = self.snapshot(symbol=symbol, mode=mode)
        payload["reconciled_stages"] = int(changed or 0)
        payload["activated_stage"] = activated
        payload["reconcile_reason"] = reason
        return payload

    def recover_stale(self, *, stale_seconds: int | None = None, max_recoveries: int | None = None) -> Dict[str, Any]:
        stale = max(60, int(stale_seconds or self.DEFAULT_STALE_SECONDS))
        cap = max(1, int(max_recoveries or self.MAX_RECOVERIES))
        if self.db is None:
            recovered = blocked = 0
            # Fallback stores are per-symbol and cannot be enumerated safely;
            # deterministic tests use recover_payload below.
            return {"ok": True, "version": self.VERSION, "state": "FALLBACK_NO_ENUMERATION", "recovered": recovered, "blocked": blocked, "stale_seconds": stale}
        rows = self.read_db.execute(
            """SELECT j.job_id::text,j.symbol,j.mode,s.stage_key,s.state,s.recovery_count,s.lease_owner
                 FROM runtime_control.priority_pipeline_jobs j
                 JOIN runtime_control.priority_pipeline_stages s ON s.job_id=j.job_id
                WHERE j.state='RUNNING' AND s.state IN ('RUNNING','QUEUED')
                  AND COALESCE(s.last_progress_at,s.updated_at) < now()-(%s||' seconds')::interval
                ORDER BY COALESCE(s.last_progress_at,s.updated_at) ASC LIMIT 100""",
            (stale,), fetch="all", statement_timeout_ms=2500,
        ) or []
        recovered = blocked = 0
        events = []
        for raw in rows:
            row = dict(raw); count = int(row.get("recovery_count") or 0) + 1
            exhausted = count > cap
            new_state = "WAITING" if exhausted else "QUEUED"
            reason = "STALE_LEASE_RECOVERY_LIMIT" if exhausted else "STALE_LEASE_RECOVERED"
            detail = "Recovery budget exhausted; waiting for physical evidence or explicit reconciliation" if exhausted else "Recovered stale lease; waiting for bounded retry"
            self.db.execute(
                """UPDATE runtime_control.priority_pipeline_stages
                      SET state=%s,detail=%s,recovery_count=%s,lease_owner=NULL,lease_expires_at=NULL,
                          last_error=%s,updated_at=now(),last_progress_at=now()
                    WHERE job_id=%s::uuid AND stage_key=%s""",
                # Exhausting stale-lease retries is a recoverable WAITING state.
                # Persist the reason on the stage so explicit reconciliation can
                # recognise it; v116 accidentally wrote NULL here, making the
                # job impossible for reconcile() to release later.
                (new_state, detail, count, reason if exhausted else None, row["job_id"], row["stage_key"]),
                statement_timeout_ms=1800,
            )
            self.db.execute(
                """INSERT INTO runtime_control.priority_pipeline_recovery_events(
                       job_id,stage_key,prior_state,new_state,reason,lease_owner,recovery_count)
                     VALUES(%s::uuid,%s,%s,%s,%s,%s,%s)""",
                (row["job_id"], row["stage_key"], row.get("state"), new_state, reason, row.get("lease_owner"), count),
                statement_timeout_ms=1800,
            )
            if exhausted:
                blocked += 1
                self.db.execute(
                    """UPDATE runtime_control.priority_pipeline_jobs
                          SET state='BLOCKED',blocker=%s,last_error=%s,recovery_count=recovery_count+1,
                              lease_owner=NULL,lease_expires_at=NULL,updated_at=now(),last_progress_at=now()
                        WHERE job_id=%s::uuid""",
                    (detail, reason, row["job_id"]), statement_timeout_ms=1800,
                )
            else:
                recovered += 1
                self.db.execute(
                    """UPDATE runtime_control.priority_pipeline_jobs
                          SET state='RUNNING',current_stage=%s,recovery_count=recovery_count+1,
                              lease_owner=NULL,lease_expires_at=NULL,updated_at=now(),last_progress_at=now()
                        WHERE job_id=%s::uuid""",
                    (row["stage_key"], row["job_id"]), statement_timeout_ms=1800,
                )
            events.append({"symbol": row.get("symbol"), "mode": row.get("mode"), "stage_key": row.get("stage_key"), "new_state": new_state, "recovery_count": count, "reason": reason})
        return {"ok": True, "version": self.VERSION, "state": "RECOVERED" if recovered else "BLOCKED" if blocked else "HEALTHY", "recovered": recovered, "blocked": blocked, "checked": len(rows), "stale_seconds": stale, "max_recoveries": cap, "events": events}

    @staticmethod
    def recover_payload(payload: Dict[str, Any], *, stale: bool = True, max_recoveries: int = 3) -> Dict[str, Any]:
        """Pure fallback used by fault-injection and unit tests."""
        out = json.loads(json.dumps(payload or {}))
        if not stale:
            return out
        stages = list(out.get("stages") or [])
        row = next((item for item in stages if item.get("state") in {"RUNNING", "QUEUED"}), None)
        if not row:
            return out
        count = int(row.get("recovery_count") or 0) + 1
        if count > max_recoveries:
            row.update({"state": "WAITING", "detail": "Recovery budget exhausted; waiting for physical evidence or explicit reconciliation", "last_error": "STALE_LEASE_RECOVERY_LIMIT", "recovery_count": count, "lease_owner": None})
            out.update({"state": "BLOCKED", "blocker": row["detail"], "last_error": row["last_error"]})
        else:
            row.update({"state": "QUEUED", "detail": "Recovered stale lease; waiting for bounded retry", "recovery_count": count, "lease_owner": None})
            out.update({"state": "RUNNING", "current_stage": row.get("stage_key"), "recovery_count": int(out.get("recovery_count") or 0) + 1})
        out["updated_at"] = now_iso(); out["last_progress_at"] = now_iso()
        return out

    def recovery_status(self) -> Dict[str, Any]:
        if self.db is None:
            return {"ok": True, "version": self.VERSION, "state": "FALLBACK", "running": 0, "stale": 0, "blocked": 0, "recent_events": []}
        counts = self.read_db.execute(
            """SELECT
                 COUNT(*) FILTER (WHERE state='RUNNING') AS running,
                 COUNT(*) FILTER (WHERE state='BLOCKED' AND COALESCE(last_error,'') NOT IN ('','STALE_LEASE_RECOVERY_LIMIT')) AS blocked,
                 COUNT(*) FILTER (WHERE state='BLOCKED' AND COALESCE(last_error,'') = '') AS policy_blocked,
                 COUNT(*) FILTER (WHERE state='BLOCKED' AND COALESCE(last_error,'') = 'STALE_LEASE_RECOVERY_LIMIT') AS waiting_reconciliation,
                 COUNT(*) FILTER (WHERE state='RUNNING' AND COALESCE(last_progress_at,updated_at)<now()-(%s||' seconds')::interval) AS stale
               FROM runtime_control.priority_pipeline_jobs""",
            (self.DEFAULT_STALE_SECONDS,), fetch="one", statement_timeout_ms=1800,
        ) or {}
        events = self.read_db.execute(
            """SELECT event_id,job_id::text,stage_key,prior_state,new_state,reason,lease_owner,recovery_count,occurred_at
                 FROM runtime_control.priority_pipeline_recovery_events ORDER BY occurred_at DESC LIMIT 20""",
            fetch="all", statement_timeout_ms=1800,
        ) or []
        stale = int(counts.get("stale") or 0); blocked = int(counts.get("blocked") or 0)
        policy_blocked = int(counts.get("policy_blocked") or 0)
        waiting_reconciliation = int(counts.get("waiting_reconciliation") or 0)
        state = (
            "STALE" if stale else
            "BLOCKED" if blocked else
            "WAITING_RECONCILIATION" if waiting_reconciliation else
            "HEALTHY_WITH_POLICY_REJECTIONS" if policy_blocked else
            "HEALTHY"
        )
        return {
            "ok": stale == 0 and blocked == 0, "version": self.VERSION, "state": state,
            "running": int(counts.get("running") or 0), "stale": stale, "blocked": blocked,
            # A stock rejected by governed risk/admission is a valid terminal
            # decision outcome, not an infrastructure recovery failure.  Keep
            # it visible without allowing it to poison global readiness.
            "policy_blocked": policy_blocked,
            "waiting_reconciliation": waiting_reconciliation,
            "recent_events": [dict(row) for row in events],
        }

    def recovery_loop(self, supervisor: Any, *, running_fn) -> None:
        name = "priority_pipeline_recovery"
        while supervisor.running and running_fn():
            supervisor.beat(name)
            try:
                result = self.recover_stale()
                auto_reconciled = []
                for event in list(result.get("events") or []):
                    event_state = str(event.get("new_state") or "").upper()
                    stage_key = str(event.get("stage_key") or "")
                    symbol = str(event.get("symbol") or "").upper().strip()
                    mode = str(event.get("mode") or "delivery").lower().strip()
                    if event_state == "QUEUED" and stage_key == "coverage":
                        self.dispatch_history_schedule(
                            symbol=symbol, mode=mode, selected_interval="day", action="stale_coverage_recovery",
                        )
                    if event_state == "WAITING" and str(event.get("reason") or "") == "STALE_LEASE_RECOVERY_LIMIT" and symbol:
                        reconcile_key = f"{mode}:{symbol}:{stage_key}"
                        if reconcile_key not in self._auto_reconciled_exhausted:
                            reconciled = dict(self.reconcile(
                                symbol=symbol, mode=mode, reason="automatic_stale_lease_exhaustion",
                            ) or {})
                            if int(reconciled.get("reconciled_stages") or 0) > 0:
                                self._auto_reconciled_exhausted.add(reconcile_key)
                                auto_reconciled.append({
                                    "symbol": symbol, "mode": mode, "stage_key": stage_key,
                                    "activated_stage": reconciled.get("activated_stage"),
                                    "reconciled_stages": int(reconciled.get("reconciled_stages") or 0),
                                })
                                if str(reconciled.get("activated_stage") or "") == "coverage":
                                    self.dispatch_history_schedule(
                                        symbol=symbol, mode=mode, selected_interval="day",
                                        action="automatic_stale_reconciliation",
                                    )
                if auto_reconciled:
                    result["auto_reconciled"] = auto_reconciled
                    result["state"] = "AUTO_RECONCILED"
                status = getattr(self.app, "status", None)
                if isinstance(status, dict):
                    status["priority_pipeline_recovery"] = result
                supervisor.progress(
                    name,
                    token=(
                        f"recovery:{int(result.get('recovered') or 0)}:"
                        f"{int(result.get('blocked') or 0)}:"
                        f"{int(result.get('running') or 0)}:"
                        f"{int(result.get('checked') or 0)}"
                    ),
                    stage="recovery_scan",
                    completed_units=int(result.get("recovered") or 0),
                    total_units=int(result.get("running") or 0) + int(result.get("blocked") or 0),
                    waiting_on="stale leases" if int(result.get("stale") or 0) else None,
                    expected_idle=not bool(result.get("recovered") or result.get("blocked") or result.get("stale")),
                )
                if result.get("recovered") or result.get("blocked"):
                    self.app.event("WARN", "priority_pipeline", "Stale priority pipeline lease handled", result)
            except Exception as exc:
                self.app.event("ERROR", "priority_pipeline", "Priority pipeline recovery failed", {"error": str(exc)[:300]})
            for _ in range(12):
                if not supervisor.running or not running_fn():
                    return
                supervisor.beat(name)
                time.sleep(5)

    def observe_corporate_action_coverage(
        self, *, symbol: str, mode: str, instrument_key: str | None = None
    ) -> Dict[str, Any]:
        """Resolve the corporate-action stage from its real PostgreSQL authority.

        Missing/unverified full-history coverage is a truthful terminal BLOCKED
        state, never an endless RUNNING placeholder. No action factors are
        inferred from price jumps.
        """
        snapshot = self.snapshot(symbol=symbol, mode=mode)
        key = str(instrument_key or snapshot.get("instrument_key") or "").strip()
        if self.read_db is None:
            return self.update_stage(
                symbol=symbol, mode=mode, stage_key="corporate_actions", state="NOT_REQUIRED",
                detail="Production corporate-action PostgreSQL authority not configured in fallback/test mode",
                evidence={"authority": "FALLBACK_TEST_ONLY"},
            )
        row = self.read_db.execute(
            """SELECT instrument_key,exchange,trading_symbol,coverage_start,coverage_end,
                      complete,verified_at,source_name,source_hash
                 FROM reference.corporate_action_coverage
                WHERE instrument_key=%s LIMIT 1""",
            (key,), fetch="one", statement_timeout_ms=1800,
        ) if key else None
        proof = dict(row or {})
        verified = bool(
            proof.get("complete") is True
            and proof.get("verified_at") not in (None, "")
            and proof.get("coverage_start") not in (None, "")
            and proof.get("coverage_end") not in (None, "")
        )
        state = "READY" if verified else "BLOCKED"
        detail = (
            f"Verified full-history corporate-action coverage {proof.get('coverage_start')}..{proof.get('coverage_end')}"
            if verified else
            "Corporate-action full-history coverage is absent or unverified; adjusted-history decisions fail closed"
        )
        return self.update_stage(
            symbol=symbol, mode=mode, stage_key="corporate_actions", state=state, detail=detail,
            evidence={"authority": "reference.corporate_action_coverage", "instrument_key": key, **proof},
        )

    def observe_coverage(self, *, symbol: str, mode: str, rows: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
        rows = [dict(row) for row in rows or []]
        ready = sum(1 for row in rows if str(row.get("state") or "").upper() in {"CURRENT", "READY"})
        state = "READY" if rows and ready == len(rows) else "RUNNING" if rows else "WAITING"
        return self.update_stage(symbol=symbol, mode=mode, stage_key="coverage", state=state,
                                 detail=f"{ready}/{len(rows)} timeframe catalogues current" if rows else "Coverage catalogue unavailable",
                                 completed_units=ready, total_units=len(rows), evidence={"timeframes": rows[:10]})

    def observe_intelligence(self, *, symbol: str, mode: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        analysis = dict(payload.get("analysis") or {}); decision = dict(analysis.get("decision") or {})
        scorecard = dict(payload.get("scorecard") or {}); pipeline = dict(payload.get("pipeline") or {})
        trade_map = dict(payload.get("trade_map") or {}); invalidation = list(payload.get("invalidation") or [])
        mtf = list(payload.get("mtf_trend") or [])
        self.update_stage(symbol=symbol, mode=mode, stage_key="timeframes", state="READY" if len(mtf) >= 6 else "RUNNING", detail=f"{len(mtf)}/10 timeframe records", completed_units=len(mtf), total_units=10)
        math_ready = scorecard.get("math_composite") is not None or decision.get("technical_score") is not None
        self.update_stage(symbol=symbol, mode=mode, stage_key="mathematics", state="READY" if math_ready else "WAITING", detail="Canonical completed-bar mathematics evaluated" if math_ready else "Waiting for qualified bars")
        feature_ready = any(scorecard.get(key) is not None for key in ("technical", "fundamental", "mtf", "math_composite"))
        self.update_stage(symbol=symbol, mode=mode, stage_key="features", state="READY" if feature_ready else "WAITING", detail="Current feature snapshot available" if feature_ready else "Feature snapshot unavailable")
        model_state = str(decision.get("model_state") or decision.get("model_ranking_stage") or "").upper()
        inference_ready = scorecard.get("model_score") is not None or bool(model_state)
        self.update_stage(symbol=symbol, mode=mode, stage_key="inference", state="READY" if inference_ready else "NOT_REQUIRED" if feature_ready else "WAITING", detail=model_state or "Deterministic fallback; no governed ML publication")
        risk_state = "BLOCKED" if invalidation else "READY" if math_ready else "WAITING"
        self.update_stage(symbol=symbol, mode=mode, stage_key="risk", state=risk_state, detail=" · ".join(map(str, invalidation[:3])) if invalidation else "Mandatory gates passed" if math_ready else "Waiting for mathematics")
        instrument_key = str(payload.get("instrument_key") or (payload.get("selected_quote") or {}).get("instrument_key") or (payload.get("identity") or {}).get("instrument_key") or ((payload.get("analysis") or {}).get("instrument") or {}).get("instrument_key") or "").strip()
        snapshot = None
        snapshot_service = getattr(self.app, "evidence_snapshots", None)
        if snapshot_service is not None and instrument_key:
            try:
                snapshot = snapshot_service.capture_from_intelligence(symbol=symbol, instrument_key=instrument_key, mode=mode, intelligence=payload, pipeline=pipeline)
            except Exception as exc:
                self.app.event("WARN", "evidence_snapshot", "Canonical evidence snapshot capture failed", {"symbol": symbol, "mode": mode, "error": str(exc)[:240]})
        publication_ready = bool(decision.get("decision")) and str(trade_map.get("state") or "").upper() in {"FINAL", "RESEARCH", "UNAVAILABLE"}
        detail = f"{decision.get('decision') or 'Evidence pending'} · map {trade_map.get('state') or 'unavailable'}"
        evidence = {"snapshot_id": (snapshot or {}).get("snapshot_id"), "payload_hash": (snapshot or {}).get("payload_hash"), "snapshot_state": (snapshot or {}).get("state")}
        publication_state = "READY" if publication_ready and snapshot else "WAITING"
        self.update_stage(symbol=symbol, mode=mode, stage_key="publication", state=publication_state, detail=detail if snapshot else detail + " · immutable snapshot pending", evidence=evidence)
        # Outcome/settlement is owned by the canonical signal lifecycle, not by
        # a selected-stock analysis request. Once a decision snapshot is
        # published there is no additional synchronous outcome work to perform.
        self.update_stage(
            symbol=symbol, mode=mode, stage_key="outcome",
            state="NOT_REQUIRED" if publication_state == "READY" else "WAITING",
            detail=("Canonical lifecycle owns any later Model-Paper settlement/outcome" if publication_state == "READY" else "Waiting for decision publication"),
            evidence={"authority": "POSTGRESQL_CANONICAL_DECISIONS", "snapshot_id": (snapshot or {}).get("snapshot_id")},
        )
        return self.snapshot(symbol=symbol, mode=mode)
