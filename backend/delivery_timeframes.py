from __future__ import annotations

from collections import OrderedDict
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List

from session_candles import candle_datetime, IST
from core.master_candle_service import evaluate_higher_timeframe_structures


def _aggregate(rows: Iterable[Dict[str, Any]], key_fn, *, source: str) -> List[Dict[str, Any]]:
    groups = OrderedDict()
    for row in rows or []:
        dt = candle_datetime(row)
        if not dt:
            continue
        groups.setdefault(key_fn(dt), []).append((dt, row))
    out = []
    for key, items in groups.items():
        items = sorted(items, key=lambda item: item[0])
        bars = [item[1] for item in items]
        try:
            start = items[0][0].astimezone(IST)
            end = items[-1][0].astimezone(IST)
            out.append({
                "period": str(key),
                "timestamp": start.isoformat(),
                "period_end": end.isoformat(),
                "open": float(bars[0]["open"]),
                "high": max(float(x["high"]) for x in bars),
                "low": min(float(x["low"]) for x in bars),
                "close": float(bars[-1]["close"]),
                "volume": sum(float(x.get("volume") or 0) for x in bars),
                "bar_count": len(bars),
                "source_sessions": len({item[0].astimezone(IST).date() for item in items}),
                "source": source,
                "is_closed": True,
                "forming": False,
                "session_partial": False,
                "pattern_eligible": True,
            })
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _state(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    if len(rows) < 3:
        return {"state": "insufficient", "bars": len(rows), "support": None, "resistance": None}
    sample = rows[-8:]
    closes = [x["close"] for x in sample]
    alpha = 2 / (min(6, len(closes)) + 1)
    ema = closes[0]
    ema_path = []
    for close in closes:
        ema = close * alpha + ema * (1 - alpha)
        ema_path.append(ema)
    ema_rising = len(ema_path) > 2 and ema_path[-1] > ema_path[-3]
    ema_falling = len(ema_path) > 2 and ema_path[-1] < ema_path[-3]
    highs, lows = [x["high"] for x in sample], [x["low"] for x in sample]
    hh_hl = highs[-1] >= max(highs[-3:-1]) and lows[-1] >= min(lows[-3:-1])
    lh_ll = highs[-1] <= max(highs[-3:-1]) and lows[-1] <= min(lows[-3:-1])
    bullish = closes[-1] > ema_path[-1] and ema_rising and hh_hl
    bearish = closes[-1] < ema_path[-1] and ema_falling and lh_ll
    peak = max(closes)
    drawdown = (closes[-1] / peak - 1) * 100 if peak else None
    extension = (closes[-1] / ema_path[-1] - 1) * 100 if ema_path[-1] else None
    return {
        "state": "bullish" if bullish else "bearish" if bearish else "neutral",
        "bars": len(rows),
        "support": round(min(x["low"] for x in sample), 2),
        "resistance": round(max(x["high"] for x in sample), 2),
        "ema": round(ema_path[-1], 2),
        "ema_slope": "rising" if ema_rising else "falling" if ema_falling else "flat",
        "structure": "higher_high_higher_low" if hh_hl else "lower_high_lower_low" if lh_ll else "mixed",
        "drawdown_pct": round(drawdown, 2) if drawdown is not None else None,
        "extension_from_ema_pct": round(extension, 2) if extension is not None else None,
        "last_completed_at": rows[-1].get("period_end") or rows[-1].get("timestamp"),
    }


def _completed_daily(daily: Iterable[Dict[str, Any]], *, now: datetime) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    session_closed = (now.hour, now.minute) >= (15, 31)
    for raw in daily or []:
        row = dict(raw or {})
        dt = candle_datetime(row)
        if dt is None:
            continue
        local = dt.astimezone(IST)
        if local.date() > now.date() or (local.date() == now.date() and not session_closed):
            continue
        out.append({**row, "is_closed": True, "forming": False, "session_partial": False, "pattern_eligible": True})
    return out


def delivery_timeframe_context(daily: List[Dict[str, Any]], instrument_key: str = "") -> Dict[str, Any]:
    """Completed daily/weekly/monthly structure for the Delivery desk.

    Weekly and monthly periods are generated from the same point-in-time daily
    series.  The current incomplete week/month is removed before trend or
    master-candle logic is evaluated.
    """
    now = datetime.now(IST)
    completed_daily = _completed_daily(daily, now=now)
    weeks = _aggregate(
        completed_daily,
        lambda dt: dt.astimezone(IST).isocalendar()[:2],
        source="completed_day_to_week",
    )
    months = _aggregate(
        completed_daily,
        lambda dt: (dt.astimezone(IST).year, dt.astimezone(IST).month),
        source="completed_day_to_month",
    )
    current_week = now.isocalendar()[:2]
    current_month = (now.year, now.month)
    weeks = [row for row in weeks if row.get("period") != str(current_week)]
    months = [row for row in months if row.get("period") != str(current_month)]
    structures = evaluate_higher_timeframe_structures(
        instrument_key=str(instrument_key or "UNKNOWN"),
        weekly=weeks,
        monthly=months,
    )
    return {
        "ok": len(completed_daily) >= 60,
        "daily_bars": len(completed_daily),
        "daily": _state(completed_daily),
        "weekly": _state(weeks),
        "monthly": _state(months),
        "weekly_bars": weeks,
        "monthly_bars": months,
        "weekly_master_candle": structures["weekly_master_candle"],
        "monthly_master_candle": structures["monthly_master_candle"],
        "completed_periods_only": True,
        "policy": "30m/1H execution timing; 4H/1D setup and retest; completed 1W master candle and primary trend; completed 1M secular trend",
        "timeframe_roster": ["30m", "1H", "4H", "1D", "1W", "1M"],
    }
