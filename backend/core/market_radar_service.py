"""Verified market-radar projection for Trending Stocks & Market Heat.

The service is intentionally pure: it ranks already acquired observations and
never performs network I/O.  Current verified coverage is preferred.  A
persisted snapshot may be used only as explicit last-known evidence and is
never relabelled live.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Sequence

from core.decision_row_projection_service import DecisionRowProjectionService
from core.production_mode_policy import require_production_mode


_SUPPORTED_FRESHNESS = {"live", "closed_market", "stale"}


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _symbol(row: Mapping[str, Any]) -> str:
    return str(row.get("symbol") or row.get("trading_symbol") or row.get("tradingsymbol") or "").upper().strip()


def _canonical_modes(value: Any) -> List[str]:
    if value not in (None, ""):
        try:
            return [require_production_mode(value)]
        except ValueError:
            return []
    # Broad verified quote coverage is useful to both observation horizons.
    # This does not promote a trade; each desk applies its own evidence gates.
    return ["intraday", "delivery"]


def _freshness_rank(row: Mapping[str, Any]) -> int:
    state = str(row.get("freshness_state") or "").lower()
    if row.get("identity_verified") is True and not row.get("stale") and state == "live":
        return 4
    if row.get("identity_verified") is True and not row.get("stale") and state == "closed_market":
        return 3
    if state == "stale" or row.get("stale"):
        return 1
    return 0


def _score(row: Mapping[str, Any]) -> float:
    explicit = _number(row.get("activity_score") or row.get("analysis_priority_score") or row.get("rank_score") or row.get("score") or row.get("priority"))
    movement = abs(_number(row.get("change_pct")))
    rvol = max(0.0, _number(row.get("relative_volume") or row.get("recent_volume_vs_base") or row.get("rvol")))
    return explicit if explicit > 0 else min(100.0, movement * 12.0 + min(35.0, rvol * 8.0))


def _delivery_setup(candidate: Mapping[str, Any], quote: Mapping[str, Any] | None = None) -> Dict[str, Any] | None:
    """Project a next-session Delivery research setup without calling it an entry.

    Candidate evidence is merged with a verified/LKG quote observation.  This
    keeps after-market research visible while preserving the strict rule that
    only a fresh READY decision can enter Today's Entries.
    """
    symbol = _symbol(candidate)
    try:
        mode = require_production_mode(candidate.get("mode") or "delivery")
    except ValueError:
        return None
    if not symbol or mode != "delivery":
        return None
    quote = dict(quote or {})
    ltp = candidate.get("ltp") if candidate.get("ltp") is not None else quote.get("ltp")
    if ltp is None:
        return None
    buckets = " ".join(str(x) for x in (candidate.get("discovery_buckets") or candidate.get("themes") or []))
    setup_text = " ".join(str(candidate.get(k) or "") for k in ("setup", "priority_reason", "reason", "waiting_for"))
    combined = f"{buckets} {setup_text}".lower()
    setup_tags: List[str] = []
    if "vcp" in combined or "contraction" in combined or _number(candidate.get("range_contraction_pct")) > 0:
        setup_tags.append("VCP / contraction")
    if "breakout" in combined or "resistance" in combined or "retest" in combined:
        setup_tags.append("breakout / retest")
    if "accumulation" in combined or "institutional" in combined or str(candidate.get("institutional_stage") or "").lower() not in {"", "dormant", "unclassified"}:
        setup_tags.append("institutional participation")
    if _number(candidate.get("recent_volume_vs_base") or candidate.get("relative_volume")) >= 1.2:
        setup_tags.append("volume expansion")
    if candidate.get("news_catalyst_verified") is True:
        setup_tags.append("verified catalyst")

    rank = _number(candidate.get("rank_score") or candidate.get("score") or candidate.get("priority_score"))
    fundamental = _number(candidate.get("fundamental_score"))
    technical = _number(candidate.get("technical_score"))
    participation = _number(candidate.get("institutional_score") or candidate.get("delivery_score"))
    setup_score = max(rank, min(100.0, fundamental * .30 + technical * .35 + participation * .20 + min(15.0, len(setup_tags) * 4.0)))
    freshness_state = str(quote.get("freshness_state") or candidate.get("freshness_state") or candidate.get("price_freshness") or "stale").lower()
    if freshness_state not in _SUPPORTED_FRESHNESS:
        freshness_state = "stale"
    stale = bool(quote.get("stale")) or freshness_state == "stale"
    item = {
        "symbol": symbol,
        "mode": "delivery",
        "modes": ["delivery"],
        "ltp": _number(ltp),
        "change_pct": candidate.get("change_pct") if candidate.get("change_pct") is not None else quote.get("change_pct"),
        "relative_volume": candidate.get("recent_volume_vs_base") or candidate.get("relative_volume") or quote.get("relative_volume"),
        "volume": candidate.get("volume") if candidate.get("volume") is not None else quote.get("volume"),
        "score": round(setup_score, 2),
        "delivery_setup_score": round(setup_score, 2),
        "sector": candidate.get("sector") or candidate.get("sector_label") or quote.get("sector") or "Sector pending",
        "freshness": quote.get("freshness") or candidate.get("price_freshness"),
        "freshness_state": freshness_state,
        "identity_verified": bool(quote.get("identity_verified") or candidate.get("identity_verified")),
        "stale": stale,
        "source_time": quote.get("source_time") or candidate.get("observed_at") or candidate.get("last_refresh") or candidate.get("last_update"),
        "radar_source": "delivery_research_projection",
        "observation_only": True,
        "next_session_only": True,
        "candidate_stage": candidate.get("prepared_state") or candidate.get("candidate_stage") or candidate.get("opportunity_stage") or "UNDER_REVIEW",
        "setup_type": " · ".join(dict.fromkeys(setup_tags)) or "quant / structure review",
        "setup_basis": "point-in-time fundamentals + completed daily structure; not an executed entry",
        "waiting_for": candidate.get("qualification_blocker") or candidate.get("promotion_blocked_by") or candidate.get("waiting_for") or "confirmed trigger and final risk admission",
        "fundamental_score": candidate.get("fundamental_score"),
        "technical_score": candidate.get("technical_score"),
        "planned_entry": candidate.get("planned_entry") or candidate.get("entry") or candidate.get("trigger"),
        "planned_stop": candidate.get("planned_sl") or candidate.get("sl") or candidate.get("invalidation"),
        "planned_target": candidate.get("planned_t1") or candidate.get("t1") or candidate.get("target"),
        "planned_rr": candidate.get("planned_rr") or candidate.get("rr"),
        "news_state": "verified catalyst" if candidate.get("news_catalyst_verified") is True else "no verified news feed",
    }
    projection_input = dict(candidate)
    projection_input.update({
        "symbol": symbol, "mode": "delivery",
        "ltp": item["ltp"], "change_pct": item["change_pct"],
        "entry": item["planned_entry"], "target": item["planned_target"], "stop": item["planned_stop"],
        "final_confidence": item["score"],
        "identity_verified": item["identity_verified"],
        "instrument_key": candidate.get("instrument_key") or quote.get("instrument_key"),
        "status": candidate.get("status") or "WATCH",
        "decision": candidate.get("decision") or "WATCH",
    })
    item["decision_row"] = DecisionRowProjectionService.project(projection_input)
    return item


def _intraday_setup(candidate: Mapping[str, Any], quote: Mapping[str, Any] | None = None) -> Dict[str, Any] | None:
    """Project a next-session Intraday watch candidate.

    Entry, target and stop deliberately remain blank because ORB/VWAP and live
    spread confirmation do not exist after the session has closed.
    """
    symbol = _symbol(candidate)
    try:
        mode = require_production_mode(candidate.get("mode") or "intraday")
    except ValueError:
        return None
    if not symbol or mode != "intraday":
        return None
    quote = dict(quote or {})
    ltp = candidate.get("ltp") if candidate.get("ltp") is not None else quote.get("ltp")
    if ltp is None:
        return None
    text = " ".join(str(candidate.get(k) or "") for k in (
        "setup", "setup_type", "priority_reason", "reason", "waiting_for",
        "orb_state", "market_structure", "volume_profile", "discovery_buckets",
    )).lower()
    change = _number(candidate.get("change_pct") if candidate.get("change_pct") is not None else quote.get("change_pct"))
    rvol = max(0.0, _number(candidate.get("relative_volume") or candidate.get("recent_volume_vs_base") or quote.get("relative_volume")))
    families: List[str] = []
    if "orb" in text or "opening range" in text:
        families.append("ORB + VWAP confirmation")
    if any(token in text for token in ("breakout", "retest", "resistance")):
        families.append("breakout / retest")
    if "support" in text and any(token in text for token in ("reclaim", "bounce", "hold", "major")):
        families.append("support reclaim")
    if "gap" in text:
        families.append("gap failure watch" if any(token in text for token in ("fail", "fade", "reversal")) else "gap continuation")
    if "climax" in text or "exhaust" in text:
        families.append("climax reversal watch")
    if not families and (abs(change) >= 1.0 or rvol >= 1.5):
        families.append("momentum / activity watch")
    if not families:
        return None
    score = _score({**dict(quote), **dict(candidate)})
    freshness_state = str(quote.get("freshness_state") or candidate.get("freshness_state") or "stale").lower()
    if freshness_state not in _SUPPORTED_FRESHNESS:
        freshness_state = "stale"
    bias = "SHORT" if change < 0 or any(token in text for token in ("bear", "breakdown", "distribution")) else "LONG"
    item = {
        "symbol": symbol, "mode": "intraday", "modes": ["intraday"], "side": bias,
        "ltp": _number(ltp), "change_pct": change, "relative_volume": rvol or None,
        "volume": candidate.get("volume") if candidate.get("volume") is not None else quote.get("volume"),
        "score": round(score, 2), "intraday_setup_score": round(score, 2),
        "sector": candidate.get("sector") or quote.get("sector") or "Sector pending",
        "freshness": quote.get("freshness") or candidate.get("price_freshness"),
        "freshness_state": freshness_state,
        "identity_verified": bool(quote.get("identity_verified") or candidate.get("identity_verified")),
        "stale": bool(quote.get("stale")) or freshness_state == "stale",
        "source_time": quote.get("source_time") or candidate.get("observed_at") or candidate.get("last_refresh"),
        "radar_source": "intraday_next_session_projection",
        "observation_only": True, "next_session_only": True,
        "candidate_stage": candidate.get("candidate_stage") or candidate.get("opportunity_stage") or "UNDER_REVIEW",
        "setup_type": " · ".join(dict.fromkeys(families)),
        "setup_basis": "completed-session structure and activity; requires live ORB/VWAP/spread confirmation",
        "waiting_for": "next-session live trigger, VWAP/ORB confirmation and final risk admission",
        "planned_entry": None, "planned_stop": None, "planned_target": None, "planned_rr": None,
        "status": "WATCH", "decision": "WATCH", "actionability_verified": False,
    }
    item["decision_row"] = DecisionRowProjectionService.project({
        "symbol": symbol, "mode": "intraday", "side": bias, "ltp": item["ltp"],
        "change_pct": item["change_pct"], "entry": None, "target": None, "stop": None,
        "final_confidence": item["score"], "identity_verified": item["identity_verified"],
        "instrument_key": candidate.get("instrument_key") or quote.get("instrument_key"),
        "status": "WATCH", "decision": "WATCH",
    })
    return item


@dataclass(frozen=True)
class MarketRadarService:
    max_rows: int = 5

    def _normalize(self, row: Mapping[str, Any], *, fallback: bool = False) -> Dict[str, Any] | None:
        symbol = _symbol(row)
        ltp = row.get("ltp")
        if not symbol or ltp is None:
            return None
        freshness_state = str(row.get("freshness_state") or ("stale" if fallback else "unverified")).lower()
        stale = bool(row.get("stale")) or fallback or freshness_state == "stale"
        identity_verified = bool(row.get("identity_verified")) and not fallback
        if freshness_state not in _SUPPORTED_FRESHNESS:
            freshness_state = "stale" if fallback else "unverified"
        if fallback:
            freshness_state = "stale"
        change = row.get("change_pct")
        relative_volume = row.get("relative_volume") or row.get("recent_volume_vs_base") or row.get("rvol")
        source_text = str(row.get("radar_source") or row.get("source") or "").lower()
        # Quote-only universe coverage is an observation pool for both desks.
        # Its historical `mode=intraday` label described the acquisition lane,
        # not the only research horizon allowed to view the quote.
        modes = ["intraday", "delivery"] if "coverage" in source_text else _canonical_modes(row.get("mode"))
        return {
            "symbol": symbol,
            "mode": modes[0] if len(modes) == 1 else "both",
            "modes": modes,
            "change_pct": None if change is None else _number(change),
            "relative_volume": None if relative_volume is None else _number(relative_volume),
            "score": round(_score(row), 2),
            "sector": row.get("sector") or row.get("sector_label") or row.get("industry") or "Sector pending",
            "ltp": _number(ltp),
            "volume": None if row.get("volume") is None else _number(row.get("volume")),
            "freshness": row.get("freshness") or row.get("price_freshness"),
            "freshness_state": freshness_state,
            "identity_verified": identity_verified,
            "stale": stale,
            "source_time": row.get("source_time") or row.get("provider_timestamp") or row.get("timestamp"),
            "change_source": row.get("change_source"),
            "radar_source": row.get("radar_source") or row.get("source") or ("persisted_lkg" if fallback else "verified_observation"),
            "observation_only": True,
        }

    def build(
        self,
        current_rows: Iterable[Mapping[str, Any]],
        *,
        persisted_rows: Iterable[Mapping[str, Any]] = (),
        heatmap: Sequence[Mapping[str, Any]] = (),
        delivery_candidates: Iterable[Mapping[str, Any]] = (),
        intraday_candidates: Iterable[Mapping[str, Any]] = (),
    ) -> Dict[str, Any]:
        best: Dict[str, Dict[str, Any]] = {}
        for raw in current_rows or ():
            item = self._normalize(raw)
            if not item:
                continue
            # Current radar accepts only exact-identity live/closed-market rows.
            if _freshness_rank(raw) < 3:
                continue
            old = best.get(item["symbol"])
            if old is None or (_freshness_rank(raw), item["score"]) > (_freshness_rank(old), old["score"]):
                best[item["symbol"]] = item

        current_count = len(best)
        if current_count < self.max_rows:
            for raw in persisted_rows or ():
                item = self._normalize(raw, fallback=True)
                if item and item["symbol"] not in best:
                    best[item["symbol"]] = item

        rows = list(best.values())
        gainers = [r for r in rows if r.get("change_pct") is not None and _number(r.get("change_pct")) > 0]
        losers = [r for r in rows if r.get("change_pct") is not None and _number(r.get("change_pct")) < 0]
        unchanged_rows = [r for r in rows if r.get("change_pct") is not None and abs(_number(r.get("change_pct"))) <= 1e-12]
        unknown_change_rows = [r for r in rows if r.get("change_pct") is None]
        volume = [r for r in rows if _number(r.get("relative_volume") or r.get("volume")) > 0]
        intraday = [dict(r, mode="intraday") for r in rows if "intraday" in (r.get("modes") or [r.get("mode")])]
        generic_delivery = []
        for r in rows:
            if "delivery" not in (r.get("modes") or [r.get("mode")]):
                continue
            item = dict(r, mode="delivery")
            movement = abs(_number(item.get("change_pct")))
            rvol = max(0.0, _number(item.get("relative_volume")))
            explicit = _number(item.get("delivery_score") or item.get("delivery_activity_score"))
            item["score"] = round(explicit if explicit > 0 else min(100.0, movement * 10.0 + min(30.0, rvol * 6.0)), 2)
            item["trend_basis"] = "completed daily change + relative volume; observation only"
            generic_delivery.append(item)

        quote_by_symbol = {r["symbol"]: r for r in rows}
        intraday_setup_best: Dict[str, Dict[str, Any]] = {}
        intraday_source = list(intraday_candidates or ()) or intraday
        for candidate in intraday_source:
            item = _intraday_setup(candidate, quote_by_symbol.get(_symbol(candidate)))
            if not item:
                continue
            old = intraday_setup_best.get(item["symbol"])
            if old is None or item["score"] > old["score"]:
                intraday_setup_best[item["symbol"]] = item
        intraday_setups = list(intraday_setup_best.values())

        setup_best: Dict[str, Dict[str, Any]] = {}
        for candidate in delivery_candidates or ():
            item = _delivery_setup(candidate, quote_by_symbol.get(_symbol(candidate)))
            if not item:
                continue
            old = setup_best.get(item["symbol"])
            if old is None or item["score"] > old["score"]:
                setup_best[item["symbol"]] = item
        delivery_setups = list(setup_best.values())

        gainers.sort(key=lambda r: (_number(r.get("change_pct")), r["score"]), reverse=True)
        losers.sort(key=lambda r: (_number(r.get("change_pct")), -r["score"]))
        volume.sort(key=lambda r: (_number(r.get("relative_volume") or r.get("volume")), r["score"]), reverse=True)
        intraday.sort(key=lambda r: (r["score"], abs(_number(r.get("change_pct")))), reverse=True)
        intraday_setups.sort(key=lambda r: (r["score"], abs(_number(r.get("change_pct"))), _number(r.get("relative_volume"))), reverse=True)
        generic_delivery.sort(key=lambda r: (r["score"], abs(_number(r.get("change_pct")))), reverse=True)
        delivery_setups.sort(key=lambda r: (r["score"], _number(r.get("planned_rr")), abs(_number(r.get("change_pct")))), reverse=True)
        delivery = delivery_setups or generic_delivery

        has_current = current_count > 0
        has_any = bool(rows)
        data_state = "ready" if current_count >= self.max_rows and (gainers or losers) else "partial" if has_any else "warming"
        reason = (
            "Verified live/closed-market universe observations"
            if data_state == "ready"
            else "Coverage is building; verified observations are shown as they arrive"
            if data_state == "partial" and has_current
            else "Showing last-known observations while verified universe coverage rebuilds"
            if data_state == "partial"
            else "Verified universe coverage has not produced any radar observations yet"
        )

        empty_reasons = {
            "top_gainers": "No verified positive day-change observations yet",
            "top_losers": "No verified negative day-change observations yet",
            "volume_shockers": "Relative/current volume is not yet available for measured rows",
            "intraday_trending": "Intraday activity ranking is warming up",
            "intraday_setups": "No completed-session Intraday watch candidate has cleared the activity/structure screen",
            "delivery_trending": "No current Delivery-ranked observations yet",
            "fo_positioning": "F&O positioning requires verified open-interest change",
        }
        safe_heat = [dict(x) for x in (heatmap or ()) if isinstance(x, Mapping) and x.get("name")]
        coverage_count = len(rows)
        verified_coverage_pct = round((current_count * 100.0 / coverage_count), 1) if coverage_count else 0.0
        return {
            "coverage": coverage_count,
            "verified_coverage": current_count,
            "verified_coverage_pct": verified_coverage_pct,
            "data_state": data_state,
            "reason": reason,
            "advances": len(gainers),
            "declines": len(losers),
            "unchanged": len(unchanged_rows),
            "change_unknown": len(unknown_change_rows),
            "breadth_measured": len(gainers) + len(losers) + len(unchanged_rows),
            "breadth_policy": "counts are across the current verified/retained Market Radar observation population; unknown change is never counted as unchanged",
            "top_gainers": gainers[: self.max_rows],
            "top_losers": losers[: self.max_rows],
            "volume_shockers": volume[: self.max_rows],
            "intraday_trending": intraday[: self.max_rows],
            "intraday_setups": intraday_setups[: self.max_rows],
            "delivery_trending": delivery[: self.max_rows],
            "delivery_setups": delivery_setups[: self.max_rows],
            "fo_positioning": [],
            "empty_reasons": empty_reasons,
            "heatmap": safe_heat,
        }
