from __future__ import annotations
from typing import Any, Dict, List, Optional

from indicators import closes, support_resistance, vwap
from models import Decision, now_iso
from session_candles import closed_candles, current_session_candles
from core.structural_trade_map_service import StructuralTradeMapService
from core.production_mode_policy import PRODUCTION_MODES, policy_for
from core.india_cost_model import IndiaCashCostModel
from core.indicator_snapshot_authority import DEFAULT_INDICATOR_SNAPSHOT_AUTHORITY
from core.trade_geometry_authority import DEFAULT_TRADE_GEOMETRY_AUTHORITY
from core.intraday_session_structure_authority import DEFAULT_INTRADAY_SESSION_STRUCTURE_AUTHORITY
from core.strategy_qualification_authority import DEFAULT_STRATEGY_QUALIFICATION_AUTHORITY
from core.strategy_mathematics_contract_authority import DEFAULT_STRATEGY_MATHEMATICS_CONTRACT_AUTHORITY
from core.numeric_semantics import finite_number


def _score_int(value: Any) -> int | None:
    number = finite_number(value)
    if number is None or not float(number).is_integer():
        return None
    integer = int(number)
    return integer if 0 <= integer <= 100 else None


def _nonnegative_int(value: Any) -> int | None:
    number = finite_number(value)
    if number is None or not float(number).is_integer() or int(number) < 0:
        return None
    return int(number)


def _min_rr_for_mode(mode: str) -> float:
    """Return the canonical production desk R:R floor; fail closed otherwise."""
    desk = str(mode or "").strip().lower()
    if desk not in PRODUCTION_MODES:
        raise ValueError(f"Unsupported production desk: {mode!r}")
    return policy_for(desk).minimum_net_rr


def _clamp_targets(side: str, entry: float, sl: float, t1: float, t2: float, support, resistance):
    """Compatibility projection; invalid inputs fail closed.

    Production geometry is owned by TradeGeometryAuthority.  This legacy helper
    must never turn booleans/NaN into finite trade geometry.
    """
    side_u = str(side or "").upper()
    e, stop, one, two = (finite_number(v) for v in (entry, sl, t1, t2))
    if side_u not in {"LONG", "SHORT"} or None in (e, stop, one, two):
        return None, None, None, "invalid"
    sup, res = finite_number(support), finite_number(resistance)
    if (support is not None and sup is None) or (resistance is not None and res is None):
        return None, None, None, "invalid"
    source = "atr"
    if side_u == "LONG":
        if res is not None and res > e:
            if one > res: one=max(e+(res-e)*0.85,e); source="structure"
            if two > res: two=res; source="structure"
        if sup is not None and sup < e and stop < sup: stop=sup*0.997; source="structure"
    else:
        if sup is not None and sup < e:
            if one < sup: one=min(e-(e-sup)*0.85,e); source="structure"
            if two < sup: two=sup; source="structure"
        if res is not None and res > e and stop > res: stop=res*1.003; source="structure"
    return stop,one,two,source



def _validate_level_map(side: str, entry, sl, t1, t2, support, resistance):
    """Compatibility facade over TradeGeometryAuthority map validation."""
    proof = DEFAULT_TRADE_GEOMETRY_AUTHORITY.validate_map(
        side=side, entry=entry, stop=sl, target_1=t1, target_2=t2,
        support=support, resistance=resistance,
    )
    return proof["valid"], proof["message"], "valid" if proof["valid"] else "invalid"



def _net_rr(entry: float, sl: float, t1: float, side: str, mode: str = "delivery"):
    """Desk-aware post-cost R:R from the canonical India cash cost model."""
    try:
        desk = str(mode or "delivery").strip().lower()
        direction = str(side or "").strip().upper()
        e, stop, target = finite_number(entry), finite_number(sl), finite_number(t1)
        if desk not in PRODUCTION_MODES or direction not in {"LONG", "SHORT"} or None in (e, stop, target):
            return None
        if min(e, stop, target) <= 0:
            return None
        report = IndiaCashCostModel.for_evidence(desk, {}).post_cost_rr(
            entry=e, stop=stop, target=target, side=direction
        )
        value = finite_number(report.get("post_cost_rr"))
        return round(value, 2) if value is not None else None
    except (TypeError, ValueError, KeyError):
        return None



def _round(v, n=2):
    value = finite_number(v)
    return round(value, n) if value is not None else None


def _fresh(ts: str) -> str:
    if not ts:
        return "pending"
    text = str(ts)
    if text.startswith("historical:"):
        return "historical @ " + text.replace("historical:", "", 1)
    return f"live @ {text}"


def _rr(entry, sl, t1):
    e, stop, target = finite_number(entry), finite_number(sl), finite_number(t1)
    if None in (e, stop, target):
        return None
    risk = abs(e - stop)
    if risk <= 0:
        return None
    return round(abs(target - e) / risk, 2)


