"""Pure row builders for scanner operator/research projections."""
from __future__ import annotations

from typing import Any, Dict

from core.production_mode_policy import require_production_mode
from core.quote_integrity_service import classify_quote
from core.india_time import india_now
from core.runtime_primitives import is_india_market_open
from models import now_iso


def scanner_stage_member(item: Dict[str, Any], state: str, reason: str | None = None) -> Dict[str, Any]:
    source = dict(item.get("inst") or item.get("priority") or item) if isinstance(item, dict) else {}
    symbol = str(source.get("trading_symbol") or source.get("symbol") or item.get("symbol") if isinstance(item, dict) else "").upper().strip()
    trade_map = dict(item.get("trade_map") or {}) if isinstance(item, dict) else {}
    raw_side = str((item.get("side") or item.get("position") or item.get("direction") or trade_map.get("side")) if isinstance(item, dict) else "").upper().strip()
    side = "LONG" if raw_side in {"BUY", "BULLISH", "LONG"} else "SHORT" if raw_side in {"SELL", "BEARISH", "SHORT"} else "DIRECTION_PENDING"
    setup = str((item.get("setup_family") or item.get("setup") or item.get("pattern") or trade_map.get("setup_family") or trade_map.get("state")) if isinstance(item, dict) else "").strip() or "Awaiting deep analysis"
    score = None
    if isinstance(item, dict):
        for key in ("final_confidence", "confidence", "evidence_score", "priority_score", "score"):
            try:
                if item.get(key) is not None:
                    score = round(float(item.get(key)), 1)
                    break
            except (TypeError, ValueError):
                continue
    row = {
        "symbol": symbol,
        "instrument_key": source.get("instrument_key") or (item.get("instrument_key") if isinstance(item, dict) else None),
        "exchange": source.get("exchange") or (item.get("exchange") if isinstance(item, dict) else None) or "NSE",
        "side": side,
        "setup": setup,
        "preliminary_score": score,
        "state": state,
        "reason": reason,
        "ltp": (item.get("ltp") or item.get("last_price") or item.get("price")) if isinstance(item, dict) else None,
        "change_pct": (item.get("change_pct") or item.get("percent_change")) if isinstance(item, dict) else None,
        "updated_at": now_iso(),
    }
    return {key: value for key, value in row.items() if value is not None}


