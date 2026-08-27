"""Project Laddu GET handlers: scanner stage evidence."""
from __future__ import annotations
from routes_get_dependencies import now_iso

def r_scanner_stage_members(app, qs, q, mode):
    """Bounded symbol evidence for one clickable scanner funnel stage.

    The route never invents members from aggregate counters. It returns exact
    stage_members when the scanner published them, otherwise only rows already
    present in the dashboard read-model and marks the result as partial.
    """
    desk = str(qs.get("desk", [mode or "delivery"])[0] or "delivery").strip().lower()
    stage = str(qs.get("stage", [""])[0] or "").strip()
    stage_key = stage.lower().replace("-", "_").replace(" ", "_").replace(":", "r")
    try:
        snapshot = app.scanner_status() or {}
    except Exception:
        snapshot = {}
    root = snapshot.get("scanner") if isinstance(snapshot.get("scanner"), dict) else snapshot
    modes = root.get("mode_scanners") or root.get("modes") or {}
    node = modes.get(desk) or root.get(desk) or {}
    analysis = node.get("analysis") if isinstance(node.get("analysis"), dict) else node
    published = analysis.get("stage_members") or node.get("stage_members") or {}
    aliases = [stage_key, stage_key.replace("quote_ready", "quote-ready"), "rr" if stage_key in {"rr", "r_r", "r"} else stage_key]
    if stage_key == "deferred":
        aliases.insert(0, "capacity_deferred")
    if stage_key in {"data_pending", "pending_data"}:
        aliases.insert(0, "data_pending")
    rows = []
    for alias in aliases:
        value = published.get(alias) if isinstance(published, dict) else None
        if isinstance(value, list):
            rows = [dict(item) for item in value if isinstance(item, dict)][:250]
            break
    authority = "SCANNER_STAGE_MEMBERS" if rows else "DASHBOARD_READ_MODEL_PARTIAL"
    if not rows:
        try:
            cache = getattr(getattr(app, "dashboard", None), "_cards_cache", {}) or {}
            cards = cache.get("all") or next(iter(cache.values()), {}) or {}
            if stage_key == "final":
                candidates = cards.get("selected") or cards.get("active_positions") or []
            else:
                candidates = cards.get("potential_entries") or cards.get("research") or cards.get("watchlist") or []
            for item in candidates:
                if not isinstance(item, dict):
                    continue
                item_desk = str(item.get("mode") or item.get("desk") or "delivery").lower()
                if item_desk != desk:
                    continue
                symbol = str(item.get("symbol") or item.get("trading_symbol") or "").upper().strip()
                if not symbol:
                    continue
                rows.append({
                    "symbol": symbol,
                    "state": stage.upper(),
                    "reason": item.get("reason_not_final") or item.get("block_reason") or item.get("reason") or item.get("setup"),
                    "side": item.get("side") or item.get("direction") or item.get("position") or item.get("proposed_side"),
                    "setup": item.get("setup") or item.get("setup_family") or item.get("pattern"),
                    "preliminary_score": item.get("preliminary_score") or item.get("shortlist_score") or item.get("confidence") or item.get("confluence_score"),
                    "ltp": item.get("ltp") or item.get("last_price") or item.get("price"),
                    "change_pct": item.get("change_pct") or item.get("percent_change"),
                    "updated_at": item.get("updated_at") or item.get("source_time") or item.get("timestamp"),
                })
                if len(rows) >= 250:
                    break
        except Exception:
            rows = []
    reason_counts = {}
    for row in rows:
        reason = str(row.get("reason") or row.get("state") or "UNSPECIFIED").strip().upper().replace(" ", "_")
        reason_counts[reason] = reason_counts.get(reason, 0) + 1
    reason_summary = [
        {"reason": reason, "count": count}
        for reason, count in sorted(reason_counts.items(), key=lambda item: (-item[1], item[0]))
    ]
    return {
        "ok": True,
        "desk": desk,
        "stage": stage,
        "state": str(analysis.get("state") or node.get("state") or "UNKNOWN"),
        "authority": authority,
        "members_recorded": authority == "SCANNER_STAGE_MEMBERS",
        "rows": rows,
        "reason_summary": reason_summary,
        "unresolved_count": sum(1 for row in rows if str(row.get("state") or "").upper() in {"SHORTLISTED", "UNKNOWN"}),
        "message": (f"{len(rows)} exact scanner members" if authority == "SCANNER_STAGE_MEMBERS" else
                    f"{len(rows)} visible read-model rows; exact per-stage members are not yet published for this cycle"),
        "time": now_iso(),
    }
