"""Project Laddu GET handlers: stock."""
from __future__ import annotations
import hashlib
import json
from routes_get_dependencies import *
from routes_get_system import _market_depth_contract


def _activate_foreground_selected(app, symbol: str, mode: str, interval: str) -> None:
    """Mark direct stock/chart reads P1 without performing provider work."""
    try:
        governor = getattr(app, "workload_governor", None)
        if governor is None:
            return
        mode2 = str(mode or "delivery").lower()
        if mode2 not in {"delivery", "intraday"}:
            mode2 = "delivery"
        governor.activate_selected(str(symbol or "").strip().upper(), mode2, ttl_seconds=20, interval=str(interval or "day"))
    except Exception:
        pass



def r_stock_snapshot(app, qs, q, mode):
    """Clean Core local-first selected-stock read model."""
    from core.stock_snapshot_service import StockSnapshotService
    symbol = str(qs.get("symbol", [q])[0] or q).strip()
    mode2 = str(qs.get("mode", [mode if mode != "all" else "delivery"])[0] or "delivery").lower()
    if not symbol:
        return ({"ok": False, "error": "symbol_required"}, 400)
    try:
        mode2 = require_production_mode(mode2)
    except UnsupportedProductionMode as exc:
        return ({"ok": False, "error": "unsupported_production_mode", "message": str(exc)}, 400)
    interval = "5minute" if mode2 == "intraday" else "day"
    _activate_foreground_selected(app, symbol, mode2, interval)
    payload = StockSnapshotService(app).read(symbol, mode2)
    # Once the canonical resolver returns the exact instrument, upgrade the
    # workload-governor selection from symbol-only to venue/instrument identity.
    # This prevents a selected-stock pipeline from remaining NOT_STARTED with a
    # null instrument_key even though Stock Intelligence resolved successfully.
    try:
        instrument_key = str((payload.get("instrument") or {}).get("instrument_key") or "").strip()
        resolved_symbol = str(payload.get("symbol") or symbol).strip().upper()
        if instrument_key:
            app.workload_governor.activate_selected(
                resolved_symbol, mode2, ttl_seconds=45, instrument_key=instrument_key, interval=interval
            )
    except Exception:
        pass
    if not payload.get("ok") and payload.get("state") == "IDENTITY_UNAVAILABLE":
        return (payload, 404)
    return payload


def r_chart_data(app, qs, q, mode):
    """Clean Core local-only chart read; provider repair is asynchronous."""
    from core.clean_chart_read_service import CleanChartReadService
    symbol = str(qs.get("symbol", [q])[0] or q).strip()
    interval = str(qs.get("interval", ["day"])[0] or "day")
    # The trader chart is session-first.  Whole-history reads belong to the
    # explicit `before` paging contract, never the initial interaction.
    limit_raw = qs.get("limit", ["500"])[0]
    before = str(qs.get("before", [""])[0] or "").strip() or None
    try:
        limit = max(50, min(1000, int(limit_raw)))
    except (TypeError, ValueError):
        limit = 500
    if not symbol:
        return ({"ok": False, "error": "symbol_required"}, 400)
    _activate_foreground_selected(app, symbol, mode if mode != "all" else "delivery", interval)
    payload = CleanChartReadService(app).read(symbol, interval, limit=limit, before=before, schedule_repair=True)
    if payload.get("state") == "IDENTITY_UNAVAILABLE":
        return (payload, 404)
    return payload


def r_live_chart_bar(app, qs, q, mode):
    """Backend-formed current-session OHLCV; never aggregate ticks in the UI."""
    from core.live_chart_bar_service import LiveChartBarService
    symbol = str(qs.get("symbol", [q])[0] or q).strip()
    interval = str(qs.get("interval", ["1minute"])[0] or "1minute")
    if not symbol:
        return ({"ok": False, "error": "symbol_required"}, 400)
    _activate_foreground_selected(app, symbol, mode if mode != "all" else "intraday", interval)
    payload = LiveChartBarService(app).read(symbol, interval)
    if payload.get("state") == "IDENTITY_UNAVAILABLE":
        return (payload, 404)
    return payload