def research_capture_row(decision: Dict[str, Any], instrument: Dict[str, Any], mode: str, quote: Dict[str, Any] | None = None) -> Dict[str, Any]:
    row = dict(decision or {})
    exact = dict(instrument or {})
    symbol = str(exact.get("trading_symbol") or exact.get("symbol") or row.get("symbol") or "").upper().strip()
    instrument_key = str(exact.get("instrument_key") or row.get("instrument_key") or "").strip()
    row.update({
        "symbol": symbol,
        "trading_symbol": symbol,
        "instrument_key": instrument_key,
        "exchange": str(exact.get("exchange") or row.get("exchange") or "NSE").upper(),
        "mode": require_production_mode(mode),
        "identity_verified": bool(symbol and instrument_key),
    })
    raw_side = str(row.get("side") or row.get("position") or row.get("direction") or (row.get("trade_map") or {}).get("side") or "").upper().strip()
    row["side"] = "LONG" if raw_side in {"BUY", "BULLISH", "LONG"} else "SHORT" if raw_side in {"SELL", "BEARISH", "SHORT"} else ""
    trade_map = dict(row.get("trade_map") or {})

    def first_number(*values):
        for value in values:
            try:
                parsed = float(value)
                if parsed > 0:
                    return parsed
            except (TypeError, ValueError):
                continue
        return None

    entry = first_number(row.get("planned_entry"), row.get("entry"), trade_map.get("entry"))
    target = first_number(row.get("planned_t1"), row.get("target"), row.get("t1"), trade_map.get("target"), trade_map.get("t1"))
    stop = first_number(row.get("planned_sl"), row.get("sl"), row.get("stop"), trade_map.get("stop"), trade_map.get("sl"))
    if entry is not None:
        row["planned_entry"] = entry
    if target is not None:
        row["planned_t1"] = target
    if stop is not None:
        row["planned_sl"] = stop
    geometry_ok = bool(
        row.get("side") == "LONG" and all(value is not None for value in (stop, entry, target)) and stop < entry < target
        or row.get("side") == "SHORT" and all(value is not None for value in (target, entry, stop)) and target < entry < stop
    )
    row["trade_map_valid"] = bool(row.get("trade_map_valid") is True or trade_map.get("ok") is True or geometry_ok)
    if row["trade_map_valid"]:
        row["_evaluation_objective"] = "TARGET_STOP_TRADE_MAP_OVERLAY"

    # R6: preserve the exact verified quote used by the scanner at the immutable
    # Research-capture boundary.  Previously the scanner passed decision +
    # instrument only, so provider timestamp/freshness was lost and otherwise
    # sufficiently-covered snapshots became PARTIAL with freshness UNKNOWN.
    # Reclassification is pure and fail-closed: receipt time is never promoted
    # to provider time and stale/unverified quotes remain ineligible.
    q = dict(quote or {})
    if q:
        integrity = classify_quote(
            q, now=india_now(), market_open=is_india_market_open(), max_live_age_sec=45.0
        )
        source_time = integrity.get("source_time")
        freshness = str(integrity.get("state") or "unverified").lower()
        row.update({
            "quote_as_of": source_time,
            "source_as_of": source_time,
            "provider_timestamp_verified": bool(integrity.get("provider_timestamp_verified")),
            "freshness_state": freshness,
            "quote_freshness_state": freshness,
            "price_freshness_state": freshness,
            "quote_age_seconds": integrity.get("age_seconds"),
            "quote_freshness_reason": integrity.get("reason"),
            "stale": freshness not in {"live", "closed_market"},
        })
        for src, dst in (("bid_price", "bid_price"), ("best_bid", "best_bid"),
                         ("ask_price", "ask_price"), ("best_ask", "best_ask")):
            if q.get(src) is not None and row.get(dst) is None:
                row[dst] = q.get(src)
        # P0-01: the snapshot lineage must bind received_at to the receipt of
        # THIS exact quote, never to whatever a prior pipeline hop happened to
        # leave on the decision row. classify_quote() deliberately never
        # emits a receipt time (a local HTTP clock is not a freshness signal),
        # so the previous logic left a stale inherited ``received_at`` in
        # place whenever the quote transport did not supply one -- and a
        # freshly re-classified ``source_as_of`` could then land after that
        # stale value, manufacturing INVALID_TIMESTAMP_ORDER on a valid quote
        # (observed on MAHABANK/MCX/PAYTM/SYNGENE). Always stamp received_at
        # to the actual local receipt of this quote (india_now(), already
        # computed above as ``now``... call site) unless the transport itself
        # supplied a more authoritative receipt clock.
        quote_received_at = q.get("received_at") or q.get("received_time")
        row["received_at"] = quote_received_at if quote_received_at not in (None, "") else india_now().isoformat()
        if quote_received_at not in (None, ""):
            row["quote_received_at"] = quote_received_at
            if q.get("received_time") not in (None, ""):
                row["received_time"] = q.get("received_time")
        else:
            row["quote_received_at"] = row["received_at"]
    return row


def apply_research_only_price_boundary(decision: Dict[str, Any], quote: Dict[str, Any] | None) -> Dict[str, Any]:
    """Demote non-executable completed-session analysis to Research truth.

    The scanner may use a verified completed-session close to perform Delivery
    analysis while the market is closed, but that observation is never a live
    execution authority.  Preserve the candidate and trade map for Research /
    next-session Model Paper admission without publishing a false FINAL.
    """
    out = dict(decision or {})
    q = dict(quote or {})
    if q.get("execution_price_authority") is not False:
        return out
    out.update({
        "status": "RESEARCH",
        "decision": "RESEARCH",
        "production_status": "RESEARCH",
        "production_decision": "RESEARCH",
        "research_only": True,
        "execution_quote_required": True,
        "ltp": q.get("ltp") if q.get("ltp") is not None else out.get("ltp"),
        "current_price": q.get("ltp") if q.get("ltp") is not None else out.get("current_price"),
        "quote_freshness_state": q.get("freshness_state") or "closed_market",
        "price_freshness_state": q.get("freshness_state") or "closed_market",
        "freshness_state": q.get("freshness_state") or "closed_market",
        "stale": bool(q.get("stale")),
        "usable_for_promotion": False,
        "analysis_price_authority": True,
        "execution_price_authority": False,
        "price_source": q.get("source"),
        "quote_as_of": q.get("provider_timestamp") or q.get("source_time") or q.get("timestamp"),
    })
    return out
