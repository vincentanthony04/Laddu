"""Single deterministic indicator calculation authority for decision-time MTF.

This service owns the indicator mathematics consumed by MTF semantics.  It does
not decide trend/bull/bear state; it returns one versioned snapshot so callers
cannot independently recalculate EMA/RSI/ATR/DMI/MACD/Supertrend with subtly
different smoothing rules.
"""
from __future__ import annotations

import math
from typing import Any, Iterable, Mapping
from core.numeric_semantics import finite_number

AUTHORITY_NAME = "IndicatorSnapshotAuthority"
AUTHORITY_VERSION = "1.3.0-strict-positive-price-contract"


def _number(value: Any) -> float | None:
    return finite_number(value)


def ema_series(values: list[float], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if len(values) < period or period <= 0:
        return out
    current = sum(values[:period]) / period
    out[period - 1] = current
    alpha = 2.0 / (period + 1.0)
    for index in range(period, len(values)):
        current = alpha * values[index] + (1.0 - alpha) * current
        out[index] = current
    return out


def ema(values: list[float], period: int) -> float | None:
    series = ema_series(values, period)
    return series[-1] if series else None


def rsi_series(values: list[float], period: int = 14) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if len(values) <= period:
        return out
    gains = [max(0.0, values[i] - values[i - 1]) for i in range(1, len(values))]
    losses = [max(0.0, values[i - 1] - values[i]) for i in range(1, len(values))]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    def value() -> float:
        if avg_loss == 0:
            return 100.0 if avg_gain > 0 else 50.0
        return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)

    out[period] = value()
    for i in range(period + 1, len(values)):
        avg_gain = ((avg_gain * (period - 1)) + gains[i - 1]) / period
        avg_loss = ((avg_loss * (period - 1)) + losses[i - 1]) / period
        out[i] = value()
    return out


def true_ranges(rows: list[dict[str, float]]) -> list[float]:
    values: list[float] = []
    previous = None
    for row in rows:
        high, low, close = row["high"], row["low"], row["close"]
        values.append(high - low if previous is None else max(high - low, abs(high - previous), abs(low - previous)))
        previous = close
    return values


