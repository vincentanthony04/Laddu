"""
Heatmap and index-drilldown row shaping.

Extracted from main.py's final_heatmap_payload / final_index_stocks_payload
(v39.1.2). Both were mixing I/O (app.heatmap_snapshot(), app.store,
app.live_quotes()) with row-shaping logic. This module owns only the
shaping: given already-fetched data, produce the exact same payload shape
the frontend expects. main.py keeps the I/O and calls these.
"""
from __future__ import annotations

from core.production_mode_policy import PRODUCTION_MODES
from typing import Any, Dict, List, Tuple
from models import now_iso

FINAL_INDEX_ALIAS = {
    "NIFTY 50": "NIFTY", "NIFTY NEXT 50": "NXT50", "NIFTY 100": "N100", "NIFTY 200": "N200", "NIFTY 500": "N500",
    "NIFTY MIDCAP 100": "MIDCAP", "NIFTY SMALLCAP 100": "SMALLCAP", "SENSEX": "SENSEX", "NIFTY BANK": "BANK",
    "NIFTY AUTO": "AUTO", "NIFTY IT": "IT", "NIFTY PHARMA": "PHARMA", "NIFTY FMCG": "FMCG",
    "NIFTY METAL": "METAL", "NIFTY REALTY": "REALTY", "NIFTY ENERGY": "ENERGY",
    "NIFTY OIL & GAS": "OILGAS", "NIFTY HEALTHCARE": "HEALTHCARE",
    "NIFTY CONSUMER DURABLES": "CONSUMDUR", "NIFTY MEDIA": "MEDIA",
    "NIFTY PSU BANK": "PSUBANK", "NIFTY PRIVATE BANK": "PVTBANK",
}


def _norm_index_name(name: str) -> str:
    return str(name or "").upper().replace("%20", " ").replace("_", " ").strip()


def _quote_verified(row: Dict[str, Any]) -> bool:
    state = str(row.get("freshness_state") or "").lower().strip()
    return bool(
        row.get("ltp") is not None
        and row.get("identity_verified") is True
        and not row.get("stale")
        and state in ("live", "closed_market")
    )


def _canonical_mode(value: Any, default: str = "delivery") -> str:
    """Return a UI desk for a read-only constituent row.

    Index members frequently have no canonical decision yet.  That absence is
    not an unsupported production desk and must not turn the entire breadth
    route into HTTP 500.  The drawer opens in the Delivery lens by default;
    persisted decisions retain their exact Intraday/Delivery desk.
    """
    mode = str(value or "").lower().strip()
    return mode if mode in PRODUCTION_MODES else default


def build_heatmap_items(existing_rows: List[Dict[str, Any]], index_universe: List[Tuple[str, str]]) -> Dict[str, Any]:
    """Shape index rows without ever relabelling an LKG/reference value as live."""
    existing: Dict[str, Dict[str, Any]] = {}
    for r in existing_rows or []:
        nm = str(r.get("name") or r.get("index") or "").upper().strip()
        if nm:
            existing[nm] = r

    items = []
    for name, category in index_universe:
        alias = FINAL_INDEX_ALIAS.get(name)
        old = dict((existing.get(alias) if alias else None) or existing.get(name) or {})
        verified = _quote_verified(old)
        freshness_state = str(old.get("freshness_state") or ("live" if verified else "stale" if old.get("ltp") is not None else "missing")).lower()
        stale = not verified
        change = old.get("change_pct")
        state = str(old.get("state") or (
            "green" if isinstance(change, (int, float)) and change > 0 else
            "red" if isinstance(change, (int, float)) and change < 0 else "pending"
        )).lower()
        ltp = old.get("ltp")
        open_px = old.get("open")
        close_px = old.get("close") or old.get("previous_close")
        point_change = old.get("point_change") or old.get("rupee_change") or old.get("day_change_abs")
        if point_change is None and ltp is not None:
            try:
                if close_px is not None:
                    point_change = round(float(ltp) - float(close_px), 2)
                elif change is not None and float(change) != -100:
                    prev = float(ltp) / (1 + float(change) / 100)
                    point_change = round(float(ltp) - prev, 2)
                    close_px = round(prev, 2)
            except (TypeError, ValueError, ZeroDivisionError):
                point_change = None
        source_time = old.get("source_time") or old.get("timestamp") or old.get("last_refresh") or old.get("freshness")
        if ltp is None:
            freshness_text = "verified quote pending"
        elif verified:
            freshness_text = old.get("freshness") or f"{freshness_state.replace('_', ' ')} @ {source_time or 'exchange snapshot'}"
        else:
            freshness_text = old.get("freshness") or f"LKG only @ {source_time or 'unknown time'}"
        items.append({
            "name": name, "index": name, "category": category, "kind": category,
            "state": state, "tone": state, "change_pct": change,
            "ltp": ltp, "open": open_px, "high": old.get("high"), "low": old.get("low"),
            "close": close_px, "previous_close": old.get("previous_close") or close_px,
            "point_change": point_change, "rupee_change": point_change, "day_change_abs": point_change,
            "change_source": old.get("change_source"), "session_close": old.get("session_close"),
            "timestamp": source_time, "source_time": source_time,
            "source": old.get("source") or ("verified exchange snapshot" if verified else "source unavailable / mapping pending"),
            "freshness": freshness_text, "freshness_state": freshness_state,
            "freshness_reason": old.get("freshness_reason") or old.get("reason"),
            "identity_verified": bool(old.get("identity_verified")), "stale": stale,
            "usable_for_promotion": bool(old.get("usable_for_promotion")) if verified else False,
            "reason": old.get("reason") or ("verified exchange snapshot" if verified else "live source unavailable; value is last-known/reference only" if ltp is not None else "live source unavailable / mapping pending"),
            "clickable": True,
            "endpoint": "/api/market/index/" + name.replace(" ", "%20") + "/stocks",
        })
    return {
        "ok": True, "count": len(items), "items": items, "updated_at": now_iso(),
        "policy": "Only exact-token, timestamped exchange snapshots are live. Older values are labelled LKG and never used for promotion.",
        "groups": ["Broad", "Sector"],
    }


