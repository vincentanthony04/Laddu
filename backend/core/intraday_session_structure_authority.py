"""Canonical Intraday session price-action / level convergence authority.

This authority turns today's actual session evidence into operating S/R without
letting any one indicator manufacture a price level.  ORB5, session H/L, VWAP,
EMA20/50, completed swing pivots and selected historical structure are clustered
into auditable zones.  Official NSE evidence may strengthen/penalise confidence
or block tradeability, but it never creates a support/resistance price.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Dict, Iterable, List, Mapping, Optional
from core.numeric_semantics import finite_number

from session_candles import candle_datetime, closed_candles


def _f(value: Any) -> Optional[float]:
    return finite_number(value)


def _r(value: Any, digits: int = 2):
    num = _f(value)
    return round(num, digits) if num is not None else None


def _pivot_rows(rows: List[Dict[str, Any]], left: int = 2, right: int = 2):
    highs = [_f(row.get("high")) for row in rows]
    lows = [_f(row.get("low")) for row in rows]
    ph, pl = [], []
    for index in range(left, len(rows) - right):
        high, low = highs[index], lows[index]
        if high is None or low is None:
            continue
        surrounding_highs = [value for value in highs[index-left:index] + highs[index+1:index+1+right] if value is not None]
        surrounding_lows = [value for value in lows[index-left:index] + lows[index+1:index+1+right] if value is not None]
        if surrounding_highs and all(high >= value for value in surrounding_highs):
            ph.append((index, high))
        if surrounding_lows and all(low <= value for value in surrounding_lows):
            pl.append((index, low))
    return ph, pl


@dataclass(frozen=True)
class SessionStructurePolicy:
    cluster_atr: float = 0.14
    cluster_pct: float = 0.12
    entry_buffer_atr: float = 0.04
    entry_buffer_pct: float = 0.04
    max_chase_atr: float = 0.55
    max_chase_pct: float = 0.65
    minimum_zone_score: float = 28.0
    a_plus_score: float = 78.0
    rvol_confirm: float = 1.15
    rvol_strong: float = 1.35


class IntradaySessionStructureAuthority:
    AUTHORITY = "IntradaySessionStructureAuthority"
    VERSION = "1.0.0-orb5-vwap-ema-volume-price-action-nse-context"

    @classmethod
    def project(
        cls,
        *,
        candles: Iterable[Mapping[str, Any]],
        current_price: Any,
        atr: Any,
        ema20: Any,
        ema50: Any,
        vwap: Any,
        orb: Mapping[str, Any] | None,
        historical_level_report: Mapping[str, Any] | None,
        market_structure: Mapping[str, Any] | None = None,
        official_nse_evidence: Mapping[str, Any] | None = None,
        session_policy: Mapping[str, Any] | None = None,
        policy: SessionStructurePolicy | None = None,
    ) -> Dict[str, Any]:
        policy = policy or SessionStructurePolicy()
        price, atr_value = _f(current_price), _f(atr)
        ema20_value, ema50_value, vwap_value = _f(ema20), _f(ema50), _f(vwap)
        orb = dict(orb or {})
        hist = dict(historical_level_report or {})
        structure = dict(market_structure or {})
        official = dict(official_nse_evidence or {})
        timing = dict(session_policy or {})
        if price is None or price <= 0 or atr_value is None or atr_value <= 0:
            return {
                "ok": False, "authority": cls.AUTHORITY, "authority_version": cls.VERSION,
                "state": "INSUFFICIENT_PRICE_OR_ATR", "promotion_allowed": False,
            }

        completed = [dict(row) for row in closed_candles(list(candles or ()), "5minute")]
        if completed:
            latest_day = candle_datetime(completed[-1])
            latest_day = latest_day.date() if latest_day else None
            day_rows = [row for row in completed if candle_datetime(row) and candle_datetime(row).date() == latest_day and (9, 15) <= (candle_datetime(row).hour, candle_datetime(row).minute) <= (15, 30)]
        else:
            day_rows = []

        cluster_distance = max(atr_value * policy.cluster_atr, price * policy.cluster_pct / 100.0)
        entry_buffer = max(atr_value * policy.entry_buffer_atr, price * policy.entry_buffer_pct / 100.0)
        candidates: List[Dict[str, Any]] = []

        def add(value: Any, role: str, source: str, weight: float, *, validated: bool = True, dynamic: bool = False, role_state: str = "NATIVE", proof: str = "", timeframe: str = "SESSION"):
            level = _f(value)
            if level is None or level <= 0 or role not in {"support", "resistance"}:
                return
            candidates.append({
                "price": level, "kind": role, "side": role, "source": source,
                "source_level": source, "timeframe": timeframe, "timeframe_authority": timeframe,
                "validated": bool(validated), "actionable": bool(validated), "dynamic": bool(dynamic),
                "role_state": role_state, "proof": proof, "base_score": float(weight),
                "freshness": "CURRENT" if timeframe == "SESSION" else "RECENT",
                "breakout_retest_state": "RETEST" if "RETEST" in role_state else "UNTESTED",
            })

        # ORB5 is the first completed 09:15-09:20 bar, not a 15-minute range.
        orb_high, orb_low = _f(orb.get("orb_high")), _f(orb.get("orb_low"))
        rvol = _f(orb.get("session_relative_volume"))
        participation_usable = orb.get("participation_decision_usable") is True
        volume_confirm = bool(participation_usable and rvol is not None and rvol >= policy.rvol_confirm)
        strong_volume = bool(participation_usable and rvol is not None and rvol >= policy.rvol_strong)
        vwap_long = vwap_value is not None and price >= vwap_value
        vwap_short = vwap_value is not None and price <= vwap_value
        ema_long = ema20_value is not None and price >= ema20_value and (ema50_value is None or ema20_value >= ema50_value)
        ema_short = ema20_value is not None and price <= ema20_value and (ema50_value is None or ema20_value <= ema50_value)

        recent = day_rows[-6:]
        retest_up = bool(orb_high is not None and any(
            _f(row.get("low")) is not None and _f(row.get("close")) is not None
            and _f(row.get("low")) <= orb_high + cluster_distance
            and _f(row.get("close")) >= orb_high
            for row in recent
        ))
        retest_down = bool(orb_low is not None and any(
            _f(row.get("high")) is not None and _f(row.get("close")) is not None
            and _f(row.get("high")) >= orb_low - cluster_distance
            and _f(row.get("close")) <= orb_low
            for row in recent
        ))
        orb_long_live = bool(orb_high is not None and price > orb_high + entry_buffer)
        orb_short_live = bool(orb_low is not None and price < orb_low - entry_buffer)
        orb_long_accept = bool(orb_long_live and (retest_up or orb.get("confirmed") is True or (volume_confirm and vwap_long and ema_long)))
        orb_short_accept = bool(orb_short_live and (retest_down or orb.get("confirmed") is True or (volume_confirm and vwap_short and ema_short)))

        if orb_high is not None:
            if orb_long_accept:
                add(orb_high, "support", "orb5_high_role_flip", 44, role_state="RESISTANCE_TO_SUPPORT_RETEST" if retest_up else "RESISTANCE_TO_SUPPORT_ACCEPTED", proof="ORB5 breakout accepted with retest or VWAP/EMA/volume confluence")
            else:
                add(orb_high, "resistance", "orb5_high", 38, proof="first completed 5-minute opening-range high")
        if orb_low is not None:
            if orb_short_accept:
                add(orb_low, "resistance", "orb5_low_role_flip", 44, role_state="SUPPORT_TO_RESISTANCE_RETEST" if retest_down else "SUPPORT_TO_RESISTANCE_ACCEPTED", proof="ORB5 breakdown accepted with retest or VWAP/EMA/volume confluence")
            else:
                add(orb_low, "support", "orb5_low", 38, proof="first completed 5-minute opening-range low")

        # Dynamic session anchors confirm structure; they do not become major historical levels.
        if vwap_value is not None:
            add(vwap_value, "support" if price >= vwap_value else "resistance", "session_vwap", 30, dynamic=True, proof="session volume-weighted mean")
        if ema20_value is not None:
            add(ema20_value, "support" if price >= ema20_value else "resistance", "ema20", 24, dynamic=True, proof="EMA20 trend anchor")
        if ema50_value is not None:
            add(ema50_value, "support" if price >= ema50_value else "resistance", "ema50", 17, dynamic=True, proof="EMA50 secondary trend anchor")

        if day_rows:
            session_high = max((_f(row.get("high")) or -math.inf) for row in day_rows)
            session_low = min((_f(row.get("low")) or math.inf) for row in day_rows)
            if math.isfinite(session_low):
                add(session_low, "support", "session_low", 26, proof="completed-session low")
            if math.isfinite(session_high):
                add(session_high, "resistance", "session_high", 26, proof="completed-session high")
            ph, pl = _pivot_rows(day_rows, 2, 2)
            for _, value in ph[-3:]:
                add(value, "resistance", "session_swing_high", 27, proof="confirmed 5m swing high")
            for _, value in pl[-3:]:
                add(value, "support", "session_swing_low", 27, proof="confirmed 5m swing low")

        for key, role, source in (("previous_day_high", "resistance", "previous_day_high"), ("previous_day_low", "support", "previous_day_low")):
            add(orb.get(key), role, source, 31, proof="previous completed NSE session", timeframe="1D")

        # Reuse validated historical structure, preserving its already-governed role-flip semantics.
        for role_key in ("support", "resistance"):
            for raw in list(hist.get(role_key) or [])[:8]:
                row = dict(raw or {})
                level = _f(row.get("price"))
                if level is None:
                    continue
                add(level, role_key, str(row.get("source_level") or "historical_structure"), min(36.0, max(18.0, _f(row.get("importance_score")) or 18.0)), validated=row.get("validated") is True, role_state=str(row.get("role_state") or "HISTORICAL"), proof="validated selected/higher-timeframe structure", timeframe=str(row.get("timeframe") or "selected"))

        # Cluster by role so nearby ORB/VWAP/EMA/swing levels become one operating zone.
        zones: List[Dict[str, Any]] = []
        for role in ("support", "resistance"):
            role_rows = sorted((row for row in candidates if row["kind"] == role), key=lambda row: row["price"])
            role_zones: List[Dict[str, Any]] = []
            for row in role_rows:
                target = next((zone for zone in role_zones if abs(float(row["price"]) - float(zone["price"])) <= cluster_distance), None)
                if target is None:
                    target = {"kind": role, "price": float(row["price"]), "low": float(row["price"]), "high": float(row["price"]), "members": []}
                    role_zones.append(target)
                target["members"].append(row)
                target["low"] = min(float(target["low"]), float(row["price"]))
                target["high"] = max(float(target["high"]), float(row["price"]))
                weights = [(float(member["price"]), max(1.0, float(member["base_score"]))) for member in target["members"]]
                target["price"] = sum(value * weight for value, weight in weights) / sum(weight for _, weight in weights)
            for zone in role_zones:
                members = zone["members"]
                source_score = sum(sorted((float(member["base_score"]) for member in members), reverse=True)[:3])
                confluence = len({member["source"] for member in members})
                score = min(100.0, source_score * 0.72 + max(0, confluence - 1) * 7.0 + (8.0 if strong_volume else 4.0 if volume_confirm else 0.0))
                zone.update({
                    "price": _r(zone["price"]), "low": _r(zone["low"] - cluster_distance * 0.20), "high": _r(zone["high"] + cluster_distance * 0.20),
                    "validated": any(member.get("validated") for member in members), "actionable": score >= policy.minimum_zone_score,
                    "touches": max(1, confluence), "importance_score": round(score, 2), "confidence_score": round(score, 2),
                    "freshness": "CURRENT", "timeframe": "SESSION" if any(member.get("timeframe") == "SESSION" for member in members) else str(members[0].get("timeframe") or "selected"),
                    "sources": sorted({member["source"] for member in members}),
                    "source_level": "+".join(sorted({member["source"] for member in members}))[:180],
                    "role_states": sorted({member.get("role_state") for member in members if member.get("role_state")}),
                    "proof": [member.get("proof") for member in members if member.get("proof")],
                    "breakout_retest_state": "RETEST" if any("RETEST" in str(member.get("role_state") or "") for member in members) else "UNTESTED",
                    "zone_low": _r(zone["low"] - cluster_distance * 0.20), "zone_high": _r(zone["high"] + cluster_distance * 0.20),
                    "line_style": "solid",
                })
                zones.append(zone)

        supports = sorted((zone for zone in zones if zone["kind"] == "support" and float(zone["price"]) < price + cluster_distance), key=lambda zone: float(zone["price"]), reverse=True)
        resistances = sorted((zone for zone in zones if zone["kind"] == "resistance" and float(zone["price"]) > price - cluster_distance), key=lambda zone: float(zone["price"]))
        operating_support = next((zone for zone in supports if zone.get("actionable")), None)
        operating_resistance = next((zone for zone in resistances if zone.get("actionable")), None)

        # Official NSE information is a confidence/risk layer only.
        official_values = dict(official.get("values") or {})
        official_features = dict(official.get("decision_features") or {})
        delivery_z = _f(official_features.get("delivery_pct_surprise") or official_values.get("delivery_pct_surprise"))
        delivered_z = _f(official_features.get("delivered_quantity_surprise") or official_values.get("delivered_quantity_surprise"))
        turnover_z = _f(official_features.get("nse_turnover_z20") or official_values.get("nse_turnover_z20"))
        trades_z = _f(official_features.get("nse_trades_z20") or official_values.get("nse_trades_z20"))
        signed_deal = _f(official_values.get("nse_signed_deal_qty"))
        nse_score = 0.0
        nse_reasons: List[str] = []
        if delivery_z is not None and delivery_z >= 0.75:
            nse_score += 4; nse_reasons.append(f"delivery% surprise {delivery_z:.2f}σ")
        if delivered_z is not None and delivered_z >= 0.75:
            nse_score += 5; nse_reasons.append(f"delivered quantity surprise {delivered_z:.2f}σ")
        if turnover_z is not None and turnover_z >= 0.75:
            nse_score += 3; nse_reasons.append(f"turnover surprise {turnover_z:.2f}σ")
        if trades_z is not None and trades_z >= 0.75:
            nse_score += 2; nse_reasons.append(f"trade-count surprise {trades_z:.2f}σ")
        if signed_deal is not None and signed_deal != 0:
            nse_reasons.append("positive signed deal flow" if signed_deal > 0 else "negative signed deal flow")
        risk_blocks = [str(item) for item in official.get("risk_blocks") or [] if str(item)]

        structure_bias = str(structure.get("bias") or "neutral").lower()
        long_checks = {
            "orb5": orb_long_accept,
            "vwap": vwap_long,
            "ema": ema_long,
            "volume": volume_confirm,
            "price_structure": structure_bias in {"long", "neutral"},
        }
        short_checks = {
            "orb5": orb_short_accept,
            "vwap": vwap_short,
            "ema": ema_short,
            "volume": volume_confirm,
            "price_structure": structure_bias in {"short", "neutral"},
        }
        long_count = sum(bool(value) for value in long_checks.values())
        short_count = sum(bool(value) for value in short_checks.values())
        long_score = min(100.0, long_count * 15.0 + (10.0 if operating_support else 0.0) + nse_score)
        short_nse = max(0.0, (3.0 if signed_deal is not None and signed_deal < 0 else 0.0) + (2.0 if trades_z is not None and trades_z >= 0.75 else 0.0))
        short_score = min(100.0, short_count * 15.0 + (10.0 if operating_resistance else 0.0) + short_nse)

        phase = str(timing.get("phase") or "ENTRY_ALLOWED")
        observe_only = phase in {"PREOPEN_INTELLIGENCE", "ORB5_OBSERVE_ONLY", "BEFORE_PREOPEN", "MARKET_CLOSED", "CALENDAR_UNVERIFIED"}
        new_entry_allowed = timing.get("new_entry_allowed") is not False and not observe_only
        a_plus_only = timing.get("a_plus_only") is True
        max_chase = max(atr_value * policy.max_chase_atr, price * policy.max_chase_pct / 100.0)
        long_trigger = (orb_high + entry_buffer) if orb_high is not None else ((operating_support or {}).get("high"))
        short_trigger = (orb_low - entry_buffer) if orb_low is not None else ((operating_resistance or {}).get("low"))
        long_chase = (price - float(long_trigger)) if long_trigger is not None and price > float(long_trigger) else 0.0
        short_chase = (float(short_trigger) - price) if short_trigger is not None and price < float(short_trigger) else 0.0
        long_extended = long_chase > max_chase
        short_extended = short_chase > max_chase
        long_ready = bool(new_entry_allowed and not risk_blocks and long_count >= 4 and orb_long_accept and operating_support and not long_extended)
        short_ready = bool(new_entry_allowed and not risk_blocks and short_count >= 4 and orb_short_accept and operating_resistance and not short_extended)
        long_a_plus = bool(long_ready and long_score >= policy.a_plus_score and strong_volume and long_checks["vwap"] and long_checks["ema"])
        short_a_plus = bool(short_ready and short_score >= policy.a_plus_score and strong_volume and short_checks["vwap"] and short_checks["ema"])
        if a_plus_only:
            long_ready = long_ready and long_a_plus
            short_ready = short_ready and short_a_plus

        combined_report = dict(hist)
        combined_report.update({
            "ok": True,
            "version": cls.VERSION,
            "method": "Intraday operating structure = ORB5 + session H/L/swings + VWAP + EMA20/50 + same-clock RVOL, reconciled with validated historical S/R; NSE official evidence is confirmation/risk only.",
            "last_close": _r(price), "atr14": _r(atr_value),
            "support": supports[:12], "resistance": resistances[:12],
            "nearest_support": operating_support, "nearest_resistance": operating_resistance,
            "major_support": hist.get("major_support"), "major_resistance": hist.get("major_resistance"),
            "ranked_levels": sorted(zones, key=lambda zone: (-float(zone.get("importance_score") or 0), abs(float(zone["price"]) - price)))[:24],
            "session_authority": cls.AUTHORITY,
        })
        blockers = list(risk_blocks)
        if observe_only:
            blockers.append("ORB5_OBSERVE_ONLY_UNTIL_09_20")
        if not new_entry_allowed and not observe_only:
            blockers.append("NO_NEW_INTRADAY_ENTRY_AFTER_14_30")
        if long_extended or short_extended:
            blockers.append("LATE_CHASE_OR_EXTENSION_REJECTED")

        return {
            "ok": True,
            "authority": cls.AUTHORITY,
            "authority_version": cls.VERSION,
            "state": "ACTIONABLE" if long_ready or short_ready else "OBSERVE" if observe_only else "FORMING",
            "phase": phase,
            "orb5_ready": orb_high is not None and orb_low is not None,
            "orb5_high": _r(orb_high), "orb5_low": _r(orb_low),
            "session_vwap": _r(vwap_value), "ema20": _r(ema20_value), "ema50": _r(ema50_value),
            "session_relative_volume": _r(rvol, 2), "volume_confirmation": volume_confirm,
            "operating_support": operating_support, "operating_resistance": operating_resistance,
            "support": _r((operating_support or {}).get("price")), "resistance": _r((operating_resistance or {}).get("price")),
            "major_support": hist.get("major_support"), "major_resistance": hist.get("major_resistance"),
            "canonical_level_report": combined_report,
            "long": {
                "checks": long_checks, "confluence_count": long_count, "score": round(long_score, 2),
                "entry_trigger": _r(long_trigger), "chase_distance": _r(long_chase), "max_chase": _r(max_chase),
                "extended": long_extended, "promotion_ready": long_ready, "a_plus": long_a_plus,
            },
            "short": {
                "checks": short_checks, "confluence_count": short_count, "score": round(short_score, 2),
                "entry_trigger": _r(short_trigger), "chase_distance": _r(short_chase), "max_chase": _r(max_chase),
                "extended": short_extended, "promotion_ready": short_ready, "a_plus": short_a_plus,
            },
            "official_nse": {
                "state": official.get("state"), "confirmation_score": round(nse_score, 2),
                "reasons": nse_reasons, "risk_blocks": risk_blocks,
                "policy": "official NSE data confirms/penalises a price-action level but never manufactures the level price",
            },
            "blockers": blockers,
            "policy": "Session operating S/R outranks stale statistical pivots for Intraday; major higher-timeframe levels remain obstacles. Role flips require acceptance/retest evidence. Structural invalidation and target feasibility still gate Final promotion.",
        }


DEFAULT_INTRADAY_SESSION_STRUCTURE_AUTHORITY = IntradaySessionStructureAuthority()
