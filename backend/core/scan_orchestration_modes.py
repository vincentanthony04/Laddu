"""Live and deep desk scan execution."""
from __future__ import annotations
from core.scan_orchestration_dependencies import *  # noqa: F401,F403
from core.scan_orchestration_progress import cumulative_delivery_sweep_stage_counts
from core.scan_orchestration_rows import scanner_stage_member, research_capture_row, apply_research_only_price_boundary
class ScanModeExecutionMixin:
    @staticmethod
    def _scanner_stage_member(item: Dict[str, Any], state: str, reason: str | None = None) -> Dict[str, Any]:
        return scanner_stage_member(item, state, reason)

    @staticmethod
    def _research_capture_row(decision: Dict[str, Any], instrument: Dict[str, Any], mode: str, quote: Dict[str, Any] | None = None) -> Dict[str, Any]:
        return research_capture_row(decision, instrument, mode, quote)

    def _live_priorities_for_mode(self, mode: str, cap: int) -> list[Dict[str, Any]]:
            mode = require_production_mode(mode)
            if mode != "intraday":
                raise UnsupportedProductionMode("live scanner accepts intraday only; delivery uses the deep scanner")
            raw = self._fast_lane_priorities(max(cap * 2, 24), is_india_market_open())
            rows = [dict(r, mode="intraday") for r in raw if str(r.get("mode") or "").lower() in ("intraday", "all")]
            rows += [
                dict(r, mode="intraday", source="premarket_opportunity_memory")
                for r in raw
                if str(r.get("source") or "") == "opportunity_memory" and int(r.get("priority_score") or 0) >= 70
            ]
            try:
                exact_queue = self.host.store.get_kv("fair_analysis_queue:last", []) or []
                coverage_candidates = self.host.store.get_kv("intraday_coverage_candidates", []) or []
            except Exception:
                exact_queue, coverage_candidates = [], []
            rows += [dict(r, mode="intraday", source="intelligent_screen_shortlist", priority_score=r.get("analysis_priority_score") or r.get("screening_score") or 35)
                     for r in exact_queue[:INTRADAY_SCREEN_SHORTLIST] if isinstance(r, dict) and r.get("instrument_key") and str(r.get("symbol") or "").strip()]
            rows += [{"symbol": str(symbol).upper(), "exchange": "NSE", "mode": "intraday", "source": "intelligent_screen_candidate", "priority_score": 35}
                     for symbol in coverage_candidates[:INTRADAY_SCREEN_SHORTLIST] if str(symbol).strip()]
            if not rows:
                rows = [
                    {"symbol": symbol, "exchange": "NSE", "mode": "intraday", "source": "core_fallback", "priority_score": 10}
                    for symbol in list(NIFTY250_CORE)[:min(cap, 24)]
                ]
            # Identity resolution is a data-agent responsibility.  Map every
            # priority to the immutable PostgreSQL snapshot before the scan so
            # the analysis lane does not spend its entire budget searching one
            # symbol at a time.
            snapshot_by_symbol = {}
            try:
                for item in self.host.immutable_scan_population("intraday") or []:
                    symbol = str(item.get("trading_symbol") or item.get("symbol") or "").upper().strip()
                    if symbol and item.get("instrument_key"):
                        snapshot_by_symbol[symbol] = item
            except Exception:
                snapshot_by_symbol = {}
            def queue_key(row: Dict[str, Any]) -> tuple[int, float, str]:
                source = str(row.get("source") or "").lower()
                protected = bool(row.get("pinned")) or any(token in source for token in ("manual", "search", "user", "open_position"))
                try:
                    score = float(row.get("priority_score") or row.get("analysis_priority_score") or row.get("screening_score") or 0.0)
                except (TypeError, ValueError):
                    score = 0.0
                return (0 if protected else 1, -score, str(row.get("symbol") or ""))

            seen = set(); out = []
            for row in sorted(rows, key=queue_key):
                symbol = str(row.get("symbol") or "").upper().strip()
                if symbol and symbol not in seen:
                    seen.add(symbol)
                    exact = snapshot_by_symbol.get(symbol) or {}
                    out.append(dict(
                        row,
                        symbol=symbol,
                        trading_symbol=exact.get("trading_symbol") or symbol,
                        instrument_key=row.get("instrument_key") or exact.get("instrument_key"),
                        exchange=row.get("exchange") or exact.get("exchange") or "NSE",
                        instrument_type=row.get("instrument_type") or exact.get("instrument_type") or "EQ",
                        mode="intraday",
                        production_policy_version=POLICY_VERSION,
                    ))
                if len(out) >= cap:
                    break
            return out
    def _scanner_quotes_for_instruments(self, instruments: List[Dict[str, Any]], *, allow_rest: bool) -> Dict[str, Dict[str, Any]]:
            """Return only already-ingested stream/cache quotes.

            Clean Core scanners are consumers of market-data read models.  They
            never invoke broker/provider quote APIs inside a scan cycle. Missing
            quotes are symbol-local data gaps and are deferred to ingestion/repair.
            ``allow_rest`` remains only for call-site compatibility and is ignored.
            """
            quote_by_key: Dict[str, Dict[str, Any]] = {}
            symbols = [
                str(inst.get("trading_symbol") or inst.get("symbol") or "").upper()
                for inst in instruments
                if str(inst.get("trading_symbol") or inst.get("symbol") or "").strip()
            ]
            try:
                for quote in self.host.runtime_market_state.latest_quotes(symbols) or []:
                    key = str(quote.get("instrument_key") or "")
                    if key and quote.get("ltp") is not None:
                        quote_by_key[key] = dict(quote)
            except Exception:
                pass
            try:
                coverage = getattr(self.host, "_coverage_quote_cache", {}) or {}
                for inst in instruments:
                    symbol = str(inst.get("trading_symbol") or inst.get("symbol") or "").upper()
                    quote = coverage.get(symbol) or coverage.get(symbolKey_py(symbol))
                    if isinstance(quote, dict) and quote.get("ltp") is not None:
                        row = dict(quote)
                        row.setdefault("instrument_key", inst.get("instrument_key"))
                        key = str(row.get("instrument_key") or "")
                        if key:
                            quote_by_key[key] = row
            except Exception:
                pass

            # Delivery research must continue after the market closes.  A
            # missing live/cache quote is not a reason to throw away an
            # otherwise complete local historical analysis.  For unresolved
            # symbols, use the verified latest completed-session close as a
            # *research-only* price observation.  It is deliberately labelled
            # closed_market / non-promotable so Model Paper admission continues
            # to require a fresh executable quote at the next open session.
            if not is_india_market_open():
                market_data = getattr(self.host, "market_data", None)
                completed_quote = getattr(market_data, "completed_session_quote", None)
                if callable(completed_quote):
                    for inst in instruments:
                        key = str(inst.get("instrument_key") or "")
                        if not key or key in quote_by_key:
                            continue
                        try:
                            row = dict(completed_quote(inst) or {})
                        except Exception as exc:
                            self.host.record_error(
                                "delivery_completed_session_quote",
                                f"{inst.get('trading_symbol') or inst.get('symbol')}: {str(exc)[:160]}",
                            )
                            continue
                        if row.get("ltp") is None:
                            continue
                        row.setdefault("instrument_key", key)
                        row.setdefault("identity_verified", True)
                        # P0-01c: this closed-market quote is a cached
                        # historical closing record -- completed_quote() may
                        # return a row whose own received_at/received_time
                        # reflects whenever that record was originally
                        # captured (hours/days earlier), not when it was
                        # retrieved just now for this research pass. Feeding
                        # that stale value through to research_capture_row()
                        # as if it were this quote's receipt time produces a
                        # false INVALID_TIMESTAMP_ORDER once feature capture
                        # stamps a fresh source_as_of/feature_as_of against
                        # it -- the exact mechanism observed on SYNGENE
                        # (source_as_of 21:28:45, stale received_at 21:17:00)
                        # persisting after market close. The true receipt
                        # time of "using this historical quote for research
                        # right now" is this local retrieval moment.
                        row.pop("received_time", None)
                        row["received_at"] = india_now().isoformat()
                        row.update({
                            "freshness_state": "closed_market",
                            "price_freshness_state": "closed_market",
                            "stale": False,
                            "usable_for_promotion": False,
                            "display_as_live": False,
                            "analysis_price_authority": True,
                            "execution_price_authority": False,
                        })
                        quote_by_key[key] = row
            return quote_by_key
    @staticmethod
    def _quote_rank(inst: Dict[str, Any], quote: Dict[str, Any] | None, priority: int = 0) -> float:
            quote = dict(quote or {})
            try:
                change = abs(float(quote.get("change_pct") or quote.get("pChange") or 0.0))
            except (TypeError, ValueError):
                change = 0.0
            try:
                volume = max(0.0, float(quote.get("volume") or 0.0))
            except (TypeError, ValueError):
                volume = 0.0
            liquidity = min(25.0, volume ** 0.25 / 2.0) if volume else 0.0
            return float(priority or 0) + min(30.0, change * 4.0) + liquidity
    def run_live_mode_scan(self, mode: str) -> Dict[str, Any]:
            try:
                canonical = require_production_mode(mode)
            except UnsupportedProductionMode as exc:
                return {"ok": False, "error": "unsupported_production_mode", "message": str(exc), "allowed_modes": ["intraday", "delivery"]}
            lane = "intraday_analysis" if canonical == "intraday" else "delivery_analysis"
            return self.lanes.execute(lane, lambda: self._run_live_mode_scan_impl(canonical))
    def _run_live_mode_scan_impl(self, mode: str) -> Dict[str, Any]:
            try:
                mode = require_production_mode(mode)
            except UnsupportedProductionMode as exc:
                return {"ok": False, "error": "unsupported_production_mode", "message": str(exc), "allowed_modes": ["intraday", "delivery"]}
            if mode != "intraday":
                return {"ok": False, "error": "wrong_scanner_lane", "message": "Delivery must use the deep scanner", "allowed_mode": "intraday"}
            st = self._mode_status("intraday")
            analysis_st = self._lane_status("intraday", "analysis")
            promotion_st = self._lane_status("intraday", "promotion")
            if not self.host.client.token_status().get("ok"):
                with self.host.lock:
                    st.update({"state": "waiting_token", "last_run": now_iso(), "next_run": "after token"})
                    analysis_st.update({"state": "waiting_token", "last_run": now_iso(), "next_run": "after token"})
                return {"ok": False, "error": "waiting_token"}
            if not is_india_market_open():
                last = self._last_completed_cycle(analysis_st)
                with self.host.lock:
                    analysis_st.update({
                        "state": "market_closed", "cycle_scanned": 0, "cycle_promoted": 0,
                        "cycle_rejected": 0, "cycle_blocked": 0, "last_run": now_iso(),
                        "next_run": "09:15 IST", "message": f"{mode} scanner paused outside NSE hours",
                    })
                    promotion_st.update({"state": "market_closed", "cycle_promoted": 0, "last_run": now_iso()})
                    # Compatibility fields retain the latest completed market-cycle
                    # counters instead of falsely erasing them to zero.
                    st.update({
                        "state": "market_closed",
                        "scanned": int(last.get("scanned") or st.get("scanned") or 0),
                        "promoted": int(last.get("promoted") or st.get("promoted") or 0),
                        "rejected": int(last.get("rejected") or st.get("rejected") or 0),
                        "blocked": int(last.get("blocked") or st.get("blocked") or 0),
                        "current_cycle_scanned": 0,
                        "current_cycle_promoted": 0,
                        "last_run": now_iso(), "next_run": "09:15 IST",
                        "message": f"{mode} scanner paused outside NSE hours; showing last completed market cycle",
                    })
                self._publish_scanner_progress("intraday", market_open=False)
                return {"ok": True, "market_open": False}
            if time.time() < self.host.quote_blocked_until:
                wait_s = int(max(1, self.host.quote_blocked_until - time.time()))
                with self.host.lock:
                    st.update({"state": "quote_rate_limited", "last_run": now_iso(), "next_run": f"after {wait_s}s"})
                    analysis_st.update({"state": "quote_rate_limited", "last_run": now_iso(), "next_run": f"after {wait_s}s"})
                return {"ok": False, "error": "quote_rate_limited"}
            cap = INTRADAY_QUOTE_BATCH
            priorities = self._live_priorities_for_mode(mode, cap)
            cycle_started = time.monotonic()
            deadline = cycle_started + max(8.0, float(INTRADAY_SCAN_BUDGET_SEC))
            with self.host.lock:
                st.update({"state": "running", "last_run": now_iso(), "universe": len(priorities),
                           "current_stage": "resolve", "cycle_started_at": now_iso(),
                           "scan_budget_sec": INTRADAY_SCAN_BUDGET_SEC, "production_policy_version": POLICY_VERSION})
                analysis_st.update({
                    "state": "running", "last_run": now_iso(), "next_run": None,
                    "candidate_universe": len(priorities), "current_stage": "resolve",
                    "cycle_started_at": now_iso(), "cycle_scanned": 0, "cycle_promoted": 0,
                    "cycle_rejected": 0, "cycle_blocked": 0,
                    "scan_budget_sec": INTRADAY_SCAN_BUDGET_SEC,
                })
                promotion_st.update({"state": "evaluating", "cycle_promoted": 0, "last_run": now_iso()})
            resolved = []
            for p in priorities:
                if deadline is not None and time.monotonic() >= deadline:
                    break
                try:
                    if p.get("instrument_key"):
                        resolved.append({"priority": p, "inst": {"instrument_key": p["instrument_key"], "trading_symbol": p["symbol"], "symbol": p["symbol"], "exchange": p.get("exchange") or "NSE", "instrument_type": p.get("instrument_type") or "EQ"}})
                        continue
                    matches = self.host.client.search_instruments(p["symbol"], limit=1)
                    if matches:
                        resolved.append({"priority": p, "inst": matches[0]})
                except Exception as exc:
                    self.host.event("WARN", f"{mode}_scanner", "Instrument resolve failed", {"symbol": p.get("symbol"), "error": str(exc)[:120]})
            with self.host.lock:
                st["current_stage"] = "quotes"
                analysis_st["current_stage"] = "quotes"
            quote_by_key = {}
            # Reuse the canonical stream / coverage-lane observations first.
            # REST is a bounded gap-fill, not the primary scanner quote source.
            symbols = [str(r["inst"].get("trading_symbol") or r["priority"].get("symbol") or "").upper() for r in resolved]
            try:
                for qd in self.host.runtime_market_state.latest_quotes(symbols) or []:
                    if qd.get("instrument_key") and qd.get("ltp") is not None:
                        quote_by_key[qd["instrument_key"]] = dict(qd)
            except Exception:
                pass
            try:
                coverage = getattr(self.host, "_coverage_quote_cache", {}) or {}
                for r in resolved:
                    inst = r["inst"]
                    symbol = str(inst.get("trading_symbol") or r["priority"].get("symbol") or "").upper()
                    qd = coverage.get(symbol) or coverage.get(symbolKey_py(symbol))
                    if isinstance(qd, dict) and qd.get("ltp") is not None:
                        row = dict(qd)
                        row.setdefault("instrument_key", inst.get("instrument_key"))
                        quote_by_key[row.get("instrument_key")] = row
            except Exception:
                pass
            missing = [r for r in resolved if r["inst"].get("instrument_key") not in quote_by_key]
            qs = []
            try:
                qs = self.host.client.quotes([r["inst"] for r in missing], persist=False) if missing else []
                for qd in qs:
                    if qd.get("instrument_key"):
                        quote_by_key[qd["instrument_key"]] = qd
                if quote_by_key:
                    try:
                        self.host.runtime_market_state.save_latest_quotes(quote_by_key.values())
                    except Exception as exc:
                        self.host.record_error(f"{mode}_runtime_quote_persist", str(exc))
                    with self.host.lock:
                        self.host.status["last_price_refresh"] = now_iso()
            except Exception as exc:
                self.host.record_error(f"{mode}_scanner", str(exc), "/v3/market-quote/ltp")
            # R20: retained daily history now participates in *scheduling* before
            # the scarce deep-analysis gate. Refresh is asynchronous/local-only;
            # missing history never removes a symbol and never grants conviction.
            try:
                self.host.historical_alpha_scheduling.refresh_async([r["inst"] for r in resolved])
            except Exception as exc:
                self.host.record_error(f"{mode}_scanner", f"historical_scheduling_refresh: {str(exc)[:120]}")
            def quote_rank(row: Dict[str, Any]) -> float:
                quote = quote_by_key.get(row["inst"].get("instrument_key")) or {}
                priority = float(row["priority"].get("priority_score") or 0)
                change = abs(float(quote.get("change_pct") or quote.get("pChange") or 0))
                volume = max(0.0, float(quote.get("volume") or 0))
                symbol = str(row["inst"].get("trading_symbol") or row["priority"].get("symbol") or "").upper()
                try:
                    historical = self.host.historical_alpha_scheduling.score_for(symbol, mode)
                    history_bonus = max(0.0, min(24.0, (float(historical.get("score") or 50.0) - 50.0) * 0.48))
                except Exception:
                    history_bonus = 0.0
                return priority + min(25.0, change * 4.0) + min(20.0, volume ** 0.25 / 2.0) + history_bonus
            deep_resolved = sorted(resolved, key=quote_rank, reverse=True)
            if mode == "intraday":
                deep_resolved = deep_resolved[:INTRADAY_DEEP_ANALYSIS]
            # Market breadth rolls up quotes already fetched. Throttled to once
            # per ~30s network-wide since every mode scanner passes through
            # here and would otherwise recompute the same thing repeatedly.
            try:
                if quote_by_key and (time.time() - getattr(self.host, "_last_breadth_ts", 0.0)) > 30:
                    self.host.reference_data.compute_market_breadth(quote_by_key, universe="NIFTY250_CORE")
                    self.host._last_breadth_ts = time.time()
            except Exception as exc:
                self.host.record_error("market_breadth", str(exc)[:160])
            scanned = promoted = rejected = blocked = 0
            quant_cycle_rows: List[Dict[str, Any]] = []
            blocked_members: List[Dict[str, Any]] = []
            try:
                self.host.research_adapter.refresh_cross_sectional(
                    {r["inst"].get("trading_symbol", "").upper(): r["inst"]["instrument_key"] for r in resolved if r["inst"].get("instrument_key")}
                )
            except Exception as exc:
                self.host.record_error(f"{mode}_scanner", f"cross_sectional_refresh: {str(exc)[:120]}")
            scan_funnel = IntradayScanFunnel()
            with self.host.lock:
                st["current_stage"] = "analysis"
                analysis_st["current_stage"] = "analysis"
            budget_exhausted = False
            quote_ready_rows = [
                r for r in deep_resolved
                if quote_by_key.get(r["inst"].get("instrument_key")) is not None
            ]
            blocked += max(0, len(deep_resolved) - len(quote_ready_rows))
            quote_ready_keys = {str(item["inst"].get("instrument_key") or "") for item in quote_ready_rows}
            blocked_members.extend(self._scanner_stage_member(item, "DATA_PENDING", "QUOTE_UNAVAILABLE") for item in deep_resolved if str(item["inst"].get("instrument_key") or "") not in quote_ready_keys)
            remaining = max(0.25, deadline - time.monotonic()) if deadline is not None else float(INTRADAY_SCAN_BUDGET_SEC)
            # Architecture rebaseline: scanner workers receive a complete local
            # candle snapshot.  They never perform provider/history I/O.  A stock
            # without the required local data is DATA_PENDING and its exact gap is
            # scheduled outside the executor instead of consuming a worker.
            analysis_ready_rows = []
            data_pending_rows = []
            for row_index, r in enumerate(quote_ready_rows):
                if deadline is not None and time.monotonic() >= deadline:
                    for deferred_row in quote_ready_rows[row_index:]:
                        pending = {"ready": False, "state": "DATA_PENDING", "reason": "SCAN_BUDGET_DEFERRED"}
                        data_pending_rows.append((deferred_row, pending))
                        blocked_members.append(self._scanner_stage_member(deferred_row, "DATA_PENDING", "SCAN_BUDGET_DEFERRED"))
                    break
                snapshot = self.host.market_data.scanner_analysis_snapshot(r["inst"], mode, schedule_refresh=True)
                if snapshot.get("ready"):
                    try:
                        prepared = self.host.scanner_prepare_analysis(
                            r["inst"], quote_by_key.get(r["inst"].get("instrument_key")), mode,
                            list(snapshot.get("candles") or []),
                        )
                        analysis_ready_rows.append((r, snapshot, prepared))
                    except Exception as exc:
                        data_pending_rows.append((r, dict(snapshot, reason="LOCAL_CONTEXT_PREPARATION_FAILED", error=str(exc)[:180])))
                        blocked_members.append(self._scanner_stage_member(r, "DATA_PENDING", "LOCAL_CONTEXT_PREPARATION_FAILED"))
                        self.host.record_error(f"{mode}_scanner_prepare", str(exc))
                else:
                    data_pending_rows.append((r, snapshot))
                    blocked_members.append(self._scanner_stage_member(r, "DATA_PENDING", str(snapshot.get("reason") or "LOCAL_HISTORY_PENDING")))
            blocked += len(data_pending_rows)
            jobs = [
                (r["inst"], quote_by_key.get(r["inst"].get("instrument_key")), {
                    "candles_override": list(snapshot.get("candles") or []),
                    "prepared_analysis": prepared,
                })
                for r, snapshot, prepared in analysis_ready_rows
            ]
            recovery = self.analysis_executor.recover_stale_generation(mode, stale_after_sec=60.0)
            outcomes = self.analysis_executor.run_many(
                jobs,
                mode,
                min(8.0, remaining),
                batch_budget_sec=remaining,
            ) if jobs else []
            analysis_timeouts = analysis_capacity = 0
            for (r, snapshot, _prepared), (result, analysis_state) in zip(analysis_ready_rows, outcomes):
                try:
                    with self.host.lock:
                        st.update({"current_symbol": str(r["priority"].get("symbol") or ""),
                                   "cycle_analyzed": scanned, "cycle_blocked": blocked})
                        analysis_st.update({
                            "current_symbol": str(r["priority"].get("symbol") or ""),
                            "cycle_scanned": scanned, "cycle_promoted": promoted,
                            "cycle_rejected": rejected, "cycle_blocked": blocked,
                        })
                    if analysis_state != "ok":
                        blocked += 1
                        blocked_members.append(self._scanner_stage_member(r, "BLOCKED", analysis_state.upper()))
                        if analysis_state == "analysis_timeout":
                            analysis_timeouts += 1
                        elif analysis_state in {"analysis_capacity", "analysis_budget_exhausted"}:
                            analysis_capacity += 1
                        if analysis_state == "analysis_error" and isinstance(result, Exception):
                            self.host.record_error(f"{mode}_scanner", str(result))
                        elif analysis_state not in {"analysis_capacity", "analysis_budget_exhausted", "analysis_timeout"}:
                            self.host.record_error(f"{mode}_scanner", analysis_state)
                        continue
                    d = self.host.finalize_scanner_analysis(
                        result, r["inst"], mode, list(snapshot.get("candles") or [])
                    )
                    scanned += 1
                    if d:
                        scan_funnel.observe(d)
                        d["scanner_engine"] = mode
                        self.host.store.save_decision(d)
                        quant_cycle_rows.append(self._research_capture_row(d, r["inst"], mode, quote_by_key.get(r["inst"].get("instrument_key"))))
                        if d.get("status") == "PROMOTED":
                            promoted += 1
                        else:
                            rejected += 1
                    else:
                        rejected += 1
                        blocked_members.append(self._scanner_stage_member(inst, "MATHEMATICALLY_REJECTED", "ANALYSIS_RETURNED_NO_QUALIFIED_DECISION"))
                except Exception as exc:
                    rejected += 1
                    self.host.record_error(f"{mode}_scanner", str(exc))
            budget_exhausted = any(state == "analysis_budget_exhausted" for _result, state in outcomes)
            funnel_counts, top_blockers = scan_funnel.report()
            completed_at = now_iso()
            intraday_stage_members = {
                "universe": [self._scanner_stage_member(item, "UNIVERSE") for item in priorities[:250]],
                "attempted": [self._scanner_stage_member(item, "ATTEMPTED") for item in resolved[:250]],
                "quote_ready": [self._scanner_stage_member(item, "QUOTE_READY") for item in quote_ready_rows[:250]],
                "shortlisted": [self._scanner_stage_member(item, "SHORTLISTED") for item in deep_resolved[:250]],
                "analysed": [self._scanner_stage_member(item, "ANALYSED", str(item.get("status") or "")) for item in quant_cycle_rows[:250]],
                "blocked": blocked_members[:250],
                "data_pending": [item for item in blocked_members if str(item.get("state") or "").upper() == "DATA_PENDING"][:250],
                "data_blocked": [item for item in blocked_members if str(item.get("state") or "").upper() == "BLOCKED"][:250],
                "capacity_deferred": [item for item in blocked_members if str(item.get("state") or "").upper() == "CAPACITY_DEFERRED"][:250],
                "mathematically_rejected": [item for item in blocked_members if str(item.get("state") or "").upper() == "MATHEMATICALLY_REJECTED"][:250],
                "map": [self._scanner_stage_member(item, "MAP", str((item.get("trade_map") or {}).get("state") or "")) for item in quant_cycle_rows if str((item.get("trade_map") or {}).get("state") or "").upper() in {"FINAL", "RESEARCH"}][:250],
                "rr": [self._scanner_stage_member(item, "R:R") for item in quant_cycle_rows if float((item.get("trade_map") or {}).get("room_rr") or item.get("reward_risk") or 0) > 0][:250],
                "final": [self._scanner_stage_member(item, "FINAL") for item in quant_cycle_rows if str(item.get("status") or "").upper() == "PROMOTED"][:250],
            }
            completed_cycle = {
                "scanned": scanned, "promoted": promoted, "rejected": rejected, "blocked": blocked,
                "quote_returned": len(quote_by_key), "quote_ready": len(quote_ready_rows),
                "analysis_ready": len(analysis_ready_rows), "data_pending": len(data_pending_rows),
                "candidate_universe": len(priorities), "analysis_timeouts": analysis_timeouts,
                "analysis_capacity": analysis_capacity, "analysis_workers": self.analysis_executor.capacity(mode),
                "analysis_recovery": recovery, "stage_members": intraday_stage_members, "budget_exhausted": budget_exhausted,
                "resolution_summary": {
                    "shortlisted": len(deep_resolved),
                    "analysed": scanned,
                    "data_pending": sum(1 for item in blocked_members if str(item.get("state") or "").upper() == "DATA_PENDING"),
                    "data_blocked": sum(1 for item in blocked_members if str(item.get("state") or "").upper() == "BLOCKED"),
                    "capacity_deferred": sum(1 for item in blocked_members if str(item.get("state") or "").upper() == "CAPACITY_DEFERRED"),
                    "mathematically_rejected": sum(1 for item in blocked_members if str(item.get("state") or "").upper() == "MATHEMATICALLY_REJECTED"),
                    "unresolved": max(0, len(deep_resolved) - scanned - sum(1 for item in blocked_members if str(item.get("state") or "").upper() in {"BLOCKED", "DATA_PENDING", "CAPACITY_DEFERRED", "MATHEMATICALLY_REJECTED"})),
                },
                "elapsed_sec": round(time.monotonic() - cycle_started, 2),
                "completed_at": completed_at,
            }
            with self.host.lock:
                st.update({"state": "waiting_next_cycle", "scanned": scanned, "promoted": promoted, "rejected": rejected, "blocked": blocked,
                           "quote_scanned": len(quote_by_key), "deep_scanned": scanned,
                           "selection_funnel": funnel_counts, "top_blockers": top_blockers,
                           "last_run": now_iso(), "next_run": f"{MODE_REFRESH_SECONDS.get(mode, 10)}s", "stale_guard": "enabled", "level_validator": "enabled",
                           "desk": "Intraday", "strategy_profiles": ["ORB", "VWAP_TREND", "MOMENTUM"],
                           "priority_lane": INTRADAY_PRIORITY_LANE, "coverage_lane": INTRADAY_COVERAGE_LANE,
                           "quote_batch": INTRADAY_QUOTE_BATCH, "deep_analysis_limit": INTRADAY_DEEP_ANALYSIS,
                           "budget_exhausted": budget_exhausted, "cycle_elapsed_sec": round(time.monotonic() - cycle_started, 2),
                           "current_stage": "idle", "current_symbol": None, "cycle_analyzed": scanned,
                           "stage_members": intraday_stage_members,
                           "current_cycle_scanned": scanned, "current_cycle_promoted": promoted,
                           "last_completed": completed_cycle})
                analysis_st.update({
                    "state": "waiting_next_cycle", "cycle_scanned": scanned, "cycle_promoted": promoted,
                    "cycle_rejected": rejected, "cycle_blocked": blocked,
                    "quote_returned": len(quote_by_key), "selection_funnel": funnel_counts,
                    "top_blockers": top_blockers, "last_run": completed_at,
                    "next_run": f"{MODE_REFRESH_SECONDS.get(mode, 10)}s",
                    "budget_exhausted": budget_exhausted,
                    "cycle_elapsed_sec": round(time.monotonic() - cycle_started, 2),
                    "current_stage": "idle", "current_symbol": None,
                    "stage_members": intraday_stage_members,
                    "last_completed": completed_cycle,
                })
                promotion_st.update({
                    "state": "waiting_next_cycle", "cycle_promoted": promoted, "last_run": completed_at,
                    "last_completed": {"promoted": promoted, "completed_at": completed_at},
                })
            with self.host.lock:
                self.host.status["last_ai_validation"] = now_iso() if scanned else self.host.status.get("last_ai_validation")
            self._publish_scanner_progress("intraday", market_open=True)
            # Compatibility headline mirrors the one canonical same-day scanner only.
            self.host.status.setdefault("fast_lane", {}).update({
                "state": "intraday_only", "scanned": scanned, "promoted": promoted, "rejected": rejected,
                "last_run": now_iso(), "next_run": "intraday cadence", "coverage": {"intraday": scanned},
                "production_policy_version": POLICY_VERSION,
            })
            quant_prediction = record_quant_scan_cycle(self.host, quant_cycle_rows, mode, completed_at, len(priorities))
            with self.host.lock:
                st["research_capture"] = quant_prediction
                analysis_st["research_capture"] = quant_prediction
            self._publish_scanner_progress(mode, market_open=True)
            cycle_evidence = self._record_scanner_cycle_evidence(
                "intraday", "market_hours_analysis",
                dict(completed_cycle, market_open=True, quant_prediction_state=(quant_prediction or {}).get("state")),
            )
            return {
                "ok": True, "mode": mode, "scanned": scanned, "promoted": promoted,
                "rejected": rejected, "blocked": blocked, "quant_prediction": quant_prediction,
                "cycle_evidence": cycle_evidence,
            }
    def run_deep_mode_scan(self, mode: str) -> Dict[str, Any]:
            try:
                canonical = require_production_mode(mode)
            except UnsupportedProductionMode as exc:
                return {"ok": False, "error": "unsupported_production_mode", "message": str(exc), "allowed_modes": ["intraday", "delivery"]}
            lane = "delivery_analysis" if canonical == "delivery" else "intraday_analysis"
            return self.lanes.execute(lane, lambda: self._run_deep_mode_scan_impl(canonical))
    def _run_deep_mode_scan_impl(self, mode: str) -> Dict[str, Any]:
            try:
                mode = require_production_mode(mode)
            except UnsupportedProductionMode as exc:
                return {"ok": False, "error": "unsupported_production_mode", "message": str(exc), "allowed_modes": ["intraday", "delivery"]}
            if mode != "delivery":
                return {"ok": False, "error": "wrong_scanner_lane", "message": "Intraday must use the live scanner", "allowed_mode": "delivery"}
            # Interactive Stock Report priority may defer expensive Delivery
            # mathematics/model work, but it must never starve the cheap immutable-
            # universe sweep.  v130 returned here before advancing the cursor, so a
            # continuously selected TCS/Stock Report could pin Delivery at one
            # checkpoint forever.  v131 records the P1 pressure and continues the
            # quote/eligibility sweep; deep-analysis admission is set to zero later.
            governor = getattr(self.host, "workload_governor", None)
            defer_deep_analysis = False
            yield_reason = ""
            if governor is not None:
                defer_deep_analysis, yield_reason = governor.should_yield("P3")
            st = self._mode_status("delivery")
            analysis_st = self._lane_status("delivery", "analysis")
            promotion_st = self._lane_status("delivery", "promotion")
            if not self.host.client.token_status().get("ok"):
                with self.host.lock:
                    st.update({"state": "waiting_token", "last_run": now_iso(), "next_run": "after token"})
                    analysis_st.update({"state": "waiting_token", "last_run": now_iso(), "next_run": "after token"})
                return {"ok": False, "error": "waiting_token"}
            historical_network_blocked = time.time() < self.host.market_data.hist_blocked_until
            if historical_network_blocked and not is_india_market_open():
                with self.host.lock:
                    st.update({"state": "historical_api_blocked", "last_run": now_iso(), "next_run": "after auth-test"})
                    analysis_st.update({"state": "historical_api_blocked", "last_run": now_iso(), "next_run": "after auth-test"})
                return {"ok": False, "error": "historical_rate_limited"}
            if historical_network_blocked:
                # During market hours the stream/cache-first scanner must keep
                # progressing. Missing history becomes symbol-level evidence;
                # a provider cooldown cannot pause the entire desk.
                with self.host.lock:
                    analysis_st.update({"historical_network_state": "cooldown_cache_first"})
            try:
                meta = self.host.client._ensure_instruments_nonblocking()
                if not self._focused_universe_ready():
                    with self.host.lock:
                        st.update({"state": "waiting_focused_universe", "last_run": now_iso(), "next_run": "after focused NSE+BSE catalogue"})
                        analysis_st.update({"state": "waiting_focused_universe", "last_run": now_iso(), "next_run": "after focused NSE+BSE catalogue"})
                    return {"ok": False, "error": "waiting_focused_universe", "instrument_meta": meta}
                self._ensure_checkpoint_reconciled("delivery")
                persisted_cursor = int(analysis_st.get("cursor") or st.get("cursor") or 0)
                # Delivery scans the complete immutable supported canonical desk
                # population.  Historical liquidity is ranking/scheduling evidence
                # only; it must not silently remove a stock from coverage.  Cheap
                # quote screening advances the full sweep while a bounded ranked
                # subset receives expensive mathematics/features/model analysis.
                universe = self.host.immutable_scan_population(mode)
                if not universe:
                    with self.host.lock:
                        st.update({"state": "waiting_universe", "last_run": now_iso(), "next_run": "after instruments"})
                        analysis_st.update({"state": "waiting_universe", "last_run": now_iso(), "next_run": "after instruments"})
                    return {"ok": True, "scanned": 0}
                universe_size = len(universe)
                sweep_number = max(1, int(analysis_st.get("sweep_number") or 1))
                if bool(analysis_st.get("sweep_complete")):
                    sweep_number += 1
                    cursor = 0
                    sweep_before = 0
                    prior_sweep_stage_counts = {}
                else:
                    cursor = max(0, min(universe_size - 1, persisted_cursor))
                    sweep_before = int(analysis_st.get("sweep_scanned") or cursor or 0)
                    prior_sweep_stage_counts = dict(analysis_st.get("sweep_stage_counts") or {})
                previous_cycle = dict(analysis_st.get("last_completed") or {})
                previous_elapsed = float(previous_cycle.get("elapsed_sec") or 0.0)
                if is_india_market_open():
                    # Wide cheap-screen batches keep the immutable sweep moving;
                    # only a ranked subset enters bounded parallel deep analysis.
                    desired_batch = 96 if previous_elapsed <= 40.0 else 64
                else:
                    # Commit smaller cache-first micro-batches. Long 120/160
                    # symbol batches hid all durable progress for longer than
                    # the operational proof window and made a live scanner look
                    # stalled even with healthy heartbeats.
                    desired_batch = 80 if previous_elapsed <= 45.0 else 64 if previous_elapsed <= 75.0 else 40
                batch_cap = min(desired_batch, universe_size)
                base_count = min(batch_cap, max(0, universe_size - cursor))
                base_batch = list(universe[cursor:cursor + base_count])
                batch = list(base_batch)
                priority_count = 0
                # v44.6: symbols the user just hit "Refresh Stock" on jump the queue --
                # they get scanned this cycle instead of waiting for the cursor to cycle
                # back around the ~2000-symbol universe.
                try:
                    pq = self.host.store.get_kv("scan_priority_queue", []) or []
                    if pq:
                        by_sym = {str(i.get("trading_symbol") or i.get("symbol") or "").upper(): i for i in universe}
                        priority_insts = [by_sym[s] for s in pq if s in by_sym]
                        if priority_insts:
                            seen_keys = {i.get("instrument_key") for i in priority_insts}
                            batch = priority_insts + [i for i in batch if i.get("instrument_key") not in seen_keys]
                            priority_count = len(priority_insts)
                        self.host.store.set_kv("scan_priority_queue", [])
                except Exception:
                    pass
                cycle_started = time.monotonic()
                with self.host.lock:
                    st.update({"state": "running", "last_run": now_iso(), "cursor": cursor, "batch": len(batch), "stale_guard": "enabled", "level_validator": "enabled"})
                    analysis_st.update({
                        "state": "running", "last_run": now_iso(), "cursor": cursor,
                        "universe_size": universe_size, "batch": len(batch),
                        "base_batch": base_count, "priority_insertions": priority_count,
                        "cycle_attempted": 0, "cycle_scanned": 0, "cycle_promoted": 0, "cycle_rejected": 0,
                        "cycle_data_missing": 0, "sweep_number": sweep_number, "sweep_complete": False,
                        "current_sweep_scanned": sweep_before,
                        "last_completed_sweep": analysis_st.get("last_completed_sweep"),
                        "deep_analysis_deferred": bool(defer_deep_analysis),
                        "deep_analysis_deferred_reason": yield_reason if defer_deep_analysis else None,
                    })
                    promotion_st.update({"state": "evaluating", "cycle_promoted": 0, "last_run": now_iso()})
                # Publish the new sweep immediately; the card must not retain the
                # prior COMPLETE state until the fifth symbol is processed.
                self._publish_scanner_progress("delivery")
                attempted = len(batch)
                scanned = promoted = rejected = prepared_candidates = data_missing = 0
                analysis_timeouts = analysis_capacity = analysis_errors = 0
                quant_cycle_rows: List[Dict[str, Any]] = []
                blocked_members: List[Dict[str, Any]] = []
                valid_batch: List[Dict[str, Any]] = []
                for inst in batch:
                    key = str(inst.get("instrument_key") or "")
                    if not key or self.host._is_bad_key(key):
                        rejected += 1
                        data_missing += 1
                        blocked_members.append(self._scanner_stage_member(inst, "BLOCKED", "INVALID_OR_QUARANTINED_INSTRUMENT_KEY"))
                        continue
                    valid_batch.append(inst)
                    try:
                        self.host._schedule_fundamental_prefetch(inst)
                    except Exception as exc:
                        self.host.record_error(f"{mode}_fundamental_prefetch", str(exc)[:160])
                with self.host.lock:
                    analysis_st.update({
                        "current_stage": "quote_screen",
                        "cycle_attempted": attempted,
                        "cycle_data_missing": data_missing,
                        "cycle_rejected": rejected,
                        # Attempting a batch is not committed sweep progress.  The
                        # cursor/sweep counters advance only after the full cycle
                        # finishes and its exact-snapshot checkpoint is persisted.
                        # This prevents an exception mid-cycle from displaying
                        # 320/1222 while restart authority is still at 160/1222.
                        "current_sweep_scanned": sweep_before,
                        "cycle_base_attempted": base_count,
                        "last_progress_at": analysis_st.get("last_progress_at") or analysis_st.get("last_run"),
                    })
                self._publish_scanner_progress("delivery")
                quote_started = time.monotonic()
                quote_by_key = self._scanner_quotes_for_instruments(
                    valid_batch,
                    allow_rest=True,
                )
                quote_elapsed = max(0.0, time.monotonic() - quote_started)
                quote_ready = [
                    inst for inst in valid_batch
                    if quote_by_key.get(str(inst.get("instrument_key") or "")) is not None
                ]
                quote_missing = max(0, len(valid_batch) - len(quote_ready))
                data_missing += quote_missing
                rejected += quote_missing
                if quote_missing:
                    quote_keys = {str(item.get("instrument_key") or "") for item in quote_ready}
                    blocked_members.extend(self._scanner_stage_member(item, "DATA_PENDING", "QUOTE_UNAVAILABLE") for item in valid_batch if str(item.get("instrument_key") or "") not in quote_keys)
                # The cheap quote sweep may cover a wide population, but deep
                # analysis must honour the actual bounded worker capacity.  The
                # previous 64/112 shortlist could be submitted while every
                # worker was still occupied, producing an apparent permanent
                # stall and hundreds of misleading capacity errors.  Admit a
                # small number of waves only; deferred ranked symbols remain
                # eligible in the next canonical sweep.
                market_open = is_india_market_open()
                recovery = self.analysis_executor.recover_stale_generation(mode, stale_after_sec=75.0 if market_open else 120.0)
                worker_truth = self.analysis_executor.capacity(mode)
                worker_truth["last_recovery"] = recovery
                available_workers = int(worker_truth.get("available") or 0)
                worker_count = max(1, int(worker_truth.get("workers") or 1))
                max_waves = 2 if market_open else 3
                deep_limit = min(32 if market_open else 40, worker_count * max_waves)
                delivery_liquidity_rank = getattr(self.host, "_delivery_liquidity_rank_by_symbol", {}) or {}
                delivery_priority_symbols = getattr(self.host, "_delivery_priority_symbols", set()) or set()
                try:
                    self.host.historical_alpha_scheduling.refresh_async(valid_batch)
                except Exception as exc:
                    self.host.record_error(f"{mode}_scanner", f"historical_scheduling_refresh: {str(exc)[:120]}")
                def delivery_quote_rank(inst: Dict[str, Any]) -> float:
                    symbol = str(inst.get("trading_symbol") or inst.get("symbol") or "").upper().strip()
                    rank = int(delivery_liquidity_rank.get(symbol) or 0)
                    # Trailing liquidity remains a bounded scheduling bonus only.
                    # A stock without a historical rank is still screened and can
                    # win priority from current movement/volume or explicit user
                    # priority; it is never excluded from the Delivery universe.
                    liquidity_bonus = max(0.0, 24.0 * (1.0 - min(rank, 1500) / 1500.0)) if rank else 0.0
                    explicit_priority = 100.0 if inst in batch[:priority_count] or symbol in delivery_priority_symbols else 0.0
                    try:
                        historical = self.host.historical_alpha_scheduling.score_for(symbol, mode)
                        history_bonus = max(0.0, min(30.0, (float(historical.get("score") or 50.0) - 50.0) * 0.60))
                    except Exception:
                        history_bonus = 0.0
                    return self._quote_rank(
                        inst,
                        quote_by_key.get(str(inst.get("instrument_key") or "")),
                        explicit_priority + liquidity_bonus + history_bonus,
                    )
                ranked = sorted(quote_ready, key=delivery_quote_rank, reverse=True)
                admitted_limit = 0 if defer_deep_analysis else (min(deep_limit, len(ranked)) if available_workers > 0 else 0)
                shortlist = ranked[:admitted_limit]
                deferred_ranked = ranked[admitted_limit:]
                capacity_deferred = max(0, len(deferred_ranked))
                if deferred_ranked:
                    blocked_members.extend(
                        self._scanner_stage_member(item, "CAPACITY_DEFERRED", "WORKER_CAPACITY_DEFERRED_TO_NEXT_SWEEP")
                        for item in deferred_ranked[:250]
                    )
                # A deferred deep analysis is not a mathematical rejection. It
                # is reported separately and must never inflate rejected/error
                # counts or poison the scanner health state.
                cheap_rejected = 0
                try:
                    self.host.research_adapter.refresh_cross_sectional(
                        {
                            str(i.get("trading_symbol") or i.get("symbol") or "").upper(): i["instrument_key"]
                            for i in valid_batch if i.get("instrument_key")
                        }
                    )
                except Exception as exc:
                    self.host.record_error(f"{mode}_scanner", f"cross_sectional_refresh: {str(exc)[:120]}")
                per_symbol_timeout = 10.0 if market_open else 18.0
                batch_budget = 44.0 if market_open else 70.0
                analysis_ready = []
                data_pending_analysis = []
                for inst in shortlist:
                    snapshot = self.host.market_data.scanner_analysis_snapshot(inst, mode, schedule_refresh=True)
                    if snapshot.get("ready"):
                        try:
                            prepared_context = self.host.scanner_prepare_analysis(
                                inst, quote_by_key.get(str(inst.get("instrument_key") or "")), mode,
                                list(snapshot.get("candles") or []),
                            )
                            analysis_ready.append((inst, snapshot, prepared_context))
                        except Exception as exc:
                            data_pending_analysis.append((inst, dict(snapshot, reason="LOCAL_CONTEXT_PREPARATION_FAILED", error=str(exc)[:180])))
                            blocked_members.append(self._scanner_stage_member(inst, "DATA_PENDING", "LOCAL_CONTEXT_PREPARATION_FAILED"))
                            self.host.record_error(f"{mode}_scanner_prepare", str(exc))
                    else:
                        data_pending_analysis.append((inst, snapshot))
                        blocked_members.append(self._scanner_stage_member(inst, "DATA_PENDING", str(snapshot.get("reason") or "LOCAL_HISTORY_PENDING")))
                data_missing += len(data_pending_analysis)
                jobs = [
                    (
                        inst,
                        quote_by_key.get(str(inst.get("instrument_key") or "")),
                        {
                            "candles_override": list(snapshot.get("candles") or []),
                            "prepared_analysis": prepared,
                        },
                    )
                    for inst, snapshot, prepared in analysis_ready
                ]
                with self.host.lock:
                    analysis_st.update({
                        "current_stage": "parallel_analysis",
                        "quote_ready": len(quote_ready),
                        "shortlisted": len(shortlist),
                        "analysis_ready": len(analysis_ready),
                        "data_pending": len(data_pending_analysis),
                        "capacity_deferred": capacity_deferred,
                        "cheap_rejected": cheap_rejected,
                        "analysis_workers": worker_truth,
                        "deep_analysis_deferred": bool(defer_deep_analysis),
                        "deep_analysis_deferred_reason": yield_reason if defer_deep_analysis else None,
                        "state": "coverage_advancing_deep_deferred" if defer_deep_analysis else "waiting_for_local_data" if data_pending_analysis and not analysis_ready else "waiting_for_workers" if not shortlist and ranked else "running",
                    })
                deep_started = time.monotonic()
                outcomes = self.analysis_executor.run_many(
                    jobs,
                    mode,
                    per_symbol_timeout,
                    batch_budget_sec=batch_budget,
                ) if jobs else []
                deep_elapsed = max(0.0, time.monotonic() - deep_started)
                for (inst, snapshot, _prepared), (result, analysis_state) in zip(analysis_ready, outcomes):
                    key = str(inst.get("instrument_key") or "")
                    if analysis_state != "ok":
                        rejected += 1
                        blocked_members.append(self._scanner_stage_member(inst, "BLOCKED", analysis_state.upper()))
                        if analysis_state == "analysis_timeout":
                            analysis_timeouts += 1
                        elif analysis_state in {"analysis_capacity", "analysis_budget_exhausted"}:
                            analysis_capacity += 1
                        else:
                            analysis_errors += 1
                        if analysis_state == "analysis_error" and isinstance(result, Exception):
                            err = str(result)
                            if "bad parameter" in err.lower() or "400" in err:
                                self.host._bad_historical_keys[key] = time.time() + self.host._BAD_KEY_TTL
                                self.host.event("WARN", f"{mode}_scanner", "Historical scan skipped malformed instrument", {"symbol": inst.get("trading_symbol"), "error": err[:160]})
                            else:
                                self.host.record_error(f"{mode}_scanner", err, "/v3/historical-candle")
                        elif analysis_state not in {"analysis_capacity", "analysis_budget_exhausted", "analysis_timeout"}:
                            self.host.record_error(f"{mode}_scanner", analysis_state)
                        continue
                    d = self.host.finalize_scanner_analysis(
                        result, inst, mode, list(snapshot.get("candles") or [])
                    )
                    scanned += 1
                    if d:
                        d = apply_research_only_price_boundary(
                            d, quote_by_key.get(str(inst.get("instrument_key") or ""))
                        )
                        d["scanner_engine"] = mode
                        self.host.store.save_decision(d)
                        quant_cycle_rows.append(self._research_capture_row(d, inst, mode, quote_by_key.get(str(inst.get("instrument_key") or ""))))
                        if not market_open:
                            prepared_candidates += self.host._store_discovery_watch(d)
                            prepared_candidates += self.host._store_premarket_candidates(d)
                        if d.get("status") == "PROMOTED":
                            promoted += 1
                        else:
                            rejected += 1
                    else:
                        rejected += 1
                        blocked_members.append(self._scanner_stage_member(inst, "MATHEMATICALLY_REJECTED", "ANALYSIS_RETURNED_NO_QUALIFIED_DECISION"))
                with self.host.lock:
                    analysis_st.update({
                        "current_symbol": None,
                        "cycle_attempted": attempted,
                        "cycle_quote_ready": len(quote_ready),
                        "cycle_shortlisted": len(shortlist),
                        "cycle_capacity_deferred": capacity_deferred,
                        "cycle_scanned": scanned,
                        "cycle_promoted": promoted,
                        "cycle_rejected": rejected,
                        "cycle_data_missing": data_missing,
                        "cycle_analysis_timeouts": analysis_timeouts,
                        "cycle_analysis_capacity": analysis_capacity,
                        "cycle_analysis_errors": analysis_errors,
                        "current_sweep_scanned": min(universe_size, sweep_before + base_count),
                        "last_progress_at": now_iso(),
                    })
                self._publish_scanner_progress("delivery")
                raw_next_cursor = cursor + base_count
                sweep_scanned = min(universe_size, sweep_before + base_count)
                sweep_complete = raw_next_cursor >= universe_size
                next_cursor = 0 if sweep_complete else raw_next_cursor
                coverage_pct = round(sweep_scanned * 100.0 / max(1, universe_size), 1)
                completed_at = now_iso()
                delivery_stage_members = {
                    "universe": [self._scanner_stage_member(item, "UNIVERSE") for item in universe[:250]],
                    "attempted": [self._scanner_stage_member(item, "ATTEMPTED") for item in batch[:250]],
                    "quote_ready": [self._scanner_stage_member(item, "QUOTE_READY") for item in quote_ready[:250]],
                    "shortlisted": [self._scanner_stage_member(item, "SHORTLISTED") for item in shortlist[:250]],
                    "analysed": [self._scanner_stage_member(item, "ANALYSED", str(item.get("status") or "")) for item in quant_cycle_rows[:250]],
                    "blocked": blocked_members[:250],
                    "data_pending": [item for item in blocked_members if str(item.get("state") or "").upper() == "DATA_PENDING"][:250],
                    "data_blocked": [item for item in blocked_members if str(item.get("state") or "").upper() == "BLOCKED"][:250],
                    "capacity_deferred": [item for item in blocked_members if str(item.get("state") or "").upper() == "CAPACITY_DEFERRED"][:250],
                    "mathematically_rejected": [item for item in blocked_members if str(item.get("state") or "").upper() == "MATHEMATICALLY_REJECTED"][:250],
                    "map": [self._scanner_stage_member(item, "MAP", str((item.get("trade_map") or {}).get("state") or "")) for item in quant_cycle_rows if str((item.get("trade_map") or {}).get("state") or "").upper() in {"FINAL", "RESEARCH"}][:250],
                    "rr": [self._scanner_stage_member(item, "R:R") for item in quant_cycle_rows if float((item.get("trade_map") or {}).get("room_rr") or item.get("reward_risk") or 0) > 0][:250],
                    "final": [self._scanner_stage_member(item, "FINAL") for item in quant_cycle_rows if str(item.get("status") or "").upper() == "PROMOTED"][:250],
                }
                # Main operator funnel counters are cumulative for the immutable
                # current sweep. Batch-local counters remain in last_completed.
                sweep_stage_counts = cumulative_delivery_sweep_stage_counts(
                    universe_size=universe_size, base_batch=base_batch, base_count=base_count,
                    stage_members=delivery_stage_members, prior_counts=prior_sweep_stage_counts,
                )
                cycle_elapsed = max(0.001, time.monotonic() - cycle_started)
                base_symbols_per_sec = round(base_count / cycle_elapsed, 2)
                quote_symbols_per_sec = round(len(valid_batch) / max(0.001, quote_elapsed), 2)
                deep_symbols_per_sec = round(scanned / max(0.001, deep_elapsed), 2) if scanned else 0.0
                worker_capacity_now = self.analysis_executor.capacity(mode)
                runtime_telemetry = {
                    "batch_elapsed_sec": round(cycle_elapsed, 3),
                    "coverage_symbols_per_sec": base_symbols_per_sec,
                    "quote_stage_elapsed_sec": round(quote_elapsed, 3),
                    "quote_symbols_per_sec": quote_symbols_per_sec,
                    "deep_stage_elapsed_sec": round(deep_elapsed, 3),
                    "deep_symbols_per_sec": deep_symbols_per_sec,
                    "active_workers": int(worker_capacity_now.get("active") or 0),
                    "available_workers": int(worker_capacity_now.get("available") or 0),
                    "worker_count": int(worker_capacity_now.get("workers") or 0),
                    "capacity_deferred": int(capacity_deferred),
                    "deep_analysis_deferred": bool(defer_deep_analysis),
                    "deep_analysis_deferred_reason": yield_reason if defer_deep_analysis else None,
                    "last_progress_at": completed_at,
                    "stall_blocker": None,
                }
                completed_cycle = {
                    "attempted": attempted, "quote_ready": len(quote_ready), "shortlisted": len(shortlist),
                    "capacity_deferred": capacity_deferred,
                    "cheap_rejected": cheap_rejected, "scanned": scanned, "promoted": promoted, "rejected": rejected,
                    "data_missing": data_missing, "analysis_timeouts": analysis_timeouts,
                    "analysis_capacity": analysis_capacity, "analysis_errors": analysis_errors,
                    "analysis_workers": self.analysis_executor.capacity(mode),
                    "resolution_summary": {
                        "shortlisted": len(shortlist),
                        "analysed": scanned,
                        "data_pending": sum(1 for item in blocked_members if str(item.get("state") or "").upper() == "DATA_PENDING"),
                        "data_blocked": sum(1 for item in blocked_members if str(item.get("state") or "").upper() == "BLOCKED"),
                        "capacity_deferred": capacity_deferred,
                        "mathematically_rejected": sum(1 for item in blocked_members if str(item.get("state") or "").upper() == "MATHEMATICALLY_REJECTED"),
                        "unresolved": max(0, len(shortlist) - scanned - sum(1 for item in blocked_members if str(item.get("state") or "").upper() in {"BLOCKED", "DATA_PENDING", "CAPACITY_DEFERRED", "MATHEMATICALLY_REJECTED"})),
                    },
                    "prepared_candidates": prepared_candidates, "base_batch": base_count,
                    "priority_insertions": priority_count, "completed_at": completed_at,
                    "elapsed_sec": round(cycle_elapsed, 2),
                    "throughput_per_minute": round(scanned * 60.0 / cycle_elapsed, 2),
                    "coverage_throughput_per_minute": round(base_count * 60.0 / cycle_elapsed, 2),
                    "runtime_telemetry": runtime_telemetry,
                }
                completed_sweep = None
                if sweep_complete:
                    completed_sweep = {
                        "sweep_number": sweep_number, "universe_size": universe_size,
                        "scanned": sweep_scanned, "completed_at": completed_at,
                    }
                try:
                    # The exact-snapshot versioned checkpoint is the only restart
                    # authority. Legacy unbound cursor keys are intentionally ignored.
                    if self.checkpoints is not None:
                        self.checkpoints.persist("delivery", "analysis", {
                        "cursor": next_cursor, "universe_size": universe_size,
                        "sweep_number": sweep_number, "sweep_scanned": sweep_scanned,
                        "coverage_pct": coverage_pct, "sweep_complete": sweep_complete,
                        "last_completed": completed_cycle,
                        "last_completed_sweep": completed_sweep or analysis_st.get("last_completed_sweep"),
                        "last_run": completed_at, "next_run": "mode-specific",
                        "stage_members": delivery_stage_members,
                        "sweep_stage_counts": sweep_stage_counts,
                        }, universe=universe, identity=self._snapshot_identity("delivery"))
                except Exception as exc:
                    self.host.record_error("delivery_scan_checkpoint_persist", str(exc))
                with self.host.lock:
                    coverage = {
                        "universe_size": universe_size, "covered": sweep_scanned,
                        "coverage_pct": coverage_pct, "sweep_number": sweep_number,
                        "sweep_complete": sweep_complete, "cursor": next_cursor,
                        "note": "Actual canonical Delivery universe; priority insertions are excluded from coverage advancement.",
                    }
                    continuation_state = "complete" if sweep_complete else "continuing_sweep"
                    continuation_delay = 300 if sweep_complete else 8
                    runtime_telemetry.update({
                        "next_retry_in_seconds": continuation_delay,
                        "blocker_reason": None,
                        "continuation_watchdog": "ARMED" if not sweep_complete else "IDLE",
                        "last_progress_age_seconds": 0,
                    })
                    st.update({
                        "state": continuation_state, "attempted": attempted, "scanned": scanned, "promoted": promoted,
                        "rejected": rejected, "data_missing": data_missing, "prepared_candidates": prepared_candidates,
                        "last_run": completed_at, "next_run": f"{continuation_delay}s",
                        "cursor": next_cursor, "coverage": coverage,
                        "last_completed": completed_cycle,
                        "runtime_telemetry": runtime_telemetry,
                        "last_progress_at": completed_at,
                        "blocker_reason": None,
                    })
                    analysis_st.update({
                        "state": continuation_state, "cursor": next_cursor,
                        "universe_size": universe_size, "sweep_number": sweep_number,
                        "sweep_scanned": sweep_scanned, "current_sweep_scanned": sweep_scanned,
                        "coverage_pct": coverage_pct, "sweep_complete": sweep_complete,
                        "cycle_attempted": attempted, "cycle_quote_ready": len(quote_ready),
                        "cycle_shortlisted": len(shortlist), "cycle_capacity_deferred": capacity_deferred,
                        "cycle_cheap_rejected": cheap_rejected,
                        "cycle_scanned": scanned, "cycle_promoted": promoted,
                        "cycle_rejected": rejected, "cycle_data_missing": data_missing,
                        "cycle_analysis_timeouts": analysis_timeouts,
                        "cycle_analysis_capacity": analysis_capacity,
                        "cycle_analysis_errors": analysis_errors,
                        "analysis_workers": self.analysis_executor.capacity(mode),
                        "stage_members": delivery_stage_members,
                        "sweep_stage_counts": sweep_stage_counts,
                        "prepared_candidates": prepared_candidates,
                        "base_batch": base_count, "priority_insertions": priority_count,
                        "current_symbol": None, "last_run": completed_at,
                        "next_run": f"{continuation_delay}s", "last_completed": completed_cycle,
                        "last_completed_sweep": completed_sweep or analysis_st.get("last_completed_sweep"),
                        "runtime_telemetry": runtime_telemetry,
                        "last_progress_at": completed_at,
                    })
                    promotion_st.update({
                        "state": continuation_state, "cycle_promoted": promoted,
                        "last_run": completed_at,
                        "last_completed": {"promoted": promoted, "completed_at": completed_at},
                    })
                self._mirror_delivery_compatibility(st)
                self._publish_scanner_progress("delivery")
                quant_prediction = record_quant_scan_cycle(self.host, quant_cycle_rows, mode, completed_at, universe_size)
                with self.host.lock:
                    st["research_capture"] = quant_prediction
                    analysis_st["research_capture"] = quant_prediction
                self._publish_scanner_progress(mode)
                full_sweep_evidence = None
                if completed_sweep is not None:
                    full_sweep_evidence = self._record_scanner_cycle_evidence(
                        "delivery", "full_sweep",
                        dict(completed_sweep, market_open=is_india_market_open(), data_missing=data_missing),
                    )
                market_cycle_evidence = None
                if is_india_market_open():
                    market_cycle_evidence = self._record_scanner_cycle_evidence(
                        "delivery", "market_hours_analysis",
                        dict(completed_cycle, market_open=True, quant_prediction_state=(quant_prediction or {}).get("state")),
                    )
                return {
                    "ok": True, "mode": mode, "attempted": attempted, "scanned": scanned,
                    "promoted": promoted, "rejected": rejected, "data_missing": data_missing,
                    "quote_ready": len(quote_ready), "shortlisted": len(shortlist),
                    "capacity_deferred": capacity_deferred,
                    "deep_analysis_deferred": bool(defer_deep_analysis),
                    "deep_analysis_deferred_reason": yield_reason if defer_deep_analysis else None,
                    "cheap_rejected": cheap_rejected, "analysis_timeouts": analysis_timeouts,
                    "analysis_capacity": analysis_capacity, "analysis_errors": analysis_errors,
                    "base_batch": base_count, "priority_insertions": priority_count,
                    "cursor": next_cursor, "coverage_pct": coverage_pct,
                    "sweep_complete": sweep_complete,
                    "quant_prediction": quant_prediction,
                    "full_sweep_evidence": full_sweep_evidence,
                    "market_cycle_evidence": market_cycle_evidence,
                    "runtime_telemetry": runtime_telemetry,
                }
            except Exception as exc:
                self.host.record_error(f"{mode}_scanner", str(exc), "/v3/historical-candle")
                with self.host.lock:
                    st.update({"state": "degraded", "last_run": now_iso(), "next_run": "after auth-test", "error": str(exc)[:160]})
                    analysis_st.update({"state": "degraded", "last_run": now_iso(), "next_run": "after auth-test", "error": str(exc)[:160]})
                return {"ok": False, "error": str(exc)}