class BaseEngine:
    """Mode-isolated research engine.

    Design rule: the two production desks do not share promotion rules blindly.
    - Intraday requires live quote and market-open/time-left checks.
    - Delivery can work from verified completed-session candles after market close.
    - Delivery cannot be promoted if mandatory fundamentals are absent.
    """

    mode = "base"
    strategy_version = "dual-desk-heuristic-strategy-1.0.0-unqualified"
    holding_policy = ""
    required_history = 40
    candle_interval = "day"
    days = 60
    same_day = False
    needs_live_quote = False
    long_only = False
    require_fundamentals = False

    def analyze(self, instrument: Dict[str, Any], quote: Dict[str, Any], candles: List[Dict[str, Any]], context: Dict[str, Any]) -> Optional[Decision]:
        if not quote or quote.get("ltp") is None:
            return None

        if self.same_day:
            candles = closed_candles(candles, self.candle_interval)

        freshness = context.get("freshness") or {}
        freshness_state = str(freshness.get("state") or "unknown").lower()
        candle_state = str(freshness.get("candle_state") or "unknown").lower()

        if self.same_day:
            if freshness_state in ("stale", "historical", "invalid", "pending", "delayed"):
                return self._blocked(instrument, quote, f"{self.mode} desk blocked by stale-data guard: quote is {freshness_state}; no same-day action from stale/delayed data")
            if candle_state in ("stale", "invalid", "pending", "forming", "delayed"):
                return self._blocked(instrument, quote, f"{self.mode} desk blocked by stale-data guard: candles are {candle_state}; no same-day action without fresh candles")
            if not context.get("market_open"):
                return self._blocked(instrument, quote, f"{self.mode} desk is same-day only; market is closed, so no fresh trade is promoted. Historical scan can only prepare next-session candidates.")
            mins = context.get("minutes_to_close")
            if context.get("hard_late_session_block") or context.get("late_session_block"):
                return self._blocked(instrument, quote, f"{self.mode} desk blocked near market close: no fresh same-day entry after cutoff; manage/exit only")
            if mins is not None and mins < self.min_minutes_to_close():
                return self._blocked(instrument, quote, f"{self.mode} desk blocked: only {mins} min to close, not enough time for managed exit")
            if str(quote.get("timestamp") or "").startswith("historical:"):
                return self._blocked(instrument, quote, f"{self.mode} desk requires live quote; historical fallback is not allowed")

        if len(candles) < self.required_history:
            return self._blocked(instrument, quote, f"Need {self.required_history}+ candles for {self.mode}; available {len(candles)}")

        values = closes(candles)
        if len(values) < self.required_history:
            return self._blocked(instrument, quote, "Historical close series is incomplete")

        ltp = finite_number(quote.get("ltp"))
        if ltp is None or ltp <= 0:
            return self._blocked(instrument, quote, "Live price is missing or non-finite")
        indicator_snapshot = DEFAULT_INDICATOR_SNAPSHOT_AUTHORITY.calculate(candles)
        indicator_metrics = dict(indicator_snapshot.get("metrics") or {})
        # Both production desks use the established EMA20/EMA50 pair.  The
        # authority owns the underlying EMA/RSI/ATR/DMI/MACD smoothing so the
        # engine cannot drift from MTF/Stock Intelligence mathematics.
        ema_fast = indicator_metrics.get(f"ema{self.fast_ema()}")
        ema_slow = indicator_metrics.get(f"ema{self.slow_ema()}")
        r = indicator_metrics.get("rsi14")
        m = {
            "macd": indicator_metrics.get("macd"),
            "signal": indicator_metrics.get("macd_signal"),
            "hist": indicator_metrics.get("macd_hist"),
        }
        a = indicator_metrics.get("atr14")
        ax = indicator_metrics.get("adx14")
        sr = support_resistance(candles, min(60, len(candles)), interval=self.candle_interval)
        vwap_rows = current_session_candles(candles, self.candle_interval) if self.same_day else candles[-min(60, len(candles)):]
        vw = vwap(vwap_rows)
        vol_state = self._volume_state(candles)
        ms = context.get("market_structure") or {}
        vp = context.get("volume_profile") or {}
        orb = context.get("orb") or {}
        fund = context.get("fundamentals") or {}
        heat = context.get("heat_context") or {}

        if ema_fast is None or ema_slow is None or r is None or a is None:
            return self._blocked(instrument, quote, "Indicator stack incomplete after historical validation")

        session_structure = {}
        if self.mode == "intraday":
            historical_report = dict(sr.get("level_report") or {})
            session_structure = DEFAULT_INTRADAY_SESSION_STRUCTURE_AUTHORITY.project(
                candles=candles, current_price=ltp, atr=a, ema20=indicator_metrics.get("ema20"),
                ema50=indicator_metrics.get("ema50"), vwap=vw, orb=orb,
                historical_level_report=historical_report, market_structure=ms,
                official_nse_evidence=context.get("official_nse_evidence") or {},
                session_policy=context.get("intraday_session") or {},
            )
            context["session_structure"] = session_structure
            if session_structure.get("ok"):
                sr["support"] = session_structure.get("support")
                sr["resistance"] = session_structure.get("resistance")
                sr["support_validated"] = bool(session_structure.get("operating_support"))
                sr["resistance_validated"] = bool(session_structure.get("operating_resistance"))
                sr["level_report"] = session_structure.get("canonical_level_report") or historical_report
                sr["method"] = "Intraday Session Structure Authority: ORB5 + VWAP/EMA + same-clock RVOL + price-action role flips + validated historical structure"

        trend_up = ema_fast > ema_slow and ltp > ema_fast
        trend_down = ema_fast < ema_slow and ltp < ema_fast
        # v36.5: "near a level" used to be a flat 0.8% price ratio regardless
        # of the instrument's actual volatility -- trivial for a high-ATR
        # stock (falsely triggers "too close"), oversized for a low-ATR one
        # (never triggers when it should). Use ATR distance instead, same
        # basis as SL/T1/T2, with the old ratio as a floor/ceiling so behavior
        # doesn't invert for near-zero-ATR edge cases.
        near_atr_mult = self.near_level_atr_mult()
        atr_dist = (a * near_atr_mult) if a else 0
        res_buffer_dist = max(atr_dist, sr["resistance"] * (1 - self.resistance_buffer())) if sr.get("resistance") else 0
        sup_buffer_dist = max(atr_dist, sr["support"] * (self.support_buffer() - 1)) if sr.get("support") else 0
        near_resistance = sr["resistance"] is not None and ltp >= (sr["resistance"] - res_buffer_dist)
        near_support = sr["support"] is not None and ltp <= (sr["support"] + sup_buffer_dist)

        score = 0
        evidence = []
        side = "WAIT"
        decision = "WAIT"
        confidence = "LOW"
        setup = "No clean setup"
        risk = "Normal"

        if trend_up:
            side = "LONG"
            score += 24
            evidence.append(f"EMA{self.fast_ema()} above EMA{self.slow_ema()}; price above fast EMA")
        elif trend_down:
            side = "SHORT"
            score += 24
            evidence.append(f"EMA{self.fast_ema()} below EMA{self.slow_ema()}; price below fast EMA")

        # Delivery is long-only. Bearish structure remains intelligence and must never become a Delivery short.
        blocked_short_mode = (side == "SHORT" and not self.allow_short_trade())
        if self.long_only and side == "SHORT":
            blocked_short_mode = True
            evidence.append("Long-only desk: bearish structure becomes avoid/reduce/watch, not stock short")

        if r is not None and side == "LONG":
            if self.long_rsi_min() <= r <= self.long_rsi_max():
                score += 15
                evidence.append("RSI supports long without exhaustion")
            elif r > self.long_rsi_max():
                score -= 8
                evidence.append("RSI overheated; avoid chasing")
        if r is not None and side == "SHORT":
            if self.short_rsi_min() <= r <= self.short_rsi_max():
                score += 15
                evidence.append("RSI supports short without exhaustion")
            elif r < self.short_rsi_min():
                score -= 8
                evidence.append("RSI oversold; avoid late short")

        if m.get("hist") is not None and ((m["hist"] > 0 and side == "LONG") or (m["hist"] < 0 and side == "SHORT")):
            score += 14
            evidence.append("MACD histogram confirms direction")
        if ax is not None and ax >= self.adx_floor():
            score += 10
            evidence.append("ADX trend strength present")
        if vol_state == "EXPANDING":
            score += 10
            evidence.append("Volume expanding versus recent average")
        elif vol_state == "LOW":
            score -= 6
            evidence.append("Volume is low; conviction reduced")
        if vw is not None and ((side == "LONG" and ltp >= vw) or (side == "SHORT" and ltp <= vw)):
            score += 8
            evidence.append("VWAP context aligned")

        # Market-aware intelligence layers. These are weighted evidence, not single hard rules.
        for layer, name, max_add in ((ms, "Market structure", 10), (vp, "Volume profile", 7), (orb, "ORB", 8)):
            if not isinstance(layer, dict) or layer.get("ok") is not True:
                continue
            bias = str(layer.get("bias") or "neutral").lower()
            lscore = _score_int(layer.get("score"))
            if lscore is None:
                continue
            summary = layer.get("summary") or layer.get("state") or ""
            if side == "LONG" and bias == "long":
                add = min(max_add, max(2, int(lscore / 10)))
                score += add
                evidence.append(f"{name} supports long: {summary}")
            elif side == "SHORT" and bias == "short":
                add = min(max_add, max(2, int(lscore / 10)))
                score += add
                evidence.append(f"{name} supports short: {summary}")
            elif side in ("LONG", "SHORT") and bias in ("long", "short") and bias.lower() != side.lower():
                score -= min(max_add, max(2, int(lscore / 12)))
                evidence.append(f"{name} conflicts: {summary}")

        if self.mode == "intraday" and session_structure.get("ok") and side in {"LONG", "SHORT"}:
            side_key = "long" if side == "LONG" else "short"
            ss_side = session_structure.get(side_key) or {}
            ss_score = finite_number(ss_side.get("score"))
            confluence_count = _nonnegative_int(ss_side.get("confluence_count"))
            if ss_side.get("promotion_ready") is True and ss_score is not None:
                add = min(16, max(6, int(ss_score / 7.0)))
                score += add
                evidence.append(f"Session structure confirms {side.lower()}: ORB5/VWAP/EMA/RVOL confluence score {ss_score:.0f}")
            elif confluence_count is not None and confluence_count >= 3:
                score += 4
                evidence.append(f"Session structure forming: {confluence_count}/5 confirmations")
            if ss_side.get("extended") is True:
                score -= 14
                evidence.append("Session structure rejects chasing: price extended beyond governed trigger distance")
            official_part = session_structure.get("official_nse") or {}
            confirmation_score = finite_number(official_part.get("confirmation_score"))
            if confirmation_score is not None and confirmation_score > 0:
                score += min(8, int(confirmation_score))
                evidence.append("Official NSE participation evidence confirms the price-action setup; it does not set the price level")

        heat_state = str(heat.get("state") or "neutral").lower() if isinstance(heat, dict) and heat.get("ok") is True else "neutral"
        if side == "LONG" and heat_state == "supportive":
            score += 8
            evidence.append("Index/sector heat strip supportive")
        elif side == "LONG" and heat_state == "weak":
            score -= 10
            evidence.append("Index/sector heat strip weak/conflicting")
        elif side == "SHORT" and heat_state == "weak":
            score += 7
            evidence.append("Weak index/sector context supports short/avoid")

        if side == "LONG" and near_resistance:
            score -= self.near_level_penalty()
            risk = "Near resistance; wait for breakout/retest"
            evidence.append("Too close to resistance")
        if side == "SHORT" and near_support:
            score -= self.near_level_penalty()
            risk = "Near support; wait for breakdown/retest"
            evidence.append("Too close to support")

        # v65.3: this is the pure technical composite -- levels/EMA/VWAP/RSI/ADX/
        # volume/index-sector heat/etc, everything scored above this line -- before
        # any fundamental blending happens. Previously only used as a scratch value
        # inside the blend formula below and then discarded; now captured on the
        # Decision so it can be shown alongside Fundamental/Final Confidence instead
        # of only the already-blended number.
        tech_score = max(0.0, min(100.0, float(score)))
        fscore = finite_number(fund.get("score")) if isinstance(fund, dict) else None
        if self.require_fundamentals:
            if fscore is not None:
                fscore = max(0.0, min(100.0, fscore))
                score = int((tech_score * (1 - self.fundamental_weight())) + (fscore * self.fundamental_weight()))
                evidence.append(f"Fundamental score applied: {round(fscore, 2)}")
            if context.get("fundamentals_ok") is not True:
                # Still return a row. Do not promote it.
                reason = fund.get("reason") if isinstance(fund, dict) else "Fundamentals missing"
                state = str(fund.get("state") or "missing") if isinstance(fund, dict) else "missing"
                if fscore is None:
                    block_msg = "Fundamentals unavailable; Delivery cannot promote without validated business quality"
                    d = self._blocked(instrument, quote, block_msg)
                    d.setup = "Technical-only watch; fundamentals unavailable"
                    d.risk = "Fundamentals unavailable"
                    precise_tail = "Promotion blocked because fundamentals are unavailable"
                else:
                    block_msg = "Promotion blocked by weak fundamentals; data is loaded but quality/valuation gate is not strong enough"
                    d = self._blocked(instrument, quote, block_msg)
                    d.setup = f"Technical watch; fundamentals {state}"
                    d.risk = f"Fundamentals {state}"
                    precise_tail = "Promotion blocked by weak fundamentals, not missing data"
                d.score = max(0, min(100, int(score)))
                d.technical_score = round(tech_score, 1)
                d.reason = "; ".join(evidence + [str(reason), precise_tail])
                d.rsi = _round(r); d.adx = _round(ax); d.vwap = _round(vw)
                d.volume_state = vol_state; d.support = _round(sr.get("support")); d.resistance = _round(sr.get("resistance"))
                self._attach_context(d, context)
                d.evidence = evidence + [precise_tail]
                return d

        if side == "SHORT" and not self.allow_short_trade():
            # Keep the intelligence, but remove tradable short action.
            d = Decision(
                symbol=instrument.get("trading_symbol") or quote.get("symbol"),
                exchange=instrument.get("exchange") or quote.get("exchange") or "NSE",
                mode=self.mode,
                side="BEARISH",
                decision="AVOID_LONG" if max(0, min(100, int(score))) >= self.watch_threshold() else "WATCH",
                ltp=_round(ltp), entry=None, t1=None, t2=None, sl=None, rr=None,
                score=max(0, min(100, int(score))), confidence="MEDIUM" if max(0, min(100, int(score))) >= self.watch_threshold() else "LOW",
                setup="Bearish bias only; stock short blocked for this desk",
                risk="Stock short not allowed for Delivery",
                reason="; ".join(evidence + ["Stock short is allowed only for Intraday; Delivery uses avoid long, reduce, or watch reversal"]),
                status="WATCH",
                price_freshness=_fresh(quote.get("timestamp")), last_refresh=quote.get("timestamp") or now_iso(), last_ai_validation=now_iso(), holding_policy=self.holding_policy,
                open=_round(quote.get("open")), change_pct=quote.get("change_pct"),
                index_context=context.get("index_context", "pending"), sector_context=context.get("sector_context", "pending"),
                rsi=_round(r), adx=_round(ax), vwap=_round(vw), volume_state=vol_state, support=_round(sr.get("support")), resistance=_round(sr.get("resistance")), evidence=evidence + ["Non-intraday stock short blocked"]
            )
            self._attach_context(d, context)
            return d

        threshold = self.promotion_threshold(context)
        display_score = max(0, min(100, int(score)))
        promotion_block = self.promotion_evidence_block(side, context, vol_state, vw, ltp)
        strategy_contract = DEFAULT_STRATEGY_MATHEMATICS_CONTRACT_AUTHORITY.build(
            mode=self.mode, strategy_version=self.strategy_version,
        )
        qualification = DEFAULT_STRATEGY_QUALIFICATION_AUTHORITY.evaluate(
            mode=self.mode,
            setup=self.setup_name(side, context) if side != "WAIT" else None,
            strategy_version=self.strategy_version,
            evidence=context.get("strategy_qualification") if isinstance(context, dict) else None,
            current_strategy_contract_hash=strategy_contract.get("strategy_contract_hash"),
        )
        if isinstance(context, dict):
            context["strategy_mathematics_contract"] = strategy_contract
            context["strategy_qualification_state"] = qualification
        if side != "WAIT" and display_score >= threshold and not promotion_block:
            decision = self.promoted_decision(side)
            confidence = "HIGH" if display_score >= threshold + 8 else "MEDIUM"
            setup = self.setup_name(side, context)
        elif side != "WAIT" and display_score >= self.watch_threshold():
            decision = "WATCH"
            confidence = "MEDIUM"
            setup = promotion_block or "Valid but waiting for trigger/safer level"
            if promotion_block:
                evidence.append("Blocked promotion: " + promotion_block)
        else:
            decision = "WAIT"
            setup = "No clean directional edge"

        # Empirical strategy mathematics is a hypothesis, not deterministic truth.
        # No hard-coded score/threshold/ATR/S-R coefficient may create an
        # actionable trade until the exact strategy version has a hash-bound
        # qualification record.  Research ranking remains visible as WATCH.
        if decision == "TRADE" and qualification.get("qualified") is not True:
            decision = "WATCH"
            confidence = "RESEARCH"
            setup = self.setup_name(side, context) if side != "WAIT" else setup
            risk = "Research-only: empirical strategy qualification pending"
            evidence.append("Actionable admission blocked: " + str(qualification.get("reason") or "strategy qualification pending"))

        # R10 entry-authority hardening: a promoted score is not enough to authorize
        # an immediate entry when the selected-timeframe structural level is already
        # inside the ATR-aware no-room buffer.  Convert it to an explicit trigger
        # watch and let planned_entry sit beyond the structure for breakout/retest.
        if decision == "TRADE" and side == "LONG" and near_resistance:
            decision = "BREAKOUT WATCH"
            setup = f"Breakout/retest required above resistance {round(float(sr['resistance']),2)}" if sr.get("resistance") is not None else "Breakout/retest required"
            evidence.append("Immediate long entry withheld: insufficient structural room below selected-timeframe resistance")
        elif decision == "TRADE" and side == "SHORT" and near_support:
            decision = "BREAKDOWN WATCH"
            setup = f"Breakdown/retest required below support {round(float(sr['support']),2)}" if sr.get("support") is not None else "Breakdown/retest required"
            evidence.append("Immediate short entry withheld: insufficient structural room above selected-timeframe support")

        entry = ltp if decision == "TRADE" else None
        sl_source = target_source = "atr"
        structural_map = None
        raw_t1_overshoot = raw_t2_overshoot = False
        raw_t1_undershoot = raw_t2_undershoot = False
        if side in ("LONG", "SHORT") and entry and a:
            geometry = DEFAULT_TRADE_GEOMETRY_AUTHORITY.project(
                mode=self.mode, side=side, entry=entry, atr=a,
                level_report=sr.get("level_report"),
                nearest_support=sr.get("support"), nearest_resistance=sr.get("resistance"),
                current_price=ltp,
            )
            sl, t1, t2 = geometry.get("stop"), geometry.get("target_1"), geometry.get("target_2")
            structural_map = geometry.get("structural_map") or {}
            sl_source = geometry.get("stop_source") or "desk_atr_policy"
            target_source = geometry.get("target_source") or "desk_atr_policy"
            res_check, sup_check = sr.get("resistance"), sr.get("support")
            raw_t1, raw_t2 = geometry.get("raw_target_1"), geometry.get("raw_target_2")
            if side == "LONG" and res_check is not None:
                raw_t1_overshoot = raw_t1 is not None and raw_t1 > res_check
                raw_t2_overshoot = raw_t2 is not None and raw_t2 > res_check
            if side == "SHORT" and sup_check is not None:
                raw_t1_undershoot = raw_t1 is not None and raw_t1 < sup_check
                raw_t2_undershoot = raw_t2 is not None and raw_t2 < sup_check
        else:
            geometry = None
            sl = t1 = t2 = None

        # v36.5: structure guard. A LONG candidate whose *unclamped* ATR
        # targets required breaking resistance is classified BREAKOUT WATCH
        # rather than BUY/TRADE-now, using the pre-clamp overshoot captured
        # above (not the post-clamp values, which the clamp itself neutralizes).
        try:
            res_level = sr.get("resistance")
            if side == "LONG" and not self.require_fundamentals and decision == "TRADE" and entry and res_level:
                if float(entry) < float(res_level) and (raw_t1_overshoot or raw_t2_overshoot or near_resistance):
                    decision = "BREAKOUT WATCH"
                    confidence = "MEDIUM"
                    setup = f"Breakout watch: resistance {round(float(res_level),2)} must clear first"
                    risk = "Resistance hurdle before target; wait for breakout/retest confirmation"
                    evidence.append(f"Breakout-only setup: entry {round(float(entry),2)} is below resistance {round(float(res_level),2)}; ATR-projected target required clearing resistance before clamp; targets are valid only after breakout/retest confirmation")
            sup_level = sr.get("support")
            if side == "SHORT" and decision == "TRADE" and entry and sup_level:
                if float(entry) > float(sup_level) and (raw_t1_undershoot or raw_t2_undershoot or near_support):
                    decision = "BREAKDOWN WATCH"
                    confidence = "MEDIUM"
                    setup = f"Breakdown watch: support {round(float(sup_level),2)} must break first"
                    risk = "Support floor before target; wait for breakdown/retest confirmation"
                    evidence.append(f"Breakdown-only setup: entry {round(float(entry),2)} is above support {round(float(sup_level),2)}; ATR-projected target required breaking support before clamp")
        except Exception:
            pass

        if decision == "TRADE" and structural_map and not structural_map.get("promotion_allowed", True):
            decision = "WATCH"
            confidence = "MEDIUM"
            setup = "Structure blocks the projected target; wait for a better entry or confirmed breakout/retest"
            risk = structural_map.get("block_reason") or "Insufficient room before the first structural obstacle"
            evidence.append("Blocked promotion: " + risk)
            entry = sl = t1 = t2 = None
            sl_source = target_source = "structure_blocked"

        level_ok, level_message, level_status = _validate_level_map(side, entry, sl, t1, t2, sr.get("support"), sr.get("resistance")) if decision == "TRADE" else (False, "Reference-only levels", "reference_only")
        if decision == "TRADE" and not level_ok:
            decision = "WATCH"
            confidence = "MEDIUM"
            setup = "Reference-only levels; actionable setup blocked"
            risk = level_message
            evidence.append("Blocked promotion: " + level_message)
            entry = sl = t1 = t2 = None
            sl_source = target_source = "reference_only"

        rr = _rr(entry, sl, t1) if entry and sl and t1 else None
        net_rr = _net_rr(entry, sl, t1, side, self.mode) if entry and sl and t1 else None
        rr_gate = _min_rr_for_mode(self.mode)
        # v35.5: hard R:R gate. A high technical score cannot promote a trade
        # whose net (cost-adjusted) reward-to-risk doesn't clear the desk's
        # minimum -- previously rr was only displayed, never enforced.
        if decision == "TRADE" and net_rr is not None and net_rr < rr_gate:
            decision = "WATCH"
            confidence = "MEDIUM"
            setup = f"Valid setup but R:R {net_rr} below minimum {rr_gate} after costs; wait for better entry/level"
            evidence.append(f"Blocked promotion: net R:R {net_rr} < required {rr_gate}")

        # Quantity is intentionally unavailable at scanner/decision-engine stage.
        # RiskAdmissionAndSizingAuthority is the sole production quantity authority.
        qty, risk_amount = (None, None)

        planned_entry = planned_sl = planned_t1 = planned_t2 = planned_rr = None
        planned_map_valid = False
        if side in ("LONG", "SHORT") and a:
            trigger_buffer = max(a * 0.05, ltp * 0.0005)
            if entry is not None:
                planned_entry = entry
            elif self.mode == "intraday" and side in {"LONG", "SHORT"}:
                side_key = "long" if side == "LONG" else "short"
                governed_trigger = ((context.get("session_structure") or {}).get(side_key) or {}).get("entry_trigger")
                planned_entry = finite_number(governed_trigger)
            elif side == "LONG" and sr.get("resistance") is not None and (near_resistance or decision == "BREAKOUT WATCH"):
                resistance_value = finite_number(sr.get("resistance"))
                planned_entry = resistance_value + trigger_buffer if resistance_value is not None else None
            elif side == "SHORT" and sr.get("support") is not None and (near_support or decision == "BREAKDOWN WATCH"):
                support_value = finite_number(sr.get("support"))
                planned_entry = support_value - trigger_buffer if support_value is not None else None
            else:
                planned_entry = ltp
            planned_geometry = DEFAULT_TRADE_GEOMETRY_AUTHORITY.project(
                mode=self.mode, side=side, entry=planned_entry, atr=a,
                level_report=sr.get("level_report"),
                nearest_support=sr.get("support"), nearest_resistance=sr.get("resistance"),
                current_price=ltp,
            )
            planned_sl = planned_geometry.get("stop")
            planned_t1 = planned_geometry.get("target_1")
            planned_t2 = planned_geometry.get("target_2")
            planned_structure = planned_geometry.get("structural_map") or {}
            planned_map_valid = bool(planned_geometry.get("promotion_allowed"))
            planned_rr = _net_rr(planned_entry, planned_sl, planned_t1, side, self.mode)
        planned_entry_number = finite_number(planned_entry)
        trigger_distance_ok = bool(a and planned_entry_number is not None and abs(planned_entry_number - ltp) <= a * 0.35)
        armed_ok = side in ("LONG", "SHORT") and planned_map_valid and planned_rr is not None and planned_rr >= rr_gate and display_score >= max(self.watch_threshold(), threshold - 5) and trigger_distance_ok and freshness_state == "live" and candle_state not in ("stale", "pending", "invalid") and self.prepared_state_allowed(side, context, vol_state, vw, ltp)
        prepared_state = "TRIGGERED" if decision == "TRADE" else "ARMED" if armed_ok else "PREPARING" if side in ("LONG", "SHORT") else "OBSERVING"

        d = Decision(
            symbol=instrument.get("trading_symbol") or quote.get("symbol"),
            exchange=instrument.get("exchange") or quote.get("exchange") or "NSE",
            mode=self.mode,
            side=side,
            decision=decision,
            ltp=_round(ltp), entry=_round(entry), t1=_round(t1), t2=_round(t2), sl=_round(sl), rr=rr,
            score=display_score, technical_score=round(tech_score, 1), confidence=confidence, setup=setup, risk=risk,
            reason="; ".join(evidence) if evidence else "No promotion evidence",
            status="PROMOTED" if decision == "TRADE" else "WATCH" if decision in ("WATCH", "BREAKOUT WATCH", "BREAKDOWN WATCH") else "WAIT",
            price_freshness=_fresh(quote.get("timestamp")), last_refresh=quote.get("timestamp") or now_iso(), last_ai_validation=now_iso(), holding_policy=self.holding_policy,
            open=_round(quote.get("open")), change_pct=quote.get("change_pct"),
            index_context=context.get("index_context", "pending"), sector_context=context.get("sector_context", "pending"),
            rsi=_round(r), adx=_round(ax), vwap=_round(vw), volume_state=vol_state, support=_round(sr.get("support")), resistance=_round(sr.get("resistance")), evidence=evidence,
            quantity=qty, risk_amount=risk_amount, est_net_rr=net_rr, rr_gate_min=rr_gate, sl_source=sl_source, target_source=target_source,
            freshness_state=str((context.get("freshness") or {}).get("state") or "unknown"),
            quote_age_seconds=(context.get("freshness") or {}).get("quote_age_seconds"),
            candle_age_seconds=(context.get("freshness") or {}).get("candle_age_seconds"),
            candle_state=str((context.get("freshness") or {}).get("candle_state") or "unknown"),
            level_status=level_status, level_message=level_message, trade_map_valid=bool(level_ok),
            planned_entry=_round(planned_entry), planned_sl=_round(planned_sl), planned_t1=_round(planned_t1), planned_t2=_round(planned_t2), planned_rr=planned_rr,
            planned_map_valid=bool(planned_map_valid), prepared_state=prepared_state,
            atr14=_round(a),
            first_obstacle=_round(((structural_map or {}).get("first_obstacle") or {}).get("price")),
            first_obstacle_low=_round(((structural_map or {}).get("first_obstacle") or {}).get("low")),
            first_obstacle_high=_round(((structural_map or {}).get("first_obstacle") or {}).get("high")),
            first_obstacle_touches=_nonnegative_int(((structural_map or {}).get("first_obstacle") or {}).get("touches")),
            room_to_obstacle=_round((structural_map or {}).get("room_to_first_obstacle")),
            obstacle_rr=(structural_map or {}).get("room_rr"),
            structural_target_state="ready" if structural_map and structural_map.get("promotion_allowed") else "blocked" if structural_map else "unchecked",
            structural_target_reason=(structural_map or {}).get("block_reason") or (structural_map or {}).get("explanation") or "",
            profit_protection_plan=(structural_map or {}).get("profit_protection_plan") or {},
        )
        self._attach_context(d, context)
        return d

    def _blocked(self, instrument, quote, reason: str) -> Decision:
        reason_text = str(reason or "")
        setup = "Desk rule blocked"
        risk = "Blocked"
        if "Need " in reason_text and "candles" in reason_text:
            setup = "Insufficient historical data"
            risk = "Data unavailable for this desk"
        elif "market is closed" in reason_text or "same-day" in reason_text:
            setup = "Live validation blocked"
            risk = "Market/timing gate"
        elif "Fundamentals" in reason_text:
            setup = "Fundamental gate blocked"
            risk = "Fundamental requirement"
        return Decision(
            symbol=instrument.get("trading_symbol") or quote.get("symbol"), exchange=instrument.get("exchange") or quote.get("exchange") or "NSE",
            mode=self.mode, side="WAIT", decision="WAIT", ltp=_round(quote.get("ltp")), entry=None, t1=None, t2=None, sl=None, rr=None, score=0,
            confidence="LOW", setup=setup, risk=risk, reason=reason_text, status="BLOCKED",
            price_freshness=_fresh(quote.get("timestamp")), last_refresh=quote.get("timestamp") or now_iso(), last_ai_validation=now_iso(), holding_policy=self.holding_policy,
            open=_round(quote.get("open")), change_pct=quote.get("change_pct"),
            evidence=[reason_text],
            freshness_state=str(({} if quote is None else quote).get("freshness_state") or "blocked"),
            level_status="reference_only", level_message=reason_text, trade_map_valid=False
        )

    def _attach_context(self, d: Decision, context: Dict[str, Any]) -> None:
        fund = context.get("fundamentals") or {}
        ms = context.get("market_structure") or {}
        vp = context.get("volume_profile") or {}
        orb = context.get("orb") or {}
        heat = context.get("heat_context") or {}
        d.fundamental_score = _round(fund.get("score")) if isinstance(fund, dict) else None
        d.fundamental_weight_pct = round(self.fundamental_weight() * 100)
        d.quality_score = _round(fund.get("quality")) if isinstance(fund, dict) else None
        d.growth_score = _round(fund.get("growth")) if isinstance(fund, dict) else None
        d.safety_score = _round(fund.get("safety")) if isinstance(fund, dict) else None
        d.valuation_score = _round(fund.get("valuation")) if isinstance(fund, dict) else None
        d.fundamental_state = str(fund.get("state") or "missing") if isinstance(fund, dict) else "missing"
        d.market_structure = str(ms.get("state") or ms.get("summary") or "pending")
        d.market_structure_score = _score_int(ms.get("score")) if isinstance(ms, dict) and ms.get("ok") is True else None
        d.volume_profile = str(vp.get("state") or vp.get("summary") or "pending")
        d.volume_profile_score = _score_int(vp.get("score")) if isinstance(vp, dict) and vp.get("ok") is True else None
        d.orb_state = str(orb.get("state") or orb.get("summary") or "pending")
        d.orb_score = _score_int(orb.get("score")) if isinstance(orb, dict) and orb.get("ok") is True else None
        d.orb_phase = str(orb.get("phase") or "pending")
        d.orb_confirmed = orb.get("confirmed") is True
        d.orb_high, d.orb_low = _round(orb.get("orb_high")), _round(orb.get("orb_low"))
        d.previous_day_high, d.previous_day_low = _round(orb.get("previous_day_high")), _round(orb.get("previous_day_low"))
        d.pivot, d.cpr_bottom, d.cpr_top = _round(orb.get("pivot")), _round(orb.get("cpr_bottom")), _round(orb.get("cpr_top"))
        d.session_relative_volume = _round(orb.get("session_relative_volume"))
        d.participation_authority = str(orb.get("participation_authority") or "") or None
        d.participation_authority_version = str(orb.get("participation_authority_version") or "") or None
        d.participation_lane = str(orb.get("participation_lane") or "") or None
        d.participation_source_time = orb.get("participation_source_time")
        d.participation_decision_usable = orb.get("participation_decision_usable") is True
        ss = context.get("session_structure") or {}
        if isinstance(ss, dict) and ss.get("ok"):
            d.session_structure_state = str(ss.get("state") or "pending")
            side_key = "long" if str(d.side or "").upper() == "LONG" else "short" if str(d.side or "").upper() == "SHORT" else None
            side_state = (ss.get(side_key) or {}) if side_key else {}
            d.session_structure_score = _round(side_state.get("score"))
            d.session_entry_trigger = _round(side_state.get("entry_trigger"))
            d.session_a_plus = (side_state.get("a_plus") if isinstance(side_state.get("a_plus"), bool) else None) if side_key else None
            d.session_support_source = str(((ss.get("operating_support") or {}).get("source_level") or "")) or None
            d.session_resistance_source = str(((ss.get("operating_resistance") or {}).get("source_level") or "")) or None
            official = ss.get("official_nse") or {}
            d.nse_confirmation_score = _round(official.get("confirmation_score"))
            d.nse_confirmation_reasons = [str(x) for x in official.get("reasons") or []]
        mtf = context.get("delivery_timeframes") or {}
        d.weekly_state, d.monthly_state = str((mtf.get("weekly") or {}).get("state") or "pending"), str((mtf.get("monthly") or {}).get("state") or "pending")
        d.market_context_score = _round(heat.get("score")) if isinstance(heat, dict) else None
        fresh = context.get("freshness") or {}
        if isinstance(fresh, dict):
            d.freshness_state = str(fresh.get("state") or d.freshness_state or "unknown")
            d.quote_age_seconds = fresh.get("quote_age_seconds", d.quote_age_seconds)
            d.candle_age_seconds = fresh.get("candle_age_seconds", d.candle_age_seconds)
            d.candle_state = str(fresh.get("candle_state") or d.candle_state or "unknown")
        d.strategy_version = self.strategy_version
        strategy_contract = context.get("strategy_mathematics_contract") if isinstance(context, dict) else None
        d.strategy_contract_hash = str((strategy_contract or {}).get("strategy_contract_hash") or "") or None
        qualification = context.get("strategy_qualification_state") if isinstance(context, dict) else None
        d.strategy_qualification_state = dict(qualification or {}) if isinstance(qualification, dict) else {}

    def _volume_state(self, candles):
        material = list(candles or [])
        if len(material) < 10:
            return "pending"
        recent_rows = material[-10:]
        vols = [finite_number(c.get("volume")) for c in recent_rows]
        if any(v is None or v < 0 for v in vols):
            return "pending"
        avg = sum(vols) / 10.0
        if avg <= 0:
            return "pending"
        recent = vols[-1]
        if recent > avg * 1.5:
            return "EXPANDING"
        if recent < avg * 0.65:
            return "LOW"
        return "NORMAL"


    def fast_ema(self): return 20
    def slow_ema(self): return 50
    def adx_floor(self): return 20
    def long_rsi_min(self): return 45
    def long_rsi_max(self): return 70
    def short_rsi_min(self): return 30
    def short_rsi_max(self): return 55
    def resistance_buffer(self): return 0.992
    def support_buffer(self): return 1.008
    def near_level_atr_mult(self): return 0.35
    def near_level_penalty(self): return 18
    def min_minutes_to_close(self): return 25
    def allow_short_trade(self): return False
    def watch_threshold(self): return 50
    def promotion_threshold(self, context): return 70
    def promotion_evidence_block(self, side, context, vol_state, vw, ltp): return None
    def promoted_decision(self, side): return "TRADE"
    def setup_name(self, side, context): return "Trend continuation" if side in ("LONG", "SHORT") else "Wait"
    def sl_atr(self): return 1.2
    def t1_atr(self): return 1.8
    def t2_atr(self): return 2.8
    def fundamental_weight(self): return 0.0




