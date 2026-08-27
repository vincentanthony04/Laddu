"""Autonomous low-priority historical PIT/WFA enrichment supervisor.

The first real historical research run is a product runtime responsibility. This worker starts with the application,
refreshes the read-only research catalogue before the canonical Delivery historical trainer, checkpoints every state
transition, and yields only to *real* higher-priority demand.  After market close it holds a bounded convergence lease:
transient pool occupancy with no waiters cannot repeatedly kill a deep trainer.  The scheduled task remains a watchdog;
runtime startup owns first use.  Research remains shadow-only with zero production influence.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import threading
import time
from typing import Any, Dict

from config import DATA_DIR, DEFAULT_PORT, ML_DELIVERY_TRAIN_MIN_DAYS, ML_DELIVERY_TRAIN_REFERENCE_DAYS
from core.market_clock import is_india_market_open
from core.ai_training_publication_service import AITrainingPublicationService
from core.research_catalogue_evidence_service import ResearchCatalogueEvidenceService
from models import now_iso


class HistoricalPitSweepService:
    VERSION = "historical-pit-sweep-1.8.0-pl46-nonblocking-research-subprocess"
    MIN_DATES = ML_DELIVERY_TRAIN_MIN_DAYS
    TRAIN_REFERENCE_DAYS = ML_DELIVERY_TRAIN_REFERENCE_DAYS

    def __init__(self, app: Any) -> None:
        self.app = app
        self._lock = threading.RLock()
        self._state: Dict[str, Any] = {
            "ok": True, "version": self.VERSION, "state": "STARTING",
            "stage": "startup", "min_dates": self.MIN_DATES, "train_reference_days": self.TRAIN_REFERENCE_DAYS, "history_policy": "ALL_ELIGIBLE_BY_STOCK_AND_MODE",
            "last_run_at": None, "last_success_at": None, "last_error": None,
            "run_count": 0, "yield_count": 0, "next_check_seconds": 0,
            "catalogue_refresh": None, "catalogue_persisted_evidence": None, "delivery_training": None, "publication_replay": None, "research_panel_engine": "R46_MATERIALIZED_FULL_TEMPORAL_DEPTH_FULL_CROSS_SECTION",
            "production_influence": 0, "broker_authority": "NONE",
        }
        self._next_attempt = 0.0
        self._run_gate = threading.Lock()
        self._off_market_waiter_since = None
        self._off_market_lease_until = 0.0
        self._checkpoint = Path(DATA_DIR) / "manifests" / "historical-pit-runtime.json"

    def _publish(self, **changes: Any) -> Dict[str, Any]:
        with self._lock:
            self._state.update(changes)
            self._state["updated_at"] = now_iso()
            payload = dict(self._state)
        try:
            self._checkpoint.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._checkpoint.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
            os.replace(tmp, self._checkpoint)
        except Exception:
            pass
        try:
            self.app.status["historical_pit_enrichment"] = payload
        except Exception:
            pass
        return payload

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._state)

    def _research_python(self) -> str | None:
        try:
            value = self.app.research_adapter.research_python()
        except Exception:
            value = None
        return str(value) if value and Path(str(value)).is_file() else None

    def _commands(self) -> list[tuple[str, list[str]]] | None:
        python_exe = self._research_python()
        backend = Path(__file__).resolve().parents[1]
        refresh = backend / "tools" / "refresh_research_catalog.py"
        corporate_sync = backend / "tools" / "sync_nse_corporate_action_history.py"
        trainer = backend / "tools" / "train_nse_smart_model.py"
        if not python_exe or not refresh.is_file() or not corporate_sync.is_file() or not trainer.is_file():
            return None
        return [
            # First refresh establishes the exact retained per-stock research horizon.
            ("research_catalogue_scope_refresh", [
                python_exe, str(refresh), "--data-dir", str(DATA_DIR),
                "--lock-wait-seconds", "900",
            ]),
            ("corporate_action_range_sync", [
                python_exe, str(corporate_sync), "--data-dir", str(DATA_DIR),
            ]),
            # Rebuild after range reconciliation so every candle carries row-level
            # corporate-action coverage before feature generation/training.
            ("research_catalogue_adjusted_refresh", [
                python_exe, str(refresh), "--data-dir", str(DATA_DIR),
                "--lock-wait-seconds", "900",
            ]),
            ("delivery_historical_training", [
                python_exe, str(trainer),
                "--data-dir", str(DATA_DIR),
                "--api-url", f"http://127.0.0.1:{DEFAULT_PORT}",
                "--horizon", "10", "--min-dates", str(self.MIN_DATES),
            ]),
        ]

    def _must_yield(self) -> tuple[bool, str]:
        """Yield to real customer/authority contention, not occupancy-only noise after close.

        During market hours the existing P5 governor remains authoritative.  After close, a deep research
        subprocess may continue through a transient `pressured` bit if there are no current waiters, no
        authority recovery and no interactive/manual priority. This prevents run/yield loops with no success.
        """
        governor = self.app.workload_governor
        should, reason = governor.should_yield("P5", record=False)
        if not should:
            return False, ""
        if is_india_market_open():
            governor.should_yield("P5", record=True)
            return True, reason
        try:
            snap = dict(governor.snapshot() or {})
        except Exception:
            governor.should_yield("P5", record=True)
            return True, reason
        if float(snap.get("manual_bulk_pause_remaining_sec") or 0) > 0:
            return True, "manual background-work pause active"
        if bool(snap.get("interactive_priority_active")):
            return True, "selected-stock priority active"
        pressure = dict(snap.get("database_pressure") or {})
        if bool(pressure.get("required_database_recovery")):
            return True, "required PostgreSQL authority recovering"
        waiter_reason = ""
        for role in ("operational", "interactive", "governance", "governance_read"):
            row = dict(pressure.get(role) or {})
            if row and (row.get("usable") is False or bool(row.get("recovering"))):
                self._off_market_waiter_since = None
                self._off_market_lease_until = 0.0
                return True, f"{role.replace('_', ' ')} PostgreSQL authority unavailable/recovering"
            if int(row.get("requests_waiting") or 0) > 0 or int(row.get("admission_waiters") or 0) > 0:
                waiter_reason = f"{role.replace('_', ' ')} PostgreSQL has current waiters"
                break
        if bool(snap.get("scanner_saturated")):
            return True, "scanner analysis capacity saturated"
        # Off-market convergence guarantee: transient waiters cannot kill/restart the same deep
        # research subprocess forever. Give live waiters a grace window; after repeated yielding
        # grant one bounded 5-minute convergence lease. Recovery/manual/interactive priority above
        # still preempts immediately.
        now = time.monotonic()
        if waiter_reason:
            if self._off_market_lease_until > now:
                return False, ""
            if self._off_market_waiter_since is None:
                self._off_market_waiter_since = now
                return False, ""
            waited = now - float(self._off_market_waiter_since)
            with self._lock:
                yields = int(self._state.get("yield_count") or 0)
            if waited < 45.0:
                return False, ""
            if yields >= 3:
                self._off_market_lease_until = now + 300.0
                self._off_market_waiter_since = None
                return False, ""
            return True, waiter_reason
        self._off_market_waiter_since = None
        # The original reason is capacity-only pressure with no live waiter. After close we retain the lease.
        return False, ""

    @staticmethod
    def _creation_flags() -> int:
        if os.name != "nt":
            return 0
        return int(getattr(subprocess, "CREATE_NO_WINDOW", 0)) | int(getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0))

    def _terminate(self, proc: subprocess.Popen[str]) -> None:
        try:
            proc.terminate()
            proc.wait(timeout=8)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    @staticmethod
    def _json_payload(stdout: str) -> Dict[str, Any]:
        stdout = str(stdout or "").strip()
        if not stdout:
            return {}
        try:
            value = json.loads(stdout)
            return dict(value) if isinstance(value, dict) else {}
        except Exception:
            start = stdout.find("{"); end = stdout.rfind("}")
            if start >= 0 and end > start:
                try:
                    value = json.loads(stdout[start:end + 1])
                    return dict(value) if isinstance(value, dict) else {}
                except Exception:
                    pass
        return {}

    @staticmethod
    def _subprocess_log_evidence(stdout_path: Path, stderr_path: Path) -> Dict[str, Any]:
        """Return bounded durable child-process diagnostics without pipe backpressure."""
        def size(path: Path) -> int:
            try:
                return int(path.stat().st_size)
            except Exception:
                return 0

        def tail(path: Path, limit: int = 6000) -> str:
            try:
                value = path.read_text(encoding="utf-8", errors="replace")
                return value[-max(1, int(limit)):]
            except Exception:
                return ""

        return {
            "stdout_log": str(stdout_path),
            "stderr_log": str(stderr_path),
            "stdout_bytes": size(stdout_path),
            "stderr_bytes": size(stderr_path),
            "stderr_tail": tail(stderr_path),
            "subprocess_output_mode": "FILE_SPOOL_NONBLOCKING",
        }

    def _run_command(self, phase: str, command: list[str], *, running_fn, sup=None) -> Dict[str, Any]:
        """Run one research phase without ever blocking on an unread OS pipe.

        The prior implementation used ``stdout=PIPE``/``stderr=PIPE`` and only
        drained them after ``poll()`` reported process exit.  A trainer/WFA that
        emitted more than the Windows pipe buffer could therefore block while
        writing its final evidence, while this parent waited for the child to
        exit.  File spooling removes that circular wait and leaves durable logs
        for every terminal path.
        """
        self._publish(state="RUNNING", stage=phase, last_error=None, waiting_on=None)
        log_dir = Path(DATA_DIR) / "logs" / "research-subprocess"
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            cutoff = time.time() - 7 * 86400
            for candidate in log_dir.glob("*.log"):
                try:
                    if candidate.stat().st_mtime < cutoff:
                        candidate.unlink(missing_ok=True)
                except Exception:
                    pass
        except Exception as exc:
            return {"ok": False, "state": "TRAINING_LOG_INIT_FAILED", "phase": phase, "reason": f"{type(exc).__name__}: {exc}"}

        safe_phase = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in str(phase))[:80] or "research"
        stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
        token = f"{stamp}-{safe_phase}-{os.getpid()}-{threading.get_ident()}"
        stdout_path = log_dir / f"{token}.stdout.log"
        stderr_path = log_dir / f"{token}.stderr.log"

        try:
            stdout_handle = stdout_path.open("w", encoding="utf-8", errors="replace", buffering=1)
            stderr_handle = stderr_path.open("w", encoding="utf-8", errors="replace", buffering=1)
        except Exception as exc:
            return {"ok": False, "state": "TRAINING_LOG_INIT_FAILED", "phase": phase, "reason": f"{type(exc).__name__}: {exc}"}

        proc = None
        started = time.monotonic()
        last_progress_bucket = -1
        try:
            try:
                proc = subprocess.Popen(
                    command,
                    cwd=str(Path(__file__).resolve().parents[1]),
                    stdout=stdout_handle, stderr=stderr_handle,
                    text=True, encoding="utf-8", errors="replace",
                    creationflags=self._creation_flags(),
                    env={**os.environ, "PROJECT_LADDU_AUTONOMOUS_PIT": "1"},
                )
            except Exception as exc:
                return {
                    "ok": False, "state": "TRAINING_START_FAILED", "phase": phase,
                    "reason": f"{type(exc).__name__}: {exc}",
                    **self._subprocess_log_evidence(stdout_path, stderr_path),
                }

            while proc.poll() is None:
                elapsed = time.monotonic() - started
                if not running_fn() or (sup is not None and not sup.running):
                    self._terminate(proc)
                    stdout_handle.flush(); stderr_handle.flush()
                    return {
                        "ok": True, "state": "STOPPED_WITH_RUNTIME", "phase": phase,
                        "reason": "runtime stopping", "phase_elapsed_seconds": int(elapsed),
                        **self._subprocess_log_evidence(stdout_path, stderr_path),
                    }
                should_yield, reason = self._must_yield()
                if should_yield:
                    self._terminate(proc)
                    stdout_handle.flush(); stderr_handle.flush()
                    return {
                        "ok": True, "state": "YIELDED_TO_HIGHER_PRIORITY", "phase": phase,
                        "reason": reason, "phase_elapsed_seconds": int(elapsed),
                        **self._subprocess_log_evidence(stdout_path, stderr_path),
                    }
                if elapsed >= 3 * 3600:
                    self._terminate(proc)
                    stdout_handle.flush(); stderr_handle.flush()
                    return {
                        "ok": False, "state": "TRAINING_TIMEOUT", "phase": phase,
                        "reason": f"{phase} exceeded 3h safety bound",
                        "phase_elapsed_seconds": int(elapsed),
                        **self._subprocess_log_evidence(stdout_path, stderr_path),
                    }

                progress_bucket = int(elapsed // 15)
                if progress_bucket != last_progress_bucket:
                    last_progress_bucket = progress_bucket
                    stdout_handle.flush(); stderr_handle.flush()
                    evidence = self._subprocess_log_evidence(stdout_path, stderr_path)
                    self._publish(
                        state="RUNNING", stage=phase, last_error=None,
                        waiting_on="research subprocess active",
                        phase_elapsed_seconds=int(elapsed), subprocess_pid=int(proc.pid),
                        subprocess_output_mode=evidence["subprocess_output_mode"],
                        subprocess_stdout_bytes=evidence["stdout_bytes"],
                        subprocess_stderr_bytes=evidence["stderr_bytes"],
                        subprocess_stdout_log=evidence["stdout_log"],
                        subprocess_stderr_log=evidence["stderr_log"],
                    )
                    if sup is not None:
                        sup.beat("historical_pit_enrichment")
                        sup.progress(
                            "historical_pit_enrichment",
                            token=f"{phase}:{progress_bucket}", stage=phase,
                            waiting_on=f"research subprocess active ({int(elapsed)}s)", expected_idle=False,
                        )
                time.sleep(2.0)

            stdout_handle.flush(); stderr_handle.flush()
        finally:
            try:
                stdout_handle.close()
            except Exception:
                pass
            try:
                stderr_handle.close()
            except Exception:
                pass

        stdout = stdout_path.read_text(encoding="utf-8", errors="replace") if stdout_path.is_file() else ""
        stderr = stderr_path.read_text(encoding="utf-8", errors="replace") if stderr_path.is_file() else ""
        payload = self._json_payload(stdout)
        ok = proc is not None and proc.returncode == 0 and payload.get("ok") is not False
        evidence = self._subprocess_log_evidence(stdout_path, stderr_path)
        return {
            "ok": ok,
            "state": payload.get("state") or ("COMPLETE" if ok else "FAILED"),
            "phase": phase,
            "returncode": int((proc.returncode if proc is not None else -1) or 0),
            "phase_elapsed_seconds": int(time.monotonic() - started),
            "model_id": payload.get("model_id"),
            "coverage": payload.get("coverage"),
            "oof_predictions": payload.get("oof_predictions"),
            "validation": payload.get("validation"),
            "capital_validation": payload.get("capital_validation"),
            "publication": payload.get("publication"),
            "feature_store": payload.get("feature_store"),
            "fold_cache": payload.get("fold_cache"),
            "factor_authority": payload.get("factor_authority"),
            "data_quality_authority": payload.get("data_quality_authority"),
            "dataset_fingerprint": payload.get("dataset_fingerprint"),
            "labelled_through": payload.get("labelled_through"),
            "latest_predictions": payload.get("latest_predictions"),
            "requested_chunks": payload.get("requested_chunks"),
            "published_chunks": payload.get("published_chunks"),
            "empty_valid_chunks": payload.get("empty_valid_chunks"),
            "failed_chunks": payload.get("failed_chunks"),
            "retryable_chunks": payload.get("retryable_chunks"),
            "progress_made": payload.get("progress_made"),
            "chunk_manifest_written": payload.get("chunk_manifest_written"),
            "coverage_written": payload.get("coverage_written"),
            "complete_market_range": payload.get("complete_market_range"),
            "complete_coverage": payload.get("complete_coverage"),
            "requests_used": payload.get("requests_used"),
            "request_budget": payload.get("request_budget"),
            "failures": payload.get("failures"),
            "reason": payload.get("reason") or payload.get("error") or (evidence.get("stderr_tail") or None),
            **evidence,
        }


    def _replay_publication_outbox(self, limit: int = 2) -> Dict[str, Any]:
        """Replay stranded trainer publications through the in-process authoritative boundary.

        The research Python must use HTTP because it is release-isolated. If HTTP publication
        fails, the bundle is durably spooled. The live runtime owns replay; without this consumer
        a transient 503 strands a valid model and its WFA evidence forever.
        """
        root = Path(DATA_DIR) / "runtime" / "publication_outbox"
        files = sorted(root.glob("*.json"), key=lambda path: path.stat().st_mtime)[:max(1, int(limit))] if root.is_dir() else []
        results = []
        for path in files:
            try:
                bundle = json.loads(path.read_text(encoding="utf-8-sig"))
                result = AITrainingPublicationService(self.app.store).publish(bundle)
                ok = isinstance(result, dict) and result.get("ok") is True
                if ok:
                    receipt_dir = Path(DATA_DIR) / "manifests" / "publication_receipts"
                    receipt_dir.mkdir(parents=True, exist_ok=True)
                    receipt = receipt_dir / (path.stem + ".json")
                    receipt.write_text(json.dumps({"source": str(path), "published_at": now_iso(), "result": result}, indent=2, default=str), encoding="utf-8")
                    path.unlink(missing_ok=True)
                results.append({"file": str(path), "ok": ok, "state": (result or {}).get("state") if isinstance(result, dict) else None, "error": None})
            except Exception as exc:
                results.append({"file": str(path), "ok": False, "state": "REPLAY_FAILED", "error": f"{type(exc).__name__}: {exc}"[:500]})
        remaining = len(list(root.glob("*.json"))) if root.is_dir() else 0
        return {"ok": all(item.get("ok") for item in results) if results else True, "attempted": len(results), "results": results, "remaining": remaining, "state": "OUTBOX_DRAINED" if remaining == 0 else ("OUTBOX_REPLAY_FAILED" if any(not item.get("ok") for item in results) else "OUTBOX_PENDING")}

    def _run_once(self, *, running_fn, sup=None) -> Dict[str, Any]:
        replay = self._replay_publication_outbox(limit=2)
        self._publish(publication_replay=replay)
        commands = self._commands()
        if not commands:
            return {"ok": False, "state": "RESEARCH_RUNTIME_UNAVAILABLE", "reason": "installed research Python, catalogue refresh or canonical Delivery trainer missing"}
        catalogue_evidence = ResearchCatalogueEvidenceService.probe(data_dir=DATA_DIR, min_dates=self.MIN_DATES)
        self._publish(catalogue_persisted_evidence=catalogue_evidence)
        latest_training_path = Path(DATA_DIR) / "manifests" / "latest-training-run.json"
        try:
            latest_training = json.loads(latest_training_path.read_text(encoding="utf-8-sig")) if latest_training_path.is_file() else {}
        except Exception:
            latest_training = {}
        capital = dict(latest_training.get("capital_validation") or {}) if isinstance(latest_training, dict) else {}
        capital_missing = str(capital.get("status") or "").upper() not in {"APPROVED", "REJECTED"}
        activate_from_persisted = bool(catalogue_evidence.get("ready") and capital_missing)
        self._publish(
            state="RUNNING",
            stage="persisted_catalogue_activation" if activate_from_persisted else "research_catalogue_refresh",
            last_run_at=now_iso(), last_error=None, waiting_on=None,
        )
        phase_results: Dict[str, Any] = {}
        for phase, command in commands:
            if phase == "research_catalogue_scope_refresh" and activate_from_persisted:
                result = {
                    "ok": True, "state": "PERSISTED_RESEARCH_PANEL_REUSED", "phase": phase,
                    "reason": "direct DuckDB panel proof supplies exact corporate-action/training scope",
                    "catalogue_evidence": catalogue_evidence,
                }
            else:
                result = self._run_command(phase, command, running_fn=running_fn, sup=sup)
            phase_results[phase] = result
            if phase.startswith("research_catalogue"):
                self._publish(catalogue_refresh=result)
            elif phase == "corporate_action_range_sync":
                self._publish(corporate_action_sync=result)
            else:
                self._publish(delivery_training=result)
            if phase == "delivery_historical_training" and isinstance(result.get("publication"), dict) and str(result["publication"].get("state") or "").upper() == "PUBLICATION_PENDING":
                replay = self._replay_publication_outbox(limit=2)
                result["publication_replay"] = replay
                self._publish(publication_replay=replay, delivery_training=result)
            state = str(result.get("state") or "").upper()
            if state in {"STOPPED_WITH_RUNTIME", "YIELDED_TO_HIGHER_PRIORITY"}:
                return {**result, "phase_results": phase_results}
            if not result.get("ok"):
                return {**result, "phase_results": phase_results}
        trainer = dict(phase_results.get("delivery_historical_training") or {})
        return {**trainer, "ok": True, "phase_results": phase_results}

    def _run_once_serialized(self, *, running_fn, sup=None, wait_timeout_seconds: float = 0.0) -> Dict[str, Any]:
        """Serialize autonomous and one-click historical training on one authority.

        A duplicate operator request never starts a competing trainer.  When the
        autonomous worker already owns the run, the one-click caller may wait for
        that exact run instead of creating a second process.
        """
        timeout = max(0.0, float(wait_timeout_seconds or 0.0))
        acquired = self._run_gate.acquire(timeout=timeout) if timeout > 0 else self._run_gate.acquire(blocking=False)
        if not acquired:
            return {
                "ok": True, "state": "TRAINING_ALREADY_IN_PROGRESS",
                "reason": "historical PIT/training authority is already executing",
                "snapshot": self.snapshot(),
            }
        try:
            return self._run_once(running_fn=running_fn, sup=sup)
        finally:
            self._run_gate.release()

    def run_on_demand(self, *, running_fn, reason: str = "operator_end_to_end") -> Dict[str, Any]:
        """Execute the canonical catalogue -> corporate-action -> Delivery ML/WFA path.

        This is the same path the autonomous worker owns.  It exists so the
        product's Run End-to-End action can request the real historical trainer
        rather than a selector-only replay.
        """
        self._next_attempt = 0.0
        self._publish(state="RUN_REQUESTED", stage="operator_end_to_end", waiting_on=None, request_reason=str(reason or "operator_end_to_end"))
        return self._run_once_serialized(
            running_fn=running_fn, sup=None, wait_timeout_seconds=3 * 3600,
        )

    def loop(self, sup=None, *, running_fn) -> None:
        name = "historical_pit_enrichment"
        for tick in range(20):
            if not running_fn() or (sup is not None and not sup.running):
                return
            if sup is not None and tick % 5 == 0:
                sup.beat(name)
            time.sleep(0.25)
        while running_fn() and (sup is None or sup.running):
            if sup is not None:
                sup.beat(name)
            should_yield, reason = self._must_yield()
            now = time.monotonic()
            if should_yield:
                with self._lock:
                    yields = int(self._state.get("yield_count") or 0) + 1
                self._publish(
                    state="YIELDING_TO_HIGHER_PRIORITY", stage="capacity_guard",
                    yield_count=yields, waiting_on=reason, next_check_seconds=10,
                )
                if sup is not None:
                    sup.progress(
                        name, token=f"yield:{reason}", stage="yielding_to_higher_priority",
                        waiting_on=reason, expected_idle=True,
                    )
                time.sleep(10.0)
                continue
            if now < self._next_attempt:
                wait = max(1, int(self._next_attempt - now))
                self._publish(state="CONTINUING_SWEEP", stage="checkpoint_wait", waiting_on="next incremental PIT/WFA eligibility check", next_check_seconds=wait)
                if sup is not None:
                    sup.set_expected_idle(name, True, waiting_on=f"next PIT/WFA eligibility check in {wait}s")
                time.sleep(min(10.0, max(1.0, self._next_attempt - now)))
                continue
            if sup is not None:
                sup.set_expected_idle(name, False, waiting_on=None)
            result = self._run_once_serialized(running_fn=running_fn, sup=sup, wait_timeout_seconds=1.0)
            with self._lock:
                runs = int(self._state.get("run_count") or 0) + 1
            if result.get("ok"):
                state = str(result.get("state") or "TRAINED").upper()
                if state == "STOPPED_WITH_RUNTIME":
                    self._publish(state="STOPPED", stage="runtime_stop", run_count=runs, last_result=result, waiting_on="runtime stopping", next_check_seconds=0)
                    return
                if state == "YIELDED_TO_HIGHER_PRIORITY":
                    cadence = 15
                    with self._lock:
                        yields = int(self._state.get("yield_count") or 0) + 1
                    self._next_attempt = time.monotonic() + cadence
                    self._publish(
                        state="YIELDING_TO_HIGHER_PRIORITY", stage="capacity_guard",
                        run_count=runs, yield_count=yields, last_result=result,
                        waiting_on=result.get("reason") or "higher-priority product work", next_check_seconds=cadence,
                    )
                else:
                    cadence = 1800 if state == "TRAINING_NOT_REQUIRED" else 600
                    self._next_attempt = time.monotonic() + cadence
                    self._publish(
                        state="CONTINUING_SWEEP", stage="qualified_checkpoint",
                        run_count=runs, last_success_at=now_iso(), last_error=None,
                        last_result=result, waiting_on="new historical labels / next WFA checkpoint",
                        next_check_seconds=cadence,
                    )
            else:
                # A partial corporate-action range with durable progress should
                # resume promptly; true no-progress failures back off longer.
                partial_progress = bool(
                    str(result.get("state") or "").upper() == "RANGE_ACQUISITION_PARTIAL"
                    and result.get("progress_made") is True
                )
                cadence = 30 if partial_progress else 180
                self._next_attempt = time.monotonic() + cadence
                progress_detail = None
                if result.get("requested_chunks") is not None:
                    progress_detail = (
                        f"corporate-action chunks published={int(result.get('published_chunks') or 0)} "
                        f"empty={int(result.get('empty_valid_chunks') or 0)} "
                        f"failed={int(result.get('failed_chunks') or 0)}/"
                        f"{int(result.get('requested_chunks') or 0)}"
                    )
                self._publish(
                    state="RETRYING", stage="corporate_action_resume" if partial_progress else "training_retry", run_count=runs,
                    last_error=result.get("reason") or result.get("state"), last_result=result,
                    waiting_on=progress_detail or "bounded retry after product capacity check", next_check_seconds=cadence,
                )
            if sup is not None:
                result_state = str(result.get("state") or ("PASS" if result.get("ok") else "FAILED"))
                sup.progress(
                    name, token=f"run:{runs}:{result_state}:{result.get('model_id') or ''}",
                    stage="historical_pit_wfa", completed_units=runs,
                    waiting_on=self.snapshot().get("waiting_on"), expected_idle=True,
                )
            time.sleep(1.0)