def build_index_stocks_rows(
    index_name: str,
    constituent_symbols: List[str],
    latest_decisions: List[Dict[str, Any]],
    live_quotes: Dict[str, Any],
    fallback_instrument_lookup,
) -> Dict[str, Any]:
    """Build index constituents with verified quote precedence.

    Decision rows are analysis snapshots, not market-data authority.  Their LTP
    may be shown only as explicitly labelled last-known/reference data when no
    verified quote exists.
    """
    name = _norm_index_name(index_name)
    bysym = {str(x.get("symbol") or "").upper(): x for x in latest_decisions if x.get("symbol")}
    rows = []
    for sym in constituent_symbols:
        inst = fallback_instrument_lookup(sym) or {}
        d = dict(bysym.get(sym) or {})
        lq = dict(live_quotes.get(sym) or {})
        live_ok = _quote_verified(lq)
        if live_ok:
            ltp = lq.get("ltp")
            change_pct = lq.get("change_pct")
            freshness_state = str(lq.get("freshness_state") or "live")
            freshness = lq.get("freshness") or f"{freshness_state.replace('_', ' ')} @ {lq.get('source_time') or lq.get('timestamp') or now_iso()}"
            decision = d.get("decision") or "Analysis pending"
            source = lq.get("source") or "verified_exchange_snapshot"
            identity_verified = True
            stale = False
        else:
            ltp = d.get("ltp")
            change_pct = d.get("change_pct")
            source_time = d.get("observed_at") or d.get("last_refresh") or d.get("timestamp")
            freshness_state = "stale" if ltp is not None else "missing"
            freshness = (f"LKG analysis snapshot @ {source_time}" if ltp is not None and source_time else
                         "LKG analysis snapshot" if ltp is not None else "verified quote pending")
            decision = d.get("decision") or ("Analysis snapshot only" if ltp is not None else "Verified quote pending")
            source = d.get("source") or "decision_snapshot_reference"
            identity_verified = False
            stale = True
        rows.append({
            "symbol": sym,
            "company": d.get("name") or inst.get("name") or sym,
            "exchange": d.get("exchange") or inst.get("exchange") or "NSE",
            "index": name,
            "sector": d.get("sector") or inst.get("sector") or "",
            "mode": _canonical_mode(d.get("mode")),
            "ltp": ltp, "change_pct": change_pct, "volume": lq.get("volume") if live_ok else d.get("volume"),
            "open": lq.get("open") if live_ok else d.get("open"),
            "high": lq.get("high") if live_ok else d.get("high"),
            "low": lq.get("low") if live_ok else d.get("low"),
            "market_cap": d.get("market_cap"), "freshness": freshness,
            "freshness_state": freshness_state, "freshness_reason": lq.get("freshness_reason") if live_ok else "verified quote unavailable",
            "identity_verified": identity_verified, "stale": stale,
            "usable_for_promotion": bool(lq.get("usable_for_promotion")) if live_ok else False,
            "source": source, "source_time": lq.get("source_time") or lq.get("timestamp") if live_ok else d.get("observed_at") or d.get("last_refresh") or d.get("timestamp"),
            "stage": d.get("candidate_stage") or d.get("status") or "WATCH",
            "decision": decision, "score": d.get("score"),
            "entry": d.get("entry"), "sl": d.get("sl"), "t1": d.get("t1"), "t2": d.get("t2"), "rr": d.get("rr"),
            "support": d.get("support"), "resistance": d.get("resistance"),
            "tf15": d.get("tf15") or d.get("state_15m") or "refresh pending",
            "tf1h": d.get("tf1h") or d.get("state_1h") or "refresh pending",
            "tf4h": d.get("tf4h") or d.get("state_4h") or "refresh pending",
            "daily": d.get("daily") or d.get("state_1d") or "refresh pending",
            "reason": d.get("reason") or (
                "Verified quote available; click stock / Refresh Stock for full completed-candle analysis."
                if live_ok else
                "Last-known analysis snapshot only; verified exchange quote is pending."
                if ltp is not None else
                "Index member loaded; verified exchange quote and analysis are pending."
            ),
        })
    return {
        "ok": True, "index": name, "count": len(rows), "rows": rows,
        "sortable": True, "filterable": True,
        "policy": "Verified exact-token quotes override analysis snapshots. Decision prices are labelled LKG/reference and cannot be presented as live.",
    }