def r_stock_intelligence(app, qs, q, mode):
    """Compatibility alias for the one canonical local-first Stock Snapshot.

    v119 still exposed a second, legacy intelligence composer behind this route.
    That duplicated technical/MTF/fundamental work, could take tens of seconds,
    and even carried an inner ``import threading`` that shadowed earlier uses of
    the module and caused installed HTTP 500s.  v120 removes that second
    authority: both route names now project the same bounded local read model.
    """
    return r_stock_snapshot(app, qs, q, mode)

def r_historical(app, qs, q, mode):
    """Bounded historical compatibility view.

    Interactive GET is a projection-only read. It never calls MarketDataService's
    provider/token/exact-gap planner and never opens cold storage on the request
    thread. Explicit refresh/schedule requests are dispatched independently.
    """
    from core.clean_chart_read_service import CleanChartReadService
    from core.background_repair_dispatcher import for_app as repair_dispatcher_for_app
    symbol = str(qs.get("symbol", [q])[0] or q).strip()
    interval = str(qs.get("interval", ["day"])[0] or "day")
    days_raw = qs.get("days", [None])[0]
    days = int(days_raw) if str(days_raw or "").isdigit() else None
    refresh = _flag(qs, "refresh")
    recent_only = _flag(qs, "recent_only") or _flag(qs, "tail_only")
    schedule_only = _flag(qs, "schedule_only")
    before = str(qs.get("before", [""])[0] or "").strip() or None
    backfill_only = _flag(qs, "backfill_only")
    if not symbol:
        return ({"ok": False, "error": "symbol_required", "candles": []}, 400)
    _activate_foreground_selected(app, symbol, mode if mode != "all" else ("intraday" if "minute" in interval else "delivery"), interval)

    if backfill_only and before:
        # Explicit operator intent may enqueue a deep page repair, but the GET
        # response itself never waits for that provider workflow.
        result = repair_dispatcher_for_app(app).submit(
            f"history-before:{symbol}:{interval}:{before}",
            lambda: app.schedule_historical_before_for_symbol(symbol, interval, before[:10], days),
        )
        return {"ok": True, "state": result.state, "scheduled": bool(result.accepted), "symbol": symbol, "interval": interval}

    if schedule_only or refresh:
        result = repair_dispatcher_for_app(app).submit(
            f"history-refresh:{symbol}:{interval}",
            lambda: app.schedule_historical_for_symbol(symbol, interval, days, force_resolve=bool(refresh)),
        )
        if schedule_only:
            return {"ok": True, "state": result.state, "scheduled": bool(result.accepted), "symbol": symbol, "interval": interval}

    # days is converted only to a display limit; it does not widen foreground I/O.
    if "minute" in interval.lower():
        approx_per_day = 80 if interval.lower().startswith("5") else 420
        limit = min(5000, max(120, int(days or 10) * approx_per_day))
    else:
        # Candidate 23: an explicit bounded daily request returns a modest guard
        # band beyond the requested horizon instead of a forced 120-row minimum.
        # Storage/research history is unchanged; this only reduces interactive
        # response bytes/JSON work.  Unbounded/default history retains the existing 290-row foreground page.
        limit = 290 if days is None else min(2000, max(60, int(days) + 15))
    if recent_only:
        limit = min(limit, 1000)
    payload = CleanChartReadService(app).read(
        symbol, interval, limit=limit, before=before, schedule_repair=False
    )
    candles = list(payload.get("candles") or [])
    levels = dict((payload.get("chart_projection") or {}).get("metrics") or {})
    return {
        **payload,
        "endpoint": "historical",
        "days": days,
        "count": len(candles),
        "candles": candles,
        "levels": levels,
        "support": levels.get("support"),
        "resistance": levels.get("resistance"),
        "recent_authority_only": bool(recent_only),
        "source": "materialized_foreground_projection",
        "refreshing": bool(refresh) or bool(payload.get("refreshing")),
        "message": payload.get("message") or "Materialized local history projection.",
    }

def r_analyze(app, qs, q, mode):
    symbol = str(qs.get("symbol", [q])[0] or q)
    return app.analyze_symbol(symbol, mode if mode != "all" else "delivery")
