"""
DecisionEngineService — v51, Cluster 4 decoupling.

Extracts market_context(), _sync_decision_context(), _fallback_analysis_from_context(),
mode_intelligence_foundation(), analyze_symbol(), and mtf_trend_for_symbol() out of the
LadduRuntime god object.

analyze_one() and mtf_trend()/_aggregate_minutes() were already extracted in earlier
clusters (engine_dispatch_service.py, market_data_service.py respectively) and are left
alone here -- LadduRuntime keeps its existing one-line delegators for those.

Same pattern as engine_dispatch_service.py: this service doesn't own dashboard state,
sector/fundamentals resolution, or discovery-candidate bookkeeping, so those are taken
as injected callables rather than faked as owned here. mode_intelligence_foundation is
pure static data and has no dependencies at all.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Callable, Dict, Optional

from engines import ENGINES
from config import DATA_DIR
from models import now_iso
from market_layers import market_structure, volume_profile, orb_context, heat_strip_context
from delivery_timeframes import delivery_timeframe_context
from core.promotion_math_service import PromotionMathService
from core.numeric_semantics import finite_number, positive_number
from core.nse_official_evidence_service import NseOfficialEvidenceService
from core.intraday_session_policy import IntradaySessionPolicy
from core.production_mode_policy import (
    FINAL_DECISION_PIPELINE_VERSION, FINAL_PROMOTION_AUTHORITY, POLICY_VERSION, UnsupportedProductionMode, policy_for, require_production_mode, production_policy_snapshot,
)


class DecisionEngineService:
    def __init__(self, logger=None):
        self.logger = logger
        self._nse_official = NseOfficialEvidenceService(DATA_DIR)
        self._intraday_session_policy = IntradaySessionPolicy()

    def _log(self, level: str, message: str, detail: Optional[Dict[str, Any]] = None) -> None:
        if self.logger is not None:
            self.logger.event(level, message, detail)

    # ------------------------------------------------------------------
    # Pure static data -- no dependencies.
    # ------------------------------------------------------------------
    def mode_intelligence_foundation(self) -> Dict[str, Any]:
        """Canonical production-desk intelligence contract.

        The production surface exposes only the two governed cash desks so
        UI/API callers cannot infer any additional executable desk.
        """
        policies = production_policy_snapshot()
        return {
            "intraday": {
                "horizon": "same day + 5D/20D context",
                "timeframes": ["5m", "15m", "30m", "1H", "4H", "1D"],
                "primary": ["verified live quote", "ORB5", "session structure", "VWAP/EMA20/50", "same-clock RVOL", "sector/index confirmation"],
                "supporting": ["price-action S/R role flips", "official NSE participation/risk evidence", "event-risk flag"],
                "fundamentals_weight": "safety filter only",
                "promotion_gate": "09:20+ ORB5/session-structure confluence + VWAP/EMA + same-clock RVOL + structural map + time-to-target + net R:R + risk authority",
                "trade_budget": "normally 0–2 trades/day",
                "candidate_model": "historical research prepares candidates; live engine validates only",
                "policy": policies["intraday"],
            },
            "delivery": {
                "horizon": "setup-declared; unavailable until the canonical setup specifies a horizon",
                "timeframes": ["30m", "1H", "4H", "1D", "1W", "1M"],
                "primary": ["verified fundamentals", "institutional delivery evidence", "daily/weekly/monthly structure", "valuation and risk"],
                "supporting": ["volume profile", "sector leadership", "event-risk flag"],
                "fundamentals_weight": "mandatory/high",
                "promotion_gate": "fundamentals + institutional evidence + MTF alignment + validated map + net R:R + risk authority",
                "trade_budget": "quality constrained; portfolio-risk authority decides admission",
                "policy": policies["delivery"],
            },
        }

    # ------------------------------------------------------------------
    # No self.* dependencies -- only free functions injected by the caller.
    # ------------------------------------------------------------------
    def sync_decision_context(self, d: Dict[str, Any], ctx: Dict[str, Any], *,
                               is_market_open_fn: Callable[[], bool]) -> Dict[str, Any]:
        """v24: keep decision.index_context / sector_context aligned with heat_context and heatmap."""
        try:
            d["market_open_at_decision"] = is_market_open_fn() is True
            d["session_date"] = datetime.now().astimezone().date().isoformat()
            fresh = ctx.get("freshness") or {}
            if isinstance(fresh, dict):
                d["freshness_state"] = fresh.get("state")
                d["price_freshness_state"] = fresh.get("state")
                d["quote_age_seconds"] = finite_number(fresh.get("quote_age_seconds"))
                d["candle_age_seconds"] = finite_number(fresh.get("candle_age_seconds"))
                d["candle_state"] = fresh.get("candle_state")
                d["candle_freshness_state"] = fresh.get("candle_state")
                d["stale_guard"] = fresh.get("stale_guard")
                d["decision_as_of"] = d.get("decision_as_of") or now_iso()
                if fresh.get("same_day_blocked") is True and str(d.get("status") or "").upper() == "PROMOTED":
                    d["decision"] = "WAIT"
                    d["status"] = "BLOCKED"
                    d["entry"] = d["t1"] = d["t2"] = d["sl"] = d["rr"] = None
                    d["setup"] = "Stale-data guard blocked actionable setup"
                    d["risk"] = "Stale/delayed data"
                    d["reason"] = (d.get("reason") or "") + "; Stale-data guard removed actionability"
            heat = ctx.get("heat_context") or {}
            if isinstance(heat, dict):
                state = heat.get("state")
                summary = heat.get("summary")
                if state and (not d.get("index_context") or str(d.get("index_context")).lower() in ("pending", "neutral")):
                    d["index_context"] = state
                if summary and (not d.get("sector_context") or str(d.get("sector_context")).lower() == "pending"):
                    d["sector_context"] = summary
                d["market_context_score"] = (
                    finite_number(heat.get("score")) if heat.get("ok") is True
                    else finite_number(d.get("market_context_score"))
                )
            # PL33: project already-authoritative PIT evidence into the canonical
            # decision row so immutable Research capture does not drop it.
            # Missing/partial official evidence stays missing; no neutral zero is
            # substituted.  Preserve the service payload for provenance.
            official = ctx.get("official_nse_evidence") or {}
            if isinstance(official, dict):
                d["official_nse_evidence"] = official
                features = official.get("decision_features") or {}
                if isinstance(features, dict):
                    delivery_pct_z = finite_number(features.get("delivery_pct_surprise"))
                    delivered_qty_z = finite_number(features.get("delivered_quantity_surprise"))
                    if delivery_pct_z is not None and d.get("delivery_pct_zscore") is None:
                        d["delivery_pct_zscore"] = delivery_pct_z
                    if delivered_qty_z is not None and d.get("delivered_qty_zscore") is None:
                        d["delivered_qty_zscore"] = delivered_qty_z
                if official.get("as_of") not in (None, ""):
                    d["official_nse_as_of"] = official.get("as_of")
                d["official_nse_state"] = official.get("state")
            fundamentals = ctx.get("fundamentals") or {}
            if isinstance(fundamentals, dict) and fundamentals.get("ok") is True:
                fund_score = finite_number(fundamentals.get("score"))
                if fund_score is not None and d.get("fundamental_score") is None:
                    d["fundamental_score"] = fund_score
                    d["fundamental_as_of"] = (
                        fundamentals.get("as_of") or fundamentals.get("updated_at")
                        or d.get("decision_as_of") or now_iso()
                    )
        except Exception:
            pass
        return d

    def fallback_analysis_from_context(self, symbol: str, mode: str, inst: Dict[str, Any],
                                        hist: Dict[str, Any], ctx: Dict[str, Any], *,
                                        apply_candidate_timing_fn: Callable[[Dict[str, Any], Dict[str, Any], list], None],
                                        ) -> Dict[str, Any]:
        """If full engine analysis fails but fundamentals/price-action exists, return a safe research row instead of 'analysis pending'."""
        candles = hist.get("candles") or []
        last = (hist.get("last_candle") or (candles[-1] if candles else {})) or {}
        raw_ltp = last.get("close") if last.get("close") is not None else last.get("ltp")
        ltp = positive_number(raw_ltp)
        ms = ctx.get("market_structure") if isinstance(ctx.get("market_structure"), dict) else {}
        fund = ctx.get("fundamentals") if isinstance(ctx.get("fundamentals"), dict) else {}
        heat = ctx.get("heat_context") if isinstance(ctx.get("heat_context"), dict) else {}
        ms_ok = ms.get("ok") is True
        fund_ok = fund.get("ok") is True
        heat_ok = heat.get("ok") is True
        side = "BEARISH" if ms_ok and str(ms.get("bias") or "").lower() == "short" else "LONG" if ms_ok and str(ms.get("bias") or "").lower() == "long" else "WAIT"
        decision = "AVOID_LONG" if side == "BEARISH" and mode == "delivery" else "WATCH"
        ms_score = finite_number(ms.get("score")) if ms_ok else None
        fund_score = finite_number(fund.get("score")) if fund_ok else None
        heat_score = finite_number(heat.get("score")) if heat_ok else None
        score_value = (ms_score or 0.0) + (fund_score or 0.0) * (0.35 if mode == "delivery" else 0.15) + abs(heat_score or 0.0)
        score = int(max(0.0, min(100.0, score_value)))
        open_price = positive_number(last.get("open"))
        d = {
            "symbol": symbol, "exchange": inst.get("exchange") or "NSE", "mode": mode, "side": side, "decision": decision, "status": "WATCH",
            "ltp": ltp, "entry": None, "t1": None, "t2": None, "sl": None, "rr": None, "score": max(0, min(100, score)), "confidence": "LOW",
            "open": open_price, "change_pct": (round(((ltp - open_price) / open_price) * 100, 2) if ltp is not None and open_price is not None else None),
            "setup": "Safe research fallback; engine analysis was partial", "risk": "Partial analysis; validate before action",
            "reason": "Full engine analysis was partial, but fundamentals/price action/heat context loaded. Use as research/watch only, not trade.",
            "price_freshness": "historical @ " + str(last.get("timestamp") or now_iso()), "last_refresh": str(last.get("timestamp") or now_iso()), "last_ai_validation": now_iso(),
            "holding_policy": ENGINES[mode].holding_policy,
            "index_context": heat.get("state") or "pending", "sector_context": heat.get("summary") or "pending",
            "support": finite_number(ms.get("support")) if ms_ok else None, "resistance": finite_number(ms.get("resistance")) if ms_ok else None,
            "fundamental_score": fund_score, "quality_score": finite_number(fund.get("quality")) if fund_ok else None, "valuation_score": finite_number(fund.get("valuation")) if fund_ok else None, "fundamental_state": fund.get("state") if fund_ok else "pending",
            "market_structure": ms.get("state") if ms_ok else "pending", "market_structure_score": ms_score,
            "volume_profile": (ctx.get("volume_profile") or {}).get("state") or "pending",
            "orb_state": (ctx.get("orb") or {}).get("state") or "not_applicable", "market_context_score": heat_score,
            "evidence": ["safe fallback row", str(ms.get("summary") or ""), str(fund.get("reason") or "")],
        }
        apply_candidate_timing_fn(d, ctx, candles)
        return {"ok": True, "symbol": symbol, "mode": mode, "instrument": inst, "quote_error": None, "decision": d, "fallback": True}

    # ------------------------------------------------------------------
    # Heavily entangled with dashboard/sector/fundamentals state that
    # hasn't been decoupled yet -- taken as injected callables, same
    # rationale as engine_dispatch_service.py's analyze_one().
    # ------------------------------------------------------------------
    def market_context(self, instrument, mode: str, candles=None, quote=None, use_api_fund: bool = False, *,
                        safe_section_fn: Callable[..., Any],
                        resolve_sector_key_fn: Callable[[Dict[str, Any]], Any],
                        heatmap_snapshot_fn: Callable[[], list],
                        sector_context_for_row_fn: Callable[[Dict[str, Any], list], Dict[str, Any]],
                        fundamental_context_fn: Callable[..., Dict[str, Any]],
                        mode_intelligence_foundation_fn: Callable[[], Dict[str, Any]],
                        price_action_intelligence_fn: Callable[[list, str], Dict[str, Any]],
                        is_market_open_fn: Callable[[], bool],
                        minutes_to_close_fn: Callable[[], Optional[int]],
                        ) -> Dict[str, Any]:
        instrument = instrument or {}
        candles = candles or []
        symbol = instrument.get("trading_symbol") or ""
        sector_key = safe_section_fn("layer_sector_resolver", lambda: resolve_sector_key_fn({"symbol": symbol, "name": instrument.get("name"), "sector": instrument.get("sector")}), None)
        sector_hint = sector_key
        # Every intelligence layer is optional when a local dataset is still warming.
        # Keep one missing layer from collapsing the complete stock-intelligence response.
        ms = safe_section_fn("layer_market_structure", lambda: market_structure(candles), None) or {"ok": False, "state": "pending", "summary": "Market structure is warming"}
        vp = safe_section_fn("layer_volume_profile", lambda: volume_profile(candles), None) or {"ok": False, "state": "pending", "summary": "Volume profile is warming"}
        orb = (safe_section_fn("layer_orb", lambda: orb_context(candles), None) or {"ok": False, "state": "pending", "bias": "neutral", "score": 0, "summary": "ORB is warming"}) if mode == "intraday" else {"ok": False, "state": "not_applicable", "bias": "neutral", "score": 0, "summary": "ORB is same-day desk layer only"}
        heat_rows = safe_section_fn("layer_heatmap", heatmap_snapshot_fn, []) or []
        heat = safe_section_fn("layer_heat_strip", lambda: heat_strip_context(heat_rows, sector_hint), None) or {"ok": False, "state": "pending", "score": 0, "summary": "Market breadth is warming"}
        sector_ctx = safe_section_fn("layer_sector_context", lambda: sector_context_for_row_fn({"symbol": symbol, "name": instrument.get("name"), "sector": instrument.get("sector")}, heat_rows), {}) or {}
        heat.update(sector_ctx)
        fund = safe_section_fn("layer_fundamentals", lambda: fundamental_context_fn(instrument, use_api=use_api_fund), None) or {"ok": False, "state": "pending", "score": 0, "summary": "Fundamentals are warming"}
        mode_intelligence = safe_section_fn("layer_mode_intelligence", mode_intelligence_foundation_fn, {}) or {}
        price_action = safe_section_fn("layer_price_action", lambda: price_action_intelligence_fn(candles, mode), None) or {"ok": False, "state": "pending", "summary": "Price action is warming"}
        delivery_mtf = delivery_timeframe_context(
            candles, instrument_key=str(instrument.get("instrument_key") or symbol or "UNKNOWN")
        ) if mode == "delivery" else {}
        # PL33: the retained/cache-first NSE official evidence authority is
        # relevant to both desks.  Delivery research previously left this
        # disconnected even though the service already computes PIT delivery-%
        # and delivered-quantity surprises.  This remains local/read-only and
        # bounded to the exact decision date; no provider HTTP is introduced.
        official_nse = (safe_section_fn(
            "layer_official_nse", lambda: self._nse_official.latest(symbol, (quote or {}).get("timestamp")), None
        ) or {"ok": False, "state": "OFFICIAL_EVIDENCE_PENDING", "decision_features": {}, "risk_blocks": []})
        intraday_session = (safe_section_fn(
            "layer_intraday_session_policy", self._intraday_session_policy.at, None
        ) or {"phase": "CALENDAR_UNVERIFIED", "new_entry_allowed": False, "a_plus_only": False}) if mode == "intraday" else {}

        if mode == "delivery":
            # Canonical FundamentalScoringAuthority owns the score->state
            # threshold.  Do not recreate an older numeric cutoff here.
            fundamentals_ok = (
                fund.get("ok") is True
                and str(fund.get("state") or "").strip().lower() in {"strong", "acceptable"}
            )
        else:
            fundamentals_ok = True

        market_open = safe_section_fn("layer_market_clock", is_market_open_fn, False) is True
        close_minutes = finite_number(safe_section_fn("layer_close_clock", minutes_to_close_fn, None))

        return {
            "market_open": market_open,
            "minutes_to_close": close_minutes,
            "late_session_block": bool(mode == "intraday" and str(intraday_session.get("phase") or "") in {"NO_NEW_INTRADAY", "MANDATORY_FLAT"}),
            "hard_late_session_block": bool(mode == "intraday" and intraday_session.get("mandatory_flat") is True),
            "index_context": heat.get("state", "neutral"),
            "sector_context": heat.get("sector_reason") or f"{sector_hint or 'sector unavailable'} · {heat.get('summary')}",
            "fundamentals_ok": fundamentals_ok,
            "fundamentals": fund,
            "market_structure": ms,
            "volume_profile": vp,
            "orb": orb,
            "official_nse_evidence": official_nse,
            "intraday_session": intraday_session,
            "heat_context": heat,
            "mode_intelligence": mode_intelligence.get(mode, {}),
            "price_action": price_action,
            "delivery_timeframes": delivery_mtf,
        }

    def analyze_symbol(self, symbol: str, mode: str = "delivery", *,
                        engines: Dict[str, Any],
                        first_instrument_fn: Callable[..., Optional[Dict[str, Any]]],
                        instrument_count_fn: Callable[[], int],
                        token_status_ok_fn: Callable[[], bool],
                        quotes_fn: Callable[[list], list],
                        record_error_fn: Callable[[str, str, str], None],
                        event_fn: Callable[[str, str, str, Dict[str, Any]], None],
                        mode_uses_history_without_live_fn: Callable[[str], bool],
                        analyze_one_fn: Callable[..., Optional[Dict[str, Any]]],
                        add_priority_fn: Callable[[str, str, str, str], None],
                        save_decision_fn: Callable[[Dict[str, Any]], None],
                        is_actionable_selected_fn: Callable[[Dict[str, Any]], bool],
                        upsert_manual_watch_fn: Callable[[Dict[str, Any]], None],
                        on_ai_validation: Optional[Callable[[], None]] = None,
                        ) -> Dict[str, Any]:
        symbol = (symbol or "").strip().upper()
        try:
            mode = require_production_mode(mode or "delivery")
        except UnsupportedProductionMode as exc:
            return {
                "ok": False,
                "error": str(exc),
                "error_code": "UNSUPPORTED_PRODUCTION_MODE",
                "symbol": symbol,
                "mode": str(mode or "").lower(),
                "allowed_modes": ["intraday", "delivery"],
            }
        if mode not in engines:
            return {"ok": False, "error": f"production engine unavailable for {mode}", "error_code": "ENGINE_REGISTRY_INCOMPLETE", "symbol": symbol, "mode": mode}
        inst = first_instrument_fn(symbol)
        if not inst:
            return {"ok": False, "error": "instrument not found", "symbol": symbol, "instrument_count": instrument_count_fn()}
        quote = None
        quote_error = None
        if token_status_ok_fn():
            try:
                qs = quotes_fn([inst])
                quote = qs[0] if qs else None
            except Exception as exc:
                quote_error = str(exc)
                record_error_fn("quote", quote_error, "/v3/market-quote/ltp")
                event_fn("WARN", "quote", "Analyze quote failed", {"symbol": symbol, "error": quote_error})
                if not mode_uses_history_without_live_fn(mode):
                    return {"ok": False, "error": "Live quote required for this mode", "symbol": symbol, "mode": mode, "instrument": inst, "quote_error": quote_error}
        try:
            decision = analyze_one_fn(inst, quote, mode, use_api_fund=True)
            if decision:
                add_priority_fn(inst.get("trading_symbol") or symbol, inst.get("exchange") or "NSE", mode, "manual_analyze")
                save_decision_fn(decision)
                if not is_actionable_selected_fn(decision):
                    upsert_manual_watch_fn(decision)
                if on_ai_validation is not None:
                    on_ai_validation()
            return {"ok": bool(decision), "symbol": symbol, "mode": mode, "instrument": inst, "quote_error": quote_error, "decision": decision}
        except Exception as exc:
            event_fn("WARN", "analysis", "Analyze failed", {"symbol": symbol, "mode": mode, "error": str(exc)})
            return {"ok": False, "error": str(exc), "symbol": symbol, "mode": mode, "instrument": inst, "quote_error": quote_error}

    def finalize_candidate(self, candidate: Dict[str, Any], *, ranking_service: Any, delivery: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Single final production state transition.

        Engines may propose PROMOTED and the evidence service may report READY,
        but only this method can stamp the final promotion authority after
        evidence, governed AI blending and portfolio/operational risk admission.
        """
        mode = require_production_mode((candidate or {}).get("mode"))
        out = ranking_service.apply(dict(candidate, mode=mode), delivery)
        out["promotion_authority"] = FINAL_PROMOTION_AUTHORITY
        out["decision_pipeline_version"] = FINAL_DECISION_PIPELINE_VERSION
        out["policy_version"] = POLICY_VERSION
        out["allowed_production_modes"] = ["intraday", "delivery"]
        out["promotion_math"] = PromotionMathService.evaluate(out)

        invariant_failures = []
        policy = policy_for(mode)
        final_score = finite_number(out.get("rank_score") if out.get("rank_score") is not None else out.get("score"))
        if final_score is None or not (0.0 <= final_score <= 100.0):
            final_score_valid = False
            final_score_for_compare = float("-inf")
        else:
            final_score_valid = True
            final_score_for_compare = final_score
        if not final_score_valid:
            invariant_failures.append("final rank score is missing/non-finite/out-of-range")
        if str(out.get("rank_readiness") or "").upper() != "READY":
            invariant_failures.append("canonical evidence readiness is not READY")
        if str(out.get("rank_scoring_state") or "").upper() != "NORMAL":
            invariant_failures.append("evidence scoring is degraded or blocked")
        if final_score_for_compare < float(policy.promotion_threshold):
            invariant_failures.append(f"final rank score is below the {policy.promotion_threshold} promotion threshold")
        if mode == "delivery" and str(out.get("side") or "").upper() != "LONG":
            invariant_failures.append("Delivery production desk is long-only")
        if mode == "intraday" and out.get("market_open_at_decision") is not True:
            invariant_failures.append("Intraday final promotion requires market-open decision time")
        if str(out.get("risk_admission_state") or "") != "APPROVED_CAPITAL":
            invariant_failures.append("capital risk admission is not approved")
        governed_edge_gates = out.get("governed_edge_gates")
        if not isinstance(governed_edge_gates, dict) or governed_edge_gates.get("passed") is not True:
            invariant_failures.append("calibrated edge / execution / event / drift admission did not pass")
        promotion_math = out.get("promotion_math") if isinstance(out.get("promotion_math"), dict) else {}
        if promotion_math.get("gate") == "BLOCK":
            invariant_failures.append(str(promotion_math.get("reason") or "promotion mathematics blocked the candidate"))
        if str(out.get("status") or "").upper() == "PROMOTED" and invariant_failures:
            out["status"] = "WATCH"
            out["decision"] = "WATCH"
            out["promotion_blocked_by"] = list(dict.fromkeys(list(out.get("promotion_blocked_by") or []) + invariant_failures))
            out["reason"] = (str(out.get("reason") or "") + "; Final decision authority blocked promotion: " + ", ".join(invariant_failures)).strip("; ")

        promoted = str(out.get("status") or "").upper() == "PROMOTED"
        out["final_decision_state"] = "PROMOTED" if promoted else (
            "RESEARCH_ONLY" if str(out.get("risk_admission_state") or "") == "APPROVED_RESEARCH_ONLY" else "WATCH_OR_BLOCKED"
        )
        out["final_promotion_invariants"] = {
            "mode_supported": True,
            "evidence_ready": str(out.get("rank_readiness") or "").upper() == "READY",
            "scoring_normal": str(out.get("rank_scoring_state") or "").upper() == "NORMAL",
            "promotion_score_met": final_score_valid and final_score_for_compare >= float(policy.promotion_threshold),
            "promotion_threshold": policy.promotion_threshold,
            "desk_direction_valid": str(out.get("side") or "").upper() == "LONG" if mode == "delivery" else str(out.get("side") or "").upper() in ("LONG", "SHORT"),
            "market_time_valid": out.get("market_open_at_decision") is True if mode == "intraday" else True,
            "capital_approved": str(out.get("risk_admission_state") or "") == "APPROVED_CAPITAL",
            "governed_edge_gates_passed": isinstance(governed_edge_gates, dict) and governed_edge_gates.get("passed") is True,
            "promotion_math_gate": promotion_math.get("gate"),
            "promotion_math_passed": promotion_math.get("gate") in {"PASS", "SHADOW"},
            "promotion_math_measured": promotion_math.get("gate") in {"PASS", "BLOCK"},
            "passed": promoted and not invariant_failures and final_score_valid,
        }
        return out

    def mtf_trend_for_symbol(self, symbol: str, *,
                              first_instrument_fn: Callable[..., Optional[Dict[str, Any]]],
                              final_fallback_instrument_fn: Callable[[str], Optional[Dict[str, Any]]],
                              safe_section_fn: Callable[..., Any],
                              mtf_trend_fn: Callable[[Dict[str, Any]], list],
                              refresh: bool = False,
                              ) -> Dict[str, Any]:
        """v36.9.15: standalone MTF endpoint, split out of symbol_market_intelligence
        so the frontend can fetch it in parallel instead of it gating the whole
        Stock Intelligence panel. All ten supported timeframes are resolved here; this is the
        only place that budget is spent now.
        """
        symbol = (symbol or "").strip().upper()
        if not symbol:
            return {"ok": False, "symbol": symbol, "mtf_trend": [], "error": "symbol required"}
        inst = first_instrument_fn(symbol) or final_fallback_instrument_fn(symbol)
        if not inst:
            return {"ok": False, "symbol": symbol, "mtf_trend": [], "error": "instrument not resolved"}
        mtf = safe_section_fn("mtf_trend", lambda: mtf_trend_fn(inst, refresh=refresh), [])
        return {"ok": True, "symbol": symbol, "instrument": inst, "mtf_trend": mtf, "refresh_requested": bool(refresh), "time": now_iso()}
