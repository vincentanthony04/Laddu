from __future__ import annotations

from core.market_sector_context_analysis_authority import DEFAULT_MARKET_SECTOR_CONTEXT_ANALYSIS_AUTHORITY
from collections import defaultdict
from datetime import datetime
from typing import Any, Dict, List, Optional
from session_candles import closed_candles, candle_datetime
from indicators import wilder_directional_metrics
from core.participation_evidence_authority import DEFAULT_PARTICIPATION_EVIDENCE_AUTHORITY
from core.numeric_semantics import finite_number


def _num(v):
    return finite_number(v)


def _round(v, n=2):
    out = finite_number(v)
    return round(out, n) if out is not None else None


def _last(candles: List[Dict[str, Any]]) -> Dict[str, Any]:
    return candles[-1] if candles else {}


def _date_part(ts: Any) -> str:
    s = str(ts or "")
    return s[:10]


def derive_prev_day_ohlc(candles: List[Dict[str, Any]]) -> Optional[tuple]:
    """Pull (prev_high, prev_low, prev_close) off the second-to-last candle for
    Camarilla input. Callers must pass daily-interval candles -- this has no
    way to verify interval itself, so on non-daily input the returned tuple
    just won't line up with "yesterday" and camarilla_levels degrades safely
    (skipped by the try/except inside support_resistance_levels).
    Single source of truth -- was previously duplicated in main.py and
    scan_orchestration_service.py; keep it here so both call sites move together."""
    if len(candles) < 2:
        return None
    prev = candles[-2]
    ph, pl, pc = (_num(prev.get(key)) for key in ("high", "low", "close"))
    if None in (ph, pl, pc) or min(ph, pl, pc) <= 0 or ph < pl:
        return None
    return (ph, pl, pc)


def camarilla_levels(prev_high, prev_low, prev_close) -> Dict[str, Optional[float]]:
    """Standard Camarilla pivots off the prior completed session's H/L/C.
    Mirrors frontend/app_chart.js::camarillaLevels -- keep both in sync."""
    ph = _num(prev_high); pl = _num(prev_low); pc = _num(prev_close)
    if ph is None or pl is None or pc is None or min(ph, pl, pc) <= 0 or ph < pl:
        return {}
    rng = ph - pl
    r4 = pc + rng * 1.1 / 2; r3 = pc + rng * 1.1 / 4; r2 = pc + rng * 1.1 / 6; r1 = pc + rng * 1.1 / 12
    s1 = pc - rng * 1.1 / 12; s2 = pc - rng * 1.1 / 6; s3 = pc - rng * 1.1 / 4; s4 = pc - rng * 1.1 / 2
    r5 = r4 + 1.168 * (r4 - r3)
    r6 = (ph / pl) * pc if pl else None
    s5 = s4 - 1.168 * (s3 - s4)
    s6 = (pc - (r6 - pc)) if r6 is not None else None
    return {"r1": _round(r1), "r2": _round(r2), "r3": _round(r3), "r4": _round(r4), "r5": _round(r5), "r6": _round(r6),
            "s1": _round(s1), "s2": _round(s2), "s3": _round(s3), "s4": _round(s4), "s5": _round(s5), "s6": _round(s6)}


