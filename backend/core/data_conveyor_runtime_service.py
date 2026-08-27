"""Bounded installed-runtime data conveyor and Research activation loop.

The v97 product relied on scheduled tasks that could start late, block for many
minutes, or leave the UI showing service readiness while every evidence count
remained zero.  This service owns a bounded, observable retry loop inside the
installed runtime.  It does not download from GET routes and never grants model
or broker authority.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Dict

from core.nse_cash_data_authority_service import NseCashDataAuthorityService
from core.nse_official_plan_service import merge_nse_source_plan
from core.nse_official_source_cycle_service import NseOfficialSourceCycleService
from core.research_lifecycle_advance_service import ResearchLifecycleAdvanceService

SERVICE_VERSION = "installed-data-conveyor-1.0.0-v98"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def latest_completed_trade_date(now: datetime | None = None) -> str:
    """Latest session eligible for official post-close report acquisition."""
    from core.official_report_publication_policy import DEFAULT_OFFICIAL_REPORT_PUBLICATION_POLICY
    evidence = DEFAULT_OFFICIAL_REPORT_PUBLICATION_POLICY.latest_eligible_trade_date(now)
    trade_date = evidence.get("trade_date")
    if not trade_date:
        raise RuntimeError("official NSE report date unavailable: exchange calendar is unverified")
    return str(trade_date)


class DataConveyorRuntimeService:
    def __init__(self, app: Any, *, data_dir: Path, install_dir: Path):
        self.app = app
        self.data_dir = Path(data_dir)
        self.install_dir = Path(install_dir)
        self.backend_dir = self.install_dir / "backend"
        self.default_plan = self.backend_dir / "resources" / "nse_official_sources.example.json"
        self.plan_path = self.data_dir / "config" / "nse_official_sources.json"
        self.last_official_attempt_at = 0.0
        self.last_research_attempt_at = 0.0
        self._last_trade_date = None
        self._last_research_progress_signature = ""
        self._last_research_progress_at = 0.0
        self._last_research_progress_iso = None
        self._official_lock = threading.Lock()
        self._research_lock = threading.Lock()
        self._status: Dict[str, Any] = {
            "version": SERVICE_VERSION,
            "state": "STARTING",
            "official": {"state": "NOT_RUN"},
            "research": {"state": "NOT_RUN"},
            "updated_at": _now(),
            "production_influence": 0.0,
            "broker_authority": "NONE",
        }
        self._publish()

    def _publish(self) -> None:
        self._status["updated_at"] = _now()
        try:
            with self.app.lock:
                self.app.status["data_conveyor"] = dict(self._status)
        except Exception:
            try:
                self.app.status["data_conveyor"] = dict(self._status)
            except Exception:
                pass

    def status(self) -> Dict[str, Any]:
        return dict(self._status)

    def _catalog_refresh(self) -> Dict[str, Any]:
        script = self.backend_dir / "tools" / "refresh_research_catalog.py"
        if not script.is_file():
            return {"ok": False, "state": "CATALOG_REFRESH_SCRIPT_MISSING", "error": str(script)}
        command = [sys.executable, str(script), "--data-dir", str(self.data_dir)]
        dsn = os.environ.get("PROJECT_LADDU_OPERATIONAL_DSN", "").strip()
        if dsn:
            command.extend(["--operational-dsn", dsn])
        try:
            completed = subprocess.run(
                command,
                cwd=str(self.install_dir),
                text=True,
                capture_output=True,
                timeout=150,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            return {"ok": False, "state": "CATALOG_REFRESH_TIMEOUT", "error": str(exc)}
        payload = None
        try:
            payload = json.loads(completed.stdout or "{}")
        except Exception:
            payload = None
        return {
            "ok": completed.returncode == 0,
            "state": (payload or {}).get("state") or ("REFRESHED" if completed.returncode == 0 else "FAILED"),
            "returncode": completed.returncode,
            "result": payload,
            "stderr_tail": (completed.stderr or "")[-1200:],
        }

    def cycle_inflight(self) -> Dict[str, bool]:
        return {"official": self._official_lock.locked(), "research": self._research_lock.locked()}

    @staticmethod
    def recovery_plan(inflight: Dict[str, bool], reason: str | None) -> Dict[str, Any]:
        """Select an independent, idempotent recovery lane.

        Official acquisition and Research admission are separate stage
        authorities.  Work in one lane must never prevent recovery of the
        other or count as a failed recovery attempt.
        """
        active = {name: bool((inflight or {}).get(name)) for name in ("official", "research")}
        detail = str(reason or "").lower()
        wants_research = any(token in detail for token in ("research", "paper", "admission", "prediction"))
        wants_official = any(token in detail for token in ("official", "nse data", "source plan"))
        if wants_research and not wants_official:
            lanes = [] if active["research"] else ["research"]
            state = "RESEARCH_CYCLE_IN_FLIGHT" if active["research"] else "RESEARCH_RECOVERY_READY"
        elif wants_official and not wants_research:
            lanes = [] if active["official"] else ["official"]
            state = "OFFICIAL_CYCLE_IN_FLIGHT" if active["official"] else "OFFICIAL_RECOVERY_READY"
        else:
            lanes = [name for name in ("official", "research") if not active[name]]
            state = "RECOVERY_CYCLES_IN_FLIGHT" if not lanes else "AVAILABLE_LANES_READY"
        return {
            "lanes": lanes,
            "inflight": active,
            "state": state,
            "idempotent": not bool(lanes),
        }

    def run_official_once(self) -> Dict[str, Any]:
        if not self._official_lock.acquire(blocking=False):
            return {
                "ok": False, "state": "OFFICIAL_CYCLE_IN_FLIGHT",
                "completed_at": _now(), "production_influence": 0.0, "broker_authority": "NONE",
            }
        try:
            return self._run_official_once_body()
        finally:
            self._official_lock.release()

    def _run_official_once_body(self) -> Dict[str, Any]:
        trade_date = latest_completed_trade_date()
        started = time.monotonic()
        try:
            plan = merge_nse_source_plan(self.default_plan, self.plan_path)
            cycle = NseOfficialSourceCycleService(self.data_dir, plan_path=self.plan_path).run(trade_date=trade_date)
            refresh = self._catalog_refresh() if cycle.get("catalog_refresh_required") else {"ok": True, "state": "NOT_REQUIRED"}
            authority = NseCashDataAuthorityService(self.app.store, self.data_dir).refresh()
            summary = dict(authority.get("summary") or {})
            critical_ready = bool(summary.get("evidence_start_ready"))
            result = {
                "ok": critical_ready,
                "state": "CRITICAL_EVIDENCE_READY" if critical_ready else "CRITICAL_EVIDENCE_BLOCKED",
                "trade_date": trade_date,
                "plan": plan,
                "cycle": cycle,
                "catalog_refresh": refresh,
                "authority_summary": summary,
                "duration_ms": round((time.monotonic() - started) * 1000),
                "completed_at": _now(),
            }
        except Exception as exc:
            result = {
                "ok": False,
                "state": "OFFICIAL_CONVEYOR_FAILED",
                "trade_date": trade_date,
                "error": f"{type(exc).__name__}: {exc}"[:1000],
                "duration_ms": round((time.monotonic() - started) * 1000),
                "completed_at": _now(),
            }
        self.last_official_attempt_at = time.monotonic()
        self._last_trade_date = trade_date
        self._status["official"] = result
        self._publish()
        return result

    @staticmethod
    def _research_progress_signature(result: Dict[str, Any]) -> str:
        """Stable business-progress signature; timestamps are excluded."""
        reconciliation = dict(result.get("reconciliation") or {})
        desks = dict(reconciliation.get("by_desk") or {})
        payload: Dict[str, Any] = {}
        for desk in ("delivery", "intraday"):
            row = dict(desks.get(desk) or {})
            stages = dict(row.get("stages") or {})
            payload[desk] = {
                "population_fingerprint": row.get("population_fingerprint"),
                "state": row.get("state"),
                "captured": int(stages.get("captured") or 0),
                "features": int(stages.get("feature_complete") or 0),
                "baseline": int(stages.get("baseline_predicted") or 0),
                "ml": int(stages.get("ml_predicted") or 0),
                "hybrid": int(stages.get("hybrid_predicted") or 0),
                "paper": int(stages.get("paper_opened") or 0),
                "settled": int(stages.get("settled") or 0),
                "ledger": int(stages.get("research_ledger") or 0),
                "performance": int(stages.get("performance_attributed") or 0),
            }
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _research_wait_class(result: Dict[str, Any]) -> tuple[bool, str | None]:
        reconciliation = dict(result.get("reconciliation") or {})
        desks = dict(reconciliation.get("by_desk") or {})
        actionable = []
        expected = []
        for desk in ("delivery", "intraday"):
            row = dict(desks.get(desk) or {})
            state = str(row.get("state") or "")
            next_action = str(row.get("next_action") or "").strip()
            if state in {"PAPER_ADMISSION_WAITING", "PAPER_ADMISSION_NOT_SELECTED", "PAPER_MODEL_EVIDENCE_WAITING", "FEATURE_EVIDENCE_PENDING"}:
                if next_action:
                    expected.append(f"{desk}: {next_action}")
            elif state in {
                "PAPER_ADMISSION_BLOCKED", "PAPER_ADMISSION_PENDING", "PAPER_MODEL_TRAINING_BLOCKED",
                "FEATURES_INCOMPLETE", "THREE_ARM_INCOMPLETE",
                "RESEARCH_LEDGER_RECONCILIATION_PENDING",
                "PERFORMANCE_ATTRIBUTION_PENDING",
            }:
                actionable.append(f"{desk}: {next_action or state}")
        if actionable:
            return False, " | ".join(actionable)[:700]
        if expected:
            return True, " | ".join(expected)[:700]
        return True, None

    def run_research_once(self) -> Dict[str, Any]:
        if not self._research_lock.acquire(blocking=False):
            return {
                "ok": False, "state": "RESEARCH_CYCLE_IN_FLIGHT",
                "completed_at": _now(), "expected_wait": False,
                "waiting_on": "existing research activation still in flight",
                "progress_signature": self._last_research_progress_signature,
                "production_influence": 0.0, "broker_authority": "NONE",
            }
        try:
            return self._run_research_once_body()
        finally:
            self._research_lock.release()

    def _run_research_once_body(self) -> Dict[str, Any]:
        started = time.monotonic()
        try:
            result = ResearchLifecycleAdvanceService(self.app).run(settlement_limit=250, advance_settlement=False)
        except Exception as exc:
            result = {
                "ok": False,
                "state": "RESEARCH_ACTIVATION_FAILED",
                "error": f"{type(exc).__name__}: {exc}"[:1000],
                "production_influence": 0.0,
                "broker_authority": "NONE",
            }
        result = dict(result)
        signature = self._research_progress_signature(result)
        now_mono = time.monotonic()
        progressed = bool(signature and signature != self._last_research_progress_signature)
        if progressed or self._last_research_progress_at <= 0:
            self._last_research_progress_at = now_mono
            self._last_research_progress_iso = _now()
        if signature:
            self._last_research_progress_signature = signature
        expected_wait, waiting_on = self._research_wait_class(result)
        result["progress_signature"] = signature
        result["business_progressed"] = progressed
        result["last_business_progress_at"] = self._last_research_progress_iso
        result["business_progress_age_sec"] = round(max(0.0, now_mono - self._last_research_progress_at), 2) if self._last_research_progress_at > 0 else None
        result["expected_wait"] = expected_wait
        result["waiting_on"] = waiting_on
        result["duration_ms"] = round((time.monotonic() - started) * 1000)
        result["completed_at"] = _now()
        self.last_research_attempt_at = time.monotonic()
        self._status["research"] = result
        official_ok = bool((self._status.get("official") or {}).get("ok"))
        research_progress = any(int((row or {}).get("candidate_count") or 0) > 0 for row in (result.get("desks") or {}).values())
        expected_wait = bool(result.get("expected_wait"))
        waiting_on = str(result.get("waiting_on") or "").strip()
        self._status["state"] = (
            "ACTIVE" if official_ok and research_progress and not waiting_on
            else "RESEARCH_WAITING" if official_ok and expected_wait
            else "RESEARCH_BLOCKED" if official_ok and waiting_on
            else "DATA_BLOCKED"
        )
        self._publish()
        return result

    def loop(self, sup=None, *, running_fn: Callable[[], bool]) -> None:
        # Let critical quote/identity workers start first. This is a bulk worker.
        for _ in range(60):
            if not running_fn() or (sup is not None and not sup.running):
                return
            if sup is not None and _ % 10 == 0:
                sup.beat("data_conveyor")
            time.sleep(0.2)
        while running_fn() and (sup is None or sup.running):
            if sup is not None:
                sup.beat("data_conveyor")
            now_monotonic = time.monotonic()
            trade_date = latest_completed_trade_date()
            official = self._status.get("official") or {}
            critical_ready = bool(official.get("ok"))
            official_interval = 6 * 3600 if critical_ready else 2 * 60
            official_due = (
                self.last_official_attempt_at <= 0
                or trade_date != self._last_trade_date
                or now_monotonic - self.last_official_attempt_at >= official_interval
            )
            did_work = False
            if official_due:
                self._status["state"] = "ACQUIRING_OFFICIAL_DATA"
                self._publish()
                if sup is not None:
                    with sup.heartbeat_guard("data_conveyor"):
                        official_result = self.run_official_once()
                else:
                    official_result = self.run_official_once()
                did_work = True
                if sup is not None:
                    official_summary = dict(official_result.get("authority_summary") or {})
                    critical_current = int(official_summary.get("critical_current") or 0)
                    critical_required = int(official_summary.get("critical_required") or 3)
                    sup.progress(
                        "data_conveyor",
                        token=(
                            f"official:{official_result.get('trade_date')}:{official_result.get('state')}:"
                            f"{critical_current}/{critical_required}"
                        ),
                        stage="official_data",
                        completed_units=critical_current,
                        total_units=max(critical_required, 1),
                        expected_idle=False,
                    )
            research_interval = 60 if bool(getattr(self.app, "market_open", lambda: False)()) else 300
            if self.last_research_attempt_at <= 0 or now_monotonic - self.last_research_attempt_at >= research_interval:
                if sup is not None:
                    with sup.heartbeat_guard("data_conveyor"):
                        research_result = self.run_research_once()
                else:
                    research_result = self.run_research_once()
                did_work = True
                if sup is not None:
                    desks = dict(research_result.get("desks") or {})
                    candidates = sum(int((row or {}).get("candidate_count") or 0) for row in desks.values())
                    sup.progress(
                        "data_conveyor",
                        token=f"research:{research_result.get('progress_signature')}",
                        stage="research_activation",
                        completed_units=candidates,
                        total_units=max(candidates, 1),
                        waiting_on=research_result.get("waiting_on"),
                        expected_idle=bool(research_result.get("expected_wait")),
                    )
            if sup is not None and not did_work:
                last_research = dict(self._status.get("research") or {})
                expected_wait = bool(last_research.get("expected_wait"))
                sup.set_expected_idle(
                    "data_conveyor", expected_wait,
                    waiting_on=(last_research.get("waiting_on") or "next official/research cadence"),
                )
            # Frequent heartbeat; work itself remains bounded and retries are sparse.
            for tick in range(50):
                if not running_fn() or (sup is not None and not sup.running):
                    return
                if sup is not None and tick % 10 == 0:
                    sup.beat("data_conveyor")
                time.sleep(0.2)
