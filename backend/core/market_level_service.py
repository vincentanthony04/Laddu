"""Canonical support/resistance, zone and Camarilla projection.

Completed candles and Wilder ATR14/ADX14/+DI/-DI define the regime authority.

All runtime surfaces must use this service so scanner decisions, historical
responses, Stock Intelligence and chart overlays cannot silently disagree.
"""
from __future__ import annotations

from datetime import datetime, time as dtime, timedelta, timezone
import math
from typing import Any, Dict, Iterable, Optional

from market_layers import camarilla_levels, derive_prev_day_ohlc, support_resistance_levels
from session_candles import candle_datetime, closed_candles
from core.timeframe import Timeframe, parse_timeframe, storage_interval
from core.india_time import INDIA_TZ
from core.trading_session_authority import DEFAULT_TRADING_SESSION_AUTHORITY
from core.indicator_snapshot_authority import true_ranges as canonical_true_ranges, wilder_series as canonical_wilder_series
from core.numeric_semantics import finite_number

LEVEL_SERVICE_VERSION = "canonical-market-levels-5.0.0-multiscope-current-role"
_INTRADAY_INTERVALS = {
    storage_interval(tf) for tf in (
        Timeframe.M1, Timeframe.M3, Timeframe.M5, Timeframe.M10,
        Timeframe.M15, Timeframe.M30, Timeframe.H1, Timeframe.H4,
    )
}
_DAILY_INTERVALS = {storage_interval(tf) for tf in (Timeframe.D1, Timeframe.W1, Timeframe.MN1)}

def _number(value: Any) -> Optional[float]:
    return finite_number(value)


def _round_level(value: Any) -> Optional[float]:
    number = _number(value)
    return round(number, 2) if number is not None else None


def _interval_key(interval: str | None) -> str:
    return storage_interval(interval)


def _date_key(candle: Dict[str, Any]) -> Optional[str]:
    raw = candle.get("timestamp") or candle.get("time") or candle.get("date")
    dt = candle_datetime(raw)
    if dt is not None:
        return dt.date().isoformat()
    text = str(raw or "")
    return text[:10] if len(text) >= 10 else None