class IntradayEngine(BaseEngine):
    mode = "intraday"
    holding_policy = "Minutes to few hours; same-day mandatory exit; no overnight; auto-exit before close."
    required_history = 50
    candle_interval = "5minute"
    days = 10
    same_day = True
    needs_live_quote = True
    def adx_floor(self): return 22
    def min_minutes_to_close(self): return 45
    def allow_short_trade(self): return True
    def promotion_threshold(self, context): return policy_for(self.mode).promotion_threshold
    def promotion_evidence_block(self, side, context, vol_state, vw, ltp):
        timing = context.get("intraday_session") or {}
        phase = str(timing.get("phase") or "")
        if phase in {"PREOPEN_INTELLIGENCE", "ORB5_OBSERVE_ONLY"}:
            return "ORB5 observe-only until 09:20 IST; no new Intraday entry"
        if timing.get("new_entry_allowed") is not True:
            return "Intraday entry permission is unavailable/closed"
        ss = context.get("session_structure") or {}
        side_key = "long" if str(side).upper() == "LONG" else "short"
        side_state = ss.get(side_key) or {}
        if (ss.get("official_nse") or {}).get("risk_blocks"):
            return "Official NSE execution/surveillance risk blocks new entry"
        if side_state.get("promotion_ready") is not True:
            return "Intraday needs accepted ORB5/session structure with VWAP/EMA, same-clock RVOL and no chase/extension"
        if timing.get("a_plus_only") is not False and timing.get("a_plus_only") is not True:
            return "Intraday timing policy is invalid/unavailable"
        if timing.get("a_plus_only") is True and side_state.get("a_plus") is not True:
            return "14:15-14:30 accepts A+ Intraday setups only"
        return None
    def prepared_state_allowed(self, side, context, vol_state, vw, ltp):
        timing = context.get("intraday_session") or {}
        if timing.get("new_entry_allowed") is not True: return False
        a_plus_only = timing.get("a_plus_only")
        if not isinstance(a_plus_only, bool): return False
        ss = context.get("session_structure") or {}; side_key = "long" if str(side).upper() == "LONG" else "short"
        side_state = ss.get(side_key) or {}
        return bool(side_state.get("promotion_ready") is True and (a_plus_only is False or side_state.get("a_plus") is True))
    def watch_threshold(self): return policy_for(self.mode).watch_threshold
    def near_level_penalty(self): return 12
    def promoted_decision(self, side): return "TRADE"
    # Explicit production risk geometry: never inherit trade-critical values
    # from BaseEngine where a future generic edit could silently retune Intraday.
    def sl_atr(self): return policy_for(self.mode).sl_atr
    def t1_atr(self): return policy_for(self.mode).t1_atr
    def t2_atr(self): return policy_for(self.mode).t2_atr
    def setup_name(self, side, context):
        ss = context.get("session_structure") or {}; orb = context.get("orb") or {}
        if ((ss.get("long") if side == "LONG" else ss.get("short")) or {}).get("promotion_ready") is True:
            if "retest_hold" in str(orb.get("state") or ""): return "ORB5 Retest + Session Support/Resistance Hold"
            return "ORB5 + VWAP/EMA/RVOL Session Structure"
        return "Session structure validation pending"

