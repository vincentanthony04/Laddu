from __future__ import annotations

from runtime_shared import *


class RuntimeReadModelsMixin:
    """Health, dashboard, live quote, depth, heatmap and market read models."""

    def health_status_snapshot(self, timeout_seconds: float = 0.02) -> Dict[str, Any]:
        try:
            self._set_status("quant_research_plane", build_research_plane_status(INSTALL_DIR))
        except Exception:
            pass
        return bounded_runtime_health_snapshot(self, timeout_seconds)

    def snapshot_status(self) -> Dict[str, Any]:
        """v60: thread-safe read of self.status.

        self.status is a nested dict mutated from background loop threads
        (scan orchestration, market data refresh, delivery sync, ...) and
        read from HTTP handler threads building health/dashboard responses.
        Before this, `self.lock` was declared in __init__ but never
        actually used anywhere -- every read of self.status handed out the
        live mutable dict itself (e.g. health()'s `"scanner": host.status`),
        so json.dumps could walk a nested structure while another thread
        was writing to it mid-serialization: a torn read, and a plausible
        source of the "inconsistent panel data" class of bug. This follows
        the same lock-protected snapshot() pattern Supervisor and
        RateController already use.
        """
        with self.lock:
            return copy.deepcopy(self.status)

    def _set_status(self, key: str, value: Any) -> None:
        """v60: thread-safe write to a top-level self.status key. Pairs with
        snapshot_status(). Only covers the direct `self.status[...] = ...`
        assignments in this file (main.py) -- core/*.py services that hold
        a reference to the same status dict (scan orchestration, market
        data, reference data) still mutate nested keys directly without
        this lock; that's unchanged pre-existing behavior, not something
        this pass claims to fix. Narrowing the remaining gap is future
        work, not silently expanded scope here."""
        with self.lock:
            self.status[key] = value
            self.health_registry.publish_runtime(self.status, state="fresh")

    def health(self):
        """v51 (Cluster 8): delegate -- see core/system_health_service.py::SystemHealthService.health"""
        return self.system_health_service.health()

    def system_health(self) -> Dict[str, Any]:
        """v51 (Cluster 8): delegate -- see core/system_health_service.py::SystemHealthService.system_health"""
        return self.system_health_service.system_health()

    def _is_actionable_selected(self, d: Dict[str, Any]) -> bool:
        """v51: delegate -- see core/scan_orchestration_service.py::ScanOrchestrationService._is_actionable_selected"""
        return self.scan_orchestration._is_actionable_selected(d)

    def _safe_section(self, name: str, fn, fallback):
        try:
            return fn()
        except Exception as exc:
            self.record_error(name, str(exc))
            detail = {"error": str(exc)}
            if name == "symbol_market_context":
                detail["trace"] = __import__("traceback").format_exc(limit=5)
            self.event("WARN", name, "Dashboard section failed; returning partial state", detail)
            return fallback

    def _visible_api_errors(self) -> list[Dict[str, Any]]:
        """v51 (Cluster 8): delegate -- see core/system_health_service.py::SystemHealthService.visible_api_errors"""
        return self.system_health_service.visible_api_errors()

    def health_light(self) -> Dict[str, Any]:
        """v51 (Cluster 8): delegate -- see core/system_health_service.py::SystemHealthService.health_light"""
        return self.system_health_service.health_light()

    def _fund_cache_lookup(self, instrument: Dict[str, Any] | None) -> Dict[str, Any] | None:
        """v51 (Cluster 7): delegate -- see core/reference_data_service.py::ReferenceDataService._fund_cache_lookup"""
        return self.reference_data._fund_cache_lookup(instrument)

    def _schedule_fundamental_prefetch(self, instrument: Dict[str, Any] | None) -> None:
        """v51 (Cluster 7): delegate -- see core/reference_data_service.py::ReferenceDataService._schedule_fundamental_prefetch"""
        return self.reference_data._schedule_fundamental_prefetch(instrument)

    def _resolve_sector_key(self, d: Dict[str, Any]) -> str:
        """v51 (Cluster 7): delegate -- see core/reference_data_service.py::ReferenceDataService._resolve_sector_key"""
        return self.reference_data._resolve_sector_key(d)

    def _sector_context_for_row(self, d: Dict[str, Any], heatmap: list[Dict[str, Any]] | None = None) -> Dict[str, Any]:
        """v51 (Cluster 7): delegate -- see core/reference_data_service.py::ReferenceDataService._sector_context_for_row"""
        return self.reference_data._sector_context_for_row(d, heatmap)

    def _card_project(self, d):
        return self.dashboard._card_project(d)

    def _compact_card_project(self, d):
        return self.dashboard._compact_card_project(d)

    def _selected_fallback_from_decisions(self, rows: list[Dict[str, Any]], mode: str = "all") -> list[Dict[str, Any]]:
        """Selected Candidates is strict: final/promoted/actionable only.
        WATCH/WAIT/BEST_AVAILABLE/SETUP rows must stay in Potential Candidates or Watch Queue.
        """
        return []

    def _group_opportunity_rows(self, rows: list[Dict[str, Any]]) -> list[Dict[str, Any]]:
        grouped: Dict[str, Dict[str, Any]] = {}
        stage_rank = {"Selected": 0, "Armed": 1, "Qualified": 2, "Potential": 3, "Watch": 4}
        for row in rows:
            d = self._normalize_opportunity_case(row)
            sym = str(d.get("symbol") or "").upper()
            if not sym:
                continue
            cur = grouped.get(sym)
            if not cur:
                d["mode_fit"] = [d.get("mode")] if d.get("mode") else []
                grouped[sym] = d
                continue
            cur_modes = set(cur.get("mode_fit") or [])
            if d.get("mode"):
                cur_modes.add(d.get("mode"))
            cur["mode_fit"] = sorted(cur_modes)
            better = (stage_rank.get(str(d.get("opportunity_stage") or d.get("candidate_stage") or "Potential"), 9), -(int(d.get("priority_score") or d.get("score") or 0))) < (stage_rank.get(str(cur.get("opportunity_stage") or cur.get("candidate_stage") or "Potential"), 9), -(int(cur.get("priority_score") or cur.get("score") or 0)))
            if better:
                d["mode_fit"] = cur["mode_fit"]
                grouped[sym] = d
        out = list(grouped.values())
        out.sort(key=lambda x: (stage_rank.get(str(x.get("opportunity_stage") or x.get("candidate_stage") or "Potential"), 9), -(int(x.get("priority_score") or x.get("score") or 0)), str(x.get("symbol") or "")))
        return out

    def _cache_live_quote_state(self, fresh_for_progress: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
        """Update only memory/cache projections from canonical live quotes.

        Browser/read routes may call this helper because it performs no DB,
        research, settlement or Model Paper mutation. All decision-side effects
        are owned by DecisionQuoteProjectionService off the HTTP path.
        """
        if not fresh_for_progress:
            return {}
        accepted: Dict[str, Dict[str, Any]] = {}
        for symbol, value in fresh_for_progress.items():
            if not isinstance(value, dict):
                continue
            norm = symbolKey_py(symbol)
            current = self.market_data._quote_delta_cache.get(norm)
            if newer_quote(current, value):
                accepted[norm] = dict(value)
        if accepted:
            self.market_data._quote_delta_cache.update(accepted)
            self.market_data._quote_delta_cache_ts = time.time()
            for symbol, value in accepted.items():
                self._coverage_quote_cache[symbol] = dict(value, radar_source="canonical_live_market_gateway", _coverage_seen_at=now_iso())
        return accepted

    def _advance_live_quote_state(self, fresh_for_progress: Dict[str, Dict[str, Any]], *, admit_final: bool = True) -> Dict[str, Any]:
        """Compatibility wrapper: cache only; no decision mutation on reads."""
        accepted = self._cache_live_quote_state(fresh_for_progress)
        return {"ok": True, "observed": len(accepted), "delegated": "decision_quote_projection"}

    def live_deltas(self, since: int = 0, symbols_csv: str = "") -> Dict[str, Any]:
        """Serve the browser from stream memory, then bounded verified fallback.

        The live WebSocket is the preferred authority, but a disconnected or
        after-hours stream must not leave Stock Report empty. When the requested
        symbols are absent from stream memory, reuse the canonical verified
        REST/cache quote path and ingest those observations into the same quote
        store. This keeps one identity/freshness contract while avoiding a
        circular dependency between intelligence and quote acquisition.
        """
        market_open_now = is_india_market_open()
        payload = self.live_market.browser_deltas(
            since=since, symbols_csv=symbols_csv, market_open=market_open_now
        )
        requested = [
            value.strip().upper()
            for value in str(symbols_csv or "").split(",")
            if value.strip()
        ][:60]
        missing = [symbol for symbol in requested if symbol not in (payload.get("quotes") or {})]
        fallback_error = None
        if missing:
            try:
                # Browser polling is a read-model path.  It must never wait for a
                # provider request across dozens of visible symbols.  Reconcile
                # canonical stream memory with verified local runtime/cache state
                # synchronously, then queue one bounded revalidation in the
                # background when gaps remain.
                fallback = self.live_quotes(",".join(missing), allow_cached=True, network_refresh=False)
                fallback_quotes = dict(fallback.get("quotes") or {})
                if fallback_quotes:
                    merged = dict(payload.get("quotes") or {})
                    merged.update(fallback_quotes)
                    payload["quotes"] = merged
                    payload["live_count"] = sum(
                        1 for row in merged.values()
                        if str((row or {}).get("freshness_state") or "") == "live"
                    )
                    payload["closed_count"] = sum(
                        1 for row in merged.values()
                        if str((row or {}).get("freshness_state") or "") == "closed_market"
                    )
                    payload["served_from"] = str(fallback.get("served_from") or "verified_local_quote_authority")
                unresolved = [symbol for symbol in missing if symbol not in fallback_quotes]
                if unresolved and fallback.get("ok") is False:
                    fallback_error = str(fallback.get("error") or fallback.get("message") or "verified quote projection pending")
            except Exception as exc:
                fallback_error = str(exc)[:240]
                self.record_error("live_browser_fallback", fallback_error)
        payload["fallback_error"] = fallback_error
        payload["retry_state"] = "automatic retry active" if fallback_error else "stream and verified local quote polling active"
        return payload

    def _schedule_browser_quote_revalidation(self, symbols: list[str]) -> None:
        """Queue one non-blocking visible-symbol revalidation.

        The browser endpoint remains cache-only while this daemon worker uses the
        existing single-flight quote path.  A ten-second gate prevents a failed
        provider from creating one thread per poll.
        """
        requested = [str(value or "").upper().strip() for value in symbols if str(value or "").strip()][:30]
        if not requested:
            return
        now_value = time.time()
        active = getattr(self, "_browser_quote_revalidation_thread", None)
        last_at = float(getattr(self, "_browser_quote_revalidation_at", 0.0) or 0.0)
        if (active is not None and active.is_alive()) or now_value - last_at < 10.0:
            return
        self._browser_quote_revalidation_at = now_value

        def _run() -> None:
            try:
                self.live_quotes(",".join(requested), allow_cached=True, network_refresh=True)
            except Exception as exc:
                self.record_error("live_browser_revalidation", str(exc)[:240])

        worker = threading.Thread(target=_run, name="ProjectLadduBrowserQuoteRevalidation", daemon=True)
        self._browser_quote_revalidation_thread = worker
        worker.start()

    def selected_market_depth(self, symbol: str, *, refresh: bool = False) -> Dict[str, Any]:
        """Projection-only selected-stock depth read.

        Rich feed acquisition is requested explicitly through the POST
        /api/live-market/subscriptions producer command.  This GET only reads the
        canonical stream (or a previously materialized fallback cache) and never
        starts provider work or changes subscription state.
        """
        symbol = str(symbol or "").strip().upper()
        if not symbol:
            return {"ok": False, "state": "BAD_REQUEST", "message": "symbol required"}
        inst = self._index_instrument_for_chart(symbol)
        if not inst:
            inst = self._first_instrument(symbol, force_refresh=False)
        if not inst or not inst.get("instrument_key"):
            return {"ok": False, "state": "IDENTITY_UNAVAILABLE", "symbol": symbol}
        key = str(inst.get("instrument_key"))
        market_open_now = is_india_market_open()
        stream_rows = self.live_market.quotes.snapshot([symbol], market_open=market_open_now, max_age_sec=8.0)
        stream_quote = dict(stream_rows.get(symbol) or {})
        stream_depth = dict(stream_quote.get("depth") or {})
        if (stream_depth.get("buy") or stream_depth.get("sell")) and (
            not market_open_now or stream_quote.get("freshness_state") == "live"
        ):
            return {
                "ok": True, "state": "READY", "symbol": symbol,
                "instrument": inst, "quote": stream_quote, "cached": False,
                "served_from": "upstox_v3_full_stream",
                "stream": self.live_market.status(), "market_open": market_open_now,
                "read_model_policy": "projection_only", "time": now_iso(),
            }
        cached = {}
        cache = getattr(self, "_selected_depth_cache", {}) or {}
        if isinstance(cache, dict):
            cached = dict(cache.get(key) or {})
        quote = dict(cached.get("quote") or {})
        if quote:
            return {
                "ok": True, "state": "READY", "symbol": symbol,
                "instrument": inst, "quote": quote, "cached": True,
                "served_from": "retained_depth_projection",
                "stream": self.live_market.status(), "market_open": market_open_now,
                "read_model_policy": "projection_only", "time": now_iso(),
            }
        return {
            "ok": True, "state": "WARMING", "symbol": symbol,
            "instrument": inst, "quote": {}, "retry_after_sec": 1,
            "served_from": "stream_projection_pending",
            "stream": self.live_market.status(), "market_open": market_open_now,
            "message": "Depth projection is warming; use the explicit live-subscription producer command to request rich feed.",
            "read_model_policy": "projection_only_no_provider_or_subscription_mutation",
            "time": now_iso(),
        }

    def live_quotes(self, symbols_csv: str, allow_cached: bool = True, network_refresh: bool = True) -> Dict[str, Any]:
        """Dedicated quote-delta endpoint for visible rows/odometer.

        Contract: return old_ltp/new_ltp/changed/source_time/received_time/stale for
        every visible quote so the frontend can animate changed digits without a
        destructive table rebuild and can mark stale values explicitly.
        """
        syms: list[str] = []
        seen = set()
        for s in (symbols_csv or "").split(","):
            s = s.strip().upper()
            if s and s not in seen:
                seen.add(s)
                syms.append(s)
        if not syms:
            return {"ok": True, "quotes": {}, "time": now_iso(), "received_time": now_iso()}

        received_time = now_iso()
        market_open_now = is_india_market_open()

        stream_rows = self.live_market.quotes.snapshot(syms, market_open=market_open_now, max_age_sec=8.0)
        stream_status = self.live_market.status()
        if stream_rows and (stream_status.get("connected") or all(symbol in stream_rows for symbol in syms)):
            accepted = self._cache_live_quote_state({k: v for k, v in stream_rows.items() if v.get("usable_for_promotion")})
            progress = {"ok": True, "observed": len(accepted), "delegated": "decision_quote_projection"}
            live_count = sum(1 for row in stream_rows.values() if row.get("freshness_state") == "live")
            closed_count = sum(1 for row in stream_rows.values() if row.get("freshness_state") == "closed_market")
            with self.lock:
                self.status.setdefault("quote_delta", {}).update({"state": "live" if live_count else "closed_market" if closed_count else "degraded", "symbols": len(stream_rows), "live_symbols": live_count, "closed_symbols": closed_count, "last_run": received_time, "served_from": "upstox_v3_canonical_stream", "next_run": "tick-driven"})
            return {"ok": True, "quotes": stream_rows, "served_from": "upstox_v3_canonical_stream", "live_count": live_count, "closed_count": closed_count, "market_open": market_open_now, "stream": stream_status, "progress": progress, "time": received_time, "received_time": received_time}

        # v36.9 quote-delta cache: frontend can poll fast for odometer updates
        # without forcing a broker HTTP request every 750ms. If all requested
        # symbols are already in the short cache, serve that cache immediately.
        cache_age = time.time() - float(self.market_data._quote_delta_cache_ts or 0.0)
        cache_ttl = 2.5 if market_open_now else 30.0
        if allow_cached and self.market_data._quote_delta_cache and cache_age <= cache_ttl:
            cached_subset = {}
            expected_state = "live" if market_open_now else "closed_market"
            for symbol in syms:
                cached_value = self.market_data._quote_delta_cache.get(symbol)
                if not cached_value:
                    continue
                checked = revalidate_cached_quote(
                    cached_value, now=india_now(), market_open=market_open_now
                )
                if str(checked.get("freshness_state") or "") == expected_state and not checked.get("stale"):
                    cached_subset[symbol] = checked
            if len(cached_subset) == len(syms):
                live_count = sum(1 for row in cached_subset.values() if row.get("freshness_state") == "live")
                closed_count = sum(1 for row in cached_subset.values() if row.get("freshness_state") == "closed_market")
                with self.lock:
                    self.status.setdefault("quote_delta", {}).update({"state": expected_state, "symbols": len(cached_subset), "live_symbols": live_count, "closed_symbols": closed_count, "last_run": received_time, "served_from": "revalidated_quote_delta_cache", "cache_age_ms": int(cache_age*1000), "next_run": "broker fetch throttled"})
                return {"ok": True, "quotes": cached_subset, "served_from": "revalidated_quote_delta_cache", "cache_age_ms": int(cache_age*1000), "live_count": live_count, "closed_count": closed_count, "market_open": market_open_now, "time": received_time, "received_time": received_time}
        try:
            runtime_previous_rows = self.runtime_market_state.latest_quotes(syms)
        except Exception as exc:
            runtime_previous_rows = []
            self.record_error("runtime_quote_read", str(exc))
        previous = {
            str(row.get("symbol") or "").upper(): dict(row)
            for row in runtime_previous_rows
            if str(row.get("symbol") or "").strip()
        }
        # Legacy operational rows are read only when the isolated runtime plane
        # has no current-session observation for a requested symbol.
        legacy_previous = self.store.latest_quotes_by_symbol(
            [symbol for symbol in syms if symbol not in previous]
        )
        previous.update(legacy_previous)

        def payload_for(sym: str, q: Dict[str, Any] | None, stale: bool = False) -> Dict[str, Any] | None:
            q = dict(q or {})
            prev = previous.get(str(sym or "").upper()) or {}
            ltp = q.get("ltp")
            if ltp is None:
                return None
            old_ltp = prev.get("ltp")
            try:
                changed = old_ltp is not None and float(old_ltp) != float(ltp)
            except Exception:
                changed = False
            close = q.get("previous_close") or q.get("close") or prev.get("close")
            rupee = q.get("rupee_change")
            if rupee is None and ltp is not None and close:
                try:
                    rupee = round(float(ltp) - float(close), 2)
                except Exception:
                    rupee = None
            integrity = classify_quote(
                q, now=india_now(), market_open=market_open_now, max_live_age_sec=45.0
            )
            if stale:
                integrity = {**integrity, "state": "stale", "reason": "last_known_database_fallback",
                             "usable_for_promotion": False, "display_as_live": False}
            source_time = integrity.get("source_time") or q.get("received_at") or prev.get("timestamp")
            state = str(integrity.get("state") or "unverified")
            return {
                "symbol": sym, "instrument_key": q.get("instrument_key"),
                "ltp": ltp, "old_ltp": old_ltp, "new_ltp": ltp, "changed": changed,
                "change_pct": q.get("change_pct"), "open": q.get("open"), "high": q.get("high"),
                "low": q.get("low"), "previous_close": close, "session_close": q.get("session_close"),
                "rupee_change": rupee, "change_source": q.get("change_source"),
                "timestamp": source_time, "source_time": source_time, "received_time": received_time,
                "freshness": state.replace("_", " ") + (f" @ {source_time}" if source_time else ""),
                "freshness_state": state, "freshness_reason": integrity.get("reason"),
                "age_seconds": integrity.get("age_seconds"),
                "identity_verified": bool(integrity.get("identity_verified")),
                "usable_for_promotion": bool(integrity.get("usable_for_promotion")),
                "display_as_live": bool(integrity.get("display_as_live")),
                "provider_timestamp_verified": bool(integrity.get("provider_timestamp_verified")),
                "stale": state not in ("live", "closed_market"),
                "quote_seq": int(time.time() * 1000), "source": q.get("source") or "quote_cache",
            }

        if not network_refresh:
            local = dict(previous)
            local.update({
                symbol: self.market_data._quote_delta_cache[symbol]
                for symbol in syms
                if symbol in self.market_data._quote_delta_cache
            })
            out = {}
            for symbol in syms:
                projected = payload_for(symbol, local.get(symbol), stale=False)
                if projected and projected.get("identity_verified") is True and not projected.get("stale"):
                    out[symbol] = projected
            live_count = sum(1 for row in out.values() if row.get("freshness_state") == "live")
            closed_count = sum(1 for row in out.values() if row.get("freshness_state") == "closed_market")
            ready = live_count > 0 if market_open_now else closed_count > 0
            return {
                "ok": ready,
                "quotes": out,
                "live_count": live_count,
                "closed_count": closed_count,
                "market_open": market_open_now,
                "served_from": "verified_local_runtime_or_quote_cache",
                "error": None if ready else "verified local quote unavailable",
                "time": received_time,
                "received_time": received_time,
            }

        if time.time() < self.quote_blocked_until:
            cached = dict(previous)
            cached.update({
                symbol: self.market_data._quote_delta_cache[symbol]
                for symbol in syms
                if symbol in self.market_data._quote_delta_cache
            })
            out = {sym: p for sym, q in cached.items() if (p := payload_for(sym, q, stale=True))}
            return {"ok": True, "quotes": out, "rate_limited": True, "live_count": 0, "closed_count": 0, "market_open": market_open_now, "next_run": f"after {int(max(1, self.quote_blocked_until - time.time()))}s", "time": received_time, "received_time": received_time}

        token = self.client.token_status()
        if not token.get("ok"):
            cached = dict(previous)
            cached.update({
                symbol: self.market_data._quote_delta_cache[symbol]
                for symbol in syms
                if symbol in self.market_data._quote_delta_cache
            })
            out = {sym: p for sym, q in cached.items() if (p := payload_for(sym, q, stale=True))}
            return {"ok": False, "error": "token_not_configured", "quotes": out, "live_count": 0, "closed_count": 0, "market_open": market_open_now, "time": received_time, "received_time": received_time}

        # v37.4: was ~35 lines of inline resolution duplicated from
        # _first_instrument/_index_instrument_for_chart, including the
        # unconditional-full-table-scan + no-negative-cache bugs described in
        # core/instrument_resolver.py. Now shares one resolver, one cache,
        # one fix, with every other symbol-resolution call site.
        insts: list[Dict[str, Any]] = []
        display_by_key: Dict[str, str] = {}
        for s in syms[:30]:
            is_index = "NIFTY" in s or "SENSEX" in s
            inst = self._index_instrument_for_chart(s) if is_index else self.instrument_resolver.resolve(s)
            if inst:
                insts.append(inst)
                if inst.get("instrument_key"):
                    display_by_key[inst["instrument_key"]] = s

        out: Dict[str, Any] = {}
        fresh_for_progress: Dict[str, Dict[str, Any]] = {}
        quote_error = None
        qs: list[Dict[str, Any]] = []
        if insts:
            keys = tuple(sorted(str(i.get("instrument_key") or "") for i in insts if i.get("instrument_key")))
            future = None
            created = False
            with self.market_data._quote_refresh_lock:
                active = self.market_data._quote_refresh_future
                if active is not None and not active.done():
                    active_keys = set(self.market_data._quote_refresh_keys or ())
                    if set(keys).issubset(active_keys):
                        future = active
                    else:
                        quote_error = "quote_refresh_inflight_for_other_symbols"
                else:
                    future = self.market_data._quote_executor.submit(self.client.full_quotes, insts, persist=False)
                    self.market_data._quote_refresh_future = future
                    self.market_data._quote_refresh_keys = keys
                    created = True
            if future is not None:
                try:
                    # The old 1.5s watchdog was shorter than urllib's 3s minimum
                    # and guaranteed stale fallback.  One verified single-flight
                    # request now gets a bounded but realistic budget.
                    qs = future.result(timeout=5.5)
                except _FutTimeout:
                    quote_error = "verified_quote_fetch_timeout_5500ms"
                    self.event("WARN", "quote", "Verified quote refresh exceeded 5.5s; last-known values will be labelled stale", {"symbol_count": len(insts), "single_flight": True})
                except Exception as exc:
                    quote_error = str(exc)
                    self.record_error("quote", quote_error, "/v2/market-quote/quotes")
                finally:
                    if future.done():
                        with self.market_data._quote_refresh_lock:
                            if self.market_data._quote_refresh_future is future:
                                self.market_data._quote_refresh_future = None
                                self.market_data._quote_refresh_keys = ()
            if qs:
                for inst in insts:
                    if inst.get("instrument_key"):
                        self.live_market.register_identity(inst.get("instrument_key"), display_by_key.get(inst.get("instrument_key")) or inst.get("trading_symbol") or inst.get("symbol"))
                self.live_market.ingest_http_snapshot(qs)
                # The bounded runtime plane is the restart/LKG owner for current
                # quotes. Operational SQLite receives only the slower audit
                # cadence below, so browser/scanner traffic cannot grow or lock
                # the canonical decision database.
                try:
                    self.runtime_market_state.save_latest_quotes(qs)
                except Exception as exc:
                    self.record_error("runtime_quote_persist", str(exc))
            for q in qs:
                key = str(q.get("instrument_key") or "")
                sym = display_by_key.get(key) or str(q.get("symbol") or "").upper()
                sym = str(sym or "").upper()
                p = payload_for(sym, q, stale=False)
                if sym and p:
                    out[sym] = p
                    raw_sym = str(q.get("symbol") or "").upper()
                    if raw_sym and raw_sym != sym:
                        out[raw_sym] = p
                    if p.get("usable_for_promotion"):
                        fresh_for_progress[sym] = p
            # Restart/LKG recovery must retain the newest verified provider
            # observation. Persist at a bounded cadence rather than on every
            # 3-second browser tick.
            if qs and time.time() - float(getattr(self, "_last_verified_quote_persist_at", 0.0) or 0.0) >= 15.0:
                try:
                    self.store.save_quotes(qs)
                    self._last_verified_quote_persist_at = time.time()
                except Exception as exc:
                    self.record_error("quote_persist", str(exc))
            # A successful broker response that omits a requested token usually
            # means the persisted instrument identity is obsolete.  Repair the
            # resolver cache now and retry on the next 3-second tick; never bind
            # by a similar symbol name or display the old DB price as live.
            if quote_error is None:
                returned_keys = {str(q.get("instrument_key") or "") for q in qs}
                for missing_key, display_symbol in list(display_by_key.items()):
                    if missing_key in returned_keys:
                        continue
                    try:
                        repaired = (self._index_instrument_for_chart(display_symbol, force_refresh=True)
                                    if ("NIFTY" in display_symbol or "SENSEX" in display_symbol)
                                    else self.instrument_resolver.resolve(display_symbol, force_refresh=True))
                        if repaired and repaired.get("instrument_key") != missing_key:
                            self.event("WARN", "quote_identity", "Instrument identity repaired; verified quote will retry next tick", {"symbol": display_symbol, "old_key": missing_key, "new_key": repaired.get("instrument_key")})
                    except Exception as exc:
                        self.record_error("quote_identity", str(exc))

        missing = [s for s in syms if s not in out]
        if missing:
            # Recover current-session LKG from the isolated runtime database
            # before consulting the slower operational audit store.
            try:
                runtime_rows = self.runtime_market_state.latest_quotes(missing)
            except Exception as exc:
                runtime_rows = []
                self.record_error("runtime_quote_read", str(exc))
            for q in runtime_rows:
                sym = str(q.get("symbol") or "").upper().strip()
                if sym and sym in missing:
                    p = payload_for(sym, q, stale=True)
                    if p:
                        out[sym] = p
            missing = [s for s in syms if s not in out]
        if missing:
            cached = self.store.latest_quotes_by_symbol(missing)
            for sym, q in cached.items():
                p = payload_for(sym, q, stale=True)
                if p:
                    out[sym] = p

        if out:
            try:
                accepted = {}
                for key, value in out.items():
                    if not isinstance(value, dict):
                        continue
                    norm = symbolKey_py(key)
                    current = self.market_data._quote_delta_cache.get(norm)
                    if newer_quote(current, value):
                        accepted[norm] = dict(value)
                self.market_data._quote_delta_cache.update(accepted)
                coverage_cache = getattr(self, "_coverage_quote_cache", None)
                if isinstance(coverage_cache, dict):
                    for symbol, value in accepted.items():
                        if str(value.get("freshness_state") or "") not in ("live", "closed_market") or value.get("stale"):
                            continue
                        previous_coverage = dict(coverage_cache.get(symbol) or {})
                        coverage_cache[symbol] = dict(
                            previous_coverage,
                            **value,
                            radar_source="verified_visible_quote_refresh",
                            _coverage_seen_at=received_time,
                        )
                # Never make an old database fallback look newly fresh merely
                # because the fallback was read again.
                verified = [v for v in accepted.values() if str(v.get("freshness_state") or "") in ("live", "closed_market") and not v.get("stale")]
                if verified:
                    self.market_data._quote_delta_cache_ts = time.time()
                with self.lock:
                    live_count = sum(1 for v in out.values() if isinstance(v, dict) and v.get("freshness_state") == "live")
                    closed_count = sum(1 for v in out.values() if isinstance(v, dict) and v.get("freshness_state") == "closed_market")
                    quote_state = "live" if live_count and quote_error is None else "closed_market" if closed_count and not market_open_now and quote_error is None else "degraded"
                    self.status.setdefault("quote_delta", {}).update({"state": quote_state, "symbols": len(out), "live_symbols": live_count, "closed_symbols": closed_count, "last_run": received_time, "served_from": "verified_broker_snapshot_or_labelled_lkg", "error": quote_error, "next_run": "3s market-open / on visible request"})
            except Exception:
                pass
        # Read-model boundary: the quote endpoint is not a decision, ledger,
        # research or Model-Paper execution authority.  Earlier releases closed
        # canonical signals and marked paper positions from this browser-facing
        # method; a slow settlement/research write could therefore make a quote
        # request (and the entire Stock Report) stall.  All mutation now runs in
        # isolated background owners: DecisionQuoteProjectionService for
        # non-risk quote evidence/admission and DeskPositionLifecycleAuthority
        # for open Model-Paper position risk/settlement.
        accepted = self._cache_live_quote_state(fresh_for_progress)
        if accepted:
            self._set_status("last_price_refresh", received_time)
        live_count = sum(1 for value in out.values() if isinstance(value, dict) and value.get("freshness_state") == "live")
        closed_count = sum(1 for value in out.values() if isinstance(value, dict) and value.get("freshness_state") == "closed_market")
        return {
            "ok": quote_error is None and (live_count > 0 if market_open_now else closed_count > 0),
            "quotes": out, "quote_error": quote_error, "live_count": live_count,
            "closed_count": closed_count, "market_open": market_open_now,
            "decision_projection": {"state": "BACKGROUND", "owner": "decision_quote_projection", "observed": len(accepted)},
            "risk_settlement": {"state": "BACKGROUND", "owner": "desk_position_lifecycle"},
            "time": received_time, "received_time": received_time,
        }

    def product_readiness(self) -> Dict[str, Any]:
        """Installed-product truth gate; cache-only and failure-first."""
        return self.product_readiness_service.assess()

    def dashboard_cards_data(self, mode: str = "all") -> Dict[str, Any]:
        return self.dashboard.dashboard_cards_data(mode)

    def dashboard_data(self, mode: str = "all") -> Dict[str, Any]:
        return self.dashboard.dashboard_data(mode)

    def _pending_heatmap(self, reason: str = "pending"):
        return self.dashboard._pending_heatmap(reason)

    def _watch_projection(self, d):
        return self.dashboard._watch_projection(d)

    def mode_try_status(self, symbol: str) -> Dict[str, Any]:
        """Return readiness for the two production desks only."""
        out = []
        symbol = (symbol or "").upper().strip()
        stock_inst = self._first_instrument(symbol)
        for mode in ("intraday", "delivery"):
            eng = ENGINES[mode]
            if not stock_inst:
                out.append({"mode": mode, "state": "BLOCKED", "reason": "Exact stock instrument unresolved"})
                continue
            if mode == "intraday" and not is_india_market_open():
                out.append({"mode": mode, "state": "PAUSED", "reason": "Intraday validation runs during NSE market hours"})
                continue
            candles = self._stored_candles(stock_inst.get("instrument_key"), eng.candle_interval, limit=max(eng.days * 5, 120))
            required = 50 if mode == "intraday" else 120
            state = "READY" if len(candles) >= required else "WARMING"
            reason = f"{len(candles)}/{required} cached candles; production policy {POLICY_VERSION}"
            out.append({"mode": mode, "state": state, "reason": reason, "candle_count": len(candles), "required_candles": required})
        return {"symbol": symbol, "modes": out, "allowed_modes": ["intraday", "delivery"], "production_policy_version": POLICY_VERSION}

    def heatmap_snapshot(self):
        """Instant, UI-safe heat strip. Never does network or instrument refresh.

        If no live heatmap is ready, return a stable pending strip instead of blocking dashboard-state.
        """
        if self._heatmap_cache:
            return [dict(row) for row in self._heatmap_cache]
        # Persisted heat is loaded by operator_read_models/market-radar workers.
        # HTTP routes never query SQLite merely to paint a dashboard strip.
        return self._pending_heatmap("warming up / background projection pending")

    def _heat_cache_by_name(self) -> Dict[str, Any]:
        return {str(x.get("name") or "").upper(): x for x in (self._heatmap_cache or [])}

    def _index_historical_row(self, inst: Dict[str, Any], name: str) -> Dict[str, Any] | None:
        instrument_key = str(inst.get("instrument_key") or "").strip()
        if not instrument_key:
            return None
        try:
            # v69.9.12: market/sector direction is cache-first and exact-identity
            # bound. A provider-specific index history failure must not trigger
            # repeated HTTP 400 calls on every dashboard refresh. Stored
            # completed candles remain authoritative; the TTL bad-key registry
            # only suppresses the unavailable network refresh, never the cache.
            # Index history is read-only market context. Provider support for
            # several index keys is inconsistent and repeated 400s were consuming
            # scanner capacity every dashboard cycle. Use verified stored closes
            # only; exact-gap workers may populate them independently.
            candles = self._stored_candles(instrument_key, "day", limit=12) or []
            vals = [c.get("close") for c in candles if c.get("close") is not None]
            if len(vals) >= 2:
                prev = float(vals[-2]); last = float(vals[-1])
                chg = round(((last - prev) / prev) * 100, 2) if prev else None
                state = "neutral" if chg is None else "green" if chg > 0.05 else "red" if chg < -0.05 else "neutral"
                candle_time = candles[-1].get("timestamp") if candles else now_iso()
                source_dt = _parse_ts_datetime(candle_time)
                if source_dt is not None:
                    from core.trading_session_authority import DEFAULT_TRADING_SESSION_AUTHORITY
                    source_window = DEFAULT_TRADING_SESSION_AUTHORITY.session_window(source_dt.date())
                    source_time = source_window.close_at().isoformat(timespec="seconds") if source_window is not None else candle_time
                else:
                    source_time = candle_time
                expected_date = CandleFreshnessService.expected_daily_date(india_now())
                is_latest_close = bool(source_dt and source_dt.date().isoformat() == expected_date)
                freshness_state = "current_at_close" if not is_india_market_open() and is_latest_close else "stale"
                return {"name": name, "state": state, "change_pct": chg, "rupee_change": round(last - prev, 2), "previous_close": prev, "last_refresh": source_time, "timestamp": source_time, "source_time": source_time, "ltp": last, "instrument_key": inst.get("instrument_key"), "trading_symbol": inst.get("trading_symbol") or name, "display_name": inst.get("name") or inst.get("display_name") or name, "chart_query": (inst.get("name") or inst.get("display_name") or name), "exchange": inst.get("exchange") or "NSE", "segment": inst.get("segment") or "NSE_INDEX", "source": "historical_last_close", "freshness": (f"CURRENT AT CLOSE · {source_time}" if freshness_state == "current_at_close" else f"STALE · {source_time}"), "freshness_state": freshness_state, "freshness_reason": "latest verified completed session" if freshness_state == "current_at_close" else "latest expected completed session is missing", "session_result_verified": True, "identity_verified": True, "identity_resolved": True, "stale": freshness_state == "stale", "usable_for_promotion": False}
        except Exception as exc:
            self.record_error("index_historical", str(exc), "/v3/historical-candle")
        return None

    def _use_cached_heat(self, name: str, reason: str) -> Dict[str, Any] | None:
        old = self._heat_cache_by_name().get(str(name).upper())
        if old and old.get("change_pct") is not None:
            cached = dict(old)
            cached["source"] = cached.get("source") or "last_known_cache"
            cached["reason"] = reason
            source_dt = _parse_ts_datetime(cached.get("source_time") or cached.get("timestamp"))
            expected_date = CandleFreshnessService.expected_daily_date(india_now())
            current_at_close = bool(not is_india_market_open() and source_dt and source_dt.date().isoformat() == expected_date and cached.get("ltp") is not None)
            cached["freshness_state"] = "current_at_close" if current_at_close else "stale"
            cached["freshness_reason"] = "latest verified completed session" if current_at_close else reason
            cached["freshness"] = ("CURRENT AT CLOSE" if current_at_close else "LKG only · completed close missing")
            cached["identity_verified"] = bool(cached.get("instrument_key") or cached.get("identity_verified"))
            cached["session_result_verified"] = bool(
                cached.get("ltp") is not None
                and cached.get("previous_close") is not None
                and cached.get("change_pct") is not None
            )
            cached["stale"] = not current_at_close
            cached["usable_for_promotion"] = False
            return cached
        return None

    def heatmap(self):
        base_rows = [
            {"name": "NIFTY", "queries": ["NIFTY 50", "NIFTY"]},
            {"name": "NXT50", "queries": ["NIFTY NEXT 50", "NIFTY NXT 50"]},
            {"name": "N100", "queries": ["NIFTY 100"]},
            {"name": "N200", "queries": ["NIFTY 200"]},
            {"name": "N500", "queries": ["NIFTY 500"]},
            {"name": "MIDCAP", "queries": ["NIFTY MIDCAP 100"]},
            {"name": "SMALLCAP", "queries": ["NIFTY SMALLCAP 100"]},
            {"name": "SENSEX", "queries": ["SENSEX", "S&P BSE SENSEX"]},
            {"name": "VIX", "queries": ["INDIA VIX", "INDIAVIX"]},
            {"name": "BANK", "queries": ["NIFTY BANK", "BANKNIFTY"]},
            {"name": "IT", "queries": ["NIFTY IT"]},
            {"name": "PHARMA", "queries": ["NIFTY PHARMA"]},
            {"name": "AUTO", "queries": ["NIFTY AUTO"]},
            {"name": "METAL", "queries": ["NIFTY METAL"]},
            {"name": "FMCG", "queries": ["NIFTY FMCG"]},
            {"name": "PSUBANK", "queries": ["NIFTY PSU BANK"]},
            # v31.1: kept to sector indices that actually feed decision scoring via
            # sector_hint_from_symbol -> heat_strip_context. Broad-cap (100/200/500/NEXT50/
            # MIDCAP/SMALLCAP) and BSE/thematic rows were removed - they never influenced any
            # trade decision, just took up chips on the heat strip.
            {"name": "REALTY", "queries": ["NIFTY REALTY"]},
            {"name": "ENERGY", "queries": ["NIFTY ENERGY"]},
            {"name": "OILGAS", "queries": ["NIFTY OIL & GAS", "NIFTY OIL AND GAS"]},
            {"name": "HEALTHCARE", "queries": ["NIFTY HEALTHCARE", "NIFTY HEALTHCARE INDEX"]},
            {"name": "CONSUMDUR", "queries": ["NIFTY CONSUMER DURABLES"]},
            {"name": "MEDIA", "queries": ["NIFTY MEDIA"]},
            {"name": "PVTBANK", "queries": ["NIFTY PRIVATE BANK"]},
        ]
        if not self.client.token_status().get("ok"):
            return self._pending_heatmap("token required")
        if time.time() < self.quote_blocked_until:
            return self._heatmap_cache or self._pending_heatmap("quote API blocked; using stale/pending heat strip")
        out = []
        instruments = []
        name_by_key = {}
        for row in base_rows:
            # Resolve against the authoritative instrument catalogue first.
            # The old deterministic table contained four provider spellings that
            # returned HTTP 400 forever and were retried on every heatmap cycle.
            inst = None
            cached_idx = self._instrument_key_cache.get("IDX:" + row["name"])
            if cached_idx and (time.time() - cached_idx[1]) < 3600:
                inst = cached_idx[0]
            else:
                for query in row["queries"]:
                    try:
                        inst = self.instrument_resolver.resolve(query, prefer_index=True)
                        if inst:
                            break
                    except Exception as exc:
                        self.event("WARN", "index", "Index instrument resolution failed", {"query": query, "error": str(exc)})
                if not inst:
                    # Alias/search resolution is convenience, not identity
                    # authority. If it misses an index spelling (Candidate 10
                    # exposed INDIA VIX), verify the exact versioned registry
                    # key against the already-loaded canonical instrument
                    # catalogue. No static row is emitted unless that exact key
                    # exists in the catalogue projection.
                    try:
                        from core.heatmap_index_catalog import heatmap_index_identity
                        canonical = heatmap_index_identity(row["name"])
                        finder = getattr(self.store, "find_instrument_by_key", None)
                        exact = finder(canonical.get("instrument_key")) if canonical and callable(finder) else None
                        if exact and str(exact.get("instrument_key") or "") == str(canonical.get("instrument_key") or ""):
                            segment = str(exact.get("segment") or exact.get("exchange") or "").upper()
                            if "INDEX" in segment or str(exact.get("instrument_type") or "").upper() == "INDEX":
                                inst = dict(exact)
                                inst.setdefault("trading_symbol", canonical.get("trading_symbol"))
                                inst.setdefault("name", canonical.get("display_name"))
                                inst["identity_resolution_source"] = "canonical_registry_exact_key_catalogue_verification"
                    except Exception as exc:
                        self.event("WARN", "index", "Exact registry-key index resolution failed", {
                            "catalog_code": row["name"], "error": str(exc)[:160]
                        })
                if inst:
                    self._instrument_key_cache["IDX:" + row["name"]] = (inst, time.time())
            if inst and inst.get("instrument_key"):
                resolved = dict(inst)
                resolved["identity_resolved"] = True
                resolved["catalog_code"] = row["name"]
                instruments.append(resolved)
                name_by_key[resolved.get("instrument_key")] = row["name"]
            else:
                self.event("WARN", "index", "Unresolved market-context identity suppressed", {
                    "catalog_code": row["name"], "queries": row.get("queries"),
                    "policy": "only instrument-catalogue-resolved indices may enter the market snapshot",
                })
        # Stream-first index quotes. A single unsupported key must never make
        # every market/sector row stale, so REST fills only missing keys in small
        # independent batches.
        market_open_now = is_india_market_open()
        stream_by_key = {}
        try:
            for row in (self.live_market.quotes.snapshot(market_open=market_open_now, max_age_sec=45.0) or {}).values():
                if row.get("instrument_key"):
                    stream_by_key[str(row.get("instrument_key"))] = dict(row)
        except Exception:
            stream_by_key = {}
        quotes = [stream_by_key[str(inst.get("instrument_key"))] for inst in instruments if str(inst.get("instrument_key")) in stream_by_key]
        quote_quarantine = getattr(self, "_index_quote_unavailable_until", None)
        if not isinstance(quote_quarantine, dict):
            quote_quarantine = {}
            self._index_quote_unavailable_until = quote_quarantine
        now_epoch = time.time()
        for key, expiry in list(quote_quarantine.items()):
            if float(expiry or 0) <= now_epoch:
                quote_quarantine.pop(key, None)
        missing_instruments = [
            inst for inst in instruments
            if str(inst.get("instrument_key")) not in stream_by_key
            and float(quote_quarantine.get(str(inst.get("instrument_key") or "")) or 0) <= now_epoch
        ]
        for offset in range(0, len(missing_instruments), 4):
            chunk = missing_instruments[offset:offset + 4]
            try:
                quotes.extend(self.client.full_quotes(chunk, persist=False) or [])
            except Exception as exc:
                # One unsupported provider index token must not erase the other
                # three rows in the batch.  Isolate the failing identity and
                # preserve prior verified values for that row only.
                self.record_error("index", str(exc), "/v2/market-quote/quotes")
                self.event("WARN", "index", "Bounded index quote batch failed; isolating identities", {"symbols": [row.get("trading_symbol") for row in chunk], "error": str(exc)[:160]})
                for instrument in chunk:
                    try:
                        quotes.extend(self.client.full_quotes([instrument], persist=False) or [])
                    except Exception as isolated:
                        key = str(instrument.get("instrument_key") or "")
                        # HTTP 400 is deterministic for the exact provider token.
                        # Do not hammer the same unsupported quote identity every
                        # 20-60 seconds; retain verified historical/LKG context and
                        # retry after a bounded quarantine window.
                        if getattr(isolated, "status", None) == 400 or "400" in str(isolated):
                            quote_quarantine[key] = time.time() + 1800.0
                        self.event("WARN", "index", "Index quote identity unavailable; retained snapshot will remain visible", {
                            "instrument_key": key,
                            "symbol": instrument.get("trading_symbol"),
                            "error": str(isolated)[:160],
                            "quote_retry_after_sec": 1800 if key in quote_quarantine else None,
                        })
        if quotes:
            self._set_status("last_price_refresh", now_iso())
        quoted = set()
        instrument_by_key = {str(inst.get("instrument_key") or ""): inst for inst in instruments}
        for q in quotes:
            integrity = classify_quote(q, now=india_now(), market_open=is_india_market_open(), max_live_age_sec=45.0)
            if integrity.get("state") not in ("live", "closed_market"):
                continue
            name = name_by_key.get(q.get("instrument_key"), q.get("symbol") or "INDEX")
            resolved_inst = instrument_by_key.get(str(q.get("instrument_key") or ""), {})
            hist_row = None
            if (not is_india_market_open()) or q.get("change_pct") is None or q.get("previous_close") is None:
                hist_row = self._index_historical_row(instrument_by_key.get(str(q.get("instrument_key") or ""), {}), name)
            chg = hist_row.get("change_pct") if hist_row else q.get("change_pct")
            state = "neutral" if chg is None else "green" if chg > 0.05 else "red" if chg < -0.05 else "neutral"
            source_time = (hist_row or {}).get("source_time") or integrity.get("source_time") or q.get("timestamp")
            out.append({
                "name": name, "state": state, "change_pct": chg,
                "last_refresh": source_time, "timestamp": source_time, "source_time": source_time,
                "instrument_key": q.get("instrument_key"),
                "trading_symbol": resolved_inst.get("trading_symbol") or q.get("symbol") or name,
                "display_name": resolved_inst.get("name") or resolved_inst.get("display_name") or name_by_key.get(q.get("instrument_key"), name),
                "chart_query": resolved_inst.get("name") or resolved_inst.get("display_name") or name,
                "exchange": q.get("exchange") or ("BSE" if str(q.get("instrument_key") or "").startswith("BSE_INDEX|") else "NSE"),
                "segment": "BSE_INDEX" if str(q.get("instrument_key") or "").startswith("BSE_INDEX|") else "NSE_INDEX",
                "instrument_type": "INDEX",
                "ltp": (hist_row or {}).get("ltp") if hist_row else q.get("ltp"), "open": q.get("open"), "high": q.get("high"), "low": q.get("low"),
                "close": (hist_row or {}).get("ltp") if hist_row else q.get("close"), "previous_close": (hist_row or {}).get("previous_close") if hist_row else q.get("previous_close"), "session_close": (hist_row or {}).get("ltp") if hist_row else q.get("session_close"),
                "point_change": (hist_row or {}).get("rupee_change") if hist_row else q.get("rupee_change"), "rupee_change": (hist_row or {}).get("rupee_change") if hist_row else q.get("rupee_change"), "change_source": "verified_completed_daily_candles" if hist_row else q.get("change_source"),
                "source": q.get("source") or "upstox_full_quote_v2",
                "freshness": ((hist_row or {}).get("freshness") or f"{str(integrity.get('state')).replace('_', ' ')} @ {source_time}"),
                "freshness_state": (hist_row or {}).get("freshness_state") or integrity.get("state"), "freshness_reason": (hist_row or {}).get("freshness_reason") or integrity.get("reason"),
                "session_result_verified": bool(hist_row or (q.get("change_pct") is not None and q.get("previous_close") is not None)),
                "identity_verified": True, "identity_resolved": True, "stale": False,
                "usable_for_promotion": bool(integrity.get("usable_for_promotion")),
            })
            quoted.add(q.get("instrument_key"))
        for inst in instruments:
            if inst.get("instrument_key") not in quoted:
                name = name_by_key.get(inst.get("instrument_key"), inst.get("trading_symbol"))
                hist_row = self._index_historical_row(inst, name)
                pending_identity = {
                    "name": name, "state": "pending", "change_pct": None,
                    "last_refresh": self.status.get("last_price_refresh"),
                    "reason": "quote unavailable",
                    "instrument_key": inst.get("instrument_key"),
                    "trading_symbol": inst.get("trading_symbol") or name,
                    "display_name": inst.get("name") or inst.get("display_name") or name,
                    "chart_query": inst.get("name") or inst.get("display_name") or name,
                    "exchange": inst.get("exchange") or ("BSE" if str(inst.get("instrument_key") or "").startswith("BSE_INDEX|") else "NSE"),
                    "segment": inst.get("segment") or ("BSE_INDEX" if str(inst.get("instrument_key") or "").startswith("BSE_INDEX|") else "NSE_INDEX"),
                    "instrument_type": "INDEX",
                    "identity_verified": True, "identity_resolved": True,
                    "session_result_verified": False, "stale": True,
                    "usable_for_promotion": False,
                    "source": "catalogue_resolved_price_pending",
                    "freshness_state": "unavailable",
                    "freshness_reason": "catalogue identity verified; current/completed price evidence unavailable",
                }
                out.append(hist_row or self._use_cached_heat(name, "quote unavailable; using last known") or pending_identity)
        order = {
            "NIFTY":0,"NXT50":1,"N100":2,"N200":3,"N500":4,"MIDCAP":5,"SMALLCAP":6,"SENSEX":7,"VIX":8,"BANK":9,
            "IT":10,"PHARMA":11,"AUTO":12,"METAL":13,"FMCG":14,"PSUBANK":15,"PVTBANK":16,"REALTY":17,"ENERGY":18,"OILGAS":19,"HEALTHCARE":20,"CONSUMDUR":21,"MEDIA":22,
        }
        # Preserve useful last-known rows if the current refresh returns pending for a chip.
        cache = self._heat_cache_by_name()
        merged = []
        for row in out:
            old = cache.get(str(row.get("name") or "").upper())
            if row.get("change_pct") is None and old and old.get("change_pct") is not None:
                keep = dict(old)
                keep["reason"] = row.get("reason") or "current refresh pending; showing last known"
                keep["freshness_state"] = "stale"
                keep["freshness_reason"] = keep["reason"]
                keep["freshness"] = "LKG only · current verified refresh pending"
                keep["identity_verified"] = False
                keep["session_result_verified"] = bool(
                    old.get("session_result_verified") is True
                    and old.get("ltp") is not None
                    and old.get("previous_close") is not None
                    and old.get("change_pct") is not None
                )
                keep["stale"] = True
                keep["usable_for_promotion"] = False
                merged.append(keep)
            else:
                merged.append(row)
        result = sorted(merged, key=lambda x: order.get(x.get("name"), 99))
        if result:
            # Background-only atomic membership/breadth projection.  This joins
            # already-acquired constituent quote memory with point-in-time
            # official membership; it performs no provider I/O.  Static lists
            # survive only as diagnostic fallback and can never authorize
            # Direction/Conviction.
            try:
                from core.index_market_context_projection_service import IndexMarketContextProjectionService
                projection = getattr(self, "_index_market_context_projection", None)
                if projection is None:
                    projection = IndexMarketContextProjectionService()
                    self._index_market_context_projection = projection
                result = projection.enrich_rows(
                    result,
                    getattr(self, "_coverage_quote_cache", {}) or {},
                    as_of=india_now().date(),
                    fallback_constituents=FINAL_INDEX_CONSTITUENTS,
                )
            except Exception as exc:
                self.record_error("index_market_context_projection", str(exc))
            # Direction evidence is materialized in this background producer,
            # not calculated by /api/indices on the browser request thread.
            try:
                from core.index_direction_evidence_authority import DEFAULT_INDEX_DIRECTION_EVIDENCE_AUTHORITY
                projected = []
                for row in result:
                    item = dict(row or {})
                    instrument_key = str(item.get("instrument_key") or "").strip()
                    candles = list(self._stored_candles(instrument_key, "day", limit=260) or []) if instrument_key else []
                    item.update(DEFAULT_INDEX_DIRECTION_EVIDENCE_AUTHORITY.scores(candles))
                    item["index_direction_projection_source"] = "BACKGROUND_HEATMAP_PRODUCER"
                    projected.append(item)
                result = projected
            except Exception as exc:
                self.record_error("index_direction_projection", str(exc))
            self._heatmap_cache = result
            self._heatmap_cache_ts = time.time()
            try:
                self.store.set_kv("heatmap_cache", result)
            except Exception:
                pass
        return result

    def market_intelligence(self):
        return {
            "state": "research-desk",
            "message": "Market-aware desk: fundamentals, structure, volume profile, ORB, participation, and index/sector heat are weighted per desk. No single hard rule promotes a stock.",
            "fundamentals": self.fundamentals.status(),
            "layers": self.status.get("market_layers"),
            "last_refresh": self.status.get("last_price_refresh"),
            "last_historical_fetch": self.status.get("last_historical_fetch"),
            "last_ai_validation": self.status.get("last_ai_validation"),
        }