def _session_snapshot(rows: Iterable[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    material = list(rows)
    if not material:
        return None
    normalized: list[tuple[float, float, float, str]] = []
    for row in material:
        high, low, close = (_number(row.get(key)) for key in ("high", "low", "close"))
        session_date = _date_key(row)
        if None in (high, low, close) or not session_date or min(high, low, close) <= 0 or high < low or not (low <= close <= high):
            return None
        normalized.append((high, low, close, session_date))
    session_dates = {row[3] for row in normalized}
    if len(session_dates) != 1:
        return None
    return {
        "high": max(row[0] for row in normalized),
        "low": min(row[1] for row in normalized),
        "close": normalized[-1][2],
        "session_date": normalized[-1][3],
        "bar_count": len(material),
    }


def previous_session_snapshot(
    candles: list[Dict[str, Any]], interval: str | None = "day", *, at: datetime | None = None
) -> Optional[Dict[str, Any]]:
    """Return the exact completed session used for Camarilla.

    Daily input is first stripped of an unfinished current-session candle; the
    latest remaining daily bar is the previous completed trading session.
    Intraday input is grouped by exchange date and the latest fully closed
    session before the active session is selected.
    """
    tf = parse_timeframe(interval)
    key = _interval_key(tf)
    completed = _completed_rows(list(candles or []), key, at=at)
    if not completed:
        return None
    if tf == Timeframe.D1:
        snapshot = _session_snapshot([completed[-1]])
        if snapshot and snapshot.get("session_date"):
            source_day = snapshot["session_date"]
            snapshot["target_session_date"] = DEFAULT_TRADING_SESSION_AUTHORITY.next_trading_day(source_day).isoformat()
            snapshot["session_authority"] = DEFAULT_TRADING_SESSION_AUTHORITY.authority
            snapshot["session_authority_version"] = DEFAULT_TRADING_SESSION_AUTHORITY.authority_version
        return snapshot
    if tf in {Timeframe.M1, Timeframe.M3, Timeframe.M5, Timeframe.M10, Timeframe.M15, Timeframe.M30, Timeframe.H1, Timeframe.H4}:
        sessions: Dict[str, list[Dict[str, Any]]] = {}
        order: list[str] = []
        for row in completed:
            session_key = _date_key(row)
            if not session_key:
                continue
            if session_key not in sessions:
                sessions[session_key] = []
                order.append(session_key)
            sessions[session_key].append(row)
        if not order:
            return None
        current = at or datetime.now(INDIA_TZ)
        if current.tzinfo is None:
            current = current.replace(tzinfo=INDIA_TZ)
        else:
            current = current.astimezone(INDIA_TZ)
        phase = DEFAULT_TRADING_SESSION_AUTHORITY.phase(current)
        current_key = current.date().isoformat()
        latest_key = order[-1]
        # During the live session, today's completed intraday bars are not the
        # prior-session OHLC used by Camarilla.  The exchange calendar, not a
        # weekday guess, decides whether the session is live.
        active = bool(phase.get("market_open"))
        selected_key = order[-2] if active and latest_key == current_key and len(order) >= 2 else latest_key
        snapshot = _session_snapshot(sessions[selected_key])
        if snapshot and snapshot.get("session_date"):
            source_day = snapshot["session_date"]
            snapshot["target_session_date"] = DEFAULT_TRADING_SESSION_AUTHORITY.next_trading_day(source_day).isoformat()
            snapshot["session_authority"] = DEFAULT_TRADING_SESSION_AUTHORITY.authority
            snapshot["session_authority_version"] = DEFAULT_TRADING_SESSION_AUTHORITY.authority_version
        return snapshot
    return None


def previous_session_ohlc(
    candles: list[Dict[str, Any]], interval: str | None = "day", *, at: datetime | None = None
) -> Optional[tuple]:
    snapshot = previous_session_snapshot(candles, interval, at=at)
    if not snapshot:
        return None
    return snapshot["high"], snapshot["low"], snapshot["close"]


def select_camarilla_breakout_trigger(
    levels: Dict[str, Any] | None,
    *,
    current_price: Any,
    side: str,
) -> Optional[Dict[str, Any]]:
    """Select the next valid Camarilla breakout tier on the correct side.

    R3/R4 and S3/S4 are conventional decision tiers; R5/R6 and S5/S6 are
    expansion fallbacks when price has already crossed the inner tier.  A
    level on the wrong side of current cash price is never relabelled as an
    entry trigger.  This keeps signal geometry fail-closed.
    """
    current = _number(current_price)
    direction = str(side or "").strip().upper()
    if current is None or direction not in {"LONG", "SHORT"}:
        return None
    material = dict(levels or {})
    keys = ("r3", "r4", "r5", "r6") if direction == "LONG" else ("s3", "s4", "s5", "s6")
    candidates: list[tuple[str, float]] = []
    for key in keys:
        value = _number(material.get(key))
        if value is None:
            continue
        if direction == "LONG" and value > current:
            candidates.append((key, value))
        elif direction == "SHORT" and value < current:
            candidates.append((key, value))
    if not candidates:
        return None
    key, price = min(candidates, key=lambda row: abs(row[1] - current))
    return {
        "key": key.upper(),
        "price": _round_level(price),
        "side": direction,
        "classification": "resistance_breakout" if direction == "LONG" else "support_breakdown",
        "policy": "next_conventional_camarilla_tier_on_correct_side_of_cash_price",
    }


def _completed_rows(
    candles: list[Dict[str, Any]], interval: str | None, *, at: datetime | None = None
) -> list[Dict[str, Any]]:
    key = _interval_key(interval)
    if key in _INTRADAY_INTERVALS:
        return closed_candles(candles, key)
    if key in _DAILY_INTERVALS and candles:
        rows = list(candles)
        latest_dt = candle_datetime(rows[-1].get("timestamp") or rows[-1].get("time") or rows[-1].get("date"))
        if latest_dt is None:
            return rows
        latest_day = latest_dt.date()
        # Outside the immutable calendar horizon we preserve historical bars;
        # completion enforcement is applied to the active governed horizon.
        current = at or datetime.now(INDIA_TZ)
        if current.tzinfo is None:
            current = current.replace(tzinfo=INDIA_TZ)
        else:
            current = current.astimezone(INDIA_TZ)
        current_day = current.date()
        # Completion authority applies only to the currently forming period.
        # Historical canonical candles are not retrospectively reclassified by
        # today's exchange calendar (important for imported/synthetic fixtures
        # and for immutable historical evidence).
        if key == storage_interval(Timeframe.D1):
            same_period = latest_day == current_day
            interval_name = "1D"
        elif key == storage_interval(Timeframe.W1):
            same_period = latest_day.isocalendar()[:2] == current_day.isocalendar()[:2]
            interval_name = "1W"
        else:
            same_period = (latest_day.year, latest_day.month) == (current_day.year, current_day.month)
            interval_name = "1M"
        if same_period and DEFAULT_TRADING_SESSION_AUTHORITY.calendar_covered(current_day):
            if not DEFAULT_TRADING_SESSION_AUTHORITY.period_complete(interval_name, latest_day, at=current):
                rows = rows[:-1]
        return rows
    return list(candles)

def _wilder_atr(candles: list[Dict[str, Any]], period: int = 14) -> Optional[float]:
    if isinstance(period, bool) or not isinstance(period, int) or period <= 0:
        return None
    rows = []
    for raw in candles or []:
        high, low, close = (_number(raw.get(key)) for key in ("high", "low", "close"))
        if None in (high, low, close) or min(high, low, close) <= 0 or high < low or not (low <= close <= high):
            return None
        rows.append({"high": high, "low": low, "close": close})
    if len(rows) < period:
        return None
    series = canonical_wilder_series(canonical_true_ranges(rows), period)
    return series[-1] if series else None

def _valid_session_ohlc(value: Any) -> Optional[tuple[float, float, float]]:
    try:
        high, low, close = value
    except (TypeError, ValueError):
        return None
    high, low, close = _number(high), _number(low), _number(close)
    if None in (high, low, close) or min(high, low, close) <= 0 or high < low or not (low <= close <= high):
        return None
    return high, low, close


def compute_levels_from_candles(
    candles: list[Dict[str, Any]] | None,
    *,
    interval: str | None = "day",
    prev_day_ohlc: Optional[tuple] = None,
    at: datetime | None = None,
) -> Dict[str, Any]:
    supplied_rows = list(candles or [])
    rows = _completed_rows(supplied_rows, interval, at=at)
    last = rows[-1] if rows else {}
    source_snapshot = previous_session_snapshot(supplied_rows, interval, at=at)
    source_ohlc = _valid_session_ohlc(prev_day_ohlc) if prev_day_ohlc is not None else (
        _valid_session_ohlc((source_snapshot["high"], source_snapshot["low"], source_snapshot["close"]))
        if source_snapshot else None
    )
    detail = support_resistance_levels(
        rows,
        lookback=min(220, len(rows)),
        prev_day_ohlc=source_ohlc,
    ) if rows else {"ok": False, "reason": "no candles"}
    support = detail.get("nearest_support") if detail.get("ok") else None
    resistance = detail.get("nearest_resistance") if detail.get("ok") else None
    support_price = _round_level((support or {}).get("price"))
    resistance_price = _round_level((resistance or {}).get("price"))
    close = _number(last.get("close"))
    atr14 = _wilder_atr(rows, 14)
    zone_half_width = max(atr14 * 0.25, close * 0.0015) if close is not None and atr14 is not None else None
    camarilla = camarilla_levels(*source_ohlc) if source_ohlc else {}
    cam_rows = [
        {"key": key.upper(), "price": _round_level(value)}
        for key, value in camarilla.items()
        if _number(value) is not None
    ]
    cam_support = max(
        (row for row in cam_rows if close is not None and row["price"] < close),
        key=lambda row: row["price"], default=None,
    )
    cam_resistance = min(
        (row for row in cam_rows if close is not None and row["price"] > close),
        key=lambda row: row["price"], default=None,
    )

    return {
        "version": LEVEL_SERVICE_VERSION,
        "interval": _interval_key(interval),
        "support": support_price,
        "resistance": resistance_price,
        "support_zone_low": _round_level(support_price - zone_half_width) if support_price is not None and zone_half_width is not None else support_price,
        "support_zone_high": _round_level(support_price + zone_half_width) if support_price is not None and zone_half_width is not None else support_price,
        "resistance_zone_low": _round_level(resistance_price - zone_half_width) if resistance_price is not None and zone_half_width is not None else resistance_price,
        "resistance_zone_high": _round_level(resistance_price + zone_half_width) if resistance_price is not None and zone_half_width is not None else resistance_price,
        "zone_half_width": _round_level(zone_half_width),
        "atr14": _round_level(atr14),
        "adx14": _round_level(detail.get("adx14")),
        "plus_di14": _round_level(detail.get("plus_di14")),
        "minus_di14": _round_level(detail.get("minus_di14")),
        "adx_change": _round_level(detail.get("adx_change")),
        "directional_regime": detail.get("directional_regime"),
        "completed_candle_count": len(rows),
        "excluded_incomplete_candle_count": max(0, len(supplied_rows) - len(rows)),
        "excluded_incomplete_count": max(0, len(supplied_rows) - len(rows)),
        "support_touches": (support or {}).get("touches"),
        "resistance_touches": (resistance or {}).get("touches"),
        "support_kind": (support or {}).get("kind"),
        "resistance_kind": (resistance or {}).get("kind"),
        "long_term_support": _round_level((detail.get("major_support") or {}).get("price")),
        "long_term_resistance": _round_level((detail.get("major_resistance") or {}).get("price")),
        "major_support_evidence": detail.get("major_support"),
        "major_resistance_evidence": detail.get("major_resistance"),
        "support_levels": list(detail.get("support") or []),
        "resistance_levels": list(detail.get("resistance") or []),
        "provisional_support": list(detail.get("provisional_support") or []),
        "provisional_resistance": list(detail.get("provisional_resistance") or []),
        "major_levels": list(detail.get("ranked_levels") or []),
        "level_report": detail,
        "camarilla": camarilla,
        "camarilla_actionable": {
            "support": cam_support,
            "resistance": cam_resistance,
            "current_price": _round_level(close),
            "classification_policy": "relative_to_current_cash_price",
        },
        "camarilla_source_ohlc": list(source_ohlc) if source_ohlc else None,
        "camarilla_source_session": source_snapshot,
        "camarilla_source_session_date": (source_snapshot or {}).get("session_date"),
        "camarilla_target_session_date": (source_snapshot or {}).get("target_session_date"),
        "camarilla_session_authority": (source_snapshot or {}).get("session_authority"),
        "camarilla_session_authority_version": (source_snapshot or {}).get("session_authority_version"),
        "method": detail.get("method") or "ranked multi-source major-level authority",
        "source": "candles" if rows else "pending",
        "status": "validated" if support or resistance else "no_validated_structure",
        "last_candle": last.get("timestamp") or last.get("time") or last.get("date"),
    }


LEVEL_SNAPSHOT_VERSION = "canonical-market-level-snapshot-1.0.0-operating-structural-major"
_FRAME_INTERVALS = {
    "1m": "1m",
    "3m": "3m",
    "5m": "5m",
    "15m": "15m",
    "30m": "30m",
    "60m": "1H",
    "1h": "1H",
    "1H": "1H",
    "240m": "4H",
    "4h": "4H",
    "4H": "4H",
    "1d": "1D",
    "day": "1D",
    "1D": "1D",
    "1w": "1W",
    "week": "1W",
    "1W": "1W",
    "1mth": "1M",
    "1mo": "1M",
    "month": "1M",
    "1M": "1M",
}


def reconcile_levels_to_price(levels: Dict[str, Any] | None, current_price: Any) -> Dict[str, Any]:
    """Project validated structural levels relative to a current cash price.

    This is deliberately a *view reconciler*, not a role-flip engine. Completed
    candles own accepted role changes. A live quote that crosses a resistance
    merely makes that resistance non-actionable on the resistance side and
    exposes it as ``BROKEN_*_PENDING_CONFIRMATION`` until completed-candle
    acceptance/retest evidence changes the canonical role.
    """
    material = dict(levels or {})
    current = _number(current_price)
    report = dict(material.get("level_report") or {})
    ranked = list(report.get("ranked_levels") or material.get("major_levels") or [])
    if current is None or current <= 0:
        return {
            **material,
            "current_price": None,
            "operating_support": None,
            "operating_resistance": None,
            "current_role_state": "CURRENT_PRICE_UNAVAILABLE",
            "decision_usable": False,
        }

    supports: list[Dict[str, Any]] = []
    resistances: list[Dict[str, Any]] = []
    crossed_pending: list[Dict[str, Any]] = []
    for raw in ranked:
        row = dict(raw or {})
        price = _number(row.get("price"))
        if price is None or price <= 0 or row.get("validated") is not True:
            continue
        kind = str(row.get("kind") or row.get("side") or "").lower()
        if kind == "support":
            if price < current:
                supports.append(row)
            else:
                crossed_pending.append({**row, "live_role_state": "BROKEN_SUPPORT_PENDING_CONFIRMATION"})
        elif kind == "resistance":
            if price > current:
                resistances.append(row)
            else:
                crossed_pending.append({**row, "live_role_state": "BROKEN_RESISTANCE_PENDING_CONFIRMATION"})

    support = max(supports, key=lambda row: float(row["price"]), default=None)
    resistance = min(resistances, key=lambda row: float(row["price"]), default=None)
    return {
        **material,
        "current_price": _round_level(current),
        "support": _round_level((support or {}).get("price")),
        "resistance": _round_level((resistance or {}).get("price")),
        "operating_support": support,
        "operating_resistance": resistance,
        "crossed_levels_pending_confirmation": sorted(
            crossed_pending,
            key=lambda row: abs(float(row.get("price") or current) - current),
        )[:8],
        "current_role_state": "RECONCILED_TO_LIVE_PRICE",
        "decision_usable": bool(support or resistance),
        "role_policy": "Completed candles own role flips; a live-price cross alone is pending confirmation and is never silently relabelled.",
    }


def compute_level_snapshot(
    source_frames: Dict[str, list[Dict[str, Any]]] | None,
    *,
    current_price: Any = None,
    at: datetime | None = None,
) -> Dict[str, Any]:
    """Build the single multi-scope S/R object consumed by customer surfaces."""
    source = dict(source_frames or {})
    by_timeframe: Dict[str, Dict[str, Any]] = {}
    for source_key, display_tf in _FRAME_INTERVALS.items():
        rows = list(source.get(source_key) or [])
        if not rows or display_tf in by_timeframe:
            continue
        try:
            computed = compute_levels_from_candles(rows, interval=display_tf, at=at)
        except Exception as exc:
            computed = {
                "version": LEVEL_SERVICE_VERSION,
                "interval": _interval_key(display_tf),
                "status": "unavailable",
                "reason": f"level computation failed: {type(exc).__name__}",
            }
        by_timeframe[display_tf] = reconcile_levels_to_price(computed, current_price) if current_price is not None else computed

    structural = dict(by_timeframe.get("1D") or {})
    return {
        "ok": any(bool(row.get("support") is not None or row.get("resistance") is not None) for row in by_timeframe.values()),
        "authority": "CANONICAL_MARKET_LEVEL_SNAPSHOT",
        "version": LEVEL_SNAPSHOT_VERSION,
        "levels_version": LEVEL_SERVICE_VERSION,
        "by_timeframe": by_timeframe,
        "structural": structural,
        "major": {
            "support": structural.get("long_term_support"),
            "resistance": structural.get("long_term_resistance"),
            "support_evidence": structural.get("major_support_evidence"),
            "resistance_evidence": structural.get("major_resistance_evidence"),
            "timeframe": "1D+structural",
        },
        "policy": "Operating S/R is selected by explicit timeframe; Structural is 1D; Major is separately labelled. No ambiguous generic S/R authority.",
    }



def reconcile_level_snapshot(snapshot: Dict[str, Any] | None, current_price: Any) -> Dict[str, Any]:
    """Reconcile every timeframe in an existing canonical snapshot to live price."""
    material = dict(snapshot or {})
    by_timeframe = {
        str(tf): reconcile_levels_to_price(dict(levels or {}), current_price)
        for tf, levels in dict(material.get("by_timeframe") or {}).items()
    }
    structural = dict(by_timeframe.get("1D") or material.get("structural") or {})
    return {
        **material,
        "by_timeframe": by_timeframe,
        "structural": structural,
        "current_price": _round_level(current_price),
        "role_reconciled": _number(current_price) not in (None, 0),
    }

def select_operating_levels(
    snapshot: Dict[str, Any] | None,
    interval: str | None,
    *,
    current_price: Any = None,
) -> Dict[str, Any]:
    material = dict(snapshot or {})
    tf = str(interval or "").strip()
    aliases = {
        "1minute":"1m", "3minute":"3m", "5minute":"5m", "15minute":"15m",
        "30minute":"30m", "60minute":"1H", "240minute":"4H",
        "day":"1D", "week":"1W", "month":"1M",
        "1h":"1H", "4h":"4H", "1d":"1D",
    }
    canonical = aliases.get(tf.lower(), tf)
    row = dict((material.get("by_timeframe") or {}).get(canonical) or {})
    if current_price is not None and row:
        row = reconcile_levels_to_price(row, current_price)
    return {
        **row,
        "authority": material.get("authority") or "CANONICAL_MARKET_LEVEL_SNAPSHOT",
        "snapshot_version": material.get("version") or LEVEL_SNAPSHOT_VERSION,
        "selected_timeframe": canonical or None,
        "scope": "OPERATING",
    }
