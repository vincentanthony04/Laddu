"""
Opportunity priority scoring.

Extracted from the LadduRuntime god-class in main.py (v39.1.2): these five
methods (_is_trigger_near, _discovery_waiting_for, _opportunity_priority_score,
_priority_reason, _opportunity_summary_from_rows) never touched `self` --
they're pure functions of a candidate dict. That made them impossible to
unit-test in isolation and easy to silently break while editing an
unrelated part of the 3,300-line class. No scoring logic changed here.

This is arguably the single highest-value extraction in the codebase:
_opportunity_priority_score decides what you actually see ranked first,
so it deserves dedicated tests more than almost anything else in the app.
"""
from __future__ import annotations
from typing import Any, Dict, List

from core.numeric_semantics import finite_number, positive_number
from core.production_mode_policy import require_production_mode


def is_trigger_near(d: Dict[str, Any], last_price: float | None = None) -> bool:
    raw_ltp = last_price if last_price is not None else d.get("ltp")
    ltp = positive_number(raw_ltp)
    if ltp is None:
        return False
    levels = []
    for k in ("trigger", "resistance", "support"):
        v = positive_number(d.get(k))
        if v is not None:
            levels.append(v)
    if not levels:
        return False
    try:
        mode = require_production_mode(d.get("mode") or "delivery")
    except ValueError:
        return False
    near_limit = 5.0 if mode == "intraday" else 8.0
    return min(abs(v - ltp) / ltp * 100 for v in levels if v > 0) <= near_limit


def discovery_waiting_for(d: Dict[str, Any]) -> str:
    try:
        mode = require_production_mode(d.get("mode") or "delivery")
    except ValueError:
        mode = "delivery"
    buckets = set(d.get("discovery_buckets") or [])
    if mode == "intraday":
        return "live ORB/VWAP/volume confirmation"
    if "near breakout / resistance test" in buckets or "VCP / volatility contraction" in buckets:
        return "breakout close or retest hold with volume"
    if "near important support" in buckets:
        return "support reclaim / higher-low confirmation"
    if "institutional accumulation" in buckets or "stake increase" in buckets:
        return "price structure confirmation near value zone"
    return "clear price trigger and risk/reward confirmation"


# v26.2 weighting table: distributed priority score; 100 must be rare.
# Priority is not conviction. It answers: how urgently should this case be
# rescanned? Scores are intentionally spread across 35-94 so the UI can
# sort meaningfully.
_BUCKET_WEIGHTS = (
    ("institutional accumulation", 14), ("stake increase", 6), ("near important support", 9),
    ("volume climax", 12), ("delivery atr absorption", 10),
    ("near breakout", 10), ("ema compression", 7), ("vcp", 10), ("candle range contraction", 5),
    ("volume expansion", 7), ("structure improving", 8), ("fundamental quality", 6),
    ("volume dry-up", 4), ("range expansion", 6),
)


def opportunity_priority_score(d: Dict[str, Any]) -> int:
    base_value = finite_number(d.get("score"))
    base = int(max(0.0, min(100.0, base_value))) if base_value is not None else 0
    buckets = [str(x).lower() for x in (d.get("discovery_buckets") or [])]
    themes = [str(t).lower() for t in (d.get("themes") or [])]
    points = 0
    for key, pts in _BUCKET_WEIGHTS:
        if any(key in b for b in buckets):
            points += pts
    if any(t and t != "sector leadership watch" for t in themes):
        points += 4
    if any("weak structure" in b or "distribution risk" in b for b in buckets):
        points -= 16
    # Map a valid 0-100 source score into the visible 35-77 base band, then
    # add independently versioned discovery evidence.  The previous formula
    # omitted the 35-point baseline and therefore collapsed ordinary cases at
    # the hard floor, destroying rank ordering (e.g. a 3% trigger and a 30%
    # trigger could both become 35).
    priority = 35 + int(round(base * 0.42)) + points

    try:
        mode = require_production_mode(d.get("mode") or "delivery")
    except ValueError:
        return 35
    bearish_long_only = mode == "delivery" and (
        str(d.get("side") or "").upper() in ("BEARISH", "SHORT", "AVOID_LONG")
        or str(d.get("decision") or "").upper() == "AVOID_LONG"
    )
    if bearish_long_only:
        priority = min(priority, 72)

    # Far triggers should stay lower priority; they are cases, not near-term armed setups.
    ltp = positive_number(d.get("ltp"))
    trigger_value = positive_number(d.get("trigger"))
    resistance_value = positive_number(d.get("resistance"))
    trg = trigger_value if trigger_value is not None else resistance_value
    if ltp is not None and trg is not None:
        dist = abs(trg - ltp) / ltp * 100
        if dist > 18:
            priority -= 18
        elif dist > 10:
            priority -= 9
        elif dist <= 5:
            priority += 5

    stage = str(d.get("candidate_stage") or d.get("opportunity_stage") or "").upper()
    if stage == "ARMED":
        priority += 5
    elif stage == "QUALIFIED":
        priority += 1

    return max(35, min(94, priority))


def priority_reason(d: Dict[str, Any]) -> str:
    buckets = d.get("discovery_buckets") or []
    ev = d.get("discovery_evidence") or []
    themes = d.get("themes") or []
    parts = []
    if buckets:
        parts.append(" + ".join([str(x) for x in buckets[:3]]))
    if themes:
        parts.append("theme: " + ", ".join([str(x) for x in themes[:2]]))
    if ev:
        parts.append(str(ev[0]))
    return "; ".join(parts) or "Potential candidate: keep under priority watch until trigger/invalidation is clear"


def opportunity_summary_from_rows(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    by_stage: Dict[str, int] = {}
    by_sector: Dict[str, int] = {}
    for d in rows:
        st = str(d.get("opportunity_stage") or d.get("candidate_stage") or "Potential")
        by_stage[st] = by_stage.get(st, 0) + 1
        sec = str(d.get("sector") or "broad")
        by_sector[sec] = by_sector.get(sec, 0) + 1
    return {
        "count": len(rows),
        "by_stage": by_stage,
        "by_sector": dict(sorted(by_sector.items(), key=lambda kv: (-kv[1], kv[0]))[:10]),
    }
