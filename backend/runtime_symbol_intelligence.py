from __future__ import annotations

from runtime_shared import *


class RuntimeSymbolIntelligenceMixin:
    """Search, instrument resolution, chart history and stock intelligence composition."""

    def _local_search_state(self) -> tuple[str, str, int]:
        """Return search readiness from the last published in-memory snapshot.

        Search must never invoke the aggregate product-readiness service, a
        broker call, scanner work or an analytical query.  This helper is used
        directly by /api/search and /api/suggest so an exact local identity
        remains responsive even while other product planes are warming.
        """
        meta = dict(getattr(self, "_instrument_health_meta", {}) or {})
        stats = dict(meta.get("universe_stats") or {})
        ready = bool(
            meta.get("loaded")
            and meta.get("cache_usable")
            and int(meta.get("count") or 0) > 0
            and meta.get("universe_revision") == ACTIVE_UNIVERSE_REVISION
            and int(stats.get("derivatives") or 0) == 0
        )
        if ready:
            return "READY", (
                f"Local NSE/BSE cash index: NSE {int(stats.get('nse_equities') or 0):,}, "
                f"BSE-only {int(stats.get('bse_only_equities') or 0):,}"
            ), int(meta.get("count") or 0)
        if int(meta.get("count") or 0) > 0:
            return "DEGRADED", "Legacy catalogue is available while focused NSE+BSE refresh completes", int(meta.get("count") or 0)
        return "DEGRADED", "Trusted exact recovery symbols only while the local catalogue warms", 0

    def search(self, q: str, mode: str) -> Dict[str, Any]:
        try:
            mode = require_production_mode(mode)
        except UnsupportedProductionMode as exc:
            return {"ok": False, "query": q, "error": "unsupported_production_mode", "message": str(exc), "allowed_modes": ["intraday", "delivery"], "matches": []}
        search_state, search_message, instrument_count = self._local_search_state()
        if search_state == "READY" and is_index_search_query(q):
            instruments = [row for row in [self._index_instrument_for_chart(q)] if row]
        elif search_state == "READY":
            instruments = [self._enrich_instrument_identity(i) for i in self.store.quick_symbol_search(q, limit=10)]
        else:
            # Never scan the provider-wide catalogue during focused refresh.
            instruments = []
        if not instruments:
            fallback = final_fallback_instrument(q)
            if fallback:
                instruments = [self._enrich_instrument_identity(fallback)]
        if instruments:
            symbol_for_priority = instruments[0].get("trading_symbol") or q
            def _remember_manual_search() -> None:
                try:
                    self.store.add_priority_symbol(symbol_for_priority, mode, source="manual_search")
                except Exception:
                    pass
            threading.Thread(target=_remember_manual_search, name="ProjectLadduManualSearchPriority", daemon=True).start()
        shell = None  # identity search is intentionally local and side-effect free
        return {
            "ok": True, "query": q, "mode": mode, "matches": instruments,
            "queued": len(instruments[:1]), "instrument_count": instrument_count,
            "quote_error": None, "immediate": [], "search_shell": shell,
            "search_state": search_state if instruments else ("UNAVAILABLE" if search_state == "READY" else search_state),
            "search_message": search_message if instruments else "No exact local stock identity resolved",
            "mode_status": None,
            "production_policy_version": POLICY_VERSION,
        }

    def suggest(self, q: str) -> Dict[str, Any]:
        # v19: suggest is strictly local-cache-only and never triggers quote/fundamental/MI work.
        # v36.7: keystroke-driven typeahead now hits the in-memory symbol index
        # first (no SQLite round-trip per key); full detail is only fetched
        # once the user actually selects a symbol (see search()/_first_instrument()).
        q = (q or "").strip()
        search_state, search_message, instrument_count = self._local_search_state()
        if q:
            quick = self.store.quick_symbol_search(q, limit=8) if search_state == "READY" else []
            recovery = fallback_instrument_matches(q, limit=8)
            if quick or recovery:
                combined = []
                seen = set()
                for row in list(quick or []) + list(recovery or []):
                    enriched = self._enrich_instrument_identity(row)
                    symbol = str((enriched or {}).get("trading_symbol") or "").upper()
                    if enriched and symbol and symbol not in seen:
                        seen.add(symbol); combined.append(enriched)
                return {"query": q, "instrument_count": instrument_count, "matches": combined[:8], "mode_status": None, "served_from": "memory_index+trusted_recovery" if quick else "trusted_recovery", "search_state": search_state, "search_message": search_message}
        instruments = []
        instruments = [i for i in instruments if i]
        if q.strip():
            fb = final_fallback_instrument(q)
            if fb:
                sq = q.upper().strip()
                exact_exists = any(str(i.get("trading_symbol") or i.get("symbol") or "").upper() == sq for i in instruments)
                if not exact_exists:
                    instruments = [fb] + instruments
        if not instruments and q.strip():
            fb = final_fallback_instrument(q)
            if fb:
                instruments = [fb]
        return {"query": q, "instrument_count": instrument_count, "matches": instruments, "mode_status": None, "search_state": search_state if instruments else ("UNAVAILABLE" if search_state == "READY" else search_state), "search_message": search_message if instruments else "No exact local match"}

    def apply_operator_capital_settings(self, settings: Dict[str, Any]) -> Dict[str, Any]:
        """Apply governed settings to future Model Paper sizing only."""
        wallet = float(settings.get("model_wallet") or TRADING_CAPITAL)
        intraday_cap = float(settings.get("intraday_exposure_ceiling") or 0.0)
        self.operator_capital = dict(settings)
        self.model_portfolio.equity = wallet
        self.model_portfolio.intraday_cap = intraday_cap
        self.model_portfolio.risk.equity = wallet
        self.model_portfolio.risk.intraday_cap = intraday_cap
        try:
            self.event("INFO", "operator_settings", "Model Paper capital settings changed", {
                "model_wallet": wallet,
                "intraday_exposure_ceiling": intraday_cap,
                "effective_at": settings.get("effective_at"),
                "open_positions_resized": False,
                "broker_authority": "NONE",
            })
        except Exception:
            pass
        return dict(settings)

    def scanner_status(self) -> Dict[str, Any]:
        """v51 (Cluster 8): delegate -- see core/system_health_service.py::SystemHealthService.scanner_status"""
        return self.system_health_service.scanner_status()

    def instruments_status(self) -> Dict[str, Any]:
        """v51 (Cluster 8): delegate -- see core/system_health_service.py::SystemHealthService.instruments_status"""
        return self.system_health_service.instruments_status()

    def _first_instrument(self, symbol: str, force_refresh: bool = False) -> Dict[str, Any] | None:
        """v37.4: delegate -- see core/instrument_resolver.py::InstrumentResolver.resolve.
        Fixes the same prefix-scan + negative-cache bugs described there;
        `final_fallback_instrument` stays here since it's a LadduRuntime-local
        static fallback list, not something the resolver should own."""
        # Let the resolver auto-detect canonical cash-index aliases. Forcing
        # prefer_index=False allowed NIFTY 50 to bind through the equity path,
        # contaminating Stock Intelligence/history while the ticker still used
        # the correct verified index quote.
        inst = self.instrument_resolver.resolve(symbol, prefer_index=None, force_refresh=force_refresh)
        if not inst:
            fb = final_fallback_instrument(symbol)
            if fb:
                return fb
            return None
        return self._enrich_instrument_identity(inst)

    def _index_instrument_for_chart(self, symbol: str, force_refresh: bool = False) -> Dict[str, Any] | None:
        """v37.4: delegate -- see core/instrument_resolver.py::InstrumentResolver.resolve.
        The shared index-alias authority normalizes chart display names,
        resolver lookups and the downstream exact-identity contract together."""
        s = (symbol or "").strip().upper().replace("_", " ")
        if not s:
            return None
        # One shared alias authority is used by resolution and the exact
        # identity contract.  The prior build duplicated this table and accepted
        # PHARMA here while rejecting the same correct identity later.
        aliases = INDEX_SYMBOL_ALIASES
        q = aliases.get(s, s)
        if "NIFTY" not in q and "SENSEX" not in q and q not in aliases.values():
            return None
        inst = self.instrument_resolver.resolve(q, prefer_index=True, force_refresh=force_refresh)
        if not inst:
            return None
        return self._enrich_instrument_identity(inst)

    def schedule_historical_for_symbol(self, symbol: str, interval: str = "day", days: int | None = None, force_resolve: bool = False) -> Dict[str, Any]:
        """Start a single-flight historical refresh without returning candle rows."""
        return self.market_data.schedule_historical_for_symbol(symbol, interval, days, reason="manual_refresh", force_resolve=force_resolve)

    def schedule_historical_before_for_symbol(self, symbol: str, interval: str, before_date: str, days: int | None = None) -> Dict[str, Any]:
        """Schedule one older chart window without blocking the HTTP route."""
        return self.market_data.schedule_historical_before_for_symbol(symbol, interval, before_date, days)

    def historical_for_symbol(self, symbol: str, interval: str = "day", days: int | None = None, refresh: bool = False, recent_only: bool = False) -> Dict[str, Any]:
        """Delegate to the canonical market-data read authority."""
        return self.market_data.historical_for_symbol(
            symbol, interval, days, refresh=refresh, recent_only=recent_only
        )

    def analyze_symbol(self, symbol: str, mode: str = "delivery") -> Dict[str, Any]:
        """v51: delegate -- see core/decision_engine_service.py::DecisionEngineService.analyze_symbol"""
        return self.decision_engine.analyze_symbol(
            symbol, mode,
            engines=ENGINES,
            first_instrument_fn=self._first_instrument,
            instrument_count_fn=self.store.instrument_count,
            token_status_ok_fn=lambda: self.client.token_status().get("ok"),
            quotes_fn=self.client.quotes,
            record_error_fn=self.record_error,
            event_fn=self.event,
            mode_uses_history_without_live_fn=mode_uses_history_without_live,
            analyze_one_fn=self.analyze_one,
            add_priority_fn=self.store.add_priority,
            save_decision_fn=self.store.save_decision,
            is_actionable_selected_fn=self._is_actionable_selected,
            upsert_manual_watch_fn=lambda decision: self.store.upsert_manual_watch(decision, source="manual_analyze"),
            on_ai_validation=lambda: self.status.__setitem__("last_ai_validation", now_iso()),
        )

    def _schedule_research_enrichment(self, symbol, mode, inst, hist, candles, selected_truth, analysis, quote_payload) -> None:
        key=f"{str(symbol).upper()}:{str(mode).lower()}"
        if key in self._research_pending or not self.research_adapter.available().get("ok"):
            return
        self._research_pending.add(key)
        def worker():
            try:
                result=self.research_adapter.run(symbol=symbol,mode=mode,inst=inst,hist=hist,candles=candles,selected_truth=selected_truth)
                self._research_result_cache[key]=(time.time(),result)
                self.decision_ledger.build_and_store(symbol=symbol,mode=mode,inst=inst,hist=hist,analysis=analysis,selected_truth=selected_truth,candles=candles,quote_payload=quote_payload if isinstance(quote_payload,dict) else None,research_result=result)
            except Exception as exc:
                self.record_error("research_enrichment",str(exc)[:180])
            finally:
                self._research_pending.discard(key)
        threading.Thread(target=worker,name=f"LadduResearch-{key}",daemon=True).start()

    def _selected_stock_truth(self, symbol: str, mode: str, inst: Dict[str, Any], hist: Dict[str, Any], analysis: Dict[str, Any], required_candles: int, quote_payload: Dict[str, Any] | None = None) -> Dict[str, Any]:
        """v38: single selected-stock truth object for Stock Intelligence + Chart Desk.

        This is deliberately compact and UI-facing: it tells the cockpit whether
        quote, candle history, chart and decision are full/partial/unavailable.
        It prevents separate widgets from inventing their own interpretation of
        the same selected stock state.
        """
        decision = (analysis or {}).get("decision") or {}
        hist_count = int(hist.get("count") or 0)
        q = dict(quote_payload or {})
        quote_state = str(q.get("freshness_state") or ("unverified" if q else "pending")).lower()
        quote_ok = bool(q.get("ltp") is not None and q.get("identity_verified") and quote_state in ("live", "closed_market"))
        historical_status = hist.get("data_status") or hist.get("coverage_status") or ("ok" if hist.get("ok") else "pending")
        chart_status = "ok" if hist_count >= required_candles else ("partial" if hist_count > 0 else "pending")
        entry = decision.get("entry")
        sl = decision.get("sl")
        t1 = decision.get("t1")
        side = str(decision.get("side") or "").upper()
        def _num(v):
            try:
                return float(v)
            except Exception:
                return None
        entry_n, sl_n, t1_n = _num(entry), _num(sl), _num(t1)
        valid_map = False
        if entry_n is not None and sl_n is not None and t1_n is not None:
            if side == "SHORT":
                valid_map = sl_n > entry_n and t1_n < entry_n
            else:
                valid_map = sl_n < entry_n and t1_n > entry_n
        raw_decision = str(decision.get("decision") or decision.get("status") or "WATCH").upper()
        raw_status = str(decision.get("status") or "").upper()
        actionable = bool(valid_map and raw_decision == "TRADE" and raw_status in ("PROMOTED", "TRIGGERED", "SELECTED", "OPEN"))
        score = decision.get("score")
        try:
            score_i = int(float(score))
        except Exception:
            score_i = 0
        confidence = str(decision.get("confidence") or ("High" if score_i >= 80 and actionable else "Medium" if score_i >= 62 else "Low"))
        reason = str(decision.get("reason") or decision.get("setup") or hist.get("message") or "Waiting for validated evidence")
        if hist_count < required_candles:
            decision_status = "partial"
            if hist.get("coverage_message") and hist.get("coverage_message") not in reason:
                reason = f"{reason} · {hist.get('coverage_message')}"
        elif not quote_ok:
            decision_status = "quote_pending"
        elif actionable:
            decision_status = "actionable"
        else:
            decision_status = "watch"
        data_status = "full" if quote_ok and hist_count >= required_candles else "partial" if quote_ok or hist_count else "pending"
        data_label = {"full": "Data Full", "partial": "Data Partial", "pending": "Data Pending"}.get(data_status, "Data Partial")
        if hist_count and hist_count < required_candles:
            data_label = "History Partial"
        return {
            "symbol": symbol,
            "exchange": (inst or {}).get("exchange") or decision.get("exchange") or "NSE",
            "mode": mode,
            "quote_status": "ok" if quote_ok else "pending",
            "quote_state": quote_state,
            "historical_status": historical_status,
            "historical_count": hist_count,
            "required_candles": required_candles,
            "coverage_status": hist.get("coverage_status") or ("full" if hist_count >= required_candles else ("partial" if hist_count else "pending")),
            "coverage_message": hist.get("coverage_message") or "",
            "chart_status": chart_status,
            "decision_status": decision_status,
            "data_status": data_status,
            "data_label": data_label,
            "actionable": actionable,
            "valid_trade_map": valid_map,
            "show_rr": bool(valid_map and decision.get("rr") is not None),
            "score": score_i,
            "confidence": confidence,
            "reason": reason,
            "last_quote_time": q.get("source_time") or q.get("timestamp") or decision.get("last_refresh"),
            "last_history_time": (hist.get("last_candle") or {}).get("timestamp") or hist.get("last_success_at"),
            "refresh_required": bool(not quote_ok or hist_count < required_candles),
        }

    def symbol_market_intelligence(self, symbol: str, mode: str = "delivery", refresh: bool = False) -> Dict[str, Any]:
        # v14: full intelligence must return partial data quickly; no hanging cockpit.
        # v65.8.1: stage timing added below (search "_stage_ms") -- read-only,
        # log-only instrumentation to answer "where does a plain click's time
        # actually go" with a real number instead of guessing. Does not change
        # any control flow; every existing call is untouched, only wrapped
        # with time.perf_counter() markers and one summary log line at the end.
        _t0 = time.perf_counter()
        _stage_ms: Dict[str, float] = {}
        def _mark(stage: str, _prev=[_t0]):
            now = time.perf_counter()
            _stage_ms[stage] = round((now - _prev[0]) * 1000, 1)
            _prev[0] = now
        symbol = (symbol or "").strip().upper()
        try:
            mode = require_production_mode(mode)
        except UnsupportedProductionMode as exc:
            return {"ok": False, "symbol": symbol, "error": "unsupported_production_mode", "message": str(exc), "allowed_modes": ["intraday", "delivery"]}
        hist_interval = "day" if mode == "delivery" else "5minute"
        hist = self._safe_section("symbol_historical", lambda: self.historical_for_symbol(symbol, hist_interval, None, refresh=refresh), {"ok": False, "symbol": symbol, "error": "historical pending", "candles": []})
        _mark("historical")
        required_candles = 120 if mode == "delivery" else 50
        selected_inst = hist.get("instrument") or self._first_instrument(symbol)
        if selected_inst and selected_inst.get("instrument_key"):
            try:
                self.store.set_kv("selected_stock:last", {
                    "symbol": symbol,
                    "instrument_key": selected_inst.get("instrument_key"),
                    "exchange": selected_inst.get("exchange") or "NSE",
                    "selected_at": now_iso(),
                })
            except Exception:
                pass
        # v44.6: "Refresh Stock" is a deliberate, waited-for user action for this one
        # symbol -- don't make it wait behind the slow universe-wide deep-history
        # queue (deep_history_backfill_loop / run_deep_mode_scan) to get 120+ daily
        # candles. Force a direct, bounded, per-symbol deep backfill right here, then
        # re-read stored candles once, before building the rest of the response.
        # v44.7: missing candle history should never sit as a silent blocker --
        # trigger a bounded per-symbol backfill immediately on first selection
        # (not only on an explicit "Refresh Stock" click), for every mode, not
        # just Delivery.
        # v44.7.2: the first version of this fired a fresh backfill thread on
        # *every* poll of a symbol with thin history (the frontend polls this
        # endpoint every few seconds), which stacked up SQLite writes against
        # the dashboard/evidence reads and produced "database is locked" /
        # slow-request warnings, and made rows intermittently vanish. Gate
        # background (non-refresh) attempts behind a persisted per-symbol
        # cooldown so each symbol is only auto-backfilled once every 30 min.
        if int(hist.get("count") or 0) < required_candles:
            inst_for_backfill = hist.get("instrument") or self._first_instrument(symbol)
            key = inst_for_backfill.get("instrument_key") if inst_for_backfill else None
            if key:
                inflight = getattr(self, "_backfill_inflight", None)
                if inflight is None:
                    inflight = self._backfill_inflight = set()
                cooldowns = self.store.get_kv("auto_backfill_cooldown", {}) or {}
                last_attempt = cooldowns.get(key)
                cooled_down = (not last_attempt) or (time.time() - float(last_attempt) > 1800)
                if refresh or (key not in inflight and cooled_down):
                    try:
                        if key not in inflight:
                            inflight.add(key)
                            cooldowns[key] = time.time()
                            self.store.set_kv("auto_backfill_cooldown", cooldowns)
                            def _bg_backfill(k=key):
                                try:
                                    self.client.deep_backfill_daily_candles(
                                        k, years=15,
                                        request_guard=lambda: self.rate.net_slot(priority="background", timeout=2.5),
                                    )
                                finally:
                                    inflight.discard(k)
                            threading.Thread(target=_bg_backfill, name=f"deep-backfill-{symbol}", daemon=True).start()
                        if refresh:
                            # Reference-data backfill remains independent of the
                            # selected-stock response and cannot hold the browser
                            # request open.
                            nse_bg_lock = getattr(self, "_nse_delivery_bg_lock", None)
                            if nse_bg_lock is None:
                                nse_bg_lock = self._nse_delivery_bg_lock = threading.Lock()
                            start_nse_bg = False
                            with nse_bg_lock:
                                if not getattr(self, "_nse_delivery_bg_running", False):
                                    self._nse_delivery_bg_running = True
                                    start_nse_bg = True
                            if start_nse_bg:
                                def _bg_nse_delivery():
                                    try:
                                        self._safe_section("nse_delivery_backfill_on_refresh", lambda: self.reference_data.backfill_missing(max_downloads=15), {})
                                    finally:
                                        with nse_bg_lock:
                                            self._nse_delivery_bg_running = False
                                threading.Thread(target=_bg_nse_delivery, name="nse-delivery-backfill", daemon=True).start()
                        pq = self.store.get_kv("scan_priority_queue", []) or []
                        if symbol not in pq:
                            self.store.set_kv("scan_priority_queue", ([symbol] + pq)[:25])
                    except Exception as exc:
                        self.record_error("symbol_refresh_deep_backfill", f"{symbol}: {str(exc)[:150]}")
        if int(hist.get("count") or 0) < required_candles:
            hist["data_status"] = "historical_refresh_pending"
            hist["required_candles"] = required_candles
            cnt = int(hist.get('count') or 0)
            hist["message"] = (f"Need {required_candles}+ candles for {mode}; refresh pending/no candle permission" if cnt == 0 else f"Need {required_candles}+ candles for {mode}; currently partial candle history")
        # v36.3.2: Stock Intelligence is cache-first and deadline-bound. Do not call
        # the synchronous /api/analyze path here because it fetches live quotes and can
        # make the browser abort the request. Build the decision proof from the same
        # cached candle evidence used by the chart; background refresh continues separately.
        inst = hist.get("instrument") or self._first_instrument(symbol) or final_fallback_instrument(symbol) or {"trading_symbol": symbol, "symbol": symbol, "exchange": "NSE"}
        candles = hist.get("candles") or []
        quote = None
        # v44.7: fundamentals missing/incomplete is never left as a dead-end
        # blocker -- attempt a live fetch on first selection for every mode,
        # not just Delivery-family. fundamental_context() only calls the API
        # when local CSV data is missing/stale, so this is a no-op cost-wise
        # when local data already covers the symbol.
        #
        # v51.0.10: that "no-op cost-wise when local data covers the symbol"
        # assumption breaks down whenever fundamentals.csv is missing/stale for
        # a symbol (see Priority 4 in the build notes) -- use_api_fund=True then
        # means every plain stock click blocks on a full live
        # fundamentals_snapshot() (up to 9 sequential Upstox calls, 6-7s timeout
        # each) before the row can render, and it ran three times over (once
        # here, once again inside market_context, and once more inside
        # analyze_one) since each is a separate call rather than a shared
        # result. Match the cache-first/deadline-bound principle already used
        # for the historical/candle path just above: only the explicit,
        # waited-for "Refresh Stock" click (refresh=True) pays the live-fetch
        # cost now. A plain open/selection uses cache/local instantly and
        # kicks off a background prefetch, so Stock Intelligence never blocks
        # the request on the network; fundamentals fill in on a follow-up
        # poll once the prefetch completes.
        use_api_fund = bool(refresh)
        refresh_fund = bool(refresh and use_api_fund)
        if not use_api_fund and inst:
            self._schedule_fundamental_prefetch(inst)
        _mark("backfill_bookkeeping")

        # v65.15.2: the interactive Stock Intelligence contract must be bounded.
        # The full market_context/analyze_one/ledger chain can contend with a
        # universe scan and previously never returned at all. Build the visible
        # card from already-persisted candles, quotes and fundamentals; deeper
        # enrichment continues through its independent workers/endpoints.
        cached_fundamentals = self._safe_section(
            "selected_fundamentals_cached",
            lambda: self.fundamental_context(inst, use_api=False) if inst else {},
            {},
        )
        cached_quote_row = dict(self.market_data._quote_delta_cache.get(symbolKey_py(symbol)) or {})
        if not cached_quote_row:
            try:
                runtime_selected = self.runtime_market_state.latest_quotes([symbol])
            except Exception as exc:
                runtime_selected = []
                self.record_error("runtime_selected_quote_read", str(exc))
            cached_quote_row = dict(runtime_selected[0] if runtime_selected else {})
        if not cached_quote_row:
            cached_quote_row = dict((self.store.latest_quotes_by_symbol([symbol]) or {}).get(symbol, {}) or {})
        def _selected_verified_snapshot():
            if not inst:
                return {"ok": False, "state": "instrument_missing"}
            self.rate.prioritize_interactive(8.0)
            with self.rate.net_slot(priority="interactive", timeout=5.5):
                return self.client.selected_full_quote(inst)
        selected_market_snapshot = self._safe_section(
            "selected_full_quote",
            _selected_verified_snapshot,
            {"ok": False, "state": "quote_unavailable"},
        )
        market_open_now = is_india_market_open()
        selected_integrity = classify_quote(
            selected_market_snapshot if selected_market_snapshot.get("ok") else {},
            now=india_now(), market_open=market_open_now, max_live_age_sec=45.0,
        )
        cached_integrity = classify_quote(
            cached_quote_row,
            now=india_now(), market_open=market_open_now, max_live_age_sec=45.0,
        )
        chosen_quote: Dict[str, Any] = {}
        chosen_integrity = selected_integrity
        if selected_integrity.get("state") in ("live", "closed_market"):
            chosen_quote = dict(selected_market_snapshot)
        elif cached_integrity.get("state") in ("live", "closed_market"):
            chosen_quote = dict(cached_quote_row)
            chosen_integrity = cached_integrity
        # Never merge a stale/unverified SQLite fallback into the selected-stock
        # LTP. During an open session, an unavailable verified quote renders as
        # unavailable; historical candles remain available only for analysis.
        last_candle = candles[-1] if candles else {}
        ltp = chosen_quote.get("ltp") if chosen_quote else None
        previous_close = chosen_quote.get("previous_close") or chosen_quote.get("close")
        change_abs = None
        change_pct = chosen_quote.get("change_pct")
        try:
            if ltp is not None and previous_close:
                change_abs = round(float(ltp) - float(previous_close), 2)
                if change_pct is None:
                    change_pct = round(change_abs * 100.0 / float(previous_close), 2)
        except Exception:
            change_abs = None
            change_pct = None

        daily_candles = self.store.get_candles(inst["instrument_key"], "day", limit=5) if inst else []
        completed_daily = daily_candles[-1] if daily_candles else {}
        if is_india_market_open() and len(daily_candles) > 1:
            latest_daily_date = str(completed_daily.get("timestamp") or "")[:10]
            india_today = datetime.now(timezone(timedelta(hours=5, minutes=30))).date().isoformat()
            if latest_daily_date == india_today:
                completed_daily = daily_candles[-2]
        previous_session = None
        try:
            previous_session = (
                float(completed_daily.get("high")),
                float(completed_daily.get("low")),
                float(completed_daily.get("close")),
            )
        except (TypeError, ValueError, AttributeError):
            previous_session = None
        level_projection = compute_levels_from_candles(
            candles, interval=hist_interval, prev_day_ohlc=previous_session
        )
        levels = dict(level_projection.get("camarilla") or {})
        structural = dict(level_projection.get("level_report") or {"ok": False})
        nearest_support = level_projection.get("support")
        nearest_resistance = level_projection.get("resistance")
        closes = [float(row.get("close")) for row in candles[-80:] if row.get("close") is not None]
        volumes = [float(row.get("volume") or 0) for row in candles[-20:]]
        # Stock Intelligence consumes the single deterministic indicator authority;
        # it must not silently seed EMA/RSI differently from MTF/decision surfaces.
        from core.indicator_snapshot_authority import DEFAULT_INDICATOR_SNAPSHOT_AUTHORITY
        indicator_snapshot = DEFAULT_INDICATOR_SNAPSHOT_AUTHORITY.calculate(candles[-80:])
        indicator_metrics = dict(indicator_snapshot.get("metrics") or {})
        ema20 = indicator_metrics.get("ema20")
        ema50 = indicator_metrics.get("ema50")
        rsi = indicator_metrics.get("rsi14")
        long_bias = bool(ema20 is not None and ema50 is not None and ema20 >= ema50)
        latest_price = float(ltp) if ltp is not None else (closes[-1] if closes else None)
        technical_score = 0
        technical_reasons = []
        if latest_price is not None and ema20 is not None:
            aligned = latest_price >= ema20 if long_bias else latest_price <= ema20
            technical_score += 30 if aligned else 5
            technical_reasons.append(f"Price {'above' if latest_price >= ema20 else 'below'} EMA20")
        if ema20 is not None and ema50 is not None:
            technical_score += 25
            technical_reasons.append(f"EMA20 {'above' if ema20 >= ema50 else 'below'} EMA50")
        if rsi is not None:
            supportive = 48 <= rsi <= 72 if long_bias else 28 <= rsi <= 52
            technical_score += 20 if supportive else 8
            technical_reasons.append(f"RSI {rsi}")
        if volumes and len(volumes) > 1:
            average_volume = sum(volumes[:-1]) / max(1, len(volumes) - 1)
            volume_ratio = volumes[-1] / average_volume if average_volume else 0
            if volume_ratio > 0.05:
                technical_score += 15 if volume_ratio >= 1 else 7
                technical_reasons.append(f"Volume {volume_ratio:.2f}x average")
        if len(closes) >= 2:
            direction_ok = closes[-1] >= closes[-2] if long_bias else closes[-1] <= closes[-2]
            technical_score += 10 if direction_ok else 3
        technical_score = min(100, technical_score)
        trend_label = "positive" if long_bias else "negative"
        price_position = "above" if latest_price is not None and ema20 is not None and latest_price >= ema20 else "below"
        if long_bias and price_position == "below":
            intelligence_summary = f"Mixed setup: medium-term trend remains positive, but price is below EMA20 and RSI is {rsi if rsi is not None else 'unavailable'}. Wait for an EMA20 reclaim with volume confirmation before considering a new entry."
        elif long_bias:
            intelligence_summary = f"Trend aligned: price and EMA structure are positive with RSI at {rsi if rsi is not None else 'unavailable'}. A new entry still requires confirmation near the Camarilla trigger and acceptable risk/reward."
        elif price_position == "above":
            intelligence_summary = f"Recovery attempt inside a negative EMA structure. RSI is {rsi if rsi is not None else 'unavailable'}; wait for EMA20/EMA50 alignment before treating the move as a durable reversal."
        else:
            intelligence_summary = f"Bearish alignment: price remains below EMA20 with negative medium-term structure. Avoid a fresh long until momentum and EMA structure improve."
        entry = levels.get("r3") if long_bias else levels.get("s3")
        target = levels.get("r4") if long_bias else levels.get("s4")
        stop = levels.get("r2") if long_bias else levels.get("s2")
        rr = None
        try:
            risk_distance = abs(float(entry) - float(stop))
            rr = round(abs(float(target) - float(entry)) / risk_distance, 2) if risk_distance else None
        except Exception:
            rr = None
        quote_timestamp = chosen_integrity.get("source_time") if chosen_quote else None
        candle_timestamp = last_candle.get("timestamp")
        quote_state = str(chosen_integrity.get("state") or "unavailable") if chosen_quote else "unavailable"
        if quote_state == "live":
            price_freshness = "verified live quote @ " + str(quote_timestamp)
        elif quote_state == "closed_market":
            price_freshness = "verified market-close snapshot @ " + str(quote_timestamp)
        else:
            price_freshness = "verified quote unavailable; technicals use completed candle @ " + str(candle_timestamp or "unavailable")
        decision = {
            "symbol": symbol,
            "exchange": inst.get("exchange") or "NSE",
            "mode": mode,
            "side": "LONG" if long_bias else "SHORT",
            "decision": "WATCH",
            "status": "CACHE_READY",
            "score": technical_score,
            "technical_score": technical_score,
            "evidence": technical_reasons,
            "ema20": ema20,
            "ema50": ema50,
            "rsi": rsi,
            "ltp": ltp,
            "change_abs": change_abs,
            "change_pct": change_pct,
            "entry": entry,
            "t1": target,
            "sl": stop,
            "rr": rr,
            "support": nearest_support,
            "resistance": nearest_resistance,
            "reason": intelligence_summary,
            "intelligence_summary": intelligence_summary,
            "price_freshness": price_freshness,
            "quote_freshness_state": quote_state,
            "quote_identity_verified": bool(chosen_integrity.get("identity_verified")) if chosen_quote else False,
            "last_ai_validation": now_iso(),
            "level_source": "camarilla_completed_daily_candle",
            "structure_level_source": structural.get("method"),
        }
        analysis = {"ok": True, "symbol": symbol, "mode": mode, "instrument": inst, "decision": decision, "source": "bounded_cache_contract"}
        quote_payload = {
            "symbol": symbol,
            "instrument_key": chosen_quote.get("instrument_key") if chosen_quote else (inst or {}).get("instrument_key"),
            "ltp": ltp,
            "change_pct": change_pct,
            "rupee_change": change_abs,
            "timestamp": quote_timestamp,
            "source_time": quote_timestamp,
            "received_time": chosen_quote.get("received_at") if chosen_quote else None,
            "freshness": price_freshness,
            "freshness_state": quote_state,
            "freshness_reason": chosen_integrity.get("reason") if chosen_quote else "verified_quote_unavailable",
            "identity_verified": bool(chosen_integrity.get("identity_verified")) if chosen_quote else False,
            "usable_for_promotion": bool(chosen_integrity.get("usable_for_promotion")) if chosen_quote else False,
            "source": chosen_quote.get("source") if chosen_quote else "completed_candle_analysis_only",
            "stale": quote_state != "live",
        }
        selected_stock_truth = self._selected_stock_truth(symbol, mode, inst, hist, analysis, required_candles, quote_payload)
        derivatives_context = self._safe_section(
            "derivatives_context_cached",
            lambda: self.derivatives_context.status(inst, spot=ltp, refresh=bool(refresh)) if inst else {
                "ok": False, "state": "IDENTITY_REQUIRED", "broker_authority": "NONE",
                "active_trading_universe": "CASH_ONLY", "production_influence": 0.0,
            },
            {"ok": False, "state": "UNAVAILABLE", "broker_authority": "NONE", "active_trading_universe": "CASH_ONLY", "production_influence": 0.0},
        )
        _stage_ms["total"] = round((time.perf_counter() - _t0) * 1000, 1)
        self.event("INFO", "perf", f"bounded symbol intelligence timing: {symbol} {mode}", _stage_ms)
        return {
            "ok": True,
            "symbol": symbol,
            "mode": mode,
            "historical": {k: v for k, v in hist.items() if k != "candles"},
            "analysis": analysis,
            "fundamentals": cached_fundamentals,
            "market_structure": {
                "state": "bullish" if long_bias else "bearish",
                "support": nearest_support,
                "resistance": nearest_resistance,
                "support_validated": bool(structural.get("nearest_support")),
                "resistance_validated": bool(structural.get("nearest_resistance")),
                "method": structural.get("method"),
                "supports": structural.get("support") or [],
                "resistances": structural.get("resistance") or [],
            },
            "volume_profile": {"state": "normal", "reason": "Calculated from stored volume history"},
            "orb": {"state": "pending"},
            "heat_context": {},
            "mode_intelligence": {},
            "price_action": {
                "camarilla": levels,
                "as_of": completed_daily.get("timestamp"),
                "source": "completed_daily_candle",
            },
            "mtf_trend": [{"tf": tf, "state": "pending", "reason": "loading via /api/mtf-trend"} for tf in ("1m", "3m", "5m", "15m", "30m", "1H", "4H", "1D", "1W", "1M")],
            "selected_stock_truth": selected_stock_truth,
            "decision_ledger": {"ok": False, "status": "deferred", "summary": "Deep decision ledger enrichment runs independently."},
            "research_adapter": {"ok": False, "status": "deferred", "summary": "Research enrichment runs independently."},
            "selected_quote": quote_payload,
            "market_snapshot": selected_market_snapshot,
            "derivatives_context": derivatives_context,
            "mode_status": {"symbol": symbol, "modes": []},
            "heatmap": self._pending_heatmap("independent"),
            "scanner": {"service": self.status.get("service"), "market_open": is_india_market_open(), "minutes_to_close": minutes_to_close(), "auth": self.status.get("auth"), "scanner": self.status},
            "selected_stock_refresh": {"requested": bool(refresh), "status": "cache_ready", "historical_status": hist.get("data_status") or ("ok" if hist.get("ok") else "failed"), "fundamental_status": cached_fundamentals.get("state"), "fundamental_source": cached_fundamentals.get("source")},
            "message": "Price, date, fundamentals and Camarilla levels are cache-ready. Deep technical enrichment loads independently.",
            "time": now_iso(),
        }

        ctx = self._safe_section("symbol_market_context", lambda: self.market_context(inst, mode, candles, quote, use_api_fund=use_api_fund) if inst else {}, {})
        _mark("market_context")
        if inst and candles:
            try:
                decision = self.analyze_one(inst, None, mode, use_api_fund=use_api_fund, candles_override=candles)
            except Exception as exc:
                self.event("WARN", "analysis", "Cache-first analysis fallback used", {"symbol": symbol, "mode": mode, "error": str(exc)[:180]})
                decision = None
            if decision:
                self._sync_decision_context(decision, ctx)
                self._apply_candidate_timing(decision, ctx, candles)
                analysis = {"ok": True, "symbol": symbol, "mode": mode, "instrument": inst, "decision": decision, "source": "cache_first_historical"}
            else:
                analysis = self._fallback_analysis_from_context(symbol, mode, inst, hist, ctx)
        else:
            analysis = {"ok": True, "symbol": symbol, "mode": mode, "instrument": inst, "decision": {"symbol": symbol, "exchange": inst.get("exchange") or "NSE", "mode": mode, "side": "NEUTRAL", "decision": "WATCH", "status": "INTELLIGENCE_PENDING", "score": 0, "reason": "Cached historical evidence is not ready yet. Chart and intelligence will hydrate from local candles first; Upstox refresh is running in background.", "price_freshness": "cache pending", "last_ai_validation": now_iso(), "rr": None, "support": None, "resistance": None}}
        _mark("analyze_one")
        quote_payload = None
        if inst:
            def _selected_quote():
                # Selected stock gets a bounded quote refresh/cache read, independent of
                # the dashboard scan. live_quotes has a 4s watchdog and falls back to
                # persisted quotes, so it should not blank the Stock Intelligence card.
                payload = self.live_quotes(symbol, allow_cached=True)
                quotes = payload.get("quotes") or {}
                return quotes.get(symbol) or quotes.get(symbol.upper()) or next(iter(quotes.values()), None)
            quote_payload = self._safe_section("selected_stock_quote", _selected_quote, None)
            if isinstance(quote_payload, dict) and quote_payload.get("ltp") is not None:
                try:
                    dec = analysis.get("decision") if isinstance(analysis, dict) else None
                    if isinstance(dec, dict):
                        dec["ltp"] = quote_payload.get("ltp")
                        dec["change_pct"] = quote_payload.get("change_pct", dec.get("change_pct"))
                        dec["change_abs"] = quote_payload.get("rupee_change", dec.get("change_abs"))
                        dec["price_freshness"] = quote_payload.get("freshness") or dec.get("price_freshness")
                except Exception:
                    pass
        _mark("selected_quote")
        selected_stock_truth = self._selected_stock_truth(symbol, mode, inst, hist, analysis, required_candles, quote_payload if isinstance(quote_payload, dict) else None)
        # A selected-stock analysis is allowed to enter the canonical lifecycle only
        # through the same fail-closed admission authority used by the scanner. This
        # closes the UI gap where Stock Report could show a fully admitted decision
        # while Action & Risk remained permanently unavailable until the broad scan
        # happened to revisit that symbol. Research/WATCH/REJECT rows are never
        # promoted here; canonical_admission_policy remains the single gate.
        selected_canonical_submission = {"attempted": False, "admitted": False, "reason": "NO_ADMISSIBLE_SELECTED_DECISION"}
        selected_decision = analysis.get("decision") if isinstance(analysis, dict) else None
        if isinstance(selected_decision, dict) and selected_decision.get("symbol"):
            try:
                from core.canonical_admission_policy import evaluate_canonical_admission
                admission = evaluate_canonical_admission(selected_decision)
                selected_canonical_submission = {"attempted": True, "admitted": bool(admission.allowed), "reason": admission.reason}
                if admission.allowed:
                    self.store.save_decision(dict(selected_decision, source=selected_decision.get("source") or "selected_stock_hot_path"))
                    selected_canonical_submission["persisted"] = True
            except Exception as exc:
                selected_canonical_submission = {"attempted": True, "admitted": False, "reason": "CANONICAL_SUBMISSION_ERROR", "error": str(exc)[:180]}
                self.record_error("selected_stock_canonical_submission", str(exc))
        # Interactive search must never wait for the isolated Vibe/Qlib worker.
        # That worker can consume its full 90-second budget and concurrent calls
        # previously made Stock Intelligence take 273 seconds. The immediate
        # decision uses the same cached candles and native engines; heavy research
        # remains a background/ledger enrichment concern.
        research_key=f"{symbol.upper()}:{mode.lower()}"
        cached_research=self._research_result_cache.get(research_key)
        research_result = cached_research[1] if cached_research and time.time()-cached_research[0] < 86400 else {"ok": False, "status": "deferred", "summary": "Research enrichment is running in the isolated background worker.", "factors": [], "evidence": {}}
        decision_ledger = self._safe_section(
            "decision_ledger",
            lambda: self.decision_ledger.build_and_store(symbol=symbol, mode=mode, inst=inst, hist=hist, analysis=analysis, selected_truth=selected_stock_truth, candles=candles, quote_payload=quote_payload if isinstance(quote_payload, dict) else None, research_result=research_result),
            {"ok": False, "symbol": symbol, "mode": mode, "summary": "Decision ledger unavailable", "factors": [], "evidence": [], "contradictions": []}
        )
        if research_result.get("status") == "deferred":
            self._schedule_research_enrichment(symbol,mode,inst,hist,candles,selected_stock_truth,analysis,quote_payload)
        _mark("decision_ledger")

        # v36.9.15: mtf_trend() fans out 6 parallel historical fetches with its
        # own 12s internal budget. Blocking this endpoint on it was the direct
        # cause of Stock Intelligence sitting on "Loading" with every field
        # blank for up to 22s (client abort) even when quote/levels/decision
        # were already available. MTF now hydrates via a separate
        # /api/mtf-trend call the frontend fires in parallel; this endpoint
        # returns immediately with a pending placeholder instead.
        mtf = [{"tf": tf, "state": "pending", "reason": "loading via /api/mtf-trend"} for tf in ("1m","3m","5m","15m","30m","1H","4H","1D","1W","1M")]
        _mode_status = self._safe_section("mode_status", lambda: self.mode_try_status(symbol), {"symbol": symbol, "modes": []})
        _heatmap = self._safe_section("symbol_heatmap", self.heatmap_snapshot, self._pending_heatmap("pending"))
        _mark("mode_status_and_heatmap")
        _stage_ms["total"] = round((time.perf_counter() - _t0) * 1000, 1)
        self.event("INFO", "perf", f"symbol_market_intelligence timing: {symbol} {mode} refresh={bool(refresh)}", _stage_ms)
        return {
            "symbol": symbol,
            "mode": mode,
            "historical": {k: v for k, v in hist.items() if k != "candles"},
            "analysis": analysis,
            "fundamentals": ctx.get("fundamentals"),
            "market_structure": ctx.get("market_structure"),
            "volume_profile": ctx.get("volume_profile"),
            "orb": ctx.get("orb"),
            "heat_context": ctx.get("heat_context"),
            "mode_intelligence": ctx.get("mode_intelligence"),
            "price_action": ctx.get("price_action"),
            "discovery": (analysis.get("decision") or {}).get("discovery_buckets") if isinstance(analysis, dict) else [],
            "mtf_trend": mtf,
            "selected_stock_truth": selected_stock_truth,
            "selected_canonical_submission": selected_canonical_submission,
            "decision_ledger": decision_ledger,
            "research_adapter": research_result,
            "selected_quote": quote_payload if isinstance(quote_payload, dict) else None,
            "mode_status": _mode_status,
            "heatmap": _heatmap,
            "scanner": {"service": self.status.get("service"), "market_open": is_india_market_open(), "minutes_to_close": minutes_to_close(), "auth": self.status.get("auth"), "scanner": self.status},
            "selected_stock_refresh": {"requested": bool(refresh), "endpoint": "/api/market-intelligence?refresh=true", "status": "complete" if ((analysis.get("ok") or hist.get("ok")) and (not refresh_fund or (ctx.get("fundamentals") or {}).get("ok"))) else "degraded", "historical_status": hist.get("data_status") or ("ok" if hist.get("ok") else "failed"), "historical_message": hist.get("message") or hist.get("error"), "fundamental_status": (ctx.get("fundamentals") or {}).get("state"), "fundamental_source": (ctx.get("fundamentals") or {}).get("source")},
            "message": f"{APP_VERSION}: use Refresh Stock for quote/history/fundamentals/case refresh; main rows stay clean with no exchange/sector subtext.",
            "time": now_iso(),
        }

    def mtf_trend_for_symbol(self, symbol: str, refresh: bool = False) -> Dict[str, Any]:
        """v51: delegate -- see core/decision_engine_service.py::DecisionEngineService.mtf_trend_for_symbol"""
        return self.decision_engine.mtf_trend_for_symbol(
            symbol,
            first_instrument_fn=self._first_instrument,
            final_fallback_instrument_fn=final_fallback_instrument,
            safe_section_fn=self._safe_section,
            mtf_trend_fn=self.mtf_trend,
            refresh=refresh,
        )

