"""Canonical session VWAP authority.

VWAP is volume-weighted typical price (H+L+C)/3 over one exchange trading
session and resets at the canonical session date boundary. Zero-volume bars do
not alter cumulative value. Missing/invalid OHLCV or missing timestamps fail
closed rather than substituting close as VWAP.
"""
from __future__ import annotations

from datetime import datetime, timezone
import math
from typing import Any, Iterable, Mapping
from core.numeric_semantics import finite_number
from core.trading_session_authority import DEFAULT_TRADING_SESSION_AUTHORITY

from core.india_time import INDIA_TZ


class SessionVWAPAuthority:
    authority = "SessionVWAPAuthority"
    authority_version = "1.0.0-typical-price-session-reset"
    price_basis = "TYPICAL_PRICE_HLC3"

    @staticmethod
    def _float(value: Any) -> float | None:
        return finite_number(value)

    @staticmethod
    def _dt(value: Any) -> datetime | None:
        if isinstance(value, datetime):
            out = value
        elif isinstance(value, (int, float)):
            raw = float(value)
            if not math.isfinite(raw):
                return None
            if abs(raw) > 1e12:
                raw /= 1000.0
            try:
                out = datetime.fromtimestamp(raw, tz=timezone.utc)
            except Exception:
                return None
        else:
            text = str(value or "").strip()
            if not text:
                return None
            try:
                out = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except Exception:
                return None
        if out.tzinfo is None:
            out = out.replace(tzinfo=INDIA_TZ)
        return out.astimezone(INDIA_TZ)

    @classmethod
    def calculate(cls, rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
        clean = []
        for raw in rows or ():
            stamp = cls._dt(raw.get("timestamp") or raw.get("time") or raw.get("date") or raw.get("datetime"))
            high, low, close, volume = (cls._float(raw.get(k)) for k in ("high", "low", "close", "volume"))
            if stamp is None or None in {high, low, close, volume} or volume < 0:
                return {
                    "authority": cls.authority,
                    "authority_version": cls.authority_version,
                    "state": "UNAVAILABLE",
                    "decision_usable": False,
                    "reason": "complete timestamp/high/low/close/nonnegative-volume required",
                    "series": [], "value": None,
                }
            if min(float(high), float(low), float(close)) <= 0 or float(high) < max(float(low), float(close)) or float(low) > float(close):
                return {
                    "authority": cls.authority, "authority_version": cls.authority_version,
                    "state": "UNAVAILABLE", "decision_usable": False,
                    "reason": "valid positive OHLC geometry required", "series": [], "value": None,
                }
            session_window = DEFAULT_TRADING_SESSION_AUTHORITY.session_window(stamp.date())
            if session_window is None or not (session_window.open_at() <= stamp < session_window.close_at()):
                return {
                    "authority": cls.authority, "authority_version": cls.authority_version,
                    "state": "UNAVAILABLE", "decision_usable": False,
                    "reason": "canonical exchange-session timestamp required", "series": [], "value": None,
                }
            clean.append((stamp, float(high), float(low), float(close), float(volume)))
        if not clean:
            return {"authority": cls.authority, "authority_version": cls.authority_version, "state": "UNAVAILABLE", "decision_usable": False, "reason": "no rows", "series": [], "value": None}
        clean.sort(key=lambda x: x[0])
        seen = set()
        current_session = None
        cumulative_pv = cumulative_volume = 0.0
        series = []
        for stamp, high, low, close, volume in clean:
            key = stamp.isoformat()
            if key in seen:
                return {"authority": cls.authority, "authority_version": cls.authority_version, "state": "UNAVAILABLE", "decision_usable": False, "reason": "duplicate timestamp", "series": [], "value": None}
            seen.add(key)
            session = stamp.date().isoformat()
            if session != current_session:
                current_session = session
                cumulative_pv = cumulative_volume = 0.0
            if volume > 0:
                typical = (high + low + close) / 3.0
                cumulative_pv += typical * volume
                cumulative_volume += volume
            value = cumulative_pv / cumulative_volume if cumulative_volume > 0 else None
            series.append({
                "time": int(stamp.astimezone(timezone.utc).timestamp()),
                "session": session,
                "value": value,
                "cumulative_volume": cumulative_volume,
            })
        return {
            "authority": cls.authority,
            "authority_version": cls.authority_version,
            "state": "READY" if series[-1]["value"] is not None else "UNAVAILABLE",
            "decision_usable": series[-1]["value"] is not None,
            "price_basis": cls.price_basis,
            "session_reset": True,
            "session": current_session,
            "value": series[-1]["value"],
            "series": series,
        }


DEFAULT_SESSION_VWAP_AUTHORITY = SessionVWAPAuthority()
