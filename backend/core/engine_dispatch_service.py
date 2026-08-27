"""
EngineDispatchService — v37.4, Cluster C decoupling.

Extracts analyze_one() and _apply_liquidity_gate() out of the LadduRuntime
god object. This is the "run one instrument through its engine and produce a
decision" path -- it doesn't own historical/quote data (that's
MarketDataService) or instrument identity (that's InstrumentResolver), it
just orchestrates: fetch candles for this instrument+mode, hand them to the
engine, apply post-processing (liquidity gate), return a decision dict.

Context-building (market_context, discovery intelligence attachment, candidate
timing, decision-ledger sync) still lives on LadduRuntime for now -- those are
deeply entangled with dashboard/candidate-list state that hasn't been pulled
apart yet (that's the next decoupling pass, not this one). Rather than fake a
clean boundary that doesn't exist yet, this service takes those four pieces
as injected callables from LadduRuntime, so the *data-fetch-and-engine-call*
logic (the part that was actually duplicated/tangled) is centralized and
testable, without pretending the rest is already separated.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from config import MIN_AVG_TURNOVER_INR, MIN_ELIGIBLE_PRICE_INR
from core.production_mode_policy import require_production_mode


class EngineDispatchService:
    def __init__(self, engines: Dict[str, Any], market_data, logger=None):
        self.engines = engines
        self.market_data = market_data
        self.logger = logger

    def _log(self, level: str, message: str, detail: Optional[Dict[str, Any]] = None) -> None:
        if self.logger is not None:
            self.logger.event(level, message, detail)

    def analyze_one(self, instrument: Dict[str, Any], quote: Optional[Dict[str, Any]], mode: str,
                     *, use_api_fund: bool = False,
                     candles_override: Optional[list] = None,
                     mode_uses_history_without_live_fn: Callable[[str], bool],
                     market_context_fn: Callable[..., Dict[str, Any]],
                     quote_freshness_guard_fn: Callable[..., Dict[str, Any]],
                     sync_decision_context_fn: Callable[[Dict[str, Any], Dict[str, Any]], None],
                     apply_candidate_timing_fn: Callable[[Dict[str, Any], Dict[str, Any], list], None],
                     attach_discovery_intelligence_fn: Callable[[Dict[str, Any], Dict[str, Any], list, Dict[str, Any]], None],
                     on_bad_key: Optional[Callable[[str], None]] = None,
                     on_historical_error: Optional[Callable[[str, str], None]] = None,
                     event_risk_fn: Optional[Callable[[str], Optional[str]]] = None,
                     settle_selector_outcomes_fn: Optional[Callable[[str, str, list], Any]] = None,
                     ) -> Optional[Dict[str, Any]]:
        mode = require_production_mode(mode)
        if mode not in self.engines:
            raise RuntimeError(f"production engine registry missing canonical mode {mode}")
        engine = self.engines[mode]
        candles: list = list(candles_override or [])
        hist_error = None
        try:
            if candles_override is None:
                candles = self.market_data.get_historical(instrument["instrument_key"], engine.candle_interval, engine.days)
        except Exception as exc:
            hist_error = str(exc)
            low = hist_error.lower()
            if "bad parameter" in low or "400" in low:
                # v26.2 behavior preserved: malformed/unsupported symbol
                # history is symbol-scoped data degradation, not a global
                # API/auth block -- blacklist just this instrument_key. The
                # TTL blacklist dict itself still lives on LadduRuntime (it's
                # consulted by scanner/prefetch loops this service doesn't
                # own), so it's reported back via callback instead of owned
                # here.
                if on_bad_key:
                    on_bad_key(str(instrument.get("instrument_key") or ""))
                self._log("WARN", "Historical candle fetch degraded for symbol",
                          {"symbol": instrument.get("trading_symbol"), "mode": mode, "error": hist_error[:180]})
            else:
                if on_historical_error:
                    on_historical_error(hist_error, "/v3/historical-candle")
                self._log("WARN", "Historical candle fetch failed",
                          {"symbol": instrument.get("trading_symbol"), "mode": mode, "error": hist_error})

        # v49: cache-first is correct for slower desks, but same-day desks
        # cannot make a decision from yesterday's cache while a refresh merely
        # runs in the background. Perform one bounded interactive refresh when
        # the candle side of the freshness contract is stale/pending, then
        # re-evaluate using the returned current-session bars.
        if candles_override is None and mode == "intraday":
            first_freshness = quote_freshness_guard_fn(mode, quote, candles, engine.candle_interval)
            if str(first_freshness.get("candle_state") or "").lower() in ("stale", "pending", "invalid"):
                try:
                    refreshed = self.market_data.get_historical(
                        instrument["instrument_key"], engine.candle_interval, engine.days,
                        force=True, max_wait_sec=2.4,
                    )
                    if refreshed:
                        candles = refreshed
                    self._log("INFO", "Intraday freshness recovery attempted", {
                        "symbol": instrument.get("trading_symbol"), "mode": mode,
                        "before": first_freshness.get("candle_state"),
                        "after": quote_freshness_guard_fn(mode, quote, candles, engine.candle_interval).get("candle_state"),
                    })
                except Exception as exc:
                    self._log("WARN", "Intraday freshness recovery failed", {"symbol": instrument.get("trading_symbol"), "error": str(exc)[:160]})

        if (not quote or quote.get("ltp") is None) and mode_uses_history_without_live_fn(mode) and candles:
            last = candles[-1]
            ltp = last.get("close") or last.get("ltp")
            if ltp is not None:
                quote = {
                    "instrument_key": instrument.get("instrument_key"),
                    "symbol": instrument.get("trading_symbol"),
                    "exchange": instrument.get("exchange") or "NSE",
                    "ltp": ltp,
                    "open": last.get("open"), "high": last.get("high"),
                    "low": last.get("low"), "close": last.get("close"),
                    "volume": last.get("volume"),
                    "timestamp": "historical:" + str(last.get("timestamp") or ""),
                    "raw": {"source": "historical_candle_fallback"},
                }

        context = market_context_fn(instrument, mode, candles, quote, use_api_fund=use_api_fund)
        context["freshness"] = quote_freshness_guard_fn(mode, quote, candles, engine.candle_interval)
        if hist_error:
            context["historical_error"] = hist_error

        d = engine.analyze(instrument, quote, candles, context)
        if not d:
            return None
        out = d.to_dict()
        sync_decision_context_fn(out, context)
        apply_candidate_timing_fn(out, context, candles)
        attach_discovery_intelligence_fn(out, context, candles, instrument)
        self.apply_liquidity_gate(out, candles, quote)
        self.apply_event_risk_flag(out, instrument, event_risk_fn)
        if settle_selector_outcomes_fn is not None and candles:
            try:
                settle_selector_outcomes_fn(
                    str(instrument.get("trading_symbol") or instrument.get("symbol") or out.get("symbol") or ""),
                    mode,
                    candles,
                )
            except Exception as exc:
                self._log("WARN", "Selector outcome settlement failed", {
                    "symbol": instrument.get("trading_symbol"), "mode": mode, "error": str(exc)[:160],
                })
        return out


    def analyze_prepared(self, instrument: Dict[str, Any], quote: Optional[Dict[str, Any]], mode: str, *,
                         candles: list, context: Dict[str, Any],
                         sync_decision_context_fn: Callable[[Dict[str, Any], Dict[str, Any]], None],
                         apply_candidate_timing_fn: Callable[[Dict[str, Any], Dict[str, Any], list], None],
                         attach_discovery_intelligence_fn: Callable[[Dict[str, Any], Dict[str, Any], list, Dict[str, Any]], None],
                         event_risk_date: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Pure scanner compute boundary.

        All provider/database/context acquisition is completed by the scanner
        orchestration layer before this method is submitted to a bounded analysis
        worker.  The worker performs mathematical analysis and deterministic
        in-memory decoration only.  It never fetches history/fundamentals, settles
        outcomes, writes decisions, or invokes ranking/governance repositories.
        This makes worker deadlines meaningful and prevents an I/O-stalled call
        from permanently consuming scanner capacity.
        """
        mode = require_production_mode(mode)
        if mode not in self.engines:
            raise RuntimeError(f"production engine registry missing canonical mode {mode}")
        engine = self.engines[mode]
        local_candles = list(candles or [])
        local_context = dict(context or {})
        d = engine.analyze(instrument, quote, local_candles, local_context)
        if not d:
            return None
        out = d.to_dict()
        sync_decision_context_fn(out, local_context)
        apply_candidate_timing_fn(out, local_context, local_candles)
        attach_discovery_intelligence_fn(out, local_context, local_candles, instrument)
        self.apply_liquidity_gate(out, local_candles, quote)
        if event_risk_date:
            out["event_risk"] = {"flag": True, "nearest_event_date": event_risk_date}
            out["reason"] = (str(out.get("reason") or "") + f"; Event risk: earnings/board meeting on {event_risk_date}").strip("; ")
        else:
            out["event_risk"] = {"flag": False}
        return out

    def apply_event_risk_flag(self, out: Dict[str, Any], instrument: Dict[str, Any], event_risk_fn) -> None:
        """Phase 6: flags a technically clean setup that has earnings/a
        board meeting within the near-term window -- does NOT block or
        downgrade the decision. Per the architecture doc: 'a technically
        clean setup with earnings in 2 days should be flagged, not
        silently promoted the same as any other setup' -- the human (or
        a later, deliberate gating pass) decides what to do with the flag,
        this only surfaces the fact."""
        if event_risk_fn is None:
            return
        try:
            symbol = instrument.get("trading_symbol") or instrument.get("symbol") or ""
            nearest = event_risk_fn(symbol) if symbol else None
            if nearest:
                out["event_risk"] = {"flag": True, "nearest_event_date": nearest}
                out["reason"] = (out.get("reason") or "") + f"; Event risk: earnings/board meeting on {nearest}"
            else:
                out["event_risk"] = {"flag": False}
        except Exception as exc:
            self._log("WARN", "Event-risk flag evaluation failed", {"error": str(exc)[:160]})

    def apply_liquidity_gate(self, out: Dict[str, Any], candles, quote) -> None:
        """Reject illiquid names from ever reaching TRADE/ACCUMULATE. A high
        technical score on a thinly-traded stock is a false positive -- you
        can't fill or exit it at the modeled price."""
        try:
            vols = [c.get("volume") for c in (candles or [])[-20:] if c.get("volume") is not None]
            closes_ = [c.get("close") for c in (candles or [])[-20:] if c.get("close") is not None]
            if vols and closes_:
                avg_vol = sum(vols) / len(vols)
                avg_price = sum(closes_) / len(closes_)
                avg_turnover = avg_vol * avg_price
            else:
                avg_turnover = None
            # Preserve the computed evidence for the immutable scan population.
            # Delivery candles are daily here. Intraday candidates are enriched
            # from canonical daily candles by QuantScanCaptureService instead;
            # do not mislabel a 20x5-minute average as daily liquidity.
            if (
                str(out.get("mode") or "").lower() == "delivery"
                and avg_turnover is not None
                and len(vols) >= 20
                and len(closes_) >= 20
            ):
                latest = (candles or [])[-1] if candles else {}
                out.update({
                    "avg_daily_value": round(float(avg_turnover), 2),
                    "avg_volume_20d": round(float(avg_vol), 2),
                    "avg_daily_value_sessions": 20,
                    "avg_daily_value_as_of": (
                        latest.get("timestamp")
                        or latest.get("ts")
                        or latest.get("time")
                        or latest.get("date")
                        or out.get("candle_as_of")
                    ),
                    "avg_daily_value_freshness_state": "VERIFIED_CLOSE",
                    "avg_daily_value_source": "delivery_analysis_daily_candles",
                })
            if out.get("status") not in ("PROMOTED",):
                return
            latest_close = None
            if candles:
                try:
                    latest_close = float((candles or [])[-1].get("close"))
                except (TypeError, ValueError, AttributeError):
                    latest_close = None
            quote_price = None
            for key in ("ltp", "last_price", "current_price", "price", "close"):
                try:
                    candidate = float((quote or {}).get(key))
                except (TypeError, ValueError, AttributeError):
                    continue
                if candidate > 0:
                    quote_price = candidate
                    break
            eligible_price = quote_price or latest_close
            if eligible_price is not None and eligible_price < MIN_ELIGIBLE_PRICE_INR:
                out["decision"] = "WATCH"
                out["status"] = "WATCH"
                out["confidence"] = "LOW"
                out["setup"] = (
                    f"Blocked by tradeability gate: price ₹{eligible_price:.2f} below configured "
                    f"minimum ₹{MIN_ELIGIBLE_PRICE_INR:.2f}"
                )
                out["reason"] = (out.get("reason") or "") + "; Penny-stock gate: price below configured production minimum"
                out["tradeability_gate"] = {
                    "state": "BLOCKED_LOW_PRICE",
                    "price": round(float(eligible_price), 4),
                    "minimum_price": round(float(MIN_ELIGIBLE_PRICE_INR), 4),
                }
                return
            if avg_turnover is not None and avg_turnover < MIN_AVG_TURNOVER_INR:
                out["decision"] = "WATCH"
                out["status"] = "WATCH"
                out["confidence"] = "LOW"
                out["setup"] = f"Blocked by liquidity gate: avg daily turnover ~₹{avg_turnover/1e7:.1f}cr below required ₹{MIN_AVG_TURNOVER_INR/1e7:.1f}cr"
                out["reason"] = (out.get("reason") or "") + "; Liquidity gate: insufficient average turnover for reliable fills"
        except Exception as exc:
            self._log("WARN", "Liquidity gate evaluation failed", {"error": str(exc)[:160]})