class DeliveryEngine(BaseEngine):
    mode = "delivery"
    # Holding horizon is setup-owned evidence. The desk must never manufacture a
    # generic weeks/months/years promise when the admitted setup has not declared
    # one. Empty means unavailable until canonical setup/decision materializes it.
    holding_policy = ""
    required_history = 120
    candle_interval = "day"
    days = 420
    same_day = False
    needs_live_quote = False
    long_only = True
    require_fundamentals = True

    def promotion_threshold(self, context): return policy_for(self.mode).promotion_threshold
    def watch_threshold(self): return policy_for(self.mode).watch_threshold
    def promoted_decision(self, side): return "TRADE"
    def sl_atr(self): return policy_for(self.mode).sl_atr
    def t1_atr(self): return policy_for(self.mode).t1_atr
    def t2_atr(self): return policy_for(self.mode).t2_atr
    def fundamental_weight(self): return 0.60

    def promotion_evidence_block(self, side, context, vol_state, vw, ltp):
        mtf = context.get("delivery_timeframes") or {}
        weekly = mtf.get("weekly") or {"state": "insufficient"}
        monthly = mtf.get("monthly") or {"state": "insufficient"}
        if weekly.get("state") in ("insufficient", "bearish") or monthly.get("state") in ("insufficient", "bearish"):
            return "Delivery needs non-bearish weekly and monthly structure before accumulation"
        return None

    def setup_name(self, side, context):
        return "Delivery daily/weekly/monthly structure alignment"


# Canonical production registry: executable desks are Intraday and Delivery only.
ENGINES = {
    "intraday": IntradayEngine(),
    "delivery": DeliveryEngine(),
}
assert frozenset(ENGINES) == PRODUCTION_MODES
