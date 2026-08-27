from __future__ import annotations

"""Canonical completed-daily-candle price performance projection.

The browser formats these values but never calculates financial returns.  Each
horizon is anchored to the last completed daily candle on or before its
calendar cutoff, so weekends and exchange holidays remain deterministic.
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, Mapping
import math


def _number(value: Any) -> float | None:
    try:
        number = float(value)
        return number if math.isfinite(number) and number > 0 else None
    except (TypeError, ValueError):
        return None


def _instant(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


class PricePerformanceService:
    VERSION = "canonical-price-performance-1.1.0-causal-range"
    HORIZONS = {
        "1w": timedelta(days=7),
        "2w": timedelta(days=14),
        "1m": timedelta(days=30),
        "3m": timedelta(days=91),
        "6m": timedelta(days=183),
        "1y": timedelta(days=365),
        "2y": timedelta(days=365 * 2 + 1),
        "3y": timedelta(days=365 * 3 + 1),
        "5y": timedelta(days=365 * 5 + 1),
    }

    @classmethod
    def reprice(
        cls,
        materialized: Mapping[str, Any] | None,
        *,
        current_price: Any = None,
        current_as_of: Any = None,
    ) -> Dict[str, Any]:
        """Reprice retained historical anchors with one current verified price.

        The expensive historical scan belongs to the background materializer.
        Foreground Stock Snapshot reads reuse those immutable anchor closes and
        perform only bounded arithmetic.  Missing/legacy materialization stays
        explicitly unavailable rather than triggering a historical scan.
        """
        basis = dict(materialized or {})
        current = _number(current_price) or _number(basis.get("current_price"))
        if str(basis.get("state") or "").upper() != "READY" or current is None:
            return {
                **basis,
                "state": str(basis.get("state") or "UNAVAILABLE").upper(),
                "pricing_state": "MATERIALIZED_ANCHORS_UNAVAILABLE",
            }
        anchors = {}
        values: Dict[str, float | None] = {}
        for key in cls.HORIZONS:
            row = dict((basis.get("horizons") or {}).get(key) or {})
            anchor_close = _number(row.get("anchor_close"))
            if str(row.get("state") or "").upper() == "READY" and anchor_close is not None:
                change_pct = round(((current - anchor_close) / anchor_close) * 100.0, 4)
                row.update({"current_price": round(current, 4), "change_pct": change_pct})
                values[key] = change_pct
            else:
                values[key] = None
            anchors[key] = row
        price_range = dict(basis.get("range_52_week") or {})
        low = _number(price_range.get("low"))
        high = _number(price_range.get("high"))
        if low is not None:
            low = min(low, current)
        if high is not None:
            high = max(high, current)
        return {
            **basis,
            "state": "READY",
            "basis_as_of": basis.get("as_of"),
            "as_of": (_instant(current_as_of) or _instant(basis.get("as_of")) or datetime.now(timezone.utc)).isoformat(),
            "current_price": round(current, 4),
            "horizons": anchors,
            **values,
            "range_52_week": {
                "low": round(low, 4) if low is not None else None,
                "high": round(high, 4) if high is not None else None,
            },
            "pricing_state": "REPRICED_FROM_MATERIALIZED_ANCHORS",
            "policy": "Historical anchors are materialized in the background; foreground repricing uses only the current verified quote and retained anchor closes.",
        }

    @classmethod
    def project(
        cls,
        candles: Iterable[Mapping[str, Any]],
        *,
        current_price: Any = None,
        current_as_of: Any = None,
    ) -> Dict[str, Any]:
        rows = []
        requested_as_of = _instant(current_as_of)
        for row in candles or []:
            stamp = _instant(row.get("timestamp") or row.get("time") or row.get("date"))
            close = _number(row.get("close"))
            high = _number(row.get("high"))
            low = _number(row.get("low"))
            if stamp is not None and close is not None:
                # Never allow a candle later than the declared observation clock
                # to influence anchors, latest close, or range statistics.
                if requested_as_of is not None and stamp > requested_as_of:
                    continue
                rows.append((stamp, close, high, low))
        rows.sort(key=lambda item: item[0])
        if not rows:
            return {
                "authority": "COMPLETED_DAILY_CANDLES",
                "authority_version": cls.VERSION,
                "state": "UNAVAILABLE",
                "as_of": None,
                "current_price": None,
                "horizons": {},
                **{key: None for key in cls.HORIZONS},
                "range_52_week": {"low": None, "high": None},
            }

        latest_at, latest_close, _latest_high, _latest_low = rows[-1]
        current = _number(current_price) or latest_close
        as_of = requested_as_of or latest_at
        anchors: Dict[str, Any] = {}
        values: Dict[str, float | None] = {}
        for key, delta in cls.HORIZONS.items():
            cutoff = as_of - delta
            eligible = [item for item in rows if item[0] <= cutoff]
            if not eligible:
                values[key] = None
                anchors[key] = {"state": "INSUFFICIENT_HISTORY", "cutoff": cutoff.isoformat()}
                continue
            anchor_at, anchor_close, _anchor_high, _anchor_low = eligible[-1]
            change_pct = round(((current - anchor_close) / anchor_close) * 100.0, 4)
            values[key] = change_pct
            anchors[key] = {
                "state": "READY",
                "cutoff": cutoff.isoformat(),
                "anchor_at": anchor_at.isoformat(),
                "anchor_close": round(anchor_close, 4),
                "current_price": round(current, 4),
                "change_pct": change_pct,
            }

        year_cutoff = as_of - timedelta(days=366)
        year_lows = [low for stamp, _close, _high, low in rows if stamp >= year_cutoff and low is not None]
        year_highs = [high for stamp, _close, high, _low in rows if stamp >= year_cutoff and high is not None]
        return {
            "authority": "COMPLETED_DAILY_CANDLES",
            "authority_version": cls.VERSION,
            "state": "READY",
            "as_of": as_of.isoformat(),
            "source_last_completed_candle": latest_at.isoformat(),
            "current_price": round(current, 4),
            "horizons": anchors,
            **values,
            "range_52_week": {
                "low": round(min(year_lows), 4) if year_lows else None,
                "high": round(max(year_highs), 4) if year_highs else None,
            },
            "policy": "Calendar cutoff with the last completed daily close on or before the cutoff; current verified quote is used when available.",
        }