def support_resistance_levels(candles: List[Dict[str, Any]], lookback: int = 220, tolerance_pct: float = 0.35, prev_day_ohlc: Optional[tuple] = None) -> Dict[str, Any]:
    """Price-action aware ranked support/resistance authority.

    Native structural roles are preserved.  A prior resistance may become
    support (and a prior support may become resistance) only after completed
    candles prove an accepted break/role flip.  Directional rejection evidence
    is side aware: support earns credit for reclaim/hold + future upside and
    resistance for rejection/hold + future downside.  Neutral references such
    as POC/PDC may take the side implied by current price, but they never force
    a native swing/period boundary to flip roles by location alone.
    """
    if not candles or len(candles) < 20:
        return {"ok": False, "reason": "insufficient candles", "support": [], "resistance": [], "ranked_levels": []}
    sample = list(candles[-max(40, int(lookback)):])
    last_close = _num(sample[-1].get("close"))
    if last_close in (None, 0):
        return {"ok": False, "reason": "last close unavailable", "support": [], "resistance": [], "ranked_levels": []}

    highs = [_num(row.get("high")) for row in sample]
    lows = [_num(row.get("low")) for row in sample]
    closes = [_num(row.get("close")) for row in sample]
    raw_volumes = [_num(row.get("volume")) for row in sample]
    if any(None in (h, l, c) or min(h, l, c) <= 0 or h < l for h, l, c in zip(highs, lows, closes)):
        return {"ok": False, "reason": "incomplete/invalid OHLC", "support": [], "resistance": [], "ranked_levels": []}
    valid_ohlc = list(zip(highs, lows, closes))
    if len(valid_ohlc) < 20:
        return {"ok": False, "reason": "incomplete OHLC", "support": [], "resistance": [], "ranked_levels": []}
    volume_evidence_complete = all(v is not None and v >= 0 for v in raw_volumes)
    volumes = [float(v) if v is not None and v >= 0 else 0.0 for v in raw_volumes]

    directional = wilder_directional_metrics(sample, 14)
    atr = _num(directional.get("atr"))
    # S/R distance, clustering and actionability are volatility-sensitive.
    # Never substitute a different ATR convention when the canonical Wilder
    # authority is unavailable; that would make the same label mean two maths.
    if atr is None or atr <= 0:
        return {"ok": False, "reason": "canonical ATR14 unavailable", "support": [], "resistance": [], "ranked_levels": []}
    adx14 = _num(directional.get("adx")); plus_di14 = _num(directional.get("plus_di")); minus_di14 = _num(directional.get("minus_di")); adx_change = _num(directional.get("adx_change"))
    directional_regime = str(directional.get("regime") or "UNAVAILABLE")
    merge_distance = max(last_close * max(0.0012, tolerance_pct / 100.0), atr * 0.18)
    evidence_distance = max(last_close * 0.0015, atr * 0.18)
    actionability_distance = max(atr * 0.18, last_close * 0.0015)

    candidates: List[Dict[str, Any]] = []
    def add(price: Any, source: str, authority: str, kind: str | None = None, base: float = 0.0, **extra: Any) -> None:
        value = _num(price)
        if value is None or value <= 0:
            return
        native = kind if kind in {"support", "resistance"} else "neutral"
        candidates.append({
            "price": value, "source_level": source, "source": source,
            "timeframe": authority, "timeframe_authority": authority,
            "native_kind": native, "base_score": float(base), **extra,
        })

    pivot_highs, pivot_lows = _pivots(sample, left=3, right=3)
    for index, price in pivot_highs:
        add(price, "confirmed_swing_high", "selected", "resistance", 18.0, pivot_index=index)
    for index, price in pivot_lows:
        add(price, "confirmed_swing_low", "selected", "support", 18.0, pivot_index=index)

    if prev_day_ohlc:
        try:
            ph, pl, pc = (_num(value) for value in prev_day_ohlc)
            add(ph, "previous_day_high", "1D", "resistance", 36.0)
            add(pl, "previous_day_low", "1D", "support", 36.0)
            add(pc, "previous_day_close", "1D", None, 24.0)
        except Exception:
            pass

    grouped_week: Dict[Any, List[Dict[str, Any]]] = defaultdict(list)
    grouped_month: Dict[Any, List[Dict[str, Any]]] = defaultdict(list)
    for row in candles[-320:]:
        dt = candle_datetime(row.get("timestamp") or row.get("time") or row.get("date"))
        if dt is None:
            continue
        iso = dt.isocalendar(); grouped_week[(int(iso.year), int(iso.week))].append(row); grouped_month[(dt.year, dt.month)].append(row)
    def add_previous_group(groups: Dict[Any, List[Dict[str, Any]]], authority: str, base: float) -> None:
        keys = sorted(groups)
        if len(keys) < 2: return
        rows = groups[keys[-2]]
        gh = [v for v in (_num(r.get("high")) for r in rows) if v is not None]
        gl = [v for v in (_num(r.get("low")) for r in rows) if v is not None]
        gc = [v for v in (_num(r.get("close")) for r in rows) if v is not None]
        if gh and gl:
            add(max(gh), f"previous_{authority.lower()}_high", authority, "resistance", base)
            add(min(gl), f"previous_{authority.lower()}_low", authority, "support", base)
            if gc: add(gc[-1], f"previous_{authority.lower()}_close", authority, None, base - 8)
    add_previous_group(grouped_week, "1W", 44.0); add_previous_group(grouped_month, "1M", 52.0)

    finite_prices = [c for c in closes if c is not None]
    low_bound, high_bound = min(finite_prices), max(finite_prices)
    if volume_evidence_complete and high_bound > low_bound and sum(volumes) > 0:
        bucket_count = min(36, max(12, int(len(sample) ** 0.5 * 2))); width = (high_bound - low_bound) / bucket_count
        buckets = [0.0] * bucket_count
        for row, volume in zip(sample, volumes):
            parts = [v for v in (_num(row.get("high")), _num(row.get("low")), _num(row.get("close"))) if v is not None]
            if not parts: continue
            typical = sum(parts) / len(parts); idx = min(bucket_count - 1, max(0, int((typical - low_bound) / max(width, 1e-9)))); buckets[idx] += volume
        peak = max(buckets) or 1.0
        for rank, idx in enumerate(sorted(range(bucket_count), key=lambda i: buckets[i], reverse=True)[:3]):
            add(low_bound + (idx + .5) * width, "volume_profile_poc" if rank == 0 else "volume_profile_hvn", "VOLUME", None, 46.0 if rank == 0 else 32.0, volume_node_strength=round(buckets[idx] / peak, 4))

    for index in range(max(1, len(sample) - 80), len(sample)):
        prev, row = sample[index - 1], sample[index]
        prev_high, prev_low, high, low = _num(prev.get("high")), _num(prev.get("low")), _num(row.get("high")), _num(row.get("low"))
        if None in (prev_high, prev_low, high, low): continue
        if low > prev_high + atr * .08:
            add(prev_high, "gap_up_lower_boundary", "GAP", "support", 30.0, gap_index=index); add(low, "gap_up_upper_boundary", "GAP", "support", 28.0, gap_index=index)
        elif high < prev_low - atr * .08:
            add(prev_low, "gap_down_upper_boundary", "GAP", "resistance", 30.0, gap_index=index); add(high, "gap_down_lower_boundary", "GAP", "resistance", 28.0, gap_index=index)

    # Cluster by native role first.  This prevents a swing-high and swing-low
    # sitting close together from being averaged into a role-ambiguous line.
    clusters: List[Dict[str, Any]] = []
    for candidate in sorted(candidates, key=lambda row: (row["native_kind"], row["price"])):
        target = next((c for c in clusters if c["native_kind"] == candidate["native_kind"] and abs(candidate["price"] - c["price"]) <= merge_distance), None)
        if target is None:
            target = {"price": candidate["price"], "members": [], "native_kind": candidate["native_kind"]}; clusters.append(target)
        target["members"].append(candidate)
        weights = [(m["price"], max(1.0, m["base_score"])) for m in target["members"]]
        target["price"] = sum(v * w for v, w in weights) / sum(w for _, w in weights)

    avg_volume = (sum(volumes[-20:]) / max(1, len(volumes[-20:]))) if volume_evidence_complete else None
    levels: List[Dict[str, Any]] = []
    recent_count = min(10, len(sample))
    for cluster in clusters:
        price = float(cluster["price"]); members = cluster["members"]; native_kind = cluster["native_kind"]
        recent_rows = sample[-recent_count:]
        recent_closes = [_num(r.get("close")) for r in recent_rows]
        recent_closes = [v for v in recent_closes if v is not None]

        # Role flip proof uses a directional break plus acceptance.  Two closes
        # beyond the level count as acceptance; a later wick/test into the zone
        # followed by a close on the new side upgrades it to RETEST.
        break_up = False; break_down = False; retest_up = False; retest_down = False
        break_up_index = None; break_down_index = None
        for i in range(1, len(sample)):
            prev_c, cur_c = _num(sample[i-1].get("close")), _num(sample[i].get("close"))
            if prev_c is None or cur_c is None: continue
            if prev_c <= price + evidence_distance and cur_c > price + evidence_distance:
                break_up = True; break_up_index = i
            if prev_c >= price - evidence_distance and cur_c < price - evidence_distance:
                break_down = True; break_down_index = i
        # A breakout candle is not its own retest. A role flip requires either
        # subsequent acceptance (two closes on the new side) or a later candle
        # that revisits the zone and closes back on the new side.
        if break_up and break_up_index is not None:
            for row in sample[break_up_index + 1:]:
                low, close = _num(row.get("low")), _num(row.get("close"))
                if low is not None and close is not None and low <= price + evidence_distance and close > price:
                    retest_up = True
        if break_down and break_down_index is not None:
            for row in sample[break_down_index + 1:]:
                high, close = _num(row.get("high")), _num(row.get("close"))
                if high is not None and close is not None and high >= price - evidence_distance and close < price:
                    retest_down = True
        above_accept = len(recent_closes) >= 2 and sum(1 for v in recent_closes[-3:] if v > price + evidence_distance) >= 2
        below_accept = len(recent_closes) >= 2 and sum(1 for v in recent_closes[-3:] if v < price - evidence_distance) >= 2

        kind = native_kind
        role_state = "NATIVE"
        role_actionable = True
        breakout_state = "UNTESTED"
        if native_kind == "resistance" and last_close > price + evidence_distance:
            if break_up and (retest_up or above_accept):
                kind = "support"; role_state = "RESISTANCE_TO_SUPPORT_RETEST" if retest_up else "RESISTANCE_TO_SUPPORT_ACCEPTED"; breakout_state = "RETEST" if retest_up else "BREAKOUT_UP"
            else:
                kind = "resistance"; role_state = "BROKEN_RESISTANCE_UNCONFIRMED"; breakout_state = "BREAKOUT_UP_UNCONFIRMED"; role_actionable = False
        elif native_kind == "support" and last_close < price - evidence_distance:
            if break_down and (retest_down or below_accept):
                kind = "resistance"; role_state = "SUPPORT_TO_RESISTANCE_RETEST" if retest_down else "SUPPORT_TO_RESISTANCE_ACCEPTED"; breakout_state = "RETEST" if retest_down else "BREAKDOWN"
            else:
                kind = "support"; role_state = "BROKEN_SUPPORT_UNCONFIRMED"; breakout_state = "BREAKDOWN_UNCONFIRMED"; role_actionable = False
        elif native_kind == "neutral":
            kind = "support" if price <= last_close else "resistance"; role_state = "NEUTRAL_REFERENCE_CURRENT_SIDE"
        elif native_kind == "resistance" and break_up and retest_up:
            breakout_state = "RETEST"
        elif native_kind == "support" and break_down and retest_down:
            breakout_state = "RETEST"

        touch_indices: List[int] = []; rejection_moves: List[float] = []; volume_at_touch: List[float] = []
        support_rejections = resistance_rejections = 0
        for index, row in enumerate(sample):
            high, low, close = _num(row.get("high")), _num(row.get("low")), _num(row.get("close"))
            if None in (high, low, close) or not (low - evidence_distance <= price <= high + evidence_distance): continue
            future = sample[index + 1:min(len(sample), index + 8)]
            if kind == "support":
                valid_touch = close >= price - evidence_distance * .35
                if valid_touch and future:
                    future_high = max((_num(x.get("high")) or price) for x in future)
                    move = max(0.0, future_high - price) / max(atr, 1e-9)
                    if move >= .25: support_rejections += 1
                    rejection_moves.append(move)
            else:
                valid_touch = close <= price + evidence_distance * .35
                if valid_touch and future:
                    future_low = min((_num(x.get("low")) or price) for x in future)
                    move = max(0.0, price - future_low) / max(atr, 1e-9)
                    if move >= .25: resistance_rejections += 1
                    rejection_moves.append(move)
            if valid_touch:
                touch_indices.append(index); volume_at_touch.append(volumes[index])
        touches = len(touch_indices); bars_since = len(sample)-1-max(touch_indices) if touch_indices else len(sample)
        recency = max(0.0, 1.0 - bars_since / max(20.0, len(sample)))
        rejection_strength = min(1.0, (sum(rejection_moves) / max(1, len(rejection_moves))) / 2.0)
        volume_strength = (
            min(1.5, (sum(volume_at_touch) / max(1, len(volume_at_touch))) / max(avg_volume, 1.0))
            if volume_evidence_complete and avg_volume is not None else 0.0
        )
        source_score = max(m["base_score"] for m in members); source_bonus = min(28.0, sum(m["base_score"] for m in members) * .18)
        distance_pct = abs(price-last_close)/last_close*100.0
        regime_adjustment = 0.0
        if adx14 is not None:
            if adx14 < 20: regime_adjustment += min(8.0, touches*1.4 + rejection_strength*4.0)
            elif adx14 >= 25 and plus_di14 is not None and minus_di14 is not None:
                bullish = plus_di14 > minus_di14
                regime_adjustment += 6.0 if ((bullish and kind == "support") or ((not bullish) and kind == "resistance")) else -3.0
                if role_state.endswith("ACCEPTED") or role_state.endswith("RETEST"): regime_adjustment += 8.0
                if adx_change is not None and adx_change > 0: regime_adjustment += 2.0
        importance = source_score + source_bonus + min(30.0, touches*5.0) + rejection_strength*18.0 + min(15.0, volume_strength*10.0) + recency*10.0 + regime_adjustment - min(18.0, distance_pct*.35)
        institutional = any(m["timeframe"] in {"1W", "1M", "VOLUME"} for m in members)
        validated = bool(role_actionable and (touches >= 2 or institutional or role_state.endswith("ACCEPTED") or role_state.endswith("RETEST")))
        distance_abs = abs(price-last_close); actionable = bool(validated and distance_abs >= actionability_distance)
        levels.append({
            "price": _round(price), "kind": kind, "side": kind, "native_kind": native_kind,
            "role_state": role_state, "validated": validated, "actionable": actionable,
            "inside_noise_zone": validated and not actionable, "minimum_actionable_distance": _round(actionability_distance), "distance_abs": _round(distance_abs),
            "touches": touches, "touch_count": touches, "support_rejections": support_rejections, "resistance_rejections": resistance_rejections,
            "bars_since_touch": bars_since, "volume_strength": round(volume_strength,3), "rejection_strength": round(rejection_strength,3),
            "freshness": "CURRENT" if bars_since <= 20 else "RECENT" if bars_since <= 60 else "OLD",
            "breakout_retest_state": breakout_state, "regime_adjustment": round(regime_adjustment,2),
            "adx14": _round(adx14), "plus_di14": _round(plus_di14), "minus_di14": _round(minus_di14), "directional_regime": directional_regime,
            "importance_score": round(max(0.0,min(100.0,importance)),2), "distance_pct": _round(distance_pct),
            "timeframe": max((m["timeframe"] for m in members), key=lambda v: {"1M":6,"1W":5,"1D":4,"VOLUME":3,"GAP":2,"selected":1}.get(v,0)),
            "sources": sorted({m["source"] for m in members}), "source_level": "+".join(sorted({m["source_level"] for m in members}))[:180],
            "zone_low": _round(price-evidence_distance*.55), "zone_high": _round(price+evidence_distance*.55), "line_style": "solid",
            "proof": f"{role_state}; {touches} directional touches; support rejections {support_rejections}; resistance rejections {resistance_rejections}",
        })

    supports = sorted((r for r in levels if r["kind"] == "support"), key=lambda r: (-r["importance_score"], abs(last_close-r["price"])))
    resistances = sorted((r for r in levels if r["kind"] == "resistance"), key=lambda r: (-r["importance_score"], abs(r["price"]-last_close)))
    validated_support = [r for r in supports if r["validated"]]; validated_resistance = [r for r in resistances if r["validated"]]
    actionable_support = [r for r in validated_support if r.get("actionable") and r["price"] < last_close]
    actionable_resistance = [r for r in validated_resistance if r.get("actionable") and r["price"] > last_close]
    nearest_support = max(actionable_support, key=lambda r:r["price"], default=None); nearest_resistance = min(actionable_resistance, key=lambda r:r["price"], default=None)
    major_support = max(actionable_support, key=lambda r:r["importance_score"], default=None); major_resistance = max(actionable_resistance, key=lambda r:r["importance_score"], default=None)
    ranked = sorted(levels, key=lambda r:(-r["importance_score"], r["distance_pct"]))
    return {
        "ok": True, "version": "major-level-authority-5.0.0-price-action-role-flip",
        "method": "Timeframe-aware Wilder ATR/ADX cash structure with native-role preservation, directional rejection scoring and completed-candle break/acceptance/retest role flips; Camarilla remains separate context",
        "last_close": _round(last_close), "atr14": _round(atr), "adx14": _round(adx14),
        "volume_evidence_complete": volume_evidence_complete, "plus_di14": _round(plus_di14), "minus_di14": _round(minus_di14), "adx_change": _round(adx_change), "directional_regime": directional_regime,
        "merge_distance": _round(merge_distance), "support": validated_support[:12], "resistance": validated_resistance[:12],
        "provisional_support": [r for r in supports if not r["validated"]][:8], "provisional_resistance": [r for r in resistances if not r["validated"]][:8],
        "inside_noise_zone": [r for r in levels if r.get("inside_noise_zone")][:12], "minimum_actionable_distance": _round(actionability_distance),
        "nearest_support": nearest_support, "nearest_resistance": nearest_resistance, "major_support": major_support, "major_resistance": major_resistance,
        "ranked_levels": ranked[:24],
        "summary": (f"Major support {major_support['price']} score {major_support['importance_score']}" if major_support else "No validated support") + "; " + (f"Major resistance {major_resistance['price']} score {major_resistance['importance_score']}" if major_resistance else "No validated resistance"),
        "policy": "A native resistance below price is not support until breakout acceptance/retest is proven; a native support above price is not resistance until breakdown acceptance/retest is proven.",
    }

