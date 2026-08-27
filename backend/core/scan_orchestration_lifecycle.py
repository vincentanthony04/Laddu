"""Scanner lifecycle, worker ownership, checkpoints and progress publication."""
from __future__ import annotations

from core.scan_orchestration_dependencies import *  # noqa: F401,F403


class ScanLifecycleMixin:
    def _record_scanner_cycle_evidence(self, desk: str, evidence_type: str, payload: Dict[str, Any]) -> Dict[str, Any]:
            """Persist a bounded Level-4 proof without creating scan authority.

            Checkpoints remain the cursor/restart authority. This record only binds
            completed counts to the immutable snapshot, ranker and market state so
            System Status can distinguish a running scanner from a proven cycle.
            """
            canonical = require_production_mode(desk)
            getter = getattr(self.host.store, "get_kv", None)
            setter = getattr(self.host.store, "set_kv", None)
            if not callable(setter):
                return {"ok": False, "state": "STORE_UNAVAILABLE"}
            try:
                current = dict(getter(f"scanner_cycle_evidence:{canonical}", {}) or {}) if callable(getter) else {}
            except Exception:
                current = {}
            snapshot = self._snapshot_identity(canonical)
            item = dict(payload or {})
            item.update({
                "desk": canonical,
                "evidence_type": evidence_type,
                "snapshot_id": snapshot.get("snapshot_id"),
                "population_count": int(snapshot.get("population_count") or item.get("universe_size") or 0),
                "content_hash": snapshot.get("content_hash"),
                "universe_revision": snapshot.get("universe_revision"),
                "ranking_version": RANKING_VERSION,
                "ranking_consumers": ["TODAY_ENTRY_SCANNER", "REASSESSMENT_SCANNER", "MANUAL_ANALYSIS"],
                "production_policy_version": POLICY_VERSION,
                "recorded_at": now_iso(),
            })
            item = attach_evidence_integrity(
                self.host.store,
                f"SCANNER_{str(evidence_type or '').upper()}",
                item,
                source_key=f"scanner_cycle_evidence:{canonical}/{evidence_type}",
            )
            current.update({
                "desk": canonical,
                "snapshot": snapshot,
                "ranking_version": RANKING_VERSION,
                "production_policy_version": POLICY_VERSION,
                "last_evidence_at": item["recorded_at"],
            })
            current[evidence_type] = item
            try:
                setter(f"scanner_cycle_evidence:{canonical}", current)
                with self.host.lock:
                    self.host.status.setdefault("scanner_cycle_evidence", {})[canonical] = current
                return {"ok": True, "state": "RECORDED", "evidence": current}
            except Exception as exc:
                recorder = getattr(self.host, "record_error", None)
                if callable(recorder):
                    recorder(f"{canonical}_scanner_cycle_evidence", str(exc)[:180])
                return {"ok": False, "state": "RECORD_FAILED", "error": str(exc)[:180]}

    def _focused_universe_ready(self) -> bool:
            meta = dict(getattr(self.host, "_instrument_health_meta", {}) or {})
            stats = dict(meta.get("universe_stats") or {})
            return bool(
                meta.get("loaded")
                and meta.get("cache_usable")
                and meta.get("universe_revision") == ACTIVE_UNIVERSE_REVISION
                and int(stats.get("derivatives") or 0) == 0
                and int(meta.get("count") or 0) > 0
            )

    def _snapshot_identity(self, mode: str) -> Dict[str, Any]:
            desk = str(mode or "").lower()
            snapshots = dict((getattr(self.host, "status", {}).get("universe_authority") or {}).get("snapshots") or {})
            row = dict(snapshots.get(desk) or {})
            snapshot_obj = dict(getattr(self.host, "_universe_snapshots", {}) or {}).get(desk)
            if snapshot_obj is not None:
                # The immutable in-memory snapshot object is the desk authority.
                # Status is only a projection and may lag or carry an old cross-desk
                # population after restart; it must never override the object.
                row["snapshot_id"] = getattr(snapshot_obj, "snapshot_id", "")
                row["content_hash"] = getattr(snapshot_obj, "content_hash", "")
                row["population_count"] = getattr(snapshot_obj, "population_count", 0)
            proof = dict(getattr(self.host, "status", {}).get("universe_authority") or {})
            row["universe_revision"] = str(proof.get("rule_version") or ACTIVE_UNIVERSE_REVISION)
            row["desk"] = desk
            return row

    def _ensure_checkpoint_reconciled(self, mode: Optional[str] = None) -> None:
            if self.checkpoints is None:
                return
            modes = (str(mode).lower(),) if mode else ("intraday", "delivery")
            for desk in modes:
                identity = self._snapshot_identity(desk)
                snapshot_id = str(identity.get("snapshot_id") or "")
                population = int(identity.get("population_count") or 0)
                if not snapshot_id or population <= 0:
                    continue
                reconcile_key = f"{snapshot_id}:{population}:{identity.get('content_hash') or ''}"
                if self._checkpoint_reconciled.get(desk) == reconcile_key:
                    continue
                lane = "coverage" if desk == "intraday" else "analysis"
                result = self.checkpoints.reconcile(desk, lane, expected=identity)
                checkpoint = dict(result.get("checkpoint") or {})
                with self.host.lock:
                    mode_state = self.host.status.setdefault("mode_scanners", {}).setdefault(desk, {})
                    lane_state = mode_state.setdefault(lane, {})
                    # Remove every prior progress projection before applying the
                    # exact-snapshot checkpoint. No old counter remains active.
                    lane_state.clear()
                    self.checkpoints.apply(lane_state, checkpoint)
                    for stale_key in (
                        "cursor", "sweep_scanned", "coverage_cursor", "coverage_universe",
                        "coverage_pct", "verified_coverage_pct", "sweep_number",
                        "sweep_complete", "current_cycle_scanned", "current_sweep_scanned",
                    ):
                        mode_state.pop(stale_key, None)
                    mode_state.pop("progress_contract", None)
                    mode_state.setdefault("analysis", {}).pop("progress_contract", None)
                    self.host.status.setdefault("scan_checkpoint_state", {})[desk] = {
                        "state": result.get("state"),
                        "reason": result.get("reason"),
                        "snapshot_id": snapshot_id,
                        "population_count": population,
                        "version": CHECKPOINT_VERSION,
                        "time": now_iso(),
                    }
                self._checkpoint_reconciled[desk] = reconcile_key

    @staticmethod
    def _scanner_progress_contract(
            mode: str,
            mode_state: Dict[str, Any],
            *,
            market_open: Optional[bool] = None,
            authoritative_population: Optional[int] = None,
        ) -> Dict[str, Any]:
            """Snapshot-bound customer progress with hard mathematical invariants."""
            desk = str(mode or "").lower()
            analysis = dict(mode_state.get("analysis") or {})
            coverage = dict(mode_state.get("coverage") or {})
            market_open = is_india_market_open() if market_open is None else bool(market_open)
            fallback_total = int(
                analysis.get("universe_size")
                or coverage.get("universe_size")
                or mode_state.get("population_count")
                or 0
            )
            total = max(0, int(authoritative_population or fallback_total or 0))
            clamp_count = lambda value: max(0, min(total, int(value or 0))) if total else max(0, int(value or 0))
            last_sweep = dict(analysis.get("last_completed_sweep") or {})
            last_cycle = dict(analysis.get("last_completed") or mode_state.get("last_completed") or {})
            if desk == "intraday":
                coverage_last = dict(coverage.get("last_completed") or {})
                screened = clamp_count(coverage.get("sweep_attempted") or coverage.get("covered") or 0)
                verified = clamp_count(coverage.get("sweep_verified") or coverage.get("verified") or 0)
                screening_observed = clamp_count(coverage.get("screening_observed") or verified)
                screening_eligible = clamp_count(coverage.get("screening_eligible") or 0)
                screening_pending = clamp_count(coverage.get("screening_pending") or 0)
                shortlisted = max(0, int(coverage.get("screening_shortlisted") or mode_state.get("screening_shortlisted") or 0))
                deep_analysed = max(0, int(analysis.get("cycle_scanned") or mode_state.get("current_cycle_scanned") or 0))
                live_analysis_paused = not market_open or str(analysis.get("state") or mode_state.get("state") or "").lower() == "market_closed"
                # The live Intraday decision lane is session-bound, but whole-
                # universe quote coverage + intelligent screening continues after
                # close to prepare tomorrow's queue. Customer status must therefore
                # say PREPARING rather than implying the desk is doing nothing.
                state = "PREPARING" if live_analysis_paused else str(coverage.get("state") or analysis.get("state") or mode_state.get("state") or "IDLE").upper()
                last_full = clamp_count(
                    coverage_last.get("verified")
                    or (verified if total and verified >= total else 0)
                    or 0
                )
                last_at = coverage_last.get("completed_at")
                if live_analysis_paused:
                    detail = (
                        f"Off-market prep {screened}/{total} · verified {verified} · shortlist {shortlisted} · "
                        f"live validation resumes 09:15 IST"
                    )
                else:
                    detail = (
                        f"Universe {total} · screened {screened} · verified {verified} · "
                        f"eligible {screening_eligible} · pending {screening_pending} · shortlist {shortlisted} · deep {deep_analysed}"
                    )
                return {
                    "version": PROGRESS_CONTRACT_VERSION,
                    "desk": "Intraday",
                    "state": state,
                    "population_count": total,
                    "current_sweep_number": max(1, int(coverage.get("sweep_number") or analysis.get("sweep_number") or 1)),
                    "current_sweep_scanned": screened,
                    "current_sweep_pct": round(min(100.0, screened * 100.0 / total), 1) if total else 0.0,
                    "current_verified": verified,
                    "screening_observed": screening_observed,
                    "screening_eligible": screening_eligible,
                    "screening_pending": screening_pending,
                    "screening_shortlisted": shortlisted,
                    "deep_analysed": deep_analysed,
                    "screening_scope": coverage.get("screening_scope"),
                    "screening_version": coverage.get("screening_version"),
                    "last_completed_sweep_count": last_full,
                    "last_completed_at": last_at,
                    "last_progress_at": coverage.get("last_run") or analysis.get("last_run") or mode_state.get("last_run"),
                    "next_run_at": "09:15 IST" if live_analysis_paused else coverage.get("next_run") or analysis.get("next_run") or mode_state.get("next_run"),
                    "pause_reason": "LIVE_INTRADAY_MARKET_CLOSED" if live_analysis_paused else None,
                    "live_analysis_paused": live_analysis_paused,
                    "preparation_active": bool(live_analysis_paused and (screened or shortlisted or str(coverage.get("state") or "").lower().startswith("closed_market"))),
                    "heartbeat_state": "PREPARING" if live_analysis_paused else "ACTIVE",
                    "display_value": f"{screened}/{total}",
                    "display_detail": detail,
                }
            current = clamp_count(
                coverage.get("sweep_scanned")
                or coverage.get("sweep_attempted")
                or analysis.get("current_sweep_scanned")
                or analysis.get("sweep_scanned")
                or coverage.get("covered")
                or 0
            )
            sweep_counts = dict(analysis.get("sweep_stage_counts") or {})
            eligible_count = clamp_count(sweep_counts.get("quote_ready") or 0)
            shortlist_count = clamp_count(sweep_counts.get("shortlisted") or 0)
            deep_analysed = clamp_count(sweep_counts.get("analysed") or 0)
            research_map_count = clamp_count(sweep_counts.get("map") or 0)
            final_count = clamp_count(sweep_counts.get("final") or 0)
            complete = bool(coverage.get("sweep_complete") or analysis.get("sweep_complete")) and bool(total) and current >= total
            raw_state = str(coverage.get("state") or analysis.get("state") or mode_state.get("state") or "IDLE").upper()
            blocker_reason = coverage.get("blocker_reason") or analysis.get("blocker_reason") or mode_state.get("blocker_reason")
            runtime_telemetry = dict(coverage.get("runtime_telemetry") or analysis.get("runtime_telemetry") or mode_state.get("runtime_telemetry") or {})
            if complete:
                state = "COMPLETE"
            elif blocker_reason:
                state = f"BLOCKED_{str(blocker_reason).upper()}"
            elif raw_state in {"WAITING_NEXT_CYCLE", "IDLE", "READY", "CONTINUING_SWEEP"}:
                # An incomplete immutable Delivery sweep is active backlog. Never
                # project it as terminal idle/waiting when no governed blocker exists.
                state = "CONTINUING_SWEEP"
            else:
                state = raw_state
            last_full = clamp_count(last_sweep.get("scanned") or (total if complete else 0))
            if complete:
                display_value = "COMPLETE"
                detail = f"Last full sweep {last_full}/{total}"
            elif current > 0:
                display_value = f"{current}/{total}"
                detail = f"Coverage {current}/{total} · eligible {eligible_count} · shortlist {shortlist_count} · deep analysed {deep_analysed} · Research/map {research_map_count} · Final {final_count}"
            elif last_full:
                display_value = "IDLE" if raw_state not in {"RUNNING", "EVALUATING"} else "STARTING"
                detail = f"Last full sweep {last_full}/{total} · next sweep pending"
            else:
                display_value = "STARTING" if raw_state in {"RUNNING", "EVALUATING"} else "PENDING"
                detail = f"Canonical universe {total} · first sweep pending"
            return {
                "version": PROGRESS_CONTRACT_VERSION,
                "desk": "Delivery",
                "state": state,
                "population_count": total,
                "current_sweep_number": max(1, int(analysis.get("sweep_number") or coverage.get("sweep_number") or 1)),
                "current_sweep_scanned": current,
                "current_sweep_pct": round(min(100.0, current * 100.0 / total), 1) if total else 0.0,
                "last_completed_sweep_count": last_full,
                "last_completed_at": last_sweep.get("completed_at"),
                "last_progress_at": coverage.get("last_progress_at") or coverage.get("last_run") or analysis.get("last_progress_at") or analysis.get("last_run") or mode_state.get("last_run"),
                "next_run_at": coverage.get("next_run") or analysis.get("next_run") or mode_state.get("next_run"),
                "next_retry_in_seconds": runtime_telemetry.get("next_retry_in_seconds"),
                "last_progress_age_seconds": runtime_telemetry.get("last_progress_age_seconds"),
                "blocker_reason": blocker_reason,
                "watchdog_triggered": bool(runtime_telemetry.get("watchdog_triggered")),
                "pause_reason": blocker_reason,
                "heartbeat_state": "ACTIVE" if not complete and not blocker_reason else "BLOCKED" if blocker_reason else "IDLE",
                "coverage_count": current,
                "eligible_count": eligible_count,
                "shortlist_count": shortlist_count,
                "deep_analysed": deep_analysed,
                "research_map_count": research_map_count,
                "final_count": final_count,
                "display_value": display_value,
                "display_detail": detail,
            }

    def _publish_scanner_progress(self, mode: str, *, market_open: Optional[bool] = None) -> Dict[str, Any]:
            self._ensure_checkpoint_reconciled(mode)
            identity = self._snapshot_identity(mode)
            with self.host.lock:
                mode_state = self.host.status.setdefault("mode_scanners", {}).setdefault(mode, {})
                contract = self._scanner_progress_contract(
                    mode,
                    mode_state,
                    market_open=market_open,
                    authoritative_population=int(identity.get("population_count") or 0),
                )
                mode_state["progress_contract"] = contract
                mode_state.setdefault("analysis", {})["progress_contract"] = dict(contract)
                return contract

    def _publish_lane_status(self, snapshot: Dict[str, Any]) -> None:
            status = getattr(self.host, "status", None)
            if not isinstance(status, dict):
                return
            lock = getattr(self.host, "lock", None)
            if lock is None:
                status["scan_lanes"] = snapshot
                return
            with lock:
                status["scan_lanes"] = snapshot

    def request_scan(self, lane: str) -> Dict[str, Any]:
            """Manual/API scan request; coalesces with supervised work."""
            name = str(lane or "").strip().lower()
            if name in ("intraday", "fast_lane", "live"):
                return self.lanes.request_async("intraday_analysis", lambda: self._run_live_mode_scan_impl("intraday"))
            if name in ("delivery", "deep_scan", "deep"):
                return self.lanes.request_async("delivery_analysis", lambda: self._run_deep_mode_scan_impl("delivery"))
            raise ValueError(f"unsupported scan lane: {lane}")

    def _log(self, level: str, message: str, detail: Optional[Dict[str, Any]] = None) -> None:
            if self.logger is not None:
                self.logger.event(level, message, detail)

    def _mode_status(self, mode: str) -> Dict[str, Any]:
            """Return the canonical mutable mode-status object under the host lock.

            Status writers still own host.status, but every scanner now separates
            coverage, analysis and promotion lanes so one worker cannot erase or
            misrepresent another worker's counters.
            """
            with self.host.lock:
                return self.host.status.setdefault("mode_scanners", {}).setdefault(mode, {})

    def _lane_status(self, mode: str, lane: str) -> Dict[str, Any]:
            with self.host.lock:
                return self._mode_status(mode).setdefault(lane, {})

    @staticmethod
    def _last_completed_cycle(state: Dict[str, Any]) -> Dict[str, Any]:
            value = state.get("last_completed")
            return dict(value) if isinstance(value, dict) else {}

    def _mirror_delivery_compatibility(self, mode_state: Dict[str, Any]) -> None:
            """Keep legacy deep_scan readers working without a second worker."""
            analysis = dict(mode_state.get("analysis") or {})
            coverage = {
                "universe_size": analysis.get("universe_size") or 0,
                "approx_scanned_cursor": analysis.get("sweep_scanned") or 0,
                "covered": analysis.get("sweep_scanned") or 0,
                "coverage_pct": analysis.get("coverage_pct") or 0.0,
                "sweep_number": analysis.get("sweep_number") or 1,
                "sweep_complete": bool(analysis.get("sweep_complete")),
                "note": "Canonical Delivery universe coverage; priority insertions do not advance the base-universe cursor.",
            }
            with self.host.lock:
                self.host.status.setdefault("deep_scan", {}).update({
                    "state": "delivery_only",
                    "scanned": mode_state.get("scanned") or 0,
                    "promoted": mode_state.get("promoted") or 0,
                    "rejected": mode_state.get("rejected") or 0,
                    "prepared_candidates": mode_state.get("prepared_candidates") or 0,
                    "last_run": mode_state.get("last_run"),
                    "next_run": mode_state.get("next_run") or "delivery cadence",
                    "cursor": analysis.get("cursor") or 0,
                    "coverage": coverage,
                    "production_policy_version": POLICY_VERSION,
                    "note": "Compatibility projection of the one canonical Delivery scanner; no independent execution authority.",
                })

    def _worker_loop(
            self,
            name: str,
            interval_open: int,
            interval_closed: int,
            task: Callable[[], Any],
            sup=None,
            *,
            running_fn: Callable[[], bool],
            continuation_seconds: int | None = None,
        ):
            """Run one scanner concern independently with truthful cadence.

            The supervisor heartbeat interval is deliberately separate from the task
            cadence.  Older code accidentally overwrote the requested cadence with
            the heartbeat interval, which could either flood a provider or hide an
            incomplete sweep behind an arbitrary sleep.  Progressive workers may use
            a short continuation cadence until their immutable sweep is complete.
            """
            next_run = 0.0
            while running_fn() and (sup is None or sup.running):
                if sup:
                    sup.beat(name)
                now = time.time()
                if now < next_run:
                    remaining = max(0, int(next_run-now))
                    with self.host.lock:
                        health = self.host.status.setdefault("worker_health", {}).setdefault(name, {})
                        health.update({
                            "state": "sleeping", "operation": None,
                            "next_run_at": datetime.fromtimestamp(next_run, tz=timezone.utc).isoformat().replace("+00:00", "Z"),
                            "seconds_to_next": remaining,
                        })
                    if sup:
                        payload = dict(result or {}) if isinstance(result, dict) else {}
                        intentional_yield = str(payload.get("state") or "").upper() == "YIELDING_TO_HIGHER_PRIORITY"
                        incomplete = payload.get("sweep_complete") is False
                        sup.set_expected_idle(
                            name, bool(intentional_yield or not incomplete),
                            waiting_on=(
                                str(payload.get("waiting_on") or payload.get("yield_reason") or "higher-priority interactive work")
                                if intentional_yield else
                                f"next continuation cycle in {remaining}s"
                                if incomplete else
                                f"next scheduled cycle in {remaining}s"
                            ),
                        )
                    time.sleep(min(1.0, max(0.05, next_run - now)))
                    continue
                if sup:
                    # Starting another scheduled attempt is activity, not useful
                    # progress.  Progress is recorded only after business counters
                    # or the immutable sweep cursor actually change.
                    sup.set_expected_idle(name, False, waiting_on=None)
                market_open = bool(is_india_market_open())
                requested_cadence = interval_open if market_open else interval_closed
                with self.host.lock:
                    health = self.host.status.setdefault("worker_health", {}).setdefault(name, {})
                    health.update({"state": "running", "operation": name, "started_at": now_iso(), "last_error": None})
                heartbeat_stop = threading.Event()
                heartbeat_thread = None
                if sup is not None:
                    heartbeat_seconds = sup.heartbeat_interval(name, default=10.0)
                    def _task_heartbeat():
                        while not heartbeat_stop.wait(heartbeat_seconds):
                            sup.beat(name)
                    heartbeat_thread = threading.Thread(target=_task_heartbeat, name=f"Heartbeat-{name}", daemon=True)
                    heartbeat_thread.start()
                result = None
                try:
                    result = task()
                    if sup:
                        payload = dict(result or {}) if isinstance(result, dict) else {}
                        analysis = dict(payload.get("analysis") or {})
                        coverage = dict(payload.get("coverage") or {})
                        completed = payload.get("scanned")
                        if completed is None:
                            completed = analysis.get("current_sweep_scanned") or coverage.get("covered") or payload.get("count")
                        total = analysis.get("universe_size") or coverage.get("universe_size") or payload.get("population_count") or payload.get("total")
                        token = "|".join(str(value) for value in (
                            payload.get("state"),
                            payload.get("mode"),
                            payload.get("cursor"),
                            payload.get("coverage_pct"),
                            completed,
                            payload.get("promoted"),
                            payload.get("rejected"),
                            payload.get("errors"),
                            payload.get("sweep_complete"),
                        ))
                        intentional_yield = str(payload.get("state") or "").upper() == "YIELDING_TO_HIGHER_PRIORITY"
                        sup.progress(
                            name,
                            token=token,
                            stage=str(payload.get("state") or "cycle_complete"),
                            current_item=analysis.get("current_symbol") or payload.get("current_item"),
                            completed_units=int(completed) if completed is not None else None,
                            total_units=int(total) if total is not None else None,
                            waiting_on=payload.get("waiting_on") or payload.get("yield_reason") or payload.get("pause_reason"),
                            expected_idle=bool(intentional_yield),
                        )
                    with self.host.lock:
                        health.update({"state": "idle", "operation": None, "last_completed_at": now_iso()})
                except Exception as exc:
                    with self.host.lock:
                        health.update({"state": "error", "operation": None, "last_error": str(exc)[:300], "last_error_at": now_iso()})
                    self.host.record_error(name, str(exc))
                    self.host.event("ERROR", name, f"{name} worker error", {"error": str(exc)[:300]})
                finally:
                    heartbeat_stop.set()
                    if heartbeat_thread is not None:
                        heartbeat_thread.join(timeout=0.2)
                sweep_complete = bool(result.get("sweep_complete")) if isinstance(result, dict) else True
                cadence = int(continuation_seconds) if continuation_seconds is not None and not sweep_complete else int(requested_cadence)
                next_run = time.time() + max(1, cadence)
                with self.host.lock:
                    health.update({
                        "state": "sleeping",
                        "next_cadence_seconds": max(1, cadence),
                        "next_run_at": datetime.fromtimestamp(next_run, tz=timezone.utc).isoformat().replace("+00:00", "Z"),
                        "seconds_to_next": max(0, int(cadence)),
                        "progressive_continuation": bool(continuation_seconds is not None and not sweep_complete),
                    })

    def intraday_loop(self, sup=None, *, running_fn: Callable[[], bool]):
            cadence = max(8, int(MODE_REFRESH_SECONDS.get("intraday", 10)))
            return self._worker_loop("intraday_scanner", cadence, 300, lambda: self.run_live_mode_scan("intraday"), sup, running_fn=running_fn)

    def intraday_coverage_loop(self, sup=None, *, running_fn: Callable[[], bool]):
            # Quote-only full-universe rotation. This loop never calls analyze_one(),
            # historical candles, fundamentals, or decision persistence, so one slow
            # candidate can no longer freeze visible coverage at the first batch.
            return self._worker_loop(
                "intraday_coverage",
                INTRADAY_COVERAGE_OPEN_SECONDS,
                INTRADAY_COVERAGE_CLOSED_SECONDS,
                self.run_intraday_coverage_pass,
                sup,
                running_fn=running_fn,
            )

    def delivery_coverage_loop(self, sup=None, *, running_fn: Callable[[], bool]):
            # Independent cache-only immutable-population rotation. Deep analysis
            # may be slow or blocked without suppressing customer-visible coverage.
            return self._worker_loop(
                "delivery_coverage", 4, 8, self.run_delivery_coverage_pass, sup,
                running_fn=running_fn, continuation_seconds=2,
            )

    def delivery_loop(self, sup=None, *, running_fn: Callable[[], bool]):
            completed_sweep_cadence = max(300, int(MODE_REFRESH_SECONDS.get("delivery", 86400)))
            # Continue the current immutable sweep promptly.  Only a completed full
            # sweep receives the low-frequency Delivery rest period.
            return self._worker_loop(
                "delivery_scanner",
                completed_sweep_cadence,
                600,
                lambda: self.run_deep_mode_scan("delivery"),
                sup,
                running_fn=running_fn,
                continuation_seconds=8,
            )

    def deep_scan_loop(self, sup=None, *, running_fn: Callable[[], bool]):
            # Broad research coverage must have its own lane.  It was accidentally
            # omitted from runtime registration in v65.26.9, which made the UI's
            # deep counter remain permanently at zero.
            return self._worker_loop("deep_scan", 900, 1800, self.run_deep_scan, sup, running_fn=running_fn)

    def index_levels_loop(self, sup=None, *, running_fn: Callable[[], bool]):
            return self._worker_loop("index_levels", 20, 240, self.run_index_level_scan, sup, running_fn=running_fn)

    def heat_loop(self, sup=None, *, running_fn: Callable[[], bool]):
            def refresh():
                self.host._safe_section("heatmap_background", self.host.heatmap, self.host._heatmap_cache or self.host._pending_heatmap("background pending"))
                # Publish the new Heat rows into the same Radar snapshot immediately.
                self.refresh_market_radar_projection()
            return self._worker_loop("market_heat", 60, 180, refresh, sup, running_fn=running_fn)

    def market_radar_loop(self, sup=None, *, running_fn: Callable[[], bool]):
            """Publish the cache-first Market Radar read model outside HTTP threads."""
            return self._worker_loop(
                "market_radar_projection", 15, 60,
                self.refresh_market_radar_projection, sup, running_fn=running_fn,
            )

    def refresh_market_radar_projection(self) -> Dict[str, Any]:
            return self.market_radar_projection.refresh()

    def scanner_loop(self, sup=None, *, running_fn: Callable[[], bool]):
            """Compatibility loop retained for external callers.

            Runtime startup now registers independent workers instead of this legacy
            sequential dispatcher. Keeping this lightweight loop avoids breaking old
            tests/tools while preventing it from scheduling duplicate desks.
            """
            while running_fn() and (sup is None or sup.running):
                if sup:
                    sup.beat("mode_scanners")
                time.sleep(1)
