"""Canonical Project Laddu timeframe authority.

This module is the only parser/normaliser for timeframe identifiers.  It
preserves the product's binding distinction between ``1m`` (one minute) and
``1M`` (one month) and provides explicit provider, storage and UI mappings.
Callers must not lower-case timeframe input before passing it here.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class InvalidTimeframeError(ValueError):
    """Raised when an external timeframe token is not in the canonical roster."""



class Timeframe(Enum):
    M1 = "M1"
    M3 = "M3"
    M5 = "M5"
    M10 = "M10"
    M15 = "M15"
    M30 = "M30"
    H1 = "H1"
    H4 = "H4"
    D1 = "D1"
    W1 = "W1"
    MN1 = "MN1"


@dataclass(frozen=True)
class TimeframeSpec:
    timeframe: Timeframe
    storage: str
    public: str
    provider: str
    minutes: int | None
    source: Timeframe | None = None


_SPECS = {
    Timeframe.M1: TimeframeSpec(Timeframe.M1, "1m", "1m", "1minute", 1),
    Timeframe.M3: TimeframeSpec(Timeframe.M3, "3m", "3m", "3minute", 3, Timeframe.M1),
    Timeframe.M5: TimeframeSpec(Timeframe.M5, "5m", "5m", "5minute", 5, Timeframe.M1),
    Timeframe.M10: TimeframeSpec(Timeframe.M10, "10m", "10m", "10minute", 10, Timeframe.M1),
    Timeframe.M15: TimeframeSpec(Timeframe.M15, "15m", "15m", "15minute", 15, Timeframe.M1),
    Timeframe.M30: TimeframeSpec(Timeframe.M30, "30m", "30m", "30minute", 30, Timeframe.M1),
    Timeframe.H1: TimeframeSpec(Timeframe.H1, "60m", "1H", "60minute", 60, Timeframe.M1),
    Timeframe.H4: TimeframeSpec(Timeframe.H4, "240m", "4H", "240minute", 240, Timeframe.H1),
    Timeframe.D1: TimeframeSpec(Timeframe.D1, "1d", "1D", "day", 1440),
    Timeframe.W1: TimeframeSpec(Timeframe.W1, "1w", "1W", "week", 10080, Timeframe.D1),
    Timeframe.MN1: TimeframeSpec(Timeframe.MN1, "1mo", "1M", "month", None, Timeframe.D1),
}

# Case-sensitive aliases are resolved first.  In particular 1M must never be
# passed through lower() before this table is consulted.
_EXACT = {
    "1M": Timeframe.MN1,
    "1D": Timeframe.D1,
    "1W": Timeframe.W1,
    "1H": Timeframe.H1,
    "4H": Timeframe.H4,
}

_ALIASES = {
    "m1": Timeframe.M1, "1m": Timeframe.M1, "minute": Timeframe.M1,
    "1minute": Timeframe.M1, "minutes1": Timeframe.M1,
    "m3": Timeframe.M3, "3m": Timeframe.M3, "3minute": Timeframe.M3,
    "m5": Timeframe.M5, "5m": Timeframe.M5, "5minute": Timeframe.M5,
    "m10": Timeframe.M10, "10m": Timeframe.M10, "10minute": Timeframe.M10,
    "m15": Timeframe.M15, "15m": Timeframe.M15, "15minute": Timeframe.M15,
    "m30": Timeframe.M30, "30m": Timeframe.M30, "30minute": Timeframe.M30,
    "h1": Timeframe.H1, "60m": Timeframe.H1, "60minute": Timeframe.H1,
    "1hour": Timeframe.H1, "hour": Timeframe.H1,
    "h4": Timeframe.H4, "240m": Timeframe.H4, "240minute": Timeframe.H4,
    "4hour": Timeframe.H4, "4hourly": Timeframe.H4,
    "d1": Timeframe.D1, "1d": Timeframe.D1, "day": Timeframe.D1,
    "1day": Timeframe.D1, "days": Timeframe.D1,
    "w1": Timeframe.W1, "1w": Timeframe.W1, "week": Timeframe.W1,
    "1week": Timeframe.W1, "weeks": Timeframe.W1,
    "mn1": Timeframe.MN1, "1mo": Timeframe.MN1, "month": Timeframe.MN1,
    "1month": Timeframe.MN1, "months": Timeframe.MN1,
}


def parse_timeframe(value: Any, default: Timeframe = Timeframe.D1) -> Timeframe:
    if isinstance(value, Timeframe):
        return value
    raw = str(value or "").strip().replace("_", "").replace(" ", "")
    if not raw:
        return default
    if raw in _EXACT:
        return _EXACT[raw]
    return _ALIASES.get(raw.lower(), default)


def parse_timeframe_strict(value: Any) -> Timeframe:
    """Parse an external token without silently converting mistakes to daily.

    Internal compatibility callers may use :func:`parse_timeframe` with an
    explicit default. Provider/API/storage boundaries must use this strict
    function so a typo can never fetch or store the wrong interval.
    """
    if isinstance(value, Timeframe):
        return value
    raw = str(value or "").strip().replace("_", "").replace(" ", "")
    if raw in _EXACT:
        return _EXACT[raw]
    resolved = _ALIASES.get(raw.lower()) if raw else None
    if resolved is None:
        raise InvalidTimeframeError(f"unsupported timeframe: {value!r}")
    return resolved


def spec(value: Any, default: Timeframe = Timeframe.D1) -> TimeframeSpec:
    return _SPECS[parse_timeframe(value, default)]


def storage_interval(value: Any, default: Timeframe = Timeframe.D1) -> str:
    return spec(value, default).storage


def public_interval(value: Any, default: Timeframe = Timeframe.D1) -> str:
    return spec(value, default).public


def provider_interval(value: Any, default: Timeframe = Timeframe.D1) -> str:
    return spec(value, default).provider


def interval_minutes(value: Any, default: int = 5) -> int:
    minutes = spec(value, Timeframe.M5).minutes
    return int(minutes if minutes is not None else default)


def is_intraday(value: Any) -> bool:
    return parse_timeframe(value) in {
        Timeframe.M1, Timeframe.M3, Timeframe.M5, Timeframe.M10,
        Timeframe.M15, Timeframe.M30, Timeframe.H1, Timeframe.H4,
    }


def is_daily(value: Any) -> bool:
    return parse_timeframe(value) == Timeframe.D1


def is_weekly(value: Any) -> bool:
    return parse_timeframe(value) == Timeframe.W1


def is_monthly(value: Any) -> bool:
    return parse_timeframe(value) == Timeframe.MN1


def derivation_source(value: Any) -> Timeframe | None:
    return spec(value).source


def all_public() -> tuple[str, ...]:
    return tuple(_SPECS[tf].public for tf in Timeframe)