def market_structure(candles: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Candle-derived structure. No SMC claim if candles are too thin."""
    if not candles or len(candles) < 20:
        return {"ok": False, "state": "pending", "score": 0, "bias": "neutral", "summary": "Need 20+ candles", "evidence": []}
    sample = candles[-60:] if len(candles) >= 60 else candles
    closes = [_num(c.get("close")) for c in sample]
    highs = [_num(c.get("high")) for c in sample]
    lows = [_num(c.get("low")) for c in sample]
    raw_vols = [_num(c.get("volume")) for c in sample]
    volume_complete = all(v is not None and v >= 0 for v in raw_vols[-20:])
    vols = [float(v) if v is not None and v >= 0 else 0.0 for v in raw_vols]
    # Every H/L/C value that can enter the structure window must be valid.
    # Checking only the last ten bars allowed an older NaN to crash/maximise
    # prior-range mathematics.
    for high, low, close in zip(highs, lows, closes):
        if None in (high, low, close) or min(high, low, close) <= 0 or high < low or not (low <= close <= high):
            return {"ok": False, "state": "pending", "score": 0, "bias": "neutral", "summary": "Incomplete/invalid OHLC", "evidence": [], "volume_evidence_complete": volume_complete}

    last_close, prev_close = closes[-1], closes[-2]
    recent_high = max(highs[-20:])
    recent_low = min(lows[-20:])
    prior_high = max(highs[-40:-20]) if len(highs) >= 40 else max(highs[:-10])
    prior_low = min(lows[-40:-20]) if len(lows) >= 40 else min(lows[:-10])
    swing_high_5 = max(highs[-5:])
    swing_low_5 = min(lows[-5:])
    avg_vol = sum(vols[-20:]) / max(len(vols[-20:]), 1)
    vol_burst = bool(volume_complete and avg_vol and vols[-1] > avg_vol * 1.5)

    evidence: List[str] = []
    score = 0
    bias = "neutral"
    state = "range"

    if recent_high > prior_high and recent_low > prior_low:
        bias = "long"
        state = "up_structure"
        score += 18
        evidence.append("Higher-high / higher-low structure")
    elif recent_high < prior_high and recent_low < prior_low:
        bias = "short"
        state = "down_structure"
        score += 18
        evidence.append("Lower-high / lower-low structure")
    else:
        evidence.append("Range or mixed structure")

    if last_close and last_close > prior_high:
        bias = "long"
        state = "break_of_structure_up"
        score += 18
        evidence.append("Break of structure above prior range")
    elif last_close and last_close < prior_low:
        bias = "short"
        state = "break_of_structure_down"
        score += 18
        evidence.append("Break of structure below prior range")

    # Liquidity sweep + reclaim/reject approximation.
    prev_high = max(highs[-12:-1]) if len(highs) >= 12 else max(highs[:-1])
    prev_low = min(lows[-12:-1]) if len(lows) >= 12 else min(lows[:-1])
    last_high, last_low = highs[-1], lows[-1]
    if last_low < prev_low and last_close > prev_low:
        bias = "long"
        state = "liquidity_sweep_reclaim"
        score += 14
        evidence.append("Liquidity sweep below recent lows followed by reclaim")
    if last_high > prev_high and last_close < prev_high:
        bias = "short"
        state = "liquidity_sweep_reject"
        score += 14
        evidence.append("Liquidity sweep above recent highs followed by rejection")

    if vol_burst:
        score += 8
        evidence.append("Volume confirms structure move")

    sr = support_resistance_levels(candles)
    # A valid S/R calculation can legitimately have no nearby validated level.
    # Treat that as absent evidence instead of dereferencing None and blanking
    # the complete Stock Intelligence response.
    validated_support = (sr.get("nearest_support") or {}).get("price") if sr.get("ok") else None
    validated_resistance = (sr.get("nearest_resistance") or {}).get("price") if sr.get("ok") else None

    return {
        "ok": True,
        "state": state,
        "bias": bias,
        "score": min(100, int(score)),
        # v36.9.11: support/resistance now come from the touch-validated cluster
        # engine, not a raw 20-bar min/max. Falls back to the raw recent_low/high
        # only if there isn't enough pivot history yet (e.g. thin candle store).
        "support": validated_support if validated_support is not None else _round(recent_low),
        "resistance": validated_resistance if validated_resistance is not None else _round(recent_high),
        "support_validated": validated_support is not None,
        "resistance_validated": validated_resistance is not None,
        "swing_high": _round(swing_high_5),
        "swing_low": _round(swing_low_5),
        "summary": "; ".join(evidence[:3]),
        "evidence": evidence,
        "volume_evidence_complete": volume_complete,
    }



def _pivots(candles: List[Dict[str, Any]], left: int = 3, right: int = 3):
    """Fractal-style swing pivots: a bar is a swing high/low if its high/low is the
    extreme within `left` bars before and `right` bars after it."""
    highs = [_num(c.get("high")) for c in candles]
    lows = [_num(c.get("low")) for c in candles]
    ph, pl = [], []
    n = len(candles)
    for i in range(left, n - right):
        h, l = highs[i], lows[i]
        if h is None or l is None:
            continue
        window_h = highs[i - left:i] + highs[i + 1:i + 1 + right]
        window_l = lows[i - left:i] + lows[i + 1:i + 1 + right]
        if window_h and all(h >= x for x in window_h if x is not None):
            ph.append((i, h))
        if window_l and all(l <= x for x in window_l if x is not None):
            pl.append((i, l))
    return ph, pl


def trendline(candles: List[Dict[str, Any]], lookback: int = 120) -> Dict[str, Any]:
    """Fits a line through the two most recent swing lows (uptrend candidate) and
    through the two most recent swing highs (downtrend candidate) using real pivot
    points from candle history -- not a generated/guessed line."""
    if not candles or len(candles) < 20:
        return {"ok": False, "reason": "insufficient candles"}
    sample = candles[-lookback:] if len(candles) > lookback else candles
    ph, pl = _pivots(sample, left=3, right=3)
    out: Dict[str, Any] = {"ok": True, "up": None, "down": None}
    if len(pl) >= 2:
        (i1, p1), (i2, p2) = pl[-2], pl[-1]
        if i2 != i1:
            slope = (p2 - p1) / (i2 - i1)
            if slope > 0:
                out["up"] = {
                    "type": "support_trendline",
                    "p1": {"index": i1, "price": _round(p1)},
                    "p2": {"index": i2, "price": _round(p2)},
                    "slope": _round(slope, 4),
                    "projected": _round(p2 + slope * (len(sample) - 1 - i2)),
                }
    if len(ph) >= 2:
        (i1, p1), (i2, p2) = ph[-2], ph[-1]
        if i2 != i1:
            slope = (p2 - p1) / (i2 - i1)
            if slope < 0:
                out["down"] = {
                    "type": "resistance_trendline",
                    "p1": {"index": i1, "price": _round(p1)},
                    "p2": {"index": i2, "price": _round(p2)},
                    "slope": _round(slope, 4),
                    "projected": _round(p2 + slope * (len(sample) - 1 - i2)),
                }
    return out


def order_blocks(candles: List[Dict[str, Any]], lookback: int = 80) -> Dict[str, Any]:
    """SMC order-block approximation from real candles: the last opposite-colour
    candle immediately before a strong impulsive move (>1.5x the prior 10-candle
    average range) is marked as the order block."""
    if not candles or len(candles) < 15:
        return {"ok": False, "bullish": None, "bearish": None}
    sample = candles[-lookback:] if len(candles) > lookback else candles
    bullish, bearish = None, None
    for i in range(10, len(sample)):
        c = sample[i]
        o, h, l, cl = _num(c.get("open")), _num(c.get("high")), _num(c.get("low")), _num(c.get("close"))
        if None in (o, h, l, cl):
            continue
        ranges = []
        for j in range(i - 10, i):
            hh, ll = _num(sample[j].get("high")), _num(sample[j].get("low"))
            if hh is not None and ll is not None:
                ranges.append(hh - ll)
        avg_range = (sum(ranges) / len(ranges)) if ranges else None
        this_range = h - l
        impulsive = avg_range and this_range > avg_range * 1.5
        if not impulsive:
            continue
        prev = sample[i - 1]
        po, pc = _num(prev.get("open")), _num(prev.get("close"))
        if po is None or pc is None:
            continue
        if cl > o and pc < po:
            bullish = {
                "index": i - 1, "high": _round(max(po, pc)), "low": _round(min(po, pc)),
                "impulse_index": i, "note": "last down candle before up impulse",
            }
        elif cl < o and pc > po:
            bearish = {
                "index": i - 1, "high": _round(max(po, pc)), "low": _round(min(po, pc)),
                "impulse_index": i, "note": "last up candle before down impulse",
            }
    return {"ok": True, "bullish": bullish, "bearish": bearish}


def retest_zone(candles: List[Dict[str, Any]], structure: Dict[str, Any]) -> Dict[str, Any]:
    """Compatibility facade over the canonical breakout/retest authority."""
    from core.breakout_retest_evidence_authority import DEFAULT_BREAKOUT_RETEST_EVIDENCE_AUTHORITY
    return DEFAULT_BREAKOUT_RETEST_EVIDENCE_AUTHORITY.project_structure_retest_zone(
        candles=candles, structure=structure
    )


def volume_profile(candles: List[Dict[str, Any]], bins: int = 12) -> Dict[str, Any]:
    """Approximate volume profile from OHLCV candles, not tick-level VP."""
    sample = candles[-80:] if len(candles) >= 80 else candles
    if not sample or len(sample) < 15:
        return {"ok": False, "state": "pending", "score": 0, "bias": "neutral", "summary": "Need 15+ candles", "method": "approx candle volume profile"}
    rows = []
    for c in sample:
        h, l, cl, vol = _num(c.get("high")), _num(c.get("low")), _num(c.get("close")), _num(c.get("volume"))
        if None in (h, l, cl, vol) or min(h, l, cl) <= 0 or h < l or not (l <= cl <= h) or vol < 0:
            return {"ok": False, "state": "pending", "score": 0, "bias": "neutral", "summary": "Incomplete/invalid HLCV", "method": "approx candle volume profile", "decision_usable": False}
        price = (h + l + cl) / 3.0
        rows.append((price, vol, cl))
    if len(rows) < 10:
        return {"ok": False, "state": "pending", "score": 0, "bias": "neutral", "summary": "Incomplete volume rows", "method": "approx candle volume profile"}
    prices = [r[0] for r in rows]
    mn, mx = min(prices), max(prices)
    if mx <= mn:
        return {"ok": False, "state": "flat", "score": 0, "bias": "neutral", "summary": "Flat price range", "method": "approx candle volume profile"}
    bucket = [0.0 for _ in range(bins)]
    width = (mx - mn) / bins
    for price, vol, _ in rows:
        idx = min(bins - 1, max(0, int((price - mn) / width)))
        bucket[idx] += vol
    total = sum(bucket)
    poc_idx = max(range(bins), key=lambda i: bucket[i])
    poc = mn + (poc_idx + 0.5) * width
    # value area: expand from POC until ~70% volume.
    included = {poc_idx}
    acc = bucket[poc_idx]
    lo = hi = poc_idx
    while total and acc / total < 0.70 and (lo > 0 or hi < bins - 1):
        left = bucket[lo - 1] if lo > 0 else -1
        right = bucket[hi + 1] if hi < bins - 1 else -1
        if right >= left and hi < bins - 1:
            hi += 1; included.add(hi); acc += bucket[hi]
        elif lo > 0:
            lo -= 1; included.add(lo); acc += bucket[lo]
        else:
            break
    val = mn + lo * width
    vah = mn + (hi + 1) * width
    last_close = rows[-1][2]
    bias = "neutral"
    state = "inside_value"
    score = 0
    evidence = ["Approx volume profile from candle data"]
    if last_close > vah:
        bias = "long"; state = "acceptance_above_value"; score += 12; evidence.append("Price accepted above value area")
    elif last_close < val:
        bias = "short"; state = "acceptance_below_value"; score += 12; evidence.append("Price accepted below value area")
    elif abs(last_close - poc) / poc < 0.01:
        state = "near_poc"; score += 4; evidence.append("Price near POC; expect chop/mean reversion unless breakout")
    lvn_idx = min(range(bins), key=lambda i: bucket[i])
    hvn_idx = poc_idx
    return {
        "ok": True, "state": state, "bias": bias, "score": min(100, int(score)), "method": "approx candle volume profile",
        "poc": _round(poc), "vah": _round(vah), "val": _round(val),
        "hvn": _round(mn + (hvn_idx + .5) * width), "lvn": _round(mn + (lvn_idx + .5) * width),
        "summary": "; ".join(evidence), "evidence": evidence,
    }


def orb_context(candles: List[Dict[str, Any]], now=None) -> Dict[str, Any]:
    """Canonical ORB5 context.

    The opening range is the first completed 09:15-09:20 five-minute candle.
    Before 09:20 it is formation evidence only.  From 09:20 onward the range is
    available immediately for live confirmation; a second five-minute candle is
    useful evidence but is never a hidden requirement to wait until 09:25/09:30.
    """
    rows = closed_candles(candles, "5minute", now)
    if not rows:
        return {"ok": False, "state": "pending", "phase": "building_5m", "bias": "neutral", "score": 0, "confirmed": False, "summary": "Need intraday candles"}
    last_dt = candle_datetime(rows[-1]); last_day = last_dt.date() if last_dt else None
    day_rows = [c for c in rows if candle_datetime(c) and candle_datetime(c).date() == last_day and (9,15) <= (candle_datetime(c).hour, candle_datetime(c).minute) <= (15,30)]
    def _valid_orb_row(c: Dict[str, Any]) -> bool:
        high, low, close, volume = _num(c.get("high")), _num(c.get("low")), _num(c.get("close")), _num(c.get("volume"))
        return bool(None not in (high, low, close, volume) and min(high, low, close) > 0 and high >= low and low <= close <= high and volume >= 0)
    if day_rows and any(not _valid_orb_row(c) for c in day_rows):
        return {"ok": False, "state": "pending", "phase": "invalid_session_evidence", "bias": "neutral", "score": 0, "confirmed": False, "summary": "ORB5 current-session HLCV incomplete/invalid"}
    prior_days = sorted({candle_datetime(c).date() for c in rows if candle_datetime(c) and last_day and candle_datetime(c).date() < last_day})
    prior = [c for c in rows if prior_days and candle_datetime(c) and candle_datetime(c).date() == prior_days[-1]]
    if prior and any(not _valid_orb_row(c) for c in prior):
        prior = []
    pdh = max((_num(c.get("high")) for c in prior), default=None) if prior else None
    pdl = min((_num(c.get("low")) for c in prior), default=None) if prior else None
    pdc = _num(prior[-1].get("close")) if prior else None
    pivot = (pdh+pdl+pdc)/3 if pdh and pdl and pdc else None; bc=(pdh+pdl)/2 if pdh and pdl else None; tc=2*pivot-bc if pivot and bc else None
    participation = DEFAULT_PARTICIPATION_EVIDENCE_AUTHORITY.intraday_same_clock_rvol(rows, at=now)
    session_rvol = participation.get("value")
    prior_levels = {
        "previous_day_high": _round(pdh), "previous_day_low": _round(pdl), "previous_day_close": _round(pdc),
        "pivot": _round(pivot), "cpr_bottom": _round(min(bc,tc)) if bc and tc else None, "cpr_top": _round(max(bc,tc)) if bc and tc else None,
        "session_relative_volume": _round(session_rvol,2), "participation_evidence": participation,
        "participation_authority": participation.get("authority"), "participation_authority_version": participation.get("authority_version"),
        "participation_lane": participation.get("lane"), "participation_source_time": participation.get("source_time"),
        "participation_decision_usable": participation.get("decision_usable") is True,
    }
    opening = next((c for c in day_rows if candle_datetime(c) and (candle_datetime(c).hour,candle_datetime(c).minute)==(9,15)), None)
    if opening is None:
        return {"ok": True, "state": "building", "phase": "building_5m", "bias": "neutral", "score": 0, "confirmed": False, **prior_levels,
                "orb_high": None, "orb_low": None, "outside_closes": 0, "volume_confirmed": False, "summary": "ORB5 forming; first 09:15-09:20 completed bar not available yet"}
    hi, lo = _num(opening.get("high")), _num(opening.get("low"))
    if hi is None or lo is None or hi <= lo:
        return {"ok": False, "state": "pending", "phase": "building_5m", "bias": "neutral", "score": 0, "confirmed": False, **prior_levels, "summary": "ORB5 OHLC incomplete"}
    phase = "orb5_ready"
    last_close = _num(day_rows[-1].get("close")) if day_rows else None
    if last_close is None:
        return {"ok": True, "state": "orb5_ready", "phase": phase, "bias": "neutral", "score": 0, "confirmed": False, **prior_levels, "orb_high":_round(hi), "orb_low":_round(lo), "summary":"ORB5 ready; waiting for live price confirmation"}
    width=max(hi-lo,1e-9); bias="neutral"; state="inside_orb"; distance=0.0
    if last_close > hi:
        bias="long"; state="orb_breakout_up"; distance=(last_close-hi)/width
    elif last_close < lo:
        bias="short"; state="orb_breakdown_down"; distance=(lo-last_close)/width
    post = [c for c in day_rows if candle_datetime(c) and (candle_datetime(c).hour,candle_datetime(c).minute) >= (9,20)]
    outside_closes = sum(1 for c in post[-3:] if (_num(c.get("close")) or (-10**18 if bias=="long" else 10**18)) > hi) if bias=="long" else sum(1 for c in post[-3:] if (_num(c.get("close")) or 10**18) < lo) if bias=="short" else 0
    opening_vol=_num(opening.get("volume")) or 0.0
    recent_vol=sum((_num(c.get("volume")) or 0.0) for c in post[-2:]) / max(1,min(2,len(post))) if post else 0.0
    volume_confirmed=bool(opening_vol and recent_vol >= opening_vol*1.15)
    retest=False
    if bias=="long":
        retest=any((_num(c.get("low")) is not None and _num(c.get("close")) is not None and _num(c.get("low")) <= hi+width*.08 and _num(c.get("close")) >= hi) for c in post[-3:])
        if retest: state="orb_retest_hold_up"
    elif bias=="short":
        retest=any((_num(c.get("high")) is not None and _num(c.get("close")) is not None and _num(c.get("high")) >= lo-width*.08 and _num(c.get("close")) <= lo) for c in post[-3:])
        if retest: state="orb_retest_hold_down"
    if bias=="neutral" and any((_num(c.get("high")) or 0)>hi and (_num(c.get("close")) or 0)<=hi for c in post[-3:]): state="failed_breakout_rejection"
    if bias=="neutral" and any((_num(c.get("low")) or 10**18)<lo and (_num(c.get("close")) or 10**18)>=lo for c in post[-3:]): state="failed_breakdown_rejection"
    confirmed = bool(bias != "neutral" and (retest or outside_closes >= 2 or volume_confirmed) and distance <= 1.5)
    score = 16 if confirmed else 7 if bias != "neutral" else 0
    return {"ok":True,"state":state,"phase":phase,"bias":bias,"score":score,"confirmed":confirmed,**prior_levels,
            "outside_closes":outside_closes,"volume_confirmed":volume_confirmed,"retest_confirmed":retest,"break_distance_orb":round(distance,3),
            "orb_high":_round(hi),"orb_low":_round(lo),"opening_bar_count":1,"opening_range_minutes":5,
            "summary":f"ORB5 {round(lo,2)}-{round(hi,2)}; {state}; closed-bar confirmation {'yes' if confirmed else 'forming/live confirmation allowed'}"}

def heat_strip_context(heatmap: List[Dict[str, Any]], sector_hint: str = "") -> Dict[str, Any]:
    """Compatibility facade over governed broad/sector session-heat analysis.

    This function no longer owns market-direction mathematics.  It preserves
    the established weighted change heat only when the input rows were already
    authorized atomically by MarketContextSnapshotAuthority.
    """
    return DEFAULT_MARKET_SECTOR_CONTEXT_ANALYSIS_AUTHORITY.analyze(heatmap, sector_hint)


def sector_hint_from_symbol(symbol: str, name: str = "") -> str:
    s = (symbol or "").upper(); n = (name or "").upper()
    it = {"TCS", "INFY", "WIPRO", "HCLTECH", "TECHM", "LTM", "LTIM", "MPHASIS", "PERSISTENT", "COFORGE"}
    psu_bank = {"SBIN", "BANKBARODA", "PNB", "CANBK", "UNIONBANK", "INDIANB", "BANKINDIA", "IOB", "CENTRALBK", "MAHABANK"}
    pvt_bank = {"HDFCBANK", "ICICIBANK", "KOTAKBANK", "AXISBANK", "IDFCFIRSTB", "INDUSINDBK", "FEDERALBNK", "RBLBANK", "YESBANK"}
    pharma = {"SUNPHARMA", "CIPLA", "DRREDDY", "DIVISLAB", "AUROPHARMA", "LUPIN", "ZYDUSLIFE"}
    auto = {"TMPV", "TATAMOTORS", "M&M", "MARUTI", "BAJAJ-AUTO", "EICHERMOT", "TVSMOTOR", "HEROMOTOCO", "MOTHERSON", "SONACOMS", "EXIDEIND", "AMARAJABAT"}
    metal = {"TATASTEEL", "JSWSTEEL", "HINDALCO", "VEDL", "SAIL", "JINDALSTEL"}
    fmcg = {"HINDUNILVR", "ITC", "NESTLEIND", "BRITANNIA", "DABUR", "MARICO", "GODREJCP", "TATACONSUM", "COLPAL"}
    realty = {"DLF", "GODREJPROP", "OBEROIRLTY", "PHOENIXLTD", "PRESTIGE", "LODHA", "BRIGADE"}
    energy = {"RELIANCE", "NTPC", "POWERGRID", "TATAPOWER", "ADANIGREEN", "ADANIPOWER", "JSWENERGY"}
    oilgas = {"ONGC", "BPCL", "HINDPETRO", "IOC", "GAIL", "OIL", "PETRONET"}
    healthcare = {"APOLLOHOSP", "MAXHEALTH", "FORTIS", "METROPOLIS", "LALPATHLAB", "NH"}
    durables = {"TITAN", "HAVELLS", "VOLTAS", "CROMPTON", "WHIRLPOOL", "BLUESTARCO", "DIXON"}
    media = {"ZEEL", "SUNTV", "PVRINOX", "NETWORK18", "SAREGAMA", "TIPSINDLTD"}
    if s in it or "TECH" in n or "CONSULT" in n: return "IT"
    if s in psu_bank: return "PSUBANK"
    if s in pvt_bank or "BANK" in n: return "BANK"
    if s in pharma or "PHARMA" in n or "LAB" in n: return "PHARMA"
    if s in auto or "MOTOR" in n or "AUTO" in n or "MOTHERSON" in n or "SAMVARDHANA" in n: return "AUTO"
    if s in metal or "STEEL" in n or "METAL" in n: return "METAL"
    if s in fmcg or "CONSUMER" in n and "DURABLE" not in n: return "FMCG"
    if s in realty or "REALTY" in n or "PROPERT" in n: return "REALTY"
    if s in oilgas or "PETRO" in n: return "OILGAS"
    if s in energy or "POWER" in n: return "ENERGY"
    if s in healthcare or "HOSPITAL" in n: return "HEALTHCARE"
    if s in durables: return "CONSUMDUR"
    if s in media or "MEDIA" in n: return "MEDIA"
    return ""
