"""
Trade journal / performance aggregation.

Extracted from main.py's final_journal_summary_payload (v39.1.2) into a
standalone, dependency-free service. The original function mixed two
concerns: (1) fetching rows from app.store, (2) aggregating them into
by-mode/by-day/by-month/by-year buckets with success/failure/accuracy
stats. This module owns concern (2) only, as a pure function of
`rows` -> summary dict, so it's testable without a database, a Store,
or an App instance.

main.py / routes_get.py still own fetching the rows and calling this.
"""
from __future__ import annotations
from typing import Any, Dict, List
from models import now_iso

DEFAULT_MODES = ["intraday", "delivery"]


def _empty_group(key: str) -> Dict[str, Any]:
    return {
        "group": key or "unknown", "suggested": 0, "success": 0, "failure": 0,
        "open": 0, "ambiguous": 0, "expired": 0, "hold": 0, "pnl": 0.0,
    }


def _classify_row(row: Dict[str, Any]) -> str:
    """Returns one of: ambiguous, expired, open, success, failure, hold."""
    status_text = str(row.get("status") or row.get("result") or "OPEN").upper()
    result_text = str(row.get("result") or "").upper()
    combined = status_text + " " + result_text
    if "AMBIGUOUS" in combined:
        return "ambiguous"
    if "EXPIRED" in combined or "EOD" in combined:
        return "expired"
    if status_text in ("OPEN", "SIGNAL_OPEN") or combined.strip() in ("OPEN", "SIGNAL_OPEN"):
        return "open"
    if "SUCCESS" in combined or "T1" in combined or "T2" in combined or "TARGET" in combined:
        return "success"
    if "FAIL" in combined or "SL" in combined:
        return "failure"
    return "hold"


def _add_row(groups: Dict[str, Dict[str, Any]], key: str, row: Dict[str, Any]) -> None:
    g = groups.setdefault(key or "unknown", _empty_group(key))
    g["suggested"] += 1
    g[_classify_row(row)] += 1
    try:
        g["pnl"] += float(row.get("pnl") or 0)
    except (TypeError, ValueError):
        pass


def _finish(groups: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for g in groups.values():
        decisive_closed = g["success"] + g["failure"]
        closed = decisive_closed + g.get("ambiguous", 0) + g.get("expired", 0)
        g["decisive_closed"] = decisive_closed
        g["decisive_win_pct"] = round(g["success"] * 100 / decisive_closed, 1) if decisive_closed else None
        g["settled_success_pct"] = round(g["success"] * 100 / closed, 1) if closed else None
        # Legacy fields remain conservative for API compatibility.
        g["success_pct"] = round(g["success"] * 100 / closed, 1) if closed else 0
        g["failure_pct"] = round(g["failure"] * 100 / decisive_closed, 1) if decisive_closed else 0
        g["ambiguous_pct"] = round(g.get("ambiguous", 0) * 100 / closed, 1) if closed else 0
        g["expired_pct"] = round(g.get("expired", 0) * 100 / closed, 1) if closed else 0
        g["pnl"] = round(g.get("pnl") or 0, 2)
        g["pnl_points"] = g["pnl"]
        g["pnl_units"] = "PRICE_POINTS"
        g["currency_pnl_available"] = False
        g["economic_performance_eligible"] = False
        out.append(g)
    return sorted(out, key=lambda x: str(x["group"]), reverse=True)


def _accuracy_state(totals: Dict[str, int]) -> tuple[str, str]:
    closed = totals["success"] + totals["failure"] + totals["ambiguous"] + totals["expired"]
    if totals["suggested"] == 0:
        return "NO_SIGNALS", "No triggered signal ledger rows in this period."
    if closed == 0 and totals["open"] > 0:
        return (
            "ACCURACY_PENDING",
            f"{totals['open']} open signal(s), 0 settled; accuracy will appear only after target/SL/expiry audit.",
        )
    return "SETTLED_AVAILABLE", f"{closed} settled signal(s), {totals['open']} still open."


def summarize_trade_journal(rows: List[Dict[str, Any]], start_date: str = "", end_date: str = "") -> Dict[str, Any]:
    """Pure aggregation: signal ledger rows -> by-mode/day/month/year performance summary."""
    by_mode: Dict[str, Dict[str, Any]] = {}
    by_day: Dict[str, Dict[str, Any]] = {}
    by_month: Dict[str, Dict[str, Any]] = {}
    by_year: Dict[str, Dict[str, Any]] = {}

    for r in rows:
        dt = str(r.get("trade_date") or r.get("opened_at") or now_iso()[:10])
        _add_row(by_mode, r.get("mode") or "unknown", r)
        _add_row(by_day, dt[:10], r)
        _add_row(by_month, dt[:7], r)
        _add_row(by_year, dt[:4], r)

    for m in DEFAULT_MODES:
        by_mode.setdefault(m, _empty_group(m))

    finished_mode = _finish(by_mode)
    totals = {
        k: sum(int(g.get(k) or 0) for g in finished_mode)
        for k in ("suggested", "open", "success", "failure", "ambiguous", "expired")
    }
    closed = totals["success"] + totals["failure"] + totals["ambiguous"] + totals["expired"]
    accuracy_state, accuracy_message = _accuracy_state(totals)

    return {
        "ok": True, "start": start_date, "end": end_date,
        "by_mode": finished_mode, "by_day": _finish(by_day),
        "by_month": _finish(by_month), "by_year": _finish(by_year),
        "rows": rows[:500], "totals": totals, "closed": closed,
        "accuracy_state": accuracy_state, "accuracy_message": accuracy_message,
        "metric_lane": "SIGNAL_ACCURACY_POINTS",
        "units": "PRICE_POINTS",
        "currency_pnl_available": False,
        "economic_performance_eligible": False,
        "policy": "Signal-history aggregation only. Decisive win rate uses target/stop outcomes; pnl aliases are price-point movement, never rupee performance. Governed economics come from Model Paper settlement.",
    }