def wilder_series(values: list[float], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if len(values) < period:
        return out
    current = sum(values[:period]) / period
    out[period - 1] = current
    for i in range(period, len(values)):
        current = ((current * (period - 1)) + values[i]) / period
        out[i] = current
    return out


def directional(rows: list[dict[str, float]], period: int = 14) -> tuple[float | None, float | None, float | None]:
    if len(rows) < period * 2 + 2:
        return None, None, None
    plus, minus, tr = [], [], []
    for i in range(1, len(rows)):
        up = rows[i]["high"] - rows[i - 1]["high"]
        down = rows[i - 1]["low"] - rows[i]["low"]
        plus.append(up if up > down and up > 0 else 0.0)
        minus.append(down if down > up and down > 0 else 0.0)
        tr.append(max(
            rows[i]["high"] - rows[i]["low"],
            abs(rows[i]["high"] - rows[i - 1]["close"]),
            abs(rows[i]["low"] - rows[i - 1]["close"]),
        ))
    atrs = wilder_series(tr, period)
    plus_smoothed = wilder_series(plus, period)
    minus_smoothed = wilder_series(minus, period)
    dx: list[float] = []
    pdi_last = mdi_last = None
    for a, p, m in zip(atrs, plus_smoothed, minus_smoothed):
        if a is None or p is None or m is None or a <= 0:
            continue
        pdi = 100.0 * p / a
        mdi = 100.0 * m / a
        pdi_last, mdi_last = pdi, mdi
        dx.append(100.0 * abs(pdi - mdi) / max(pdi + mdi, 1e-9))
    adx = wilder_series(dx, period)[-1] if len(dx) >= period else None
    return pdi_last, mdi_last, adx


def macd_series(values: list[float]) -> tuple[list[float | None], list[float | None], list[float | None]]:
    """Return MACD line/signal/histogram aligned to the input candle index."""
    fast, slow = ema_series(values, 12), ema_series(values, 26)
    line: list[float | None] = [None] * len(values)
    compact: list[float] = []
    compact_indices: list[int] = []
    for index, (fast_value, slow_value) in enumerate(zip(fast, slow)):
        if fast_value is None or slow_value is None:
            continue
        value = fast_value - slow_value
        line[index] = value
        compact.append(value)
        compact_indices.append(index)
    compact_signal = ema_series(compact, 9)
    signal: list[float | None] = [None] * len(values)
    hist: list[float | None] = [None] * len(values)
    for compact_index, original_index in enumerate(compact_indices):
        sig = compact_signal[compact_index]
        signal[original_index] = sig
        if sig is not None and line[original_index] is not None:
            hist[original_index] = float(line[original_index]) - sig
    return line, signal, hist


def macd(values: list[float]) -> tuple[float | None, float | None, float | None]:
    line, signal, hist = macd_series(values)
    return (line[-1] if line else None, signal[-1] if signal else None, hist[-1] if hist else None)


def supertrend_series(rows: list[dict[str, float]], period: int = 10, multiplier: float = 3.0) -> tuple[list[float | None], list[int | None]]:
    values: list[float | None] = [None] * len(rows)
    directions: list[int | None] = [None] * len(rows)
    if len(rows) < period + 2:
        return values, directions
    atrs = wilder_series(true_ranges(rows), period)
    direction = 1
    upper = lower = None
    for i, row in enumerate(rows):
        atr = atrs[i]
        if atr is None:
            continue
        middle = (row["high"] + row["low"]) / 2.0
        basic_upper, basic_lower = middle + multiplier * atr, middle - multiplier * atr
        previous_close = rows[i - 1]["close"] if i else row["close"]
        if upper is None:
            upper, lower = basic_upper, basic_lower
        else:
            upper = basic_upper if basic_upper < upper or previous_close > upper else upper
            lower = basic_lower if basic_lower > lower or previous_close < lower else lower
        if direction < 0 and row["close"] > upper:
            direction = 1
        elif direction > 0 and row["close"] < lower:
            direction = -1
        values[i] = lower if direction > 0 else upper
        directions[i] = direction
    return values, directions


def supertrend(rows: list[dict[str, float]], period: int = 10, multiplier: float = 3.0) -> tuple[int, float | None]:
    values, directions = supertrend_series(rows, period=period, multiplier=multiplier)
    direction = next((value for value in reversed(directions) if value is not None), 0)
    line = next((value for value in reversed(values) if value is not None), None)
    return int(direction), line


class IndicatorSnapshotAuthority:
    authority = AUTHORITY_NAME
    authority_version = AUTHORITY_VERSION

    def calculate(self, rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
        # Mathematical authority never repairs, drops or coerces malformed bars.
        # Continuity/freshness is owned upstream, but every bar supplied here must
        # still be finite and internally valid; otherwise the entire snapshot is
        # unavailable so a hidden gap cannot be smoothed over by indicators.
        source_rows = list(rows or ())
        clean: list[dict[str, float]] = []
        for index, raw in enumerate(source_rows):
            row = {field: _number(raw.get(field)) for field in ("open", "high", "low", "close", "volume")}
            if any(row[field] is None for field in ("open", "high", "low", "close")):
                return {
                    "authority": self.authority, "authority_version": self.authority_version,
                    "state": "UNAVAILABLE", "decision_usable": False,
                    "reason": f"invalid/non-finite OHLC at row {index}",
                    "input_count": len(source_rows), "accepted_count": 0,
                    "metrics": {}, "series": {},
                }
            o, h, l, c = (float(row[k]) for k in ("open", "high", "low", "close"))
            if min(o, h, l, c) <= 0:
                return {
                    "authority": self.authority, "authority_version": self.authority_version,
                    "state": "UNAVAILABLE", "decision_usable": False,
                    "reason": f"non-positive OHLC at row {index}",
                    "input_count": len(source_rows), "accepted_count": 0,
                    "metrics": {}, "series": {},
                }
            if h < max(o, c, l) or l > min(o, c, h):
                return {
                    "authority": self.authority, "authority_version": self.authority_version,
                    "state": "UNAVAILABLE", "decision_usable": False,
                    "reason": f"invalid OHLC ordering at row {index}",
                    "input_count": len(source_rows), "accepted_count": 0,
                    "metrics": {}, "series": {},
                }
            if row["volume"] is not None and float(row["volume"]) < 0:
                return {
                    "authority": self.authority, "authority_version": self.authority_version,
                    "state": "UNAVAILABLE", "decision_usable": False,
                    "reason": f"negative volume at row {index}",
                    "input_count": len(source_rows), "accepted_count": 0,
                    "metrics": {}, "series": {},
                }
            clean.append(row)
        closes = [float(row["close"]) for row in clean]
        if not closes:
            return {"authority": self.authority, "authority_version": self.authority_version, "state": "UNAVAILABLE", "decision_usable": False, "reason": "no valid rows", "input_count": len(source_rows), "accepted_count": 0, "metrics": {}, "series": {}}
        ema9s, ema20s, ema21s, ema50s = ema_series(closes, 9), ema_series(closes, 20), ema_series(closes, 21), ema_series(closes, 50)
        rsi14s = rsi_series(closes, 14)
        atr14s = wilder_series(true_ranges(clean), 14)
        pdi, mdi, adx = directional(clean, 14)
        macd_lines, macd_signals, macd_hists = macd_series(closes)
        st_values, st_directions = supertrend_series(clean)
        macd_line = macd_lines[-1] if macd_lines else None
        macd_signal = macd_signals[-1] if macd_signals else None
        macd_hist = macd_hists[-1] if macd_hists else None
        st_direction = next((value for value in reversed(st_directions) if value is not None), 0)
        st_value = next((value for value in reversed(st_values) if value is not None), None)
        atr14 = atr14s[-1] if atr14s else None
        last_close = closes[-1] if closes else None
        atr14_pct = (100.0 * atr14 / last_close) if atr14 is not None and last_close not in (None, 0.0) else None
        metrics = {
            "ema9": ema9s[-1], "ema20": ema20s[-1], "ema21": ema21s[-1], "ema50": ema50s[-1],
            "rsi14": rsi14s[-1], "atr14": atr14, "atr14_pct": atr14_pct,
            "plus_di14": pdi, "minus_di14": mdi, "adx14": adx,
            "macd": macd_line, "macd_signal": macd_signal, "macd_hist": macd_hist,
            "supertrend_direction": st_direction, "supertrend_value": st_value,
        }
        return {
            "authority": self.authority,
            "authority_version": self.authority_version,
            "state": "READY",
            "decision_usable": True,
            "input_count": len(source_rows),
            "accepted_count": len(clean),
            "metrics": metrics,
            "series": {
                "ema9": ema9s, "ema20": ema20s, "ema21": ema21s, "ema50": ema50s,
                "rsi14": rsi14s, "atr14": atr14s,
                "macd": macd_lines, "macd_signal": macd_signals, "macd_hist": macd_hists,
                "supertrend": st_values, "supertrend_direction": st_directions,
            },
            "row_count": len(clean),
        }


DEFAULT_INDICATOR_SNAPSHOT_AUTHORITY = IndicatorSnapshotAuthority()
