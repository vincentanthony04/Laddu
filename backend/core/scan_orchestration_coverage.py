"""Index-level and complete intraday-coverage lane orchestration."""
from __future__ import annotations

from core.scan_orchestration_dependencies import *  # noqa: F401,F403


class ScanCoverageMixin:
    def run_index_level_scan(self) -> Dict[str, Any]:
            """Refresh required index levels cache-first without stealing interactive I/O.

            R50 removes the previous four-index x four-timeframe foreground provider
            burst.  Local candle authority is consumed first.  Missing local series
            are scheduled for the existing historical refresh lane; when selected-
            stock priority is active the index worker yields truthfully instead of
            spinning into NO_PROGRESS/recovery loops.
            """
            with self.host.lock:
                st = self.host.status.setdefault("mode_scanners", {}).setdefault("index_levels", {})
                st.update({"state": "running", "last_run": now_iso()})
            governor = getattr(self.host, "workload_governor", None)
            try:
                governor_snapshot = dict(governor.snapshot() or {}) if governor is not None else {}
            except Exception:
                governor_snapshot = {}
            interactive_priority = bool(governor_snapshot.get("interactive_priority_active"))
            count = errors = deferred = 0
            for name in ("NIFTY 50", "SENSEX", "NIFTY BANK", "FINNIFTY"):
                try:
                    inst = self.host._index_instrument_for_chart(name)
                    if not inst or not inst.get("instrument_key"):
                        errors += 1
                        continue
                    instrument_key = inst.get("instrument_key")
                    for interval, days, local_limit in (
                        ("1minute", 3, 1600), ("5minute", 7, 900),
                        ("15minute", 15, 700), ("day", 120, 180),
                    ):
                        candles = []
                        try:
                            candles = list(self.host._stored_candles(instrument_key, interval, limit=local_limit) or [])
                        except Exception:
                            candles = []
                        if not candles:
                            # Hydrate via the existing bounded background lane; do not
                            # make the index worker compete with an interactive stock.
                            try:
                                self.host._schedule_historical_refresh(
                                    instrument_key, interval, days,
                                    reason=f"index-levels:{name}:{interval}",
                                )
                            except Exception:
                                pass
                            if interactive_priority:
                                deferred += 1
                                continue
                            try:
                                candles = self.host.historical_candles(
                                    instrument_key, interval, days, max_wait_sec=0.35
                                )
                            except Exception:
                                candles = []
                        if not candles:
                            deferred += 1
                            continue
                        levels = compute_levels_from_candles(candles, interval=interval)
                        levels.update({
                            "symbol": name, "interval": interval,
                            "instrument_key": instrument_key, "cached_at": now_iso(),
                            "source": "LOCAL_CANDLE_AUTHORITY",
                        })
                        self.host._level_cache[f"{symbolKey_py(name)}|{interval}"] = levels
                        count += 1
                except Exception as exc:
                    errors += 1
                    self.host.event("WARN", "index_levels", "Index S/R prewarm failed", {"index": name, "error": str(exc)[:160]})
            self.host._level_cache_ts = time.time()
            refreshed_at = now_iso() if count > 0 else None
            with self.host.lock:
                st.update({
                    "state": "idle" if count > 0 else "yielding_to_higher_priority" if deferred else "degraded",
                    "levels_cached": count,
                    "errors": errors,
                    "deferred": deferred,
                    "last_run": now_iso(),
                    "last_success_at": refreshed_at or st.get("last_success_at"),
                    "refresh_generation": int(st.get("refresh_generation") or 0) + (1 if count > 0 else 0),
                    "next_run": "20s market-open / 4m closed",
                })
                generation = int(st.get("refresh_generation") or 0)
            if count > 0:
                return {
                    "ok": True, "state": "REFRESHED",
                    "cursor": f"index-levels:{generation}:{refreshed_at}",
                    "count": count, "levels_cached": count, "errors": errors,
                    "deferred": deferred,
                }
            if deferred:
                return {
                    "ok": True, "state": "YIELDING_TO_HIGHER_PRIORITY",
                    "cursor": f"index-levels:{generation}:deferred:{deferred}",
                    "count": 0, "levels_cached": 0, "errors": errors,
                    "deferred": deferred,
                    "waiting_on": "local index candle cache hydration" + ("; selected-stock priority active" if interactive_priority else ""),
                    "yield_reason": "index candle cache warming under bounded I/O",
                }
            return {
                "ok": False, "state": "NO_LEVELS_REFRESHED", "cursor": None,
                "count": 0, "levels_cached": 0, "errors": errors,
            }

    @staticmethod
    def _coverage_reference_close(rows: List[Dict[str, Any]], *, today=None) -> tuple[float | None, List[float]]:
            """Return the completed-session previous close and baseline volumes.

            Daily rows arrive newest-first from the batch repository.  When the
            newest row is dated today it represents the current completed session,
            so the comparison close is the row before it.  During market hours the
            newest completed row is normally yesterday and is itself the reference.
            """
            today = today or india_now().date()
            clean = [row for row in (rows or []) if row.get("close") is not None]
            if not clean:
                return None, []
            latest_date = None
            try:
                stamp = datetime.fromisoformat(str(clean[0].get("timestamp") or "").replace("Z", "+00:00"))
                latest_date = stamp.astimezone(IST).date() if stamp.tzinfo else stamp.date()
            except (TypeError, ValueError):
                pass
            reference_index = 1 if latest_date is not None and latest_date >= today and len(clean) > 1 else 0
            try:
                previous_close = float(clean[reference_index].get("close"))
            except (TypeError, ValueError, IndexError):
                previous_close = None
            baseline_rows = clean[reference_index: reference_index + 20]
            volumes = []
            for row in baseline_rows:
                try:
                    value = float(row.get("volume"))
                    if value > 0:
                        volumes.append(value)
                except (TypeError, ValueError):
                    continue
            return previous_close, volumes

    def _enrich_coverage_quotes(self, quotes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
            """Compute cheap activity evidence without historical/storage I/O.

            Whole-universe coverage must advance at provider-quote speed.  Upstox
            V3 LTP already exposes the documented previous close (``cp``) when
            available and the REST adapter normalises it into ``change_pct``.
            Current volume is likewise used only when the quote carries it.

            No CandleLake/Parquet/SQLite/PostgreSQL lookup is permitted here.
            Missing movement/volume evidence contributes zero to analysis priority
            and never blocks the sweep or creates trade authority.  Trailing
            liquidity rank is applied later by IntelligentUniverseScreeningService.
            """
            out = []
            for raw in quotes or []:
                row = dict(raw)
                movement = abs(float(row.get("change_pct") or 0.0))
                rvol = max(0.0, float(row.get("relative_volume") or 0.0))
                volume = max(0.0, float(row.get("volume") or 0.0))
                row["activity_score"] = round(
                    min(100.0, movement * 12.0 + min(30.0, rvol * 8.0) + min(20.0, log10(volume + 1.0) * 3.0)),
                    2,
                )
                row["coverage_enrichment_source"] = "provider_quote_only"
                row["coverage_historical_io"] = False
                row["trade_confidence_affected"] = False
                out.append(row)
            return out

    def _persist_market_radar_snapshot(self, cache: Dict[str, Dict[str, Any]]) -> None:
            now = time.time()
            last = float(getattr(self.host, "_last_market_radar_persist_at", 0.0) or 0.0)
            if now - last < 60.0:
                return
            rows = [dict(row) for row in (cache or {}).values() if isinstance(row, dict) and row.get("identity_verified") and row.get("ltp") is not None]
            rows.sort(key=lambda row: (float(row.get("activity_score") or 0.0), abs(float(row.get("change_pct") or 0.0))), reverse=True)
            snapshot = []
            for row in rows[:160]:
                item = dict(row)
                item["radar_source"] = "persisted_verified_coverage"
                snapshot.append(item)
            try:
                self.host.store.set_kv("market_radar_coverage:last", snapshot)
                self.host._last_market_radar_persist_at = now
            except Exception:
                pass

    def run_intraday_coverage_pass(self) -> Dict[str, Any]:
            return self.lanes.execute("intraday_coverage", self._run_intraday_coverage_pass_impl)

    def _run_intraday_coverage_pass_impl(self) -> Dict[str, Any]:
            self._ensure_checkpoint_reconciled("intraday")
            mode_st = self._mode_status("intraday")
            coverage_st = self._lane_status("intraday", "coverage")
            if not self.host.client.token_status().get("ok"):
                with self.host.lock:
                    coverage_st.update({"state": "waiting_token", "last_run": now_iso()})
                    mode_st.update({"coverage_state": "waiting_token", "coverage_last_run": now_iso()})
                return {"ok": False, "error": "waiting_token"}
            if not self._focused_universe_ready():
                with self.host.lock:
                    coverage_st.update({"state": "waiting_focused_universe", "last_run": now_iso()})
                    mode_st.update({"coverage_state": "waiting_focused_universe", "coverage_last_run": now_iso()})
                return {"ok": False, "error": "waiting_focused_universe"}
            market_open = is_india_market_open()
            # Closed-market quotes are still useful for verified end-of-day Radar
            # and restart recovery. This lane never promotes trades, so it may run
            # on the slower closed-market cadence without violating Intraday gates.
            if not market_open:
                with self.host.lock:
                    coverage_st.update({"state": "closed_market_refresh", "last_run": now_iso()})
                    mode_st.update({"coverage_state": "closed_market_refresh", "coverage_last_run": now_iso()})
            if time.time() < self.host.quote_blocked_until:
                with self.host.lock:
                    coverage_st.update({"state": "quote_rate_limited", "last_run": now_iso()})
                    mode_st.update({"coverage_state": "quote_rate_limited", "coverage_last_run": now_iso()})
                return {"ok": False, "error": "quote_rate_limited"}

            universe_rows = self.host.immutable_scan_population("intraday")
            universe = [row for row in universe_rows if row.get("instrument_key") and str(row.get("trading_symbol") or "").strip()]
            if not universe:
                with self.host.lock:
                    coverage_st.update({"state": "waiting_universe", "last_run": now_iso()})
                    mode_st.update({"coverage_state": "waiting_universe", "coverage_last_run": now_iso()})
                return {"ok": False, "error": "waiting_universe"}

            universe_size = len(universe)
            previous_complete = bool(coverage_st.get("sweep_complete"))
            sweep_number = max(1, int(coverage_st.get("sweep_number") or 1))
            if previous_complete:
                # Begin the next sweep exactly once. The completed 100% snapshot
                # remains visible until this next bounded attempt starts.
                sweep_number += 1
                cursor = 0
                attempted_before = returned_before = verified_before = missing_before = unverified_before = 0
            else:
                cursor = max(0, min(universe_size - 1, int(coverage_st.get("cursor") or coverage_st.get("coverage_cursor") or 0)))
                attempted_before = int(coverage_st.get("sweep_attempted") or coverage_st.get("sweep_scanned") or 0)
                returned_before = int(coverage_st.get("sweep_returned") or 0)
                verified_before = int(coverage_st.get("sweep_verified") or 0)
                missing_before = int(coverage_st.get("sweep_missing") or 0)
                unverified_before = int(coverage_st.get("sweep_unverified") or 0)
            remaining = max(0, universe_size - cursor)
            coverage_size = min(INTRADAY_COVERAGE_LANE, remaining)
            batch = universe[cursor:cursor + coverage_size]
            started = time.time()
            quotes = []
            coverage_rows: List[Dict[str, Any]] = []
            error = None
            try:
                try:
                    quotes = self.host.client.quotes(batch, persist=False) or []
                except TypeError:
                    # Compatibility with test doubles and older adapters.
                    quotes = self.host.client.quotes(batch) or []
                quotes = self._enrich_coverage_quotes(quotes)
                try:
                    self.host.runtime_market_state.save_latest_quotes(quotes)
                except Exception as exc:
                    self.host.record_error("runtime_coverage_quote_persist", str(exc))
                display_quotes = MarketRadarQuoteService.fetch_display_quotes(
                    self.host.client, batch, quotes, enrich=self._enrich_coverage_quotes
                )
                coverage_rows = MarketRadarQuoteService.classify_rows(
                    quotes, display_quotes, market_open=market_open, now=india_now()
                )
                if coverage_rows:
                    # Publish memory under the runtime lock; persist after release.
                    with self.host.lock:
                        self.host.status["last_price_refresh"] = now_iso()
                        cache = getattr(self.host, "_coverage_quote_cache", None)
                        if cache is None:
                            cache = {}
                            self.host._coverage_quote_cache = cache
                        MarketRadarQuoteService.merge_cache(
                            cache, coverage_rows, seen_at=now_iso(),
                            max_entries=max(2500, universe_size + 256),
                        )
                        cache_for_persist = dict(cache)
                    self._persist_market_radar_snapshot(cache_for_persist)
            except Exception as exc:
                error = str(exc)[:240]
                self.host.record_error("intraday_coverage", str(exc), "/v3/market-quote/ltp")

            # Attempted, returned and identity-verified are separate truths.  The
            # cursor advances by the exact non-wrapping base batch. Missing symbols
            # are retried on a future sweep; they are never reported as verified.
            attempted_batch = coverage_size
            returned_batch = min(attempted_batch, len(quotes))
            verified_batch = min(returned_batch, len([
                row for row in quotes
                if row.get("identity_verified") and row.get("ltp") is not None
            ]))
            missing_batch = max(0, attempted_batch - returned_batch)
            unverified_batch = max(0, returned_batch - verified_batch)
            attempted = min(universe_size, attempted_before + attempted_batch)
            returned_total = min(attempted, returned_before + returned_batch)
            verified_total = min(returned_total, verified_before + verified_batch)
            missing_total = min(attempted, missing_before + missing_batch)
            unverified_total = min(returned_total, unverified_before + unverified_batch)
            raw_next_cursor = cursor + coverage_size
            sweep_complete = raw_next_cursor >= universe_size
            next_cursor = 0 if sweep_complete else raw_next_cursor
            completed_snapshot = None
            if sweep_complete:
                completed_snapshot = {
                    "sweep_number": sweep_number,
                    "universe_size": universe_size,
                    "attempted": attempted,
                    "returned": returned_total,
                    "verified": verified_total,
                    "missing": missing_total,
                    "unverified": unverified_total,
                    "completed_at": now_iso(),
                }

            # Whole-universe intelligent screen.  The coverage cache accumulates
            # the current verified quote sweep across the immutable Intraday
            # snapshot. Screening is cheap and only schedules scarce deep
            # analysis; it never changes trade confidence or promotion gates.
            with self.host.lock:
                cache_for_screen = dict(getattr(self.host, "_coverage_quote_cache", {}) or {})
            screen_quote_rows = [
                MarketRadarQuoteService.classify_row(row, market_open=market_open, now=india_now())
                for row in cache_for_screen.values()
            ]
            screening = IntelligentUniverseScreeningService().classify(
                universe,
                screen_quote_rows,
                liquidity_rank_by_symbol=getattr(self.host, "_intraday_liquidity_rank_by_symbol", {}) or {},
                priority_symbols=getattr(self.host, "_intraday_priority_symbols", set()) or set(),
                market_open=market_open,
            )
            ranked = SelectionFairnessService().rank_for_analysis(
                screening.get("eligible_rows") or [], INTRADAY_SCREEN_SHORTLIST
            )
            candidates = [str(q.get("symbol") or q.get("trading_symbol") or "").upper().strip() for q in ranked]
            candidates = [x for x in candidates if x]
            coverage_by_symbol = MarketRadarQuoteService.by_symbol(screen_quote_rows)
            fair_queue_rows = []
            for q in ranked:
                symbol = str(q.get("symbol") or q.get("trading_symbol") or "").upper().strip()
                if not symbol:
                    continue
                price_truth = coverage_by_symbol.get(symbol) or MarketRadarQuoteService.classify_row(
                    q, market_open=market_open, now=india_now()
                )
                fair_queue_rows.append({
                    "symbol": symbol, "trading_symbol": q.get("trading_symbol") or symbol,
                    "instrument_key": q.get("instrument_key"), "instrument_type": q.get("instrument_type") or "EQ",
                    "exchange": q.get("exchange") or "NSE",
                    "sector": q.get("sector") or q.get("industry") or q.get("sector_bucket") or "Sector pending",
                    "ltp": q.get("ltp"), "change_pct": q.get("change_pct"), "volume": q.get("volume"),
                    "source": "full_universe_intelligent_screen", "candidate_stage": "SCREENED_SHORTLIST",
                    "opportunity_stage": "Screened", "status": "WATCH", "decision": "ANALYSIS_PENDING",
                    "screening_score": q.get("screening_score"),
                    "screening_score_breakdown": q.get("screening_score_breakdown"),
                    "screening_version": q.get("screening_version") or SCREENING_VERSION,
                    "trailing_liquidity_rank": q.get("trailing_liquidity_rank"),
                    "spread_bps": q.get("spread_bps"),
                    "analysis_priority_score": q.get("analysis_priority_score"),
                    "base_analysis_priority": q.get("base_analysis_priority"),
                    "fairness_adjustment": q.get("fairness_adjustment"),
                    "analysis_priority_breakdown": q.get("analysis_priority_breakdown"),
                    "fairness_version": q.get("fairness_version") or FAIRNESS_VERSION,
                    "identity_verified": bool(price_truth.get("identity_verified")),
                    "freshness_state": price_truth.get("freshness_state") or "unverified",
                    "stale": bool(price_truth.get("stale")),
                    "source_time": price_truth.get("source_time"),
                    "reason": "Whole-universe cheap screen + fairness ranking only; MTF/model/thesis/risk evidence is still pending.",
                    "priority_reason": "Analysis scheduling score only; not trade confidence.",
                })
            screening_scope = "FULL_SWEEP" if sweep_complete else "PARTIAL_SWEEP"
            screening_summary = {
                "version": screening.get("version") or SCREENING_VERSION,
                "scope": screening_scope,
                "population_count": universe_size,
                "observed_count": int(screening.get("observed_count") or 0),
                "eligible_count": int(screening.get("eligible_count") or 0),
                "rejected_count": int(screening.get("rejected_count") or 0),
                "pending_count": int(screening.get("pending_count") or 0),
                "rejection_reasons": dict(screening.get("rejection_reasons") or {}),
                "pending_reasons": dict(screening.get("pending_reasons") or {}),
                "shortlist_count": len(fair_queue_rows),
                "shortlist_cap": INTRADAY_SCREEN_SHORTLIST,
                "deep_analysis_per_cycle": INTRADAY_DEEP_ANALYSIS,
                "trade_confidence_affected": False,
                "updated_at": now_iso(),
            }
            try:
                self.host.store.set_kv("intraday_coverage_candidates", candidates)
                self.host.store.set_kv("fair_analysis_queue:last", fair_queue_rows)
                self.host.store.set_kv("intelligent_screening:last", screening_summary)
            except Exception:
                pass

            state_name = ("idle" if market_open else "closed_market_ready") if not error else "degraded"
            coverage_pct = round(attempted * 100.0 / max(1, universe_size), 1)
            verified_pct = round(verified_total * 100.0 / max(1, universe_size), 1)
            remaining_cycles = 0 if sweep_complete else ceil(max(0, universe_size - raw_next_cursor) / max(1, INTRADAY_COVERAGE_LANE))
            cadence_seconds = INTRADAY_COVERAGE_OPEN_SECONDS if market_open else INTRADAY_COVERAGE_CLOSED_SECONDS
            next_batch_epoch = time.time() + cadence_seconds
            next_batch_at = datetime.fromtimestamp(next_batch_epoch, tz=india_now().tzinfo).isoformat(timespec="seconds")
            try:
                if self.checkpoints is not None:
                    self.checkpoints.persist("intraday", "coverage", {
                    "cursor": next_cursor, "universe_size": universe_size,
                    "sweep_number": sweep_number, "sweep_attempted": attempted,
                    "sweep_returned": returned_total, "sweep_verified": verified_total,
                    "sweep_missing": missing_total, "sweep_unverified": unverified_total,
                    "coverage_pct": coverage_pct, "verified_pct": verified_pct,
                    "sweep_complete": sweep_complete, "estimated_cycles_remaining": remaining_cycles,
                    "screening_observed": screening_summary["observed_count"],
                    "screening_eligible": screening_summary["eligible_count"],
                    "screening_rejected": screening_summary["rejected_count"],
                    "screening_pending": screening_summary["pending_count"],
                    "screening_shortlisted": screening_summary["shortlist_count"],
                    "screening_scope": screening_summary["scope"],
                    "screening_version": screening_summary["version"],
                    "last_completed": completed_snapshot or coverage_st.get("last_completed"),
                    "last_run": now_iso(), "next_batch_at": next_batch_at,
                    "next_run": f"{cadence_seconds}s", "selection_scheduler": FAIRNESS_VERSION,
                    "selection_policy": "whole-universe intelligent screen then fair analysis scheduling; promotion confidence unchanged",
                    }, universe=universe, identity=self._snapshot_identity("intraday"))
            except Exception as exc:
                self.host.record_error("intraday_scan_checkpoint_persist", str(exc))
            with self.host.lock:
                coverage_st.update({
                    "state": state_name,
                    "cursor": next_cursor,
                    "universe_size": universe_size,
                    "sweep_number": sweep_number,
                    "sweep_attempted": attempted,
                    "sweep_returned": returned_total,
                    "sweep_verified": verified_total,
                    "sweep_missing": missing_total,
                    "sweep_unverified": unverified_total,
                    "coverage_pct": coverage_pct,
                    "verified_pct": verified_pct,
                    "sweep_complete": sweep_complete,
                    "estimated_cycles_remaining": remaining_cycles,
                    "screening_observed": screening_summary["observed_count"],
                    "screening_eligible": screening_summary["eligible_count"],
                    "screening_rejected": screening_summary["rejected_count"],
                    "screening_pending": screening_summary["pending_count"],
                    "screening_shortlisted": screening_summary["shortlist_count"],
                    "screening_scope": screening_summary["scope"],
                    "screening_version": screening_summary["version"],
                    "screening_rejection_reasons": screening_summary["rejection_reasons"],
                    "screening_pending_reasons": screening_summary["pending_reasons"],
                    "batch_attempted": attempted_batch,
                    "batch_returned": returned_batch,
                    "batch_verified": verified_batch,
                    "batch_missing": missing_batch,
                    "batch_unverified": unverified_batch,
                    "elapsed_sec": round(time.time() - started, 2),
                    "last_run": now_iso(),
                    "error": error,
                    "last_completed": completed_snapshot or coverage_st.get("last_completed"),
                    "selection_scheduler": FAIRNESS_VERSION,
                    "selection_policy": "whole-universe intelligent screen then fair analysis scheduling; promotion confidence unchanged",
                    "cadence_seconds": cadence_seconds,
                    "next_batch_at": next_batch_at,
                    "next_run": f"{cadence_seconds}s",
                })
                # Compatibility aliases are projections only. New UI/API consumers
                # read the nested coverage contract above.
                mode_st.update({
                    "coverage_state": state_name,
                    "coverage_cursor": next_cursor,
                    "coverage_universe": universe_size,
                    "sweep_scanned": attempted,
                    "coverage_pct": coverage_pct,
                    "verified_coverage_pct": verified_pct,
                    "sweep_number": sweep_number,
                    "sweep_complete": sweep_complete,
                    "estimated_cycles_remaining": remaining_cycles,
                    "coverage_quote_scanned": returned_batch,
                    "coverage_verified": verified_batch,
                    "coverage_missing": missing_batch,
                    "coverage_unverified": unverified_batch,
                    "radar_measured": verified_batch,
                    "radar_change_ready": len([row for row in quotes if row.get("change_pct") is not None]),
                    "radar_volume_ready": len([row for row in quotes if row.get("relative_volume") is not None or row.get("volume") is not None]),
                    "coverage_batch": coverage_size,
                    "coverage_elapsed_sec": round(time.time() - started, 2),
                    "coverage_last_run": now_iso(),
                    "coverage_error": error,
                    "selection_scheduler": FAIRNESS_VERSION,
                    "selection_policy": "whole-universe intelligent screen then fair analysis scheduling; promotion confidence unchanged",
                    "screening_observed": screening_summary["observed_count"],
                    "screening_eligible": screening_summary["eligible_count"],
                    "screening_rejected": screening_summary["rejected_count"],
                    "screening_pending": screening_summary["pending_count"],
                    "screening_shortlisted": screening_summary["shortlist_count"],
                    "screening_scope": screening_summary["scope"],
                    "screening_version": screening_summary["version"],
                    "coverage_cadence_seconds": cadence_seconds,
                    "coverage_next_batch_at": next_batch_at,
                    "coverage_next_run": f"{cadence_seconds}s",
                })
            # Market Radar owns an independent projection worker.  Coverage only
            # publishes its verified cache and marks it dirty; synchronously
            # rebuilding the read model here coupled quote coverage to unrelated
            # BSE/cost/DB projection latency and could strand this lane for minutes.
            with self.host.lock:
                self.host.status["market_radar_projection_dirty_at"] = now_iso()
            cycle_evidence = None
            if completed_snapshot is not None:
                cycle_evidence = self._record_scanner_cycle_evidence(
                    "intraday", "full_sweep", dict(
                        completed_snapshot, market_open=market_open,
                        screening=screening_summary,
                    )
                )
            return {
                "ok": not error,
                "state": state_name,
                "market_open": market_open,
                # R40: supervisor-facing truth is cumulative within the immutable
                # sweep; batch counts remain separately named for diagnostics.
                "scanned": attempted,
                "population_count": universe_size,
                "total": universe_size,
                "coverage_pct": coverage_pct,
                "covered": attempted_batch,
                "attempted": attempted_batch,
                "returned": returned_batch,
                "verified": verified_batch,
                "quotes": len(quotes),
                "cursor": next_cursor,
                "sweep_number": sweep_number,
                "sweep_complete": sweep_complete,
                "screening": screening_summary,
                "shortlisted": len(fair_queue_rows),
                "cycle_evidence": cycle_evidence,
            }

    def run_delivery_coverage_pass(self) -> Dict[str, Any]:
            """Advance the immutable Delivery universe independently of deep analysis.

            This lane is intentionally local/cache-only.  It never calls provider
            quote APIs, historical acquisition, fundamentals, mathematics or model
            inference.  Its only authority is coverage/accounting of the exact
            immutable Delivery snapshot so slow deep work cannot pin the operator
            progress counter at one committed batch.
            """
            return self.lanes.execute("delivery_coverage", self._run_delivery_coverage_pass_impl)

    def _run_delivery_coverage_pass_impl(self) -> Dict[str, Any]:
            mode_st = self._mode_status("delivery")
            coverage_st = self._lane_status("delivery", "coverage")
            if not self._focused_universe_ready():
                with self.host.lock:
                    coverage_st.update({"state": "waiting_focused_universe", "last_run": now_iso()})
                return {"ok": False, "error": "waiting_focused_universe", "sweep_complete": False}

            universe_rows = self.host.immutable_scan_population("delivery")
            universe = [row for row in universe_rows if row.get("instrument_key") and str(row.get("trading_symbol") or row.get("symbol") or "").strip()]
            if not universe:
                with self.host.lock:
                    coverage_st.update({"state": "waiting_universe", "last_run": now_iso()})
                return {"ok": False, "error": "waiting_universe", "sweep_complete": False}

            universe_size = len(universe)
            identity = self._snapshot_identity("delivery")
            # Coverage owns its own exact-snapshot checkpoint. Deep analysis keeps
            # its existing delivery:analysis checkpoint and can lag safely.
            if self.checkpoints is not None:
                try:
                    reconciled = self.checkpoints.reconcile("delivery", "coverage", expected=identity)
                    checkpoint = dict(reconciled.get("checkpoint") or {})
                    checkpoint_key = f"{checkpoint.get('snapshot_id')}:{checkpoint.get('population_count')}:{checkpoint.get('content_hash')}"
                    active_key = f"{coverage_st.get('snapshot_id')}:{coverage_st.get('population_count')}:{coverage_st.get('content_hash')}"
                    if checkpoint_key != active_key or not coverage_st.get("version"):
                        self.checkpoints.apply(coverage_st, checkpoint)
                except Exception as exc:
                    self.host.record_error("delivery_coverage_checkpoint_reconcile", str(exc)[:180])

            previous_complete = bool(coverage_st.get("sweep_complete"))
            sweep_number = max(1, int(coverage_st.get("sweep_number") or 1))
            if previous_complete:
                sweep_number += 1
                cursor = 0
                scanned_before = verified_before = missing_before = 0
            else:
                cursor = max(0, min(universe_size - 1, int(coverage_st.get("cursor") or 0)))
                scanned_before = int(coverage_st.get("sweep_scanned") or coverage_st.get("sweep_attempted") or 0)
                verified_before = int(coverage_st.get("sweep_verified") or 0)
                missing_before = int(coverage_st.get("sweep_missing") or 0)

            # Cache-only lane can safely rotate wide batches; no provider I/O and
            # no deep-analysis worker is consumed here.
            batch_size = min(256, max(0, universe_size - cursor))
            batch = universe[cursor:cursor + batch_size]
            quote_by_key: Dict[str, Dict[str, Any]] = {}
            try:
                symbols = [str(row.get("trading_symbol") or row.get("symbol") or "").upper() for row in batch]
                for quote in self.host.runtime_market_state.latest_quotes(symbols) or []:
                    key = str(quote.get("instrument_key") or "")
                    if key and quote.get("ltp") is not None:
                        quote_by_key[key] = dict(quote)
            except Exception:
                pass
            try:
                cache = getattr(self.host, "_coverage_quote_cache", {}) or {}
                for inst in batch:
                    symbol = str(inst.get("trading_symbol") or inst.get("symbol") or "").upper()
                    quote = cache.get(symbol) or cache.get(symbolKey_py(symbol))
                    if isinstance(quote, dict) and quote.get("ltp") is not None:
                        row = dict(quote)
                        row.setdefault("instrument_key", inst.get("instrument_key"))
                        quote_by_key[str(row.get("instrument_key") or "")] = row
            except Exception:
                pass

            scanned_batch = len(batch)
            verified_batch = sum(1 for inst in batch if str(inst.get("instrument_key") or "") in quote_by_key)
            missing_batch = max(0, scanned_batch - verified_batch)
            scanned = min(universe_size, scanned_before + scanned_batch)
            verified = min(scanned, verified_before + verified_batch)
            missing = min(scanned, missing_before + missing_batch)
            raw_next_cursor = cursor + scanned_batch
            sweep_complete = raw_next_cursor >= universe_size
            next_cursor = 0 if sweep_complete else raw_next_cursor
            coverage_pct = round(scanned * 100.0 / max(1, universe_size), 1)
            completed_at = now_iso()
            last_completed = coverage_st.get("last_completed")
            if sweep_complete:
                last_completed = {
                    "sweep_number": sweep_number,
                    "universe_size": universe_size,
                    "scanned": scanned,
                    "verified": verified,
                    "missing": missing,
                    "completed_at": completed_at,
                }
            cadence_seconds = 4 if is_india_market_open() else 8
            runtime_telemetry = {
                "coverage_lane": "CACHE_ONLY",
                "provider_io": False,
                "deep_analysis_io": False,
                "batch_size": scanned_batch,
                "verified_batch": verified_batch,
                "missing_batch": missing_batch,
                "next_retry_in_seconds": cadence_seconds,
                "last_progress_age_seconds": 0,
                "blocker_reason": None,
            }
            payload = {
                "cursor": next_cursor,
                "universe_size": universe_size,
                "sweep_number": sweep_number,
                "sweep_scanned": scanned,
                "sweep_attempted": scanned,
                "sweep_verified": verified,
                "sweep_missing": missing,
                "coverage_pct": coverage_pct,
                "verified_pct": round(verified * 100.0 / max(1, universe_size), 1),
                "sweep_complete": sweep_complete,
                "last_completed": last_completed,
                "last_run": completed_at,
                "next_run": f"{cadence_seconds}s",
            }
            if self.checkpoints is not None:
                try:
                    self.checkpoints.persist("delivery", "coverage", payload, universe=universe, identity=identity)
                except Exception as exc:
                    self.host.record_error("delivery_coverage_checkpoint_persist", str(exc)[:180])
            with self.host.lock:
                coverage_st.update(payload)
                coverage_st.update({
                    "state": "complete" if sweep_complete else "continuing_sweep",
                    "snapshot_id": identity.get("snapshot_id"),
                    "population_count": universe_size,
                    "content_hash": identity.get("content_hash"),
                    "runtime_telemetry": runtime_telemetry,
                    "last_progress_at": completed_at,
                })
                mode_st.update({
                    "coverage_state": coverage_st["state"],
                    "coverage_cursor": next_cursor,
                    "coverage_universe": universe_size,
                    "coverage_pct": coverage_pct,
                    "coverage_last_run": completed_at,
                    "coverage_next_run": f"{cadence_seconds}s",
                })
            self._publish_scanner_progress("delivery")
            return {
                "ok": True,
                "mode": "delivery",
                "state": coverage_st.get("state"),
                "covered": scanned_batch,
                "scanned": scanned,
                "verified": verified,
                "missing": missing,
                "cursor": next_cursor,
                "population_count": universe_size,
                "coverage_pct": coverage_pct,
                "sweep_number": sweep_number,
                "sweep_complete": sweep_complete,
                "runtime_telemetry": runtime_telemetry,
            }

