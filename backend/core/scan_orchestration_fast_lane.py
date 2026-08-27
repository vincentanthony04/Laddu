"""Bounded fast-lane and cross-desk deep scan execution."""
from __future__ import annotations

from core.scan_orchestration_dependencies import *  # noqa: F401,F403


class ScanFastLaneMixin:
    def _fast_lane_priorities(self, cap: int, market_open: bool) -> list[Dict[str, Any]]:
            """Bounded Intraday scan queue. Priority is analysis opportunity, not recommendation."""
            raw = self.host.store.priority_list(limit=max(cap * 5, 40))
            # Prepared overnight Intraday candidates get scheduling priority at the
            # next open, while their mathematical/ranking score remains untouched.
            # Once live validation rejects one, StoreDecisionPipeline retires its
            # replaceable watch/memory row so it cannot linger indefinitely.
            try:
                prepared_rows = list(self.host.store.opportunity_candidates("intraday", limit=max(cap * 4, 60)) or [])
            except Exception:
                prepared_rows = []
            prepared_priority = []
            for candidate in prepared_rows:
                context = " ".join(str(candidate.get(k) or "") for k in ("setup", "reason", "waiting_for", "target_window")).lower()
                if not any(token in context for token in ("pre-market", "premarket", "next-session", "live validation required", "live confirmation")):
                    continue
                row = dict(candidate)
                row["scheduling_priority"] = 120
                row["scheduling_reason"] = "prepared_next_session_live_validation"
                prepared_priority.append(row)
            raw = prepared_priority + list(raw)
            try:
                fl_status = self.host.status.setdefault("fast_lane", {})
                auto_cursor = int(fl_status.get("auto_cursor") or 0)
                auto_window = max(cap, 10)
                auto_rows = self.host.store.auto_live_priority(limit=auto_window, cursor=auto_cursor)
                fl_status["auto_cursor"] = auto_cursor + auto_window
                raw = list(raw) + list(auto_rows)
            except Exception:
                raw = list(raw)
            best: Dict[str, Dict[str, Any]] = {}
            for source in raw:
                symbol = str(source.get("symbol") or "").upper().strip()
                try:
                    source_mode = require_production_mode(source.get("mode"))
                except ValueError:
                    continue
                if not symbol or source_mode != "intraday":
                    continue
                row = dict(source, symbol=symbol, mode="intraday", production_policy_version=POLICY_VERSION)
                current = best.get(symbol)
                candidate_priority = int(row.get("scheduling_priority") or row.get("priority_score") or 0)
                current_priority = int((current or {}).get("scheduling_priority") or (current or {}).get("priority_score") or 0)
                if current is None or candidate_priority > current_priority:
                    best[symbol] = row
            selected = sorted(
                best.values(),
                key=lambda row: (int(row.get("scheduling_priority") or row.get("priority_score") or 0), str(row.get("created_at") or "")),
                reverse=True,
            )[:cap]
            self.host.status.setdefault("fast_lane", {})["coverage"] = {"intraday": len(selected)}
            self.host.status.setdefault("fast_lane", {})["market_timezone"] = "Asia/Kolkata"
            self.host.status.setdefault("fast_lane", {})["production_policy_version"] = POLICY_VERSION
            return selected

    def run_fast_lane(self):
            # Compatibility entry point only: all Intraday callers converge on the
            # same canonical live-analysis implementation and lane ownership.
            return self.run_live_mode_scan("intraday")

    def _run_fast_lane_impl(self):
            token = self.host.client.token_status()
            if not token["ok"]:
                with self.host.lock:
                    self.host.status["fast_lane"].update({"state": "waiting_token", "last_run": now_iso(), "next_run": "after token"})
                return
            if time.time() < self.host.quote_blocked_until:
                wait_s = int(max(1, self.host.quote_blocked_until - time.time()))
                with self.host.lock:
                    self.host.status["fast_lane"].update({"state": "quote_rate_limited", "last_run": now_iso(), "next_run": f"after {wait_s}s"})
                return
            market_open = is_india_market_open()
            # v32.2: this used to hardcode "8 if market_open else 3" regardless of
            # MAX_FASTLANE in config.py (=50), silently throttling every scan cycle to
            # 8 symbols. Raised toward the configured cap while staying conservative
            # against Upstox quote-endpoint rate limits (this loop already runs every
            # 15s while the market is open, so 8->20 roughly 2.5x's same-day coverage
            # without hammering the quote API).
            fast_cap = min(MAX_FASTLANE, 20 if market_open else 5)
            priorities = self._fast_lane_priorities(fast_cap, market_open)
            if market_open and priorities:
                try:
                    # Rich feed is bounded to the pre-qualified priority list.  It
                    # supplies volume/VWAP/depth evidence for opening-drive and
                    # climax analysis without escalating the complete universe.
                    self.host.set_priority_live_subscriptions(
                        [row.get("symbol") for row in priorities[:fast_cap]],
                        mode="full", ttl_seconds=600,
                    )
                except Exception as exc:
                    self.host.record_error("opening_rich_feed_plan", str(exc))
            if not market_open:
                with self.host.lock:
                    self.host.status["fast_lane"].update({
                        "state": "market_closed", "scanned": 0, "promoted": 0, "rejected": 0,
                        "last_run": now_iso(), "next_run": "09:15 IST", "market_timezone": "Asia/Kolkata",
                        "message": "Live Intraday validation is paused outside NSE hours; no same-day trades are promoted."
                    })
                return
            if not priorities:
                with self.host.lock:
                    self.host.status["fast_lane"].update({"state": "waiting_priority", "last_run": now_iso(), "next_run": "on search/watchlist/auto live seeds"})
                return
            scanned = promoted = rejected = data_missing = below_threshold = 0
            with self.host.lock:
                self.host.status["fast_lane"].update({"state": "running", "last_run": now_iso(), "market_timezone": "Asia/Kolkata"})
            # v36.0-fix: previously called self.host.client.quotes([inst]) once PER priority item
            # inside the loop below -- up to `fast_cap` (20) separate Upstox HTTP calls every
            # 15s, on top of live_quotes' own polling. That's what was actually tripping real
            # 429s from Upstox (confirmed in backend.log) and triggering the multi-minute
            # quote_blocked_until freeze that froze every price on screen. Resolve instruments
            # first, then fetch all quotes in ONE batched call, same pattern as live_quotes().
            resolved: List[Dict[str, Any]] = []
            for p in priorities[:fast_cap]:
                q = p["symbol"]; mode = "intraday"
                if not market_open:
                    rejected += 1; continue
                try:
                    matches = self.host.client.search_instruments(q, limit=1)
                except Exception:
                    matches = []
                if not matches:
                    data_missing += 1
                    self.host.event("WARN", "search", "No exact instrument match", {"symbol": q, "options": False})
                    continue
                resolved.append({"priority": p, "inst": matches[0], "mode": mode})
            try:
                self.host.research_adapter.refresh_cross_sectional(
                    {r["inst"].get("trading_symbol", "").upper(): r["inst"]["instrument_key"] for r in resolved if r["inst"].get("instrument_key")}
                )
            except Exception as exc:
                self.host.record_error("fast_lane", f"cross_sectional_refresh: {str(exc)[:120]}")
            quote_by_key: Dict[str, Dict[str, Any]] = {}
            needs_live = [r for r in resolved if not ((not market_open) and mode_uses_history_without_live(r["mode"]))]
            if needs_live and time.time() >= self.host.quote_blocked_until:
                try:
                    qs = self.host.client.quotes([r["inst"] for r in needs_live], persist=False)
                    for qd in qs:
                        if qd.get("instrument_key"):
                            quote_by_key[qd["instrument_key"]] = qd
                    if qs:
                        try:
                            self.host.runtime_market_state.save_latest_quotes(qs)
                        except Exception as exc:
                            self.host.record_error("fast_lane_runtime_quote_persist", str(exc))
                        with self.host.lock:
                            self.host.status["last_price_refresh"] = now_iso()
                except Exception as exc:
                    self.host.record_error("fast_lane", str(exc), "/v3/market-quote/ltp")
                    self.host.event("WARN", "fast_lane", "Batched priority quote fetch failed", {"count": len(needs_live), "error": str(exc)})
            elif needs_live:
                with self.host.lock:
                    self.host.status["fast_lane"].update({"state": "quote_api_blocked", "next_run": "after auth-test"})
            for r in resolved:
                q = r["priority"]["symbol"]; mode = r["mode"]; inst = r["inst"]
                try:
                    quote = None
                    if (not market_open) and mode_uses_history_without_live(mode):
                        quote = None
                    else:
                        quote = quote_by_key.get(inst.get("instrument_key"))
                        if quote is None and not mode_uses_history_without_live(mode):
                            data_missing += 1
                            continue
                    d = self.host.analyze_one(inst, quote, mode)
                    scanned += 1
                    if d:
                        opening = None
                        try:
                            now_ist = india_now().time().replace(tzinfo=None)
                            if dtime(9, 15) <= now_ist <= dtime(9, 35):
                                bars = self.host.runtime_market_state.canonical_bars(
                                    str(inst.get("instrument_key") or ""), "1m",
                                    limit=20, include_forming=False,
                                )
                                opening = self.opening_intelligence.assess(bars, quote or {})
                                d["opening_intelligence"] = opening
                                d["opening_state"] = opening.get("state")
                                d["opening_continuation_score"] = opening.get("continuation_score")
                                d["opening_climax_score"] = opening.get("climax_score")
                                d["opening_false_break_score"] = opening.get("false_break_score")
                                # Opening intelligence is explicitly diagnostic/research-only
                                # until prospective calibration qualifies an influence policy.
                                # It must never mutate canonical admission status here.
                                d["opening_production_influence"] = 0
                        except Exception as exc:
                            self.host.record_error("opening_intelligence", str(exc))
                        self.host.store.save_decision(d)
                        if d.get("status") == "PROMOTED":
                            promoted += 1
                        elif d.get("status") == "WATCH":
                            below_threshold += 1
                        else:
                            rejected += 1
                    else:
                        rejected += 1
                except Exception as exc:
                    rejected += 1
                    self.host.record_error("fast_lane", str(exc))
                    self.host.event("WARN", "fast_lane", "Priority scan failed", {"symbol": q, "mode": mode, "error": str(exc)})
            with self.host.lock:
                self.host.status["last_ai_validation"] = now_iso() if scanned else self.host.status.get("last_ai_validation")
            # v36.8: expose the breakdown (not just scanned/promoted/rejected) so an empty
            # Selected list can be diagnosed instantly -- "no qualifying setups" (below_threshold)
            # vs "couldn't see the market this cycle" (data_missing) vs a real analysis error (rejected).
            with self.host.lock:
                self.host.status["fast_lane"].update({
                    "state": "idle", "scanned": scanned, "promoted": promoted, "rejected": rejected,
                    "data_missing": data_missing, "below_threshold": below_threshold,
                    "last_run": now_iso(), "next_run": "10s market-open / 09:15 IST if closed",
                    "diagnosis": (
                        "no_data" if scanned == 0 and data_missing > 0 else
                        "no_qualifying_setups" if promoted == 0 and below_threshold > 0 else
                        "ok" if promoted > 0 else "idle"
                    ),
                })

    def run_deep_scan(self):
            """Compatibility trigger for the one canonical Delivery scanner.

            v65.26.33 removes the second Delivery execution authority. Historical
            callers and /api/deep-scan still work, but they delegate to the same
            worker, cursor, persistence path and status contract as delivery_loop().
            """
            result = self.run_deep_mode_scan("delivery")
            with self.host.lock:
                self.host.status.setdefault("deep_scan", {})["trigger_source"] = "compatibility_delegate"
            return result
