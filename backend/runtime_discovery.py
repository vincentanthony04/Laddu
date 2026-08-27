from __future__ import annotations

from runtime_shared import *
from core.participation_evidence_authority import DEFAULT_PARTICIPATION_EVIDENCE_AUTHORITY


class RuntimeDiscoveryMixin:
    """Historical data, analysis, candidate discovery and learning orchestration."""

    def _stored_candles(self, instrument_key: str, interval: str, limit: int = 5000):
        """v37.2: delegate -- see core/market_data_service.py::MarketDataService.stored_candles"""
        return self.market_data.stored_candles(instrument_key, interval, limit=limit)

    def _schedule_historical_refresh(self, instrument_key: str, interval: str, days: int = 20, reason: str = "background"):
        """v37.2: delegate -- see core/market_data_service.py::MarketDataService.schedule_historical_refresh"""
        return self.market_data.schedule_historical_refresh(instrument_key, interval, days, reason=reason)

    def historical_candles(self, instrument_key: str, interval: str, days: int = 20, *, force: bool = False, max_wait_sec: float = 2.8):
        """v37.2: delegate -- see core/market_data_service.py::MarketDataService.get_historical"""
        return self.market_data.get_historical(instrument_key, interval, days, force=force, max_wait_sec=max_wait_sec)

    def _event_risk_map_cached(self) -> Dict[str, str]:
        """Phase 6: one DB query per ~2min, shared across every candidate in
        a scan cycle instead of a query per symbol -- event_risk_fn above
        is called once per analyze_one, which can be hundreds of times per
        scan pass."""
        now = time.time()
        cached = getattr(self, "_event_risk_cache", None)
        if cached is not None and (now - cached[0]) < 120:
            return cached[1]
        try:
            m = self.earnings_calendar.event_risk_map(3)
        except Exception:
            m = {}
        self._event_risk_cache = (now, m)
        return m

    def analyze_one(self, instrument, quote, mode: str, use_api_fund: bool = False, candles_override=None) -> Dict[str, Any] | None:
        """v37.4: delegate -- see core/engine_dispatch_service.py::EngineDispatchService.analyze_one.
        Context-building helpers are still owned by LadduRuntime (dashboard/
        discovery state isn't decoupled yet), so they're passed in as bound
        methods; the fetch-candles-and-run-engine logic they used to be
        tangled with now lives in one tested place instead of duplicated
        inline here."""
        out = self.engine_dispatch.analyze_one(
            instrument, quote, mode, use_api_fund=use_api_fund, candles_override=candles_override,
            mode_uses_history_without_live_fn=mode_uses_history_without_live,
            market_context_fn=self.market_context,
            quote_freshness_guard_fn=quote_freshness_guard,
            sync_decision_context_fn=self._sync_decision_context,
            apply_candidate_timing_fn=self._apply_candidate_timing,
            attach_discovery_intelligence_fn=self._attach_discovery_intelligence,
            on_bad_key=lambda key: self._bad_historical_keys.__setitem__(key, time.time() + self._BAD_KEY_TTL),
            on_historical_error=lambda err, endpoint: self.record_error("historical", err, endpoint),
            event_risk_fn=lambda symbol: self._event_risk_map_cached().get(str(symbol or "").upper()),
            settle_selector_outcomes_fn=lambda symbol, canonical_mode, candles: (self.evidence_score_validation.mark_candles(symbol, canonical_mode, candles, identity_verified=True), self.selection_outcome_settlement.settle_symbol(symbol, canonical_mode, candles)),
        )
        if out is not None:
            out = self.decision_engine.finalize_candidate(
                out,
                ranking_service=self.production_ranker,
                delivery=out.get("institutional_signal"),
            )
            try:
                out["counterfactual_observation"] = self.counterfactual_learning.record(out)
            except Exception as exc:
                out["counterfactual_observation"] = {"ok": False, "state": "RECORD_FAILED", "error": str(exc)[:160]}
                self.record_error("counterfactual_learning", str(exc))
            self._set_status("last_historical_fetch", now_iso())
        return out

    def scanner_prepare_analysis(self, instrument: Dict[str, Any], quote: Dict[str, Any] | None, mode: str, candles: list[Dict[str, Any]]) -> Dict[str, Any]:
        """Prepare every non-mathematical scanner dependency outside workers.

        The returned object is immutable-by-convention input for the bounded
        compute executor.  Any local repository/cache read happens here, never in
        an analysis worker.  Provider access is disabled.
        """
        canonical_mode = require_production_mode(mode)
        context = self.market_context(instrument, canonical_mode, candles, quote, use_api_fund=False)
        symbol = str(instrument.get("trading_symbol") or instrument.get("symbol") or "").upper()
        event_risk_date = self._event_risk_map_cached().get(symbol) if symbol else None
        return {
            "context": dict(context or {}),
            "event_risk_date": event_risk_date,
            "prepared_at": now_iso(),
        }

    def scanner_analyze_compute(self, instrument, quote, mode: str, *, candles_override=None, prepared_analysis=None, **_ignored) -> Dict[str, Any] | None:
        """Bounded scanner worker entrypoint: deterministic local compute only."""
        if candles_override is None:
            raise ValueError("SCANNER_ANALYSIS_REQUIRES_LOCAL_CANDLE_SNAPSHOT")
        prepared = dict(prepared_analysis or {})
        if not isinstance(prepared.get("context"), dict):
            raise ValueError("SCANNER_ANALYSIS_REQUIRES_PREPARED_CONTEXT")
        return self.engine_dispatch.analyze_prepared(
            instrument, quote, mode,
            candles=list(candles_override or []),
            context=dict(prepared.get("context") or {}),
            sync_decision_context_fn=self._sync_decision_context,
            apply_candidate_timing_fn=self._apply_candidate_timing,
            attach_discovery_intelligence_fn=self._attach_discovery_intelligence,
            event_risk_date=prepared.get("event_risk_date"),
        )

    def finalize_scanner_analysis(self, candidate: Dict[str, Any] | None, instrument: Dict[str, Any], mode: str, candles: list[Dict[str, Any]]) -> Dict[str, Any] | None:
        """Govern/persist analysis after the compute worker has returned.

        Ranking, outcome settlement and counterfactual persistence are allowed to
        touch repositories, so they deliberately execute outside the bounded
        compute pool.  A slow governance store can delay orchestration, but it can
        no longer strand every scanner worker.
        """
        if not candidate:
            return None
        out = self.decision_engine.finalize_candidate(
            dict(candidate), ranking_service=self.production_ranker,
            delivery=candidate.get("institutional_signal"),
        )
        symbol = str(instrument.get("trading_symbol") or instrument.get("symbol") or out.get("symbol") or "").upper()
        try:
            if candles and symbol:
                self.evidence_score_validation.mark_candles(symbol, require_production_mode(mode), candles, identity_verified=True)
                self.selection_outcome_settlement.settle_symbol(symbol, require_production_mode(mode), candles)
        except Exception as exc:
            self.record_error("selector_outcome_settlement", str(exc))
        try:
            out["counterfactual_observation"] = self.counterfactual_learning.record(out)
        except Exception as exc:
            out["counterfactual_observation"] = {"ok": False, "state": "RECORD_FAILED", "error": str(exc)[:160]}
            self.record_error("counterfactual_learning", str(exc))
        self._set_status("last_historical_fetch", now_iso())
        return out

    def _apply_liquidity_gate(self, out: Dict[str, Any], candles, quote) -> None:
        """v37.4: delegate -- see core/engine_dispatch_service.py::EngineDispatchService.apply_liquidity_gate."""
        return self.engine_dispatch.apply_liquidity_gate(out, candles, quote)

    def _isin_from_instrument(self, instrument: Dict[str, Any] | None) -> str:
        """v19/v24: tolerate Upstox/local rows where isin is blank but instrument_key embeds it.
        Example: NSE_EQ|INE670A01012 -> INE670A01012. Also ignore malformed non-dict rows safely.
        """
        if not instrument or not isinstance(instrument, dict):
            return ""
        isin = str(instrument.get("isin") or "").strip().upper()
        if re.fullmatch(r"[A-Z]{2}[A-Z0-9]{10}", isin or ""):
            return isin
        key = str(instrument.get("instrument_key") or "").strip().upper()
        m = re.search(r"\bIN[A-Z0-9]{10}\b", key)
        return m.group(0) if m else ""

    def _enrich_instrument_identity(self, instrument: Dict[str, Any] | None) -> Dict[str, Any] | None:
        if not instrument or not isinstance(instrument, dict):
            return None
        out = dict(instrument)
        isin = self._isin_from_instrument(out)
        if isin and not str(out.get("isin") or "").strip():
            out["isin"] = isin
            out["isin_source"] = "instrument_key_fallback"
        ex = str(out.get("exchange") or "").upper()
        seg = str(out.get("segment") or "").upper()
        if ex == "NSE_EQ":
            out["exchange"] = "NSE"
        elif ex == "BSE_EQ":
            out["exchange"] = "BSE"
        if not seg and ex in ("NSE", "BSE"):
            out["segment"] = ex + "_EQ"
        return out

    def _coverage_bucket(self, symbol: str) -> str:
        sym = str(symbol or "").upper()
        if sym in NIFTY50_CORE:
            return "NIFTY50"
        if sym in NEXT50_CORE:
            return "NEXT50"
        if sym in NIFTY250_EXTRA:
            return "NIFTY250"
        return "BROAD"

    def _coverage_snapshot(self, cursor: int | None = None) -> Dict[str, Any]:
        """v51: delegate -- see core/scan_orchestration_service.py::ScanOrchestrationService._coverage_snapshot"""
        return self.scan_orchestration._coverage_snapshot(cursor)

    def _sector_theme_profile(self, instrument: Dict[str, Any] | None) -> Dict[str, Any]:
        inst = instrument or {}
        sym = str(inst.get("trading_symbol") or "").upper()
        name = str(inst.get("name") or "").upper()
        sector = sector_hint_from_symbol(sym, name) or "broad"
        themes = []
        def add(theme):
            if theme not in themes:
                themes.append(theme)
        # Future/capex themes. This is evidence tagging only; it never promotes alone.
        if any(x in sym + " " + name for x in ("BEL","HAL","MAZDOCK","COCHINSHIP","BHEL","BEML","BDL","GRSE","ASTRAMICRO")):
            add("defence / strategic manufacturing")
        if any(x in sym + " " + name for x in ("IRFC","IRCTC","RVNL","RAIL","TITAGARH","BHEL","SIEMENS","ABB")):
            add("railway / public capex")
        if any(x in sym + " " + name for x in ("TATAPOWER","NTPC","POWERGRID","TORNTPOWER","JSWENERGY","ADANIGREEN","SUZLON","RENEW","IREDA")):
            add("power / renewables / grid capex")
        if any(x in sym + " " + name for x in ("TMPV","TATAMOTORS","M&M","MARUTI","TVSMOTOR","BAJAJ-AUTO","EICHERMOT","SONACOMS","EXIDE","AMARAJABAT","MOTHERSON")):
            add("EV / auto ancillaries / auto transition")
        # Evidence-based theme tags: do not assign broad future themes to every large IT stock.
        if any(x in sym + " " + name for x in ("KAYNES","DIXON")):
            add("electronics manufacturing / semiconductor supply chain")
        if any(x in sym + " " + name for x in ("TATAELXSI","CYIENT","KPITTECH")):
            add("digital engineering / auto-tech / embedded systems")
        if any(x in sym + " " + name for x in ("LT","NCC","PNCINFRA","KNRCON","ASHOKA","IRB","ADANIPORTS","CONCOR")):
            add("infrastructure / logistics capex")
        if sector and sector != "broad":
            add("sector leadership watch")
        return {"sector": sector, "themes": themes[:5], "coverage_bucket": self._coverage_bucket(sym)}

    def _attach_discovery_intelligence(self, d: Dict[str, Any], ctx: Dict[str, Any], candles: list[Dict[str, Any]] | None, instrument: Dict[str, Any] | None = None) -> Dict[str, Any]:
        """v26 opportunity-memory adaptive discovery: not fixed user filters, but evidence buckets.
        This only tags watch/armed candidates; final selection remains strict.
        """
        candles = candles or []
        profile = self._sector_theme_profile(instrument or {"trading_symbol": d.get("symbol")})
        d.update(profile)
        buckets = []
        evidence = []
        def add(bucket, ev):
            if bucket not in buckets:
                buckets.append(bucket)
            if ev and ev not in evidence:
                evidence.append(ev)
        vals = closes(candles)
        last = None
        try:
            last = float(d.get("ltp") or (vals[-1] if vals else 0) or 0)
        except Exception:
            last = None
        if last and len(vals) >= 110:
            e20 = ema(vals, 20); e50 = ema(vals, 50); e100 = ema(vals, 100)
            if e20 and e50 and e100:
                spread = (max(e20, e50, e100) - min(e20, e50, e100)) / last * 100
                d["ema_compression_pct"] = round(spread, 2)
                if spread <= 3.5:
                    add("EMA compression / coil", f"EMA20/50/100 compressed within {round(spread,2)}%")
        if last and candles:
            sup = d.get("support") or (ctx.get("market_structure") or {}).get("support") or (ctx.get("price_action") or {}).get("long_term_support")
            res = d.get("resistance") or (ctx.get("market_structure") or {}).get("resistance") or (ctx.get("price_action") or {}).get("long_term_resistance")
            try:
                if sup and last <= float(sup) * 1.06:
                    add("near important support", f"LTP within 6% of support {round(float(sup),2)}")
                if res and last >= float(res) * 0.94:
                    add("near breakout / resistance test", f"LTP within 6% of resistance {round(float(res),2)}")
            except Exception:
                pass
        if str(d.get("mode") or "").lower() == "delivery":
            rc_rule = RangeCompressionRuleService.evaluate(candles, as_of=india_now())
            d["range_compression_rule"] = rc_rule
            d["range_compression_rule_id"] = rc_rule.get("rule_id")
            d["range_compression_score"] = rc_rule.get("score")
            d["range_compression_qualified"] = rc_rule.get("qualified") is True
            if rc_rule.get("qualified") is True:
                add("RC range compression 1-to-6", "Latest completed daily range is strictly below each of the prior six sessions")
        if len(candles) >= 35:
            try:
                participation = DEFAULT_PARTICIPATION_EVIDENCE_AUTHORITY.delivery_recent_volume(candles, at=india_now())
                recent_vol = sum(float(c.get("volume") or 0) for c in candles[-5:]) / 5
                base_vol = float(participation.get("baseline_value") or 0.0)
                last_vol = float(candles[-1].get("volume") or 0)
                if participation.get("value") is not None:
                    d["recent_volume_vs_base"] = round(float(participation["value"]), 2)
                    d["participation_evidence"] = participation
                    d["participation_authority"] = participation.get("authority")
                    d["participation_authority_version"] = participation.get("authority_version")
                    d["participation_lane"] = participation.get("lane")
                    d["participation_source_time"] = participation.get("source_time")
                    d["participation_decision_usable"] = participation.get("decision_usable") is True
                    if base_vol and (last_vol > base_vol * 1.45 or recent_vol > base_vol * 1.25):
                        add("volume expansion", f"Recent/last volume above base average ({round(max(last_vol/base_vol, recent_vol/base_vol),2)}x)")
                    elif base_vol and recent_vol < base_vol * 0.72:
                        add("volume dry-up", f"Volume drying during consolidation ({round(recent_vol/base_vol,2)}x)")
                def avg_range(cs):
                    vals2=[]
                    for c in cs:
                        hi=c.get("high"); lo=c.get("low"); cl=c.get("close")
                        if hi is not None and lo is not None and cl:
                            vals2.append((float(hi)-float(lo))/float(cl)*100)
                    return sum(vals2)/len(vals2) if vals2 else None
                r_recent = avg_range(candles[-7:]); r_prior = avg_range(candles[-28:-7])
                if r_recent and r_prior:
                    d["range_contraction_pct"] = round((1 - r_recent/r_prior) * 100, 1)
                    if r_recent < r_prior * 0.72:
                        add("candle range contraction", f"Recent candle range contracted vs prior range ({round(r_recent,2)}% vs {round(r_prior,2)}%)")
                    elif r_recent > r_prior * 1.3 and last_vol > base_vol * 1.25:
                        add("range expansion with volume", "Candle range expansion came with higher volume")
            except Exception:
                pass
        ms = ctx.get("market_structure") or {}
        vp = ctx.get("volume_profile") or {}
        if str(ms.get("state") or "").lower() in ("break_of_structure_up", "choch_up", "higher_high_higher_low") or str(ms.get("bias") or "").lower() == "long":
            add("structure improving", ms.get("summary") or "market structure improving")
        if str(ms.get("state") or "").lower() in ("break_of_structure_down", "choch_down") or str(ms.get("bias") or "").lower() == "short":
            add("weak structure / avoid long", ms.get("summary") or "market structure weak")
        if str(vp.get("state") or "").lower() in ("acceptance_above_value", "inside_value"):
            add("volume profile context", vp.get("summary") or "volume profile useful")
        fund = ctx.get("fundamentals") or {}
        if isinstance(fund, dict):
            try:
                inst_delta = float(fund.get("institutional_delta") or 0)
                d["institutional_delta"] = round(inst_delta, 2)
                if inst_delta > 0.5:
                    add("institutional accumulation", f"FII/MF/DII net stake increased {round(inst_delta,2)} pp in latest reported period")
                elif inst_delta < -0.5:
                    add("institutional distribution risk", f"Institutional stake reduced {round(inst_delta,2)} pp in latest reported period")
            except Exception:
                pass
            sh = fund.get("shareholding") or {}
            for key, label in (("fii","FII"),("mutual_funds","MF"),("other_dii","DII"),("promoters","promoter")):
                try:
                    delta = float((sh.get(key) or {}).get("delta") or 0)
                    if delta > 0.25:
                        add("stake increase", f"{label} stake increased {round(delta,2)} pp in latest reported period")
                except Exception:
                    pass
            if fund.get("score") is not None and float(fund.get("score") or 0) >= 70:
                add("fundamental quality", f"Fundamental score {round(float(fund.get('score')),1)}")
        if str(d.get("mode") or "").lower() in PRODUCTION_MODES:
            try:
                institutional = self.delivery_context(str(d.get("symbol") or profile.get("symbol") or ""), record=False)
                d["institutional_signal"] = institutional
                d["institutional_score"] = institutional.get("score")
                d["institutional_stage"] = institutional.get("stage")
                d["institutional_model_version"] = institutional.get("model_version")
                sig = institutional.get("signals") or {}
                if sig.get("hidden_accumulation"):
                    add("institutional accumulation", "Hidden Accumulation: 20D delivery quantity z-shock + ATR compression + delivery % confirmation")
                if sig.get("volume_climax"):
                    add("volume climax", "Volume Climax: traded and deliverable quantity exceeded their 20D z-score gates")
                if sig.get("absorption"):
                    add("delivery ATR absorption", "Large delivered quantity was absorbed inside a quiet ATR-normalized price move")
                dwap = institutional.get("dwap") or {}
                if dwap.get("value") is not None:
                    d["dwap"] = dwap.get("value"); d["dwap_support_low"] = dwap.get("support_low"); d["dwap_support_high"] = dwap.get("support_high")
                if institutional.get("state") == "collecting_evidence":
                    d["institutional_coverage"] = institutional.get("coverage")
            except Exception as exc:
                d["institutional_signal"] = {"ok": False, "state": "error", "summary": str(exc)[:120]}
        themes = profile.get("themes") or []
        specific_themes = [t for t in themes if str(t).lower() != "sector leadership watch"]
        if specific_themes:
            add("future theme tailwind", ", ".join(specific_themes[:3]))
        # VCP proxy needs compression + range contraction with dry-up or tight price action.
        if "EMA compression / coil" in buckets and ("candle range contraction" in buckets or "volume dry-up" in buckets):
            add("VCP / volatility contraction", "EMA compression plus range/volume contraction suggests pressure building")
        score = int(d.get("score") or 0)
        long_only_bearish = str(d.get("mode") or "").lower() == "delivery" and (str(d.get("side") or "").upper() in ("BEARISH", "SHORT") or str(d.get("decision") or "").upper() == "AVOID_LONG")
        trigger_near = self._is_trigger_near(d, last)
        constructive = ("structure improving" in buckets) or ("near breakout / resistance test" in buckets) or ("VCP / volatility contraction" in buckets) or ("institutional accumulation" in buckets)
        if str(d.get("status") or "").upper() == "PROMOTED":
            stage = "SELECTED"
        elif str(d.get("prepared_state") or "").upper() == "ARMED":
            stage = "ARMED"
        elif long_only_bearish:
            stage = "QUALIFIED" if score >= 74 and ("near important support" in buckets or "fundamental quality" in buckets) else "POTENTIAL"
        elif score >= 76 and len(buckets) >= 2 and trigger_near and constructive:
            stage = "ARMED"
        elif score >= 62 and len(buckets) >= 1:
            stage = "QUALIFIED"
        elif len(buckets) >= 1:
            stage = "WATCH"
        else:
            stage = "UNQUALIFIED"
        d["candidate_stage"] = stage
        d["discovery_buckets"] = buckets[:8]
        d["opportunity_bucket"] = " + ".join(buckets[:2]) if buckets else "no clear edge yet"
        d["discovery_evidence"] = evidence[:10]
        d["candidate_model"] = "canonical evidence: institutional delivery/ATR/DWAP + sector/theme + fundamentals + structure + participation + tradeability + regime"
        if buckets and d.get("status") not in ("PROMOTED", "SIGNAL_OPEN"):
            d["watch_type"] = d.get("watch_type") or "auto_discovery"
            d["waiting_for"] = d.get("waiting_for") or self._discovery_waiting_for(d)
            d["trigger"] = d.get("trigger") or d.get("planned_entry") or d.get("resistance") or "breakout/reclaim confirmation"
            d["invalidation"] = d.get("invalidation") or d.get("planned_sl") or d.get("support") or "base/support failure"
            d["reason"] = (d.get("reason") or "") + "; Discovery: " + " | ".join(evidence[:4])
        return d

    def _is_trigger_near(self, d: Dict[str, Any], last_price: float | None = None) -> bool:
        from core.opportunity_scoring_service import is_trigger_near
        return is_trigger_near(d, last_price)

    def _discovery_waiting_for(self, d: Dict[str, Any]) -> str:
        from core.opportunity_scoring_service import discovery_waiting_for
        return discovery_waiting_for(d)

    def _opportunity_priority_score(self, d: Dict[str, Any]) -> int:
        from core.opportunity_scoring_service import opportunity_priority_score
        return opportunity_priority_score(d)

    def _priority_reason(self, d: Dict[str, Any]) -> str:
        from core.opportunity_scoring_service import priority_reason
        return priority_reason(d)

    def _opportunity_summary_from_rows(self, rows: list[Dict[str, Any]]) -> Dict[str, Any]:
        from core.opportunity_scoring_service import opportunity_summary_from_rows
        return opportunity_summary_from_rows(rows)

    def _best_available_watch_seeds(self, mode: str = "all") -> list[Dict[str, Any]]:
        """v51: delegate -- see core/scan_orchestration_service.py::ScanOrchestrationService._best_available_watch_seeds"""
        return self.scan_orchestration._best_available_watch_seeds(mode)

    def potential_candidates(self, mode: str = "all", limit: int = 60, compact: bool = False) -> Dict[str, Any]:
        """v51: delegate -- see core/scan_orchestration_service.py::ScanOrchestrationService.potential_candidates"""
        return self.scan_orchestration.potential_candidates(mode, limit, compact)

    def sector_cycle_board(self, mode: str = "all") -> Dict[str, Any]:
        """v51 (Cluster 9): delegate -- see core/scan_orchestration_service.py::ScanOrchestrationService.sector_cycle_board"""
        return self.scan_orchestration.sector_cycle_board(mode)

    def stock_case_file(self, symbol: str, mode: str = "all") -> Dict[str, Any]:
        sym = str(symbol or "").upper().strip()
        rows = []
        rows += [d for d in self.store.opportunity_candidates("all", limit=300) if str(d.get("symbol") or "").upper()==sym]
        rows += [d for d in self._research_candidates("all", limit=300) if str(d.get("symbol") or "").upper()==sym]
        rows += [d for d in self.store.latest_decisions("all", limit=300) if str(d.get("symbol") or "").upper()==sym]
        best = rows[0] if rows else {"symbol": sym, "mode": mode, "opportunity_stage":"Dormant", "priority_reason":"No current case in memory; search/analyze or wait for deep scan."}
        return {
            "symbol": sym, "mode": mode, "case": self._card_project(best),
            "why_discovered": best.get("priority_reason") or best.get("reason"),
            "why_now": best.get("opportunity_bucket") or best.get("setup"),
            "sector": best.get("sector"), "themes": best.get("themes"),
            "stage": best.get("opportunity_stage") or best.get("candidate_stage") or "Dormant",
            "trigger": best.get("trigger"), "invalidation": best.get("invalidation"), "target_window": best.get("target_window"),
            "evidence": best.get("discovery_evidence") or best.get("evidence") or [],
            "policy":"Case file explains opportunity status. It is not a trade unless status becomes Triggered/Selected and enters signal ledger."
        }

    def compute_daily_learning_review(self) -> Dict[str, Any]:
        """Computes the end-of-day review dict. Split out from the loop
        below so on-demand callers (if any) still get the same shape."""
        perf = self.store.daily_performance()
        opp = self.store.opportunity_candidates("all", limit=80)
        prepared = len(opp)
        triggered = sum(int(x.get("trades") or 0) for x in perf)
        failed = sum(int(x.get("fail") or 0) for x in perf)
        open_trades = sum(int(x.get("open") or 0) for x in perf)
        review = {
            "state":"ready", "date": now_iso()[:10], "prepared_candidates": prepared, "triggered_trades": triggered,
            "failed_trades": failed, "open_trades": open_trades,
            "questions":["What was prepared?", "What triggered?", "What failed?", "What moved without us?", "Which stock should have been in Potential earlier?"],
            "missed_move_audit":"pending_live_eod_audit",
            "top_potential_for_review":[self._compact_card_project(d) for d in self._group_opportunity_rows(opp)[:5]],
            "policy":"After close, compare prepared candidates against triggered trades and big movers; promote missed patterns into Opportunity Memory."
        }
        return review

    def deep_history_backfill_loop(self, sup=None):
        """v51 (Cluster 6): delegate -- see core/market_data_service.py::MarketDataService.deep_history_backfill_loop"""
        return self.market_data.deep_history_backfill_loop(sup)

    def storage_maintenance_loop(self, sup=None):
        """Post-close, bounded retention for transient market-data tables."""
        time.sleep(30.0)
        last_run_date = None
        while CONTROL.running and (sup is None or sup.running):
            if sup: sup.beat("storage_maintenance")
            try:
                now = india_now()
                today = now.date().isoformat()
                eligible = (now.weekday() >= 5) or now.time().replace(tzinfo=None) >= dtime(15, 45)
                if is_india_market_open():
                    with self.lock:
                        self.status["storage_maintenance"] = {"state": "paused_market_open", "last_run": self.status.get("storage_maintenance", {}).get("last_run"), "message": "retention runs after 15:45 IST"}
                    if sup:
                        sup.set_expected_idle("storage_maintenance", True, waiting_on="market open; retention runs after 15:45 IST")
                elif eligible and last_run_date != today:
                    with self.lock:
                        self.status["storage_maintenance"] = {"state": "running", "last_run": self.status.get("storage_maintenance", {}).get("last_run"), "message": "bounded chunk cleanup in progress"}
                    result = self.store.prune_runtime_data(now_iso=now.astimezone(timezone.utc).isoformat(), chunk_size=5000, max_chunks_per_table=8)
                    last_run_date = today
                    with self.lock:
                        self.status["last_market_data_maintenance"] = now_iso()
                        self.status["storage_maintenance"] = {"state": "complete", "last_run": now_iso(), "result": result, "message": "signal ledger preserved; no live VACUUM"}
                    self.event("INFO", "storage_maintenance", "Bounded post-close retention completed", result)
                    if sup:
                        sup.progress("storage_maintenance", token=f"retention:{today}", stage="retention_complete", completed_units=1, total_units=1, expected_idle=True, waiting_on="next post-close maintenance window")
                elif sup and not is_india_market_open():
                    sup.set_expected_idle("storage_maintenance", True, waiting_on="next post-close maintenance window")
            except Exception as exc:
                self.record_error("storage_maintenance", str(exc)[:200])
                with self.lock:
                    self.status["storage_maintenance"] = {"state": "failed", "last_run": now_iso(), "error": str(exc)[:200]}
            time.sleep(900)

    def daily_learning_loop(self, sup=None):
        """v43.1: this previously computed a review dict and returned it with
        nothing calling it and nothing persisting the result -- the
        `daily_learning` table had 0 rows in production despite
        Store.record_daily_learning() existing and working correctly. Now a
        real scheduled loop (same once-per-day-near-close shape as
        earnings_calendar_loop) that actually persists via
        record_daily_learning. See VALIDATION_FINDINGS_2026-07-18.md
        section 11. Must be registered with the supervisor in start() --
        see the self.supervisor.register(...) block above.
        """
        time.sleep(9.0)
        while CONTROL.running and (sup is None or sup.running):
            if sup: sup.beat("daily_learning")
            try:
                today = now_iso()[:10]
                mins_to_close = minutes_to_close()
                already_ran_today = (getattr(self, "_last_daily_learning_date", None) == today)
                should_run_now = (not already_ran_today) and (
                    not is_india_market_open() or (mins_to_close is not None and mins_to_close <= 5)
                )
                if should_run_now:
                    governed_learning = self.outcome_learning.summary()
                    review = self.compute_daily_learning_review()
                    review["governed_outcome_learning"] = {
                        "closed_outcomes": governed_learning.get("closed_outcomes"),
                        "backfilled_now": governed_learning.get("backfilled_now"),
                        "freshness_repaired_now": governed_learning.get("freshness_repaired_now"),
                        "normalized_outcomes_repaired_now": governed_learning.get("normalized_outcomes_repaired_now"),
                        "segments": governed_learning.get("segments") or [],
                        "policy": governed_learning.get("policy") or {},
                    }
                    self.store.record_daily_learning(review)
                    self._last_daily_learning_date = today
                    if sup:
                        sup.progress("daily_learning", token=f"{today}:{review.get('triggered_trades')}:{review.get('failed_trades')}", stage="daily_learning_complete", completed_units=1, total_units=1, expected_idle=True, waiting_on="next eligible daily cycle")
                elif sup:
                    sup.set_expected_idle("daily_learning", True, waiting_on="next eligible daily cycle")
            except Exception as exc:
                self.record_error("daily_learning", str(exc)[:200])
                if sup:
                    sup.progress("daily_learning", token=f"error:{today}:{str(exc)[:80]}", stage="daily_learning_error", waiting_on="scheduled retry", expected_idle=True)
            time.sleep(300)

    def _normalize_opportunity_case(self, d: Dict[str, Any]) -> Dict[str, Any]:
        out = dict(d or {})
        mode = str(out.get("mode") or "").lower()
        side = str(out.get("side") or "").upper()
        decision = str(out.get("decision") or "").upper()
        stage = str(out.get("opportunity_stage") or out.get("candidate_stage") or "Potential").title()
        bearish_long_only = mode == "delivery" and (side in ("BEARISH", "SHORT") or decision == "AVOID_LONG")
        if bearish_long_only:
            out["opportunity_stage"] = "Qualified" if stage == "Armed" else stage
            out["candidate_stage"] = out["opportunity_stage"]
            out["decision"] = "WATCH"
            out["side"] = "AVOID_LONG"
            if "near important support" in (out.get("discovery_buckets") or []):
                out["setup"] = "Support Reclaim Watch; current structure is bearish"
                out["waiting_for"] = out.get("waiting_for") or "support reclaim / higher-low confirmation"
            else:
                out["setup"] = "Avoid Long / Reversal Watch"
            out["risk"] = "Not a trade; long-only desk is waiting for reversal/reclaim evidence"
        return out

    def _store_discovery_watch(self, d: Dict[str, Any]) -> int:
        try:
            stage = str(d.get("candidate_stage") or "").upper()
            if stage not in ("ARMED", "QUALIFIED", "WATCH"):
                return 0
            if int(d.get("score") or 0) < 50:
                return 0
            row = dict(d)
            row["status"] = "WATCH"
            row["decision"] = "WATCH"
            row["watch_type"] = "auto_discovery"
            row["source"] = "auto_discovery"
            # v26.2 Opportunity Memory: not a trade, but an important case to rescan before the move.
            row["opportunity_stage"] = "Potential" if stage == "WATCH" else stage.title()
            row = self._normalize_opportunity_case(row)
            row["priority_score"] = self._opportunity_priority_score(row)
            row["priority_reason"] = self._priority_reason(row)
            self.store.upsert_manual_watch(row, source="auto_discovery")
            self.store.upsert_opportunity_memory(row, source="auto_discovery")
            try:
                self._set_status("opportunity_memory", dict(self.store.opportunity_summary(), state="active", last_update=now_iso(), purpose="remember potential stocks and prioritize rescans before breakout"))
            except Exception:
                pass
            return 1
        except Exception as exc:
            self.event("WARN", "discovery", "Discovery watch upsert skipped", {"error": str(exc)})
            return 0

    def _research_candidates(self, mode: str = "all", limit: int = 40) -> list[Dict[str, Any]]:
        """v51: delegate -- see core/scan_orchestration_service.py::ScanOrchestrationService._research_candidates"""
        return self.scan_orchestration._research_candidates(mode, limit)

    def candidate_discovery(self, mode: str = "all") -> Dict[str, Any]:
        """v51: delegate -- see core/scan_orchestration_service.py::ScanOrchestrationService.candidate_discovery"""
        return self.scan_orchestration.candidate_discovery(mode)

    def fundamental_context(self, instrument: Dict[str, Any], use_api: bool = False) -> Dict[str, Any]:
        """v51 (Cluster 7): delegate -- see core/reference_data_service.py::ReferenceDataService.fundamental_context"""
        return self.reference_data.fundamental_context(instrument, use_api=use_api)

    def mtf_trend(self, instrument: Dict[str, Any], refresh: bool = False) -> list[Dict[str, Any]]:
        """Cache-first MTF analysis; explicit refresh is bounded to source frames."""
        return self.market_data.mtf_trend(instrument, refresh=refresh)

    def _aggregate_minutes(self, candles: list[Dict[str, Any]], group: int = 4) -> list[Dict[str, Any]]:
        """v37.2: delegate -- see core/market_data_service.py::MarketDataService._aggregate_minutes"""
        return self.market_data._aggregate_minutes(candles, group)

    def market_context(self, instrument, mode: str, candles=None, quote=None, use_api_fund: bool = False) -> Dict[str, Any]:
        """v51: delegate -- see core/decision_engine_service.py::DecisionEngineService.market_context"""
        return self.decision_engine.market_context(
            instrument, mode, candles, quote, use_api_fund=use_api_fund,
            safe_section_fn=self._safe_section,
            resolve_sector_key_fn=self._resolve_sector_key,
            heatmap_snapshot_fn=self.heatmap_snapshot,
            sector_context_for_row_fn=self._sector_context_for_row,
            fundamental_context_fn=self.fundamental_context,
            mode_intelligence_foundation_fn=self.mode_intelligence_foundation,
            price_action_intelligence_fn=self.price_action_intelligence,
            is_market_open_fn=is_india_market_open,
            minutes_to_close_fn=minutes_to_close,
        )

    def _sync_decision_context(self, d: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        """v51: delegate -- see core/decision_engine_service.py::DecisionEngineService.sync_decision_context"""
        return self.decision_engine.sync_decision_context(d, ctx, is_market_open_fn=is_india_market_open)

    def _apply_candidate_timing(self, d: Dict[str, Any], ctx: Dict[str, Any], candles: list[Dict[str, Any]] | None = None) -> Dict[str, Any]:
        """Attach governed holding-window and thesis-expiry context."""
        mode = require_production_mode(d.get("mode"))
        now = datetime.now().astimezone()
        if mode == "intraday":
            d.update({
                "target_window": "same day, preferably 09:20–14:30; A+ only 14:15–14:30",
                "max_holding_period": "same session; exit before market close",
                "thesis_expiry": "same day; no fresh entry near close",
                "review_cadence": "every 5–15 minutes",
                "trade_budget_note": "Intraday budget: normally 0–2 trades/day across all stocks.",
                "time_window_reason": "The target needs sufficient same-day runway; promotion is blocked when the target is not realistically reachable before close.",
                "entry_cutoff": "ORB5 observe-only 09:15–09:20; entries from 09:20; A+ only 14:15–14:30; no new Intraday admission after 14:30; hard flat by governed IntradaySessionPolicy",
            })
            if ctx.get("late_session_block"):
                d["late_session_block"] = True
                d["risk"] = "Near market close; fresh same-day entry blocked"
        else:
            # Delivery horizon is setup evidence, not a desk-wide default.  Preserve
            # an explicit canonical horizon when one exists; otherwise expose the
            # absence of evidence rather than inventing months/years semantics.
            declared_horizon = (
                d.get("expected_horizon") or d.get("holding_period") or
                d.get("target_window")
            )
            d.update({
                "target_window": declared_horizon or None,
                "max_holding_period": None,
                "thesis_expiry": d.get("thesis_expiry") or None,
                "review_cadence": d.get("review_cadence") or None,
                "holding_period_state": "SETUP_DECLARED" if declared_horizon else "UNAVAILABLE",
                "trade_budget_note": "Delivery admission is portfolio-risk and quality constrained, not a daily quota.",
                "time_window_reason": (
                    "Canonical setup-declared horizon retained." if declared_horizon
                    else "No canonical setup-declared holding horizon is available; no generic horizon is manufactured."
                ),
            })
        d["production_policy_version"] = POLICY_VERSION
        return d

    def _fallback_analysis_from_context(self, symbol: str, mode: str, inst: Dict[str, Any], hist: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        """v51: delegate -- see core/decision_engine_service.py::DecisionEngineService.fallback_analysis_from_context"""
        return self.decision_engine.fallback_analysis_from_context(
            symbol, mode, inst, hist, ctx,
            apply_candidate_timing_fn=self._apply_candidate_timing,
        )

    def mode_intelligence_foundation(self) -> Dict[str, Any]:
        """v51: delegate -- see core/decision_engine_service.py::DecisionEngineService.mode_intelligence_foundation"""
        return self.decision_engine.mode_intelligence_foundation()

    def price_action_intelligence(self, candles: list[Dict[str, Any]], mode: str) -> Dict[str, Any]:
        return PriceActionIntelligenceService().analyze(candles, mode)

    def order_flow_proxy(self, candles: list[Dict[str, Any]]) -> Dict[str, Any]:
        return PriceActionIntelligenceService().order_flow(candles)

    def fundamentals_for_symbol(self, symbol: str) -> Dict[str, Any]:
        """v51 (Cluster 7): delegate -- see core/reference_data_service.py::ReferenceDataService.fundamentals_for_symbol"""
        return self.reference_data.fundamentals_for_symbol(symbol)

