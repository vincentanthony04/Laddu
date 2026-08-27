"""Bounded early-session intelligence for pre-qualified Intraday candidates.

This service is evidence, never a third production desk and never a bypass of
canonical promotion/risk gates.  It classifies the opening tape from completed
one-minute bars and a verified rich quote so Laddu can distinguish continuation
from climax/false-break risk before the conventional 15-minute ORB matures.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from math import exp, isfinite
from statistics import median
from typing import Any, Iterable, Mapping

from core.session_vwap_authority import DEFAULT_SESSION_VWAP_AUTHORITY

SERVICE_VERSION = "opening-intelligence-1.1.0-diagnostic-scores-zero-influence"


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if isfinite(result) else None


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def _logistic(value: float) -> float:
    return 1.0 / (1.0 + exp(-max(-20.0, min(20.0, value))))


def _vwap(rows: list[Mapping[str, Any]]) -> float | None:
    result = DEFAULT_SESSION_VWAP_AUTHORITY.calculate(rows)
    return _number(result.get("value")) if result.get("state") == "READY" else None


@dataclass(frozen=True)
class OpeningAssessment:
    state: str
    continuation_score: float
    climax_score: float
    false_break_score: float
    confidence: float
    direction: str
    time_of_day_rvol: float | None
    vwap: float | None
    price_efficiency: float | None
    momentum_score: float
    climax_risk_flag: bool
    reasons: tuple[str, ...]
    missing: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": True,
            "service_version": SERVICE_VERSION,
            "state": self.state,
            "continuation_score": round(self.continuation_score, 2),
            "climax_score": round(self.climax_score, 2),
            "false_break_score": round(self.false_break_score, 2),
            "confidence": round(self.confidence, 2),
            "direction": self.direction,
            "time_of_day_rvol": None if self.time_of_day_rvol is None else round(self.time_of_day_rvol, 3),
            "vwap": None if self.vwap is None else round(self.vwap, 4),
            "price_efficiency": None if self.price_efficiency is None else round(self.price_efficiency, 3),
            "momentum_score": round(self.momentum_score, 2),
            "climax_risk_flag": self.climax_risk_flag,
            "calibrated_probability": False,
            "production_influence": 0,
            "decision_usable": False,
            "reasons": list(self.reasons),
            "missing": list(self.missing),
            "authority": "DIAGNOSTIC_OPENING_EVIDENCE_ZERO_PRODUCTION_INFLUENCE",
        }


class OpeningIntelligenceService:
    """Classify a small priority list; never scan the full universe here."""

    def assess(
        self,
        bars: Iterable[Mapping[str, Any]],
        quote: Mapping[str, Any] | None = None,
        *,
        market_alignment: float = 0.0,
        sector_alignment: float = 0.0,
    ) -> dict[str, Any]:
        rows = [dict(row) for row in bars or () if _number(row.get("close")) is not None]
        rows = rows[-15:]
        missing: list[str] = []
        if len(rows) < 2:
            return {
                "ok": False, "service_version": SERVICE_VERSION, "state": "BUILDING",
                "continuation_score": 0.0, "climax_score": 0.0,
                "false_break_score": 0.0, "confidence": 0.0,
                "direction": "NEUTRAL", "climax_risk_flag": False,
                "calibrated_probability": False, "production_influence": 0, "decision_usable": False,
                "reasons": ["At least two completed one-minute bars are required."],
                "missing": ["completed_1m_bars"],
                "authority": "DIAGNOSTIC_OPENING_EVIDENCE_ZERO_PRODUCTION_INFLUENCE",
            }

        opens = [_number(row.get("open")) for row in rows]
        highs = [_number(row.get("high")) for row in rows]
        lows = [_number(row.get("low")) for row in rows]
        closes = [_number(row.get("close")) for row in rows]
        volumes = [max(0.0, _number(row.get("volume")) or 0.0) for row in rows]
        first_open = opens[0] or closes[0]
        last_close = closes[-1]
        if first_open in (None, 0) or last_close is None:
            missing.append("valid_open_close")
            first_open = last_close or 1.0
        signed_return = ((last_close or first_open) / first_open - 1.0) * 100.0
        direction = "LONG" if signed_return > 0.05 else "SHORT" if signed_return < -0.05 else "NEUTRAL"

        ranges = [max(0.0, (high or close or 0.0) - (low or close or 0.0)) for high, low, close in zip(highs, lows, closes)]
        typical_range = median([value for value in ranges[:-1] if value > 0] or [ranges[-1] or 0.01])
        opening_range = max(value for value in highs if value is not None) - min(value for value in lows if value is not None)
        range_extension = opening_range / max(typical_range, 1e-9)
        path = sum(abs((closes[i] or 0.0) - (closes[i - 1] or 0.0)) for i in range(1, len(closes)))
        progress = abs((last_close or 0.0) - (closes[0] or 0.0))
        efficiency = progress / path if path > 0 else 0.0

        volume_baseline = median([value for value in volumes[:-1] if value > 0] or [0.0])
        quote_volume = _number((quote or {}).get("volume_traded_today") or (quote or {}).get("volume"))
        latest_volume = volumes[-1] if volumes[-1] > 0 else quote_volume
        rvol = latest_volume / volume_baseline if latest_volume is not None and volume_baseline > 0 else None
        if rvol is None:
            missing.append("time_of_day_volume_baseline")

        vwap = _vwap(rows)
        vwap_distance = ((last_close - vwap) / max(opening_range, typical_range, 1e-9)) if vwap not in (None, 0) else 0.0
        if vwap is None:
            missing.append("vwap")

        latest = rows[-1]
        latest_high = highs[-1] or last_close
        latest_low = lows[-1] or last_close
        latest_open = opens[-1] or last_close
        candle_range = max((latest_high or 0.0) - (latest_low or 0.0), 1e-9)
        upper_wick = ((latest_high or 0.0) - max(latest_open or 0.0, last_close or 0.0)) / candle_range
        lower_wick = (min(latest_open or 0.0, last_close or 0.0) - (latest_low or 0.0)) / candle_range
        rejection = upper_wick if direction == "LONG" else lower_wick if direction == "SHORT" else max(upper_wick, lower_wick)

        recent_returns = [((closes[i] or 0.0) - (closes[i - 1] or 0.0)) / max(abs(closes[i - 1] or 0.0), 1e-9) * 100.0 for i in range(1, len(closes))]
        acceleration = (recent_returns[-1] - recent_returns[-2]) if len(recent_returns) >= 2 else recent_returns[-1]
        directional_acceleration = acceleration if direction == "LONG" else -acceleration if direction == "SHORT" else 0.0

        spread = None
        bid = _number((quote or {}).get("bid_price")); ask = _number((quote or {}).get("ask_price"))
        if bid is not None and ask is not None and last_close:
            spread = max(0.0, ask - bid) / last_close * 10_000.0
        rich_quote = str((quote or {}).get("stream_mode") or "").lower() == "full"
        if not rich_quote:
            missing.append("full_feed")

        continuation_raw = (
            min(2.5, abs(signed_return) / 0.35) * 0.75
            + min(2.0, range_extension / 4.0) * 0.45
            + efficiency * 1.4
            + max(-1.0, min(1.0, directional_acceleration / 0.15)) * 0.55
            + max(-1.0, min(1.0, (vwap_distance if direction == "LONG" else -vwap_distance))) * 0.55
            + max(-1.0, min(1.0, market_alignment)) * 0.30
            + max(-1.0, min(1.0, sector_alignment)) * 0.35
            + min(1.5, max(0.0, (rvol or 1.0) - 1.0)) * 0.45
            - rejection * 1.1
            - 1.5
        )
        continuation = _logistic(continuation_raw) * 100.0
        climax_raw = (
            max(0.0, range_extension - 3.0) * 0.55
            + max(0.0, (rvol or 1.0) - 2.0) * 0.45
            + rejection * 1.8
            + max(0.0, 0.45 - efficiency) * 2.0
            + max(0.0, -directional_acceleration / 0.12) * 0.7
            + max(0.0, abs(vwap_distance) - 1.0) * 0.65
            - 1.8
        )
        climax = _logistic(climax_raw) * 100.0
        false_break_raw = (
            rejection * 1.6
            + max(0.0, 0.35 - efficiency) * 2.0
            + max(0.0, -directional_acceleration / 0.12) * 0.8
            + (0.6 if last_close < vwap and direction == "LONG" else 0.6 if last_close > vwap and direction == "SHORT" else 0.0)
            - 1.4
        )
        false_break = _logistic(false_break_raw) * 100.0

        confidence = _clamp(35.0 + min(30.0, len(rows) * 3.0) + (15.0 if rich_quote else 0.0) + (10.0 if rvol is not None else 0.0) - len(missing) * 6.0)
        reasons: list[str] = []
        if efficiency >= 0.65:
            reasons.append("Opening price progress is efficient rather than oscillating.")
        if rvol is not None and rvol >= 1.5:
            reasons.append("Time-of-day volume participation is elevated.")
        if vwap is not None and ((direction == "LONG" and last_close >= vwap) or (direction == "SHORT" and last_close <= vwap)):
            reasons.append("Price is holding on the directional side of session VWAP.")
        if rejection >= 0.35:
            reasons.append("Latest completed bar shows material rejection wick.")
        if directional_acceleration < -0.05:
            reasons.append("Directional momentum is decelerating.")
        if spread is not None and spread > 12:
            reasons.append("Spread is wide for an early-session entry.")

        climax_risk_flag = bool((climax >= 68.0 or false_break >= 72.0) and confidence >= 45.0)
        if climax_risk_flag:
            state = "CLIMAX_RISK" if climax >= false_break else "FALSE_BREAK_RISK"
        elif continuation >= 68.0 and confidence >= 50.0 and direction != "NEUTRAL":
            state = "OPENING_DRIVE_CONTINUATION"
        elif efficiency < 0.30 and abs(signed_return) < 0.35:
            state = "CHOP"
        else:
            state = "BUILDING"
        momentum = _clamp((continuation - climax) * (1.0 if direction != "SHORT" else -1.0), -100.0, 100.0)
        return OpeningAssessment(
            state=state,
            continuation_score=continuation,
            climax_score=climax,
            false_break_score=false_break,
            confidence=confidence,
            direction=direction,
            time_of_day_rvol=rvol,
            vwap=vwap,
            price_efficiency=efficiency,
            momentum_score=momentum,
            climax_risk_flag=climax_risk_flag,
            reasons=tuple(reasons[:6]),
            missing=tuple(sorted(set(missing))),
        ).as_dict()
