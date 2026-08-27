"""Separate Intraday/Delivery scanner and lifecycle authorities.

Discovery remains broad. These desk authorities own final candidate-state
checkpointing and open-position reassessment cadence while sharing one canonical
PostgreSQL decision/position authority.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable, Dict
import json
from datetime import datetime, timezone

from core.open_position_gap_recovery_authority import DEFAULT_OPEN_POSITION_GAP_RECOVERY_AUTHORITY
from core.market_clock import is_india_market_open


class DeskCandidateScannerAuthority:
    VERSION = "desk-candidate-scanner-authority-1.5.0-recovery-rebase-aware"

    def __init__(self, host, desk: str, repository=None):
        if desk not in {"intraday", "delivery"}:
            raise ValueError("unsupported desk")
        self.host = host
        self.desk = desk
        self.repository = repository
        self._hot: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.RLock()
        # Intraday has no immutable sweep cursor. A fully returned, non-coalesced
        # live scan is therefore its accountable unit of operational progress.
        self._completed_cycles = 0

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {"desk": self.desk, "count": len(self._hot), "candidates": list(self._hot.values())[:200], "version": self.VERSION}

    def _capture(self, result: Dict[str, Any]) -> None:
        mode_state = dict((getattr(self.host, "status", {}).get("mode_scanners") or {}).get(self.desk) or {})
        lane = dict(mode_state.get("analysis") or {})
        payload = {"result": dict(result or {}), "analysis": lane, "promotion": dict(mode_state.get("promotion") or {})}
        if self.repository is not None:
            self.repository.checkpoint(f"{self.desk}_candidate_scanner", self.desk, "candidate", str(lane.get("state") or result.get("state") or "idle"), payload)

    def run_once(self) -> Dict[str, Any]:
        result = (self.host.scan_orchestration.run_live_mode_scan("intraday") if self.desk == "intraday"
                  else self.host.scan_orchestration.run_deep_mode_scan("delivery"))
        self._capture(dict(result or {}))
        return dict(result or {})

    @staticmethod
    def cadence_seconds(desk: str, *, market_open: bool, sweep_complete: bool) -> int:
        """Return the next governed scanner cadence.

        An incomplete immutable sweep is backlog, not idle time. Delivery therefore
        continues quickly until the current population is covered, then returns to
        its sparse refresh cadence. Intraday remains paused/60s observable while the
        exchange is closed and fast while live.
        """
        desk = str(desk or "").lower()
        if desk == "intraday":
            return 5 if market_open else 60
        if desk != "delivery":
            raise ValueError("unsupported desk")
        if sweep_complete:
            return 300 if market_open else 600
        # Market-hours incomplete sweeps are active backlog, not scheduled idle.
        # Keep continuation short enough that a 4k+ universe can rotate within a
        # useful trading window while deep analysis remains separately bounded.
        return 8 if market_open else 20

    @staticmethod
    def _named_blocker(result: Dict[str, Any]) -> str | None:
        error = str((result or {}).get("error") or "").strip().lower()
        state = str((result or {}).get("state") or "").strip().lower()
        if error in {"waiting_token", "token_missing", "token_invalid"} or state == "waiting_token":
            return "TOKEN_UNAVAILABLE"
        if error in {"waiting_focused_universe", "waiting_universe"} or state in {"waiting_focused_universe", "waiting_universe"}:
            return "INSTRUMENT_UNIVERSE_UNAVAILABLE"
        if error in {"historical_rate_limited", "provider_throttle", "market_data_unavailable"}:
            return "PROVIDER_THROTTLE" if "rate" in error or "throttle" in error else "MARKET_DATA_UNAVAILABLE"
        if error in {"database_unavailable", "postgres_unavailable", "checkpoint_persist_failed"}:
            return "DATABASE_UNAVAILABLE"
        if state in {"shutdown", "stopping"}:
            return "SHUTDOWN"
        return None

    @staticmethod
    def continuation_delay_seconds(*, desk: str, market_open: bool, sweep_complete: bool,
                                   blocker_reason: str | None, last_progress_age_seconds: float,
                                   normal_delay: int) -> tuple[int, bool]:
        """Governed self-reschedule policy for incomplete scanner backlog.

        An incomplete Delivery sweep may never become terminal WAITING_NEXT_CYCLE.
        If useful progress is older than twice the normal continuation cadence and
        no governed blocker exists, the watchdog accelerates the next attempt.
        """
        if sweep_complete or blocker_reason:
            return max(1, int(normal_delay)), False
        if str(desk).lower() == "delivery" and last_progress_age_seconds > max(12.0, normal_delay * 2.0):
            return 1, True
        return max(1, int(normal_delay)), False

    @staticmethod
    def progress_rebase_required(*, current_progress: int | None, prior_progress: int | None,
                                 current_generation: str | None, prior_generation: str | None) -> bool:
        if current_progress is None or prior_progress is None:
            return False
        if int(current_progress) < int(prior_progress):
            return True
        return bool(
            prior_generation not in (None, current_generation)
            and int(current_progress) <= int(prior_progress)
        )

    def _intraday_lane_inflight(self) -> tuple[bool, float, str]:
        """Return bounded truth for the one canonical Intraday analysis lane.

        A coalesced request is expected while an existing governed run owns the
        lane, but only for a bounded interval.  It must never hide a genuinely
        wedged lane indefinitely.
        """
        if self.desk != "intraday":
            return False, 0.0, ""
        try:
            lanes = dict((self.host.scan_orchestration.lanes.snapshot() or {}).get("lanes") or {})
            row = dict(lanes.get("intraday_analysis") or {})
            state = str(row.get("state") or "").lower()
            started = float(row.get("last_started_at") or 0.0)
            age = max(0.0, time.time() - started) if started > 0 else 0.0
            pending = "intraday_analysis" in set((self.host.scan_orchestration.lanes.snapshot() or {}).get("pending_async") or [])
            active = state in {"running", "running_coalesced", "queued", "queued_coalesced"} or pending
            # Scan budget is 30s; 75s gives quote/preparation jitter room while
            # still surfacing a real stall well before the 180s NO_PROGRESS gate.
            bounded = bool(active and (age <= 75.0 if started > 0 else pending))
            return bounded, round(age, 2), state
        except Exception:
            return False, 0.0, "unknown"

    def loop(self, sup=None, *, running_fn: Callable[[], bool]):
        name = f"{self.desk}_scanner"
        last_committed_progress = None
        last_progress_generation = None
        last_progress_monotonic = time.monotonic()
        while running_fn() and (sup is None or sup.running):
            result: Dict[str, Any] = {}
            bounded_inflight = False
            inflight_age = 0.0
            inflight_state = ""
            if sup: sup.beat(name)
            try:
                with self.host.lock:
                    health = self.host.status.setdefault("worker_health", {}).setdefault(name, {})
                    health.update({"state": "running", "operation": name, "started_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")})
            except Exception:
                pass
            try:
                if sup:
                    with sup.heartbeat_guard(name):
                        result = self.run_once()
                else:
                    result = self.run_once()
                result_state = str((result or {}).get("state") or "").upper()
                if result_state != "COALESCED" and result_state != "YIELDING_TO_HIGHER_PRIORITY":
                    self._completed_cycles += 1
                    try:
                        with self.host.lock:
                            health = self.host.status.setdefault("worker_health", {}).setdefault(name, {})
                            health["last_completed_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
                    except Exception:
                        pass
                if self.desk == "intraday" and result_state == "COALESCED":
                    bounded_inflight, inflight_age, inflight_state = self._intraday_lane_inflight()
                if sup:
                    mode_state = dict((getattr(self.host, "status", {}).get("mode_scanners") or {}).get(self.desk) or {})
                    analysis = dict(mode_state.get("analysis") or {})
                    contract = dict(mode_state.get("progress_contract") or analysis.get("progress_contract") or {})
                    if self.desk == "delivery":
                        completed = analysis.get("current_sweep_scanned") or analysis.get("sweep_scanned")
                        total = analysis.get("universe_size")
                        token = "|".join(str(v) for v in (
                            contract.get("state") or result.get("state"),
                            contract.get("current_sweep_number"), completed, total,
                            analysis.get("cycle_attempted"), analysis.get("cycle_quote_ready"),
                            analysis.get("cycle_shortlisted"),
                            analysis.get("data_pending") or analysis.get("cycle_data_missing"),
                            analysis.get("cycle_capacity_deferred"), analysis.get("cycle_scanned"),
                            analysis.get("cycle_promoted"), analysis.get("cycle_rejected"),
                            analysis.get("cycle_analysis_errors"),
                        ))
                    else:
                        # Live analysis and whole-universe coverage are independent
                        # authorities.  The supervisor tracks completed live-analysis
                        # cycles only; coverage is instrumented by intraday_coverage.
                        last_cycle = dict(analysis.get("last_completed") or {})
                        completed = self._completed_cycles
                        total = None
                        token = "|".join(str(v) for v in (
                            "intraday_live_analysis", self._completed_cycles,
                            last_cycle.get("completed_at"), last_cycle.get("scanned"),
                            last_cycle.get("promoted"), last_cycle.get("rejected"),
                            last_cycle.get("blocked"), last_cycle.get("analysis_timeouts"),
                            last_cycle.get("analysis_capacity"),
                        ))
                    deliberate_yield = result_state == "YIELDING_TO_HIGHER_PRIORITY"
                    sup.progress(
                        name, token=token,
                        stage=("analysis_in_flight" if bounded_inflight else str(analysis.get("current_stage") or contract.get("state") or "cycle_complete")),
                        current_item=analysis.get("current_symbol"),
                        completed_units=int(completed) if completed is not None else None,
                        total_units=int(total) if total is not None else None,
                        waiting_on=(
                            f"existing governed Intraday analysis in flight · {inflight_age:.0f}s"
                            if bounded_inflight else contract.get("pause_reason") or analysis.get("waiting_on")
                        ),
                        expected_idle=bool(deliberate_yield or bounded_inflight),
                    )
            except Exception as exc:
                recorder = getattr(self.host, "record_error", None)
                if callable(recorder): recorder(name, str(exc))
                if self.repository is not None:
                    try: self.repository.checkpoint(name, self.desk, "candidate", "error", {"error": str(exc)[:300]})
                    except Exception: pass
            # Use the canonical TradingSessionAuthority-backed market clock.
            # The runtime host intentionally has no market_open() method; the
            # previous getattr fallback therefore classified every cadence as
            # closed-market even while run_once() correctly executed live scans.
            market_open = bool(is_india_market_open())
            mode_state = dict((getattr(self.host, "status", {}).get("mode_scanners") or {}).get(self.desk) or {})
            analysis = dict(mode_state.get("analysis") or {})
            contract = dict(mode_state.get("progress_contract") or analysis.get("progress_contract") or {})
            if self.desk == "delivery":
                completed = analysis.get("current_sweep_scanned") or analysis.get("sweep_scanned")
                total = analysis.get("universe_size")
                sweep_complete = bool((result or {}).get("sweep_complete") or (total and completed is not None and int(completed) >= int(total)))
            else:
                # Intraday live analysis is recurrent throughout the market session;
                # whole-universe coverage completion must never make it expected-idle.
                completed = self._completed_cycles
                total = None
                sweep_complete = False
            normal_delay = self.cadence_seconds(self.desk, market_open=market_open, sweep_complete=sweep_complete)
            blocker_reason = self._named_blocker(result)
            current_progress = int(completed) if completed is not None else None
            progress_generation = "|".join(str(value or "") for value in (
                contract.get("current_sweep_number"),
                contract.get("population_fingerprint") or contract.get("universe_fingerprint"),
                contract.get("population_count") or total,
                analysis.get("recovery_generation") or contract.get("recovery_generation"),
            )) if self.desk == "delivery" else f"intraday:{self._completed_cycles}"
            progress_rebased = self.progress_rebase_required(
                current_progress=current_progress,
                prior_progress=last_committed_progress,
                current_generation=progress_generation,
                prior_generation=last_progress_generation,
            )
            # A governed recovery may rewind an immutable sweep checkpoint.  The
            # prior high-water mark belongs to the abandoned generation; keeping
            # it would make real resumed work look stalled until the cursor again
            # exceeded the old value.  Rebase only on an observed counter rewind
            # or generation change.  This records operational progress; it never
            # changes candidate ranking, trade geometry, or admission thresholds.
            if progress_rebased:
                last_committed_progress = current_progress
                last_progress_generation = progress_generation
                last_progress_monotonic = time.monotonic()
            elif current_progress is not None and (last_committed_progress is None or current_progress > last_committed_progress):
                last_committed_progress = current_progress
                last_progress_generation = progress_generation
                last_progress_monotonic = time.monotonic()
            progress_age = max(0.0, time.monotonic() - last_progress_monotonic)
            delay, watchdog_triggered = self.continuation_delay_seconds(
                desk=self.desk, market_open=market_open, sweep_complete=sweep_complete,
                blocker_reason=blocker_reason, last_progress_age_seconds=progress_age, normal_delay=normal_delay,
            )
            try:
                with self.host.lock:
                    live_state = self.host.status.setdefault("mode_scanners", {}).setdefault(self.desk, {})
                    live_analysis = live_state.setdefault("analysis", {})
                    if self.desk == "delivery" and not sweep_complete and not blocker_reason:
                        live_state["state"] = "continuing_sweep"
                        live_analysis["state"] = "continuing_sweep"
                    live_state["next_run"] = f"{int(delay)}s"
                    live_analysis["next_run"] = f"{int(delay)}s"
                    live_analysis["next_scan_delay_seconds"] = int(delay)
                    live_analysis["continuation_policy"] = "WATCHDOG_ACCELERATED" if watchdog_triggered else "FAST_INCOMPLETE_SWEEP" if not sweep_complete else "SPARSE_AFTER_COMPLETE"
                    live_analysis["continuation_market_open"] = bool(market_open)
                    telemetry = live_analysis.setdefault("runtime_telemetry", {})
                    telemetry.update({
                        "last_progress_age_seconds": round(progress_age, 1),
                        "next_retry_in_seconds": int(delay),
                        "blocker_reason": blocker_reason,
                        "watchdog_triggered": bool(watchdog_triggered),
                        "progress_generation": progress_generation,
                        "progress_rebased_after_recovery": progress_rebased,
                        "committed_progress_baseline": last_committed_progress,
                    })
                    live_state["runtime_telemetry"] = dict(telemetry)
                    live_state["blocker_reason"] = blocker_reason
                    health = self.host.status.setdefault("worker_health", {}).setdefault(name, {})
                    health.update({
                        "state": "sleeping",
                        "operation": None,
                        "next_cadence_seconds": int(delay),
                        "next_run_at": datetime.fromtimestamp(time.time() + delay, tz=timezone.utc).isoformat().replace("+00:00", "Z"),
                        "seconds_to_next": int(delay),
                    })
                publisher = getattr(self.host.scan_orchestration, "_publish_scanner_progress", None)
                if callable(publisher):
                    publisher(self.desk, market_open=market_open)
            except Exception:
                pass
            if sup:
                # Intraday is legitimately idle while the exchange is closed.
                # Delivery with an incomplete sweep is NOT expected-idle: its
                # useful-progress clock must survive the cadence sleep so OCC can
                # detect a cursor that fails to advance across scheduled cycles.
                deliberate_yield = str((result or {}).get("state") or "").upper() == "YIELDING_TO_HIGHER_PRIORITY"
                expected_idle = (self.desk == "intraday" and not market_open) or sweep_complete or deliberate_yield or bounded_inflight
                waiting_on = (
                    "market closed; resumes 09:15 IST"
                    if self.desk == "intraday" and not market_open
                    else f"existing governed Intraday analysis in flight · {inflight_age:.0f}s"
                    if bounded_inflight
                    else str((result or {}).get("waiting_on") or "higher-priority interactive work")
                    if deliberate_yield
                    else f"next full sweep in {int(delay)}s"
                    if sweep_complete
                    else f"next governed live-analysis cycle in {int(delay)}s"
                    if self.desk == "intraday"
                    else f"incomplete sweep; next governed cycle in {int(delay)}s"
                )
                sup.set_expected_idle(name, expected_idle, waiting_on=waiting_on)
            for tick in range(max(1, int(delay * 5))):
                if not running_fn() or (sup is not None and not sup.running): return
                if sup is not None and tick % 25 == 0:
                    sup.beat(name)
                if tick % 5 == 0:
                    try:
                        with self.host.lock:
                            health = self.host.status.setdefault("worker_health", {}).setdefault(name, {})
                            health["seconds_to_next"] = max(0, int(delay - (tick / 5.0)))
                    except Exception:
                        pass
                time.sleep(0.2)


class DeskPositionLifecycleAuthority:
    VERSION = "desk-position-lifecycle-authority-1.0.0"

    def __init__(self, host, desk: str, repository=None):
        if desk not in {"intraday", "delivery"}:
            raise ValueError("unsupported desk")
        self.host = host
        self.desk = desk
        self.repository = repository

    def run_once(self) -> Dict[str, Any]:
        positions = [dict(row) for row in self.host.model_portfolio.open_positions() if str(row.get("mode") or "").lower() == self.desk]
        symbols = [str(row.get("symbol") or "").upper() for row in positions]
        max_age = 8.0 if self.desk == "intraday" else 60.0
        quotes = self.host.live_market.quotes.snapshot(symbols, market_open=self.host.market_open(), max_age_sec=max_age) if symbols else {}
        thesis_packets: Dict[str, Dict[str, Any]] = {}
        gap_bars: Dict[str, list[Dict[str, Any]]] = {}
        for position in positions:
            symbol = str(position.get("symbol") or "").upper()
            quote = quotes.get(symbol) or quotes.get(position.get("symbol"))
            if not isinstance(quote, dict) or not DEFAULT_OPEN_POSITION_GAP_RECOVERY_AUTHORITY.needs_recovery(position, quote):
                continue
            payload = {}
            try:
                payload = json.loads(position.get("payload_json") or "{}")
            except Exception:
                payload = {}
            instrument_key = str(
                position.get("instrument_key") or payload.get("instrument_key")
                or payload.get("provider_instrument_key") or ""
            ).strip()
            market_data = getattr(self.host, "market_data", None)
            if instrument_key and market_data is not None:
                try:
                    # Local authorities only: Parquet/QuestDB/hot-runtime merge.
                    # No provider fetch is permitted in open-position recovery.
                    gap_bars[symbol] = list(market_data.stored_candles(instrument_key, "1m", limit=5000) or [])
                except Exception:
                    gap_bars[symbol] = []
            else:
                gap_bars[symbol] = []
        evidence_service = getattr(self.host, "current_thesis_evidence", None)
        if evidence_service is not None:
            for position in positions:
                symbol = str(position.get("symbol") or "").upper()
                quote = quotes.get(symbol) or quotes.get(position.get("symbol"))
                if not isinstance(quote, dict):
                    continue
                try:
                    thesis_packets[symbol] = evidence_service.build(position, quote)
                except Exception as exc:
                    thesis_packets[symbol] = {
                        "authority": "CurrentThesisEvidenceService", "state": "UNAVAILABLE",
                        "full_thesis_ready": False, "blockers": ["CURRENT_THESIS_EVIDENCE_BUILD_FAILED"],
                        "error": str(exc)[:200], "provider_io": False, "broker_authority": "NONE",
                    }
        result = self.host.model_portfolio.mark_quotes(
            quotes, mode=self.desk, thesis_evidence=thesis_packets, gap_bars=gap_bars
        ) if positions else {"ok": True, "updated": [], "phase": {}}
        payload = {
            "open_positions": len(positions), "updated": len(result.get("updated") or []),
            "phase": result.get("phase") or {}, "thesis_packets": len(thesis_packets),
            "gap_recovery_candidates": len(gap_bars),
            "full_thesis_ready": sum(1 for packet in thesis_packets.values() if packet.get("full_thesis_ready") is True),
        }
        if self.repository is not None:
            self.repository.checkpoint(f"{self.desk}_lifecycle", self.desk, "lifecycle", "ready", payload)
        return {"ok": True, **payload}

    def loop(self, sup=None, *, running_fn: Callable[[], bool]):
        name = f"{self.desk}_lifecycle"
        while running_fn() and (sup is None or sup.running):
            if sup: sup.beat(name)
            try:
                result = self.run_once()
                if sup:
                    completed = int(result.get("updated") or 0)
                    total = int(result.get("open_positions") or 0)
                    updates = list(result.get("updated") or [])
                    mark_token = tuple(sorted(
                        (
                            str(row.get("symbol") or ""), str(row.get("status") or ""),
                            str(row.get("action") or ""), str(row.get("exit_reason") or ""),
                            str(row.get("last_price") or row.get("price") or ""),
                            str(row.get("net_pnl") or ""),
                        )
                        for row in updates if isinstance(row, dict)
                    ))
                    sup.progress(
                        name, token=f"{total}:{completed}:{mark_token}",
                        stage="position_lifecycle", completed_units=completed, total_units=total,
                        waiting_on=None, expected_idle=(total == 0),
                    )
            except Exception as exc:
                recorder = getattr(self.host, "record_error", None)
                if callable(recorder): recorder(name, str(exc))
                if self.repository is not None:
                    try: self.repository.checkpoint(name, self.desk, "lifecycle", "error", {"error": str(exc)[:300]})
                    except Exception: pass
            delay = 0.5 if self.desk == "intraday" and self.host.market_open() else (5.0 if self.desk == "intraday" else 30.0)
            for _ in range(max(1, int(delay * 5))):
                if not running_fn() or (sup is not None and not sup.running): return
                time.sleep(0.2)
