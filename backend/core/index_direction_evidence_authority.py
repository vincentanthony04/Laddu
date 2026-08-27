"""Canonical signed market-direction evidence for index/sector snapshots.

This authority is an extraction of the established v106/v107 completed-daily
Trend/Momentum/Participation formula.  The formula is deliberately preserved;
the architectural change is that it now lives in core rather than an HTTP
route/browser projection.

Breadth/session gating remains owned by MarketContextSnapshotAuthority.  This
module provides the structural evidence and a deterministic direction/
conviction projection once that gate is open.
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping

from core.indicator_snapshot_authority import ema_series
from core.numeric_semantics import finite_number

AUTHORITY_NAME = "IndexDirectionEvidenceAuthority"
AUTHORITY_VERSION = "1.2.0-canonical-indicator-fail-closed-input"
EVIDENCE_MODEL = "independent_completed_daily_trend_momentum_participation_v1"


def _num(value: Any) -> float | None:
    return finite_number(value)


def _ema_values(values: list[float], period: int) -> list[float | None]:
    # Canonical SMA-seeded EMA; no alternate index-specific seed convention.
    return list(ema_series(values, period))


class IndexDirectionEvidenceAuthority:
    authority = AUTHORITY_NAME
    authority_version = AUTHORITY_VERSION
    _ema_values = staticmethod(_ema_values)

    def scores(self, candles: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
        closes: list[float] = []
        volumes: list[float | None] = []
        source_time = None
        source_rows = list(candles or ())
        for index, candle in enumerate(source_rows):
            close = _num(candle.get("close"))
            if close is None or close <= 0:
                return {
                    "trend_score": None, "momentum_score": None, "participation_score": None, "confluence": None,
                    "trend": "unavailable", "momentum": "unavailable", "evidence_quality": 0.0,
                    "evidence_reason": f"invalid/non-finite close at row {index}", "evidence_bars": 0,
                    "evidence_model": EVIDENCE_MODEL, "evidence_authority": self.authority,
                    "evidence_authority_version": self.authority_version, "evidence_source_time": source_time,
                    "participation_evidence_complete": False,
                }
            closes.append(close)
            volumes.append(_num(candle.get("volume")))
            source_time = candle.get("timestamp") or candle.get("time") or candle.get("date") or source_time
        if len(closes) < 21:
            return {
                "trend_score": None,
                "momentum_score": None,
                "participation_score": None,
                "confluence": None,
                "trend": "unavailable",
                "momentum": "unavailable",
                "evidence_quality": round(min(100.0, len(closes) * 100.0 / 21.0), 1),
                "evidence_reason": f"{len(closes)}/21 completed daily bars",
                "evidence_bars": len(closes),
                "evidence_model": EVIDENCE_MODEL,
                "evidence_authority": self.authority,
                "evidence_authority_version": self.authority_version,
                "evidence_source_time": source_time,
            }

        ema9 = _ema_values(closes, 9)
        ema21 = _ema_values(closes, 21)
        ema50 = _ema_values(closes, 50)
        latest = closes[-1]
        trend_parts: list[float] = []
        if len(closes) >= 50:
            if latest > ema9[-1] > ema21[-1] > ema50[-1]:
                trend_parts.append(45.0)
            elif latest < ema9[-1] < ema21[-1] < ema50[-1]:
                trend_parts.append(-45.0)
            else:
                trend_parts.append(max(-25.0, min(25.0, (ema9[-1] - ema21[-1]) * 2500.0 / max(latest, 1.0))))
            slope_base = ema21[-6] if len(ema21) >= 6 else ema21[0]
            trend_parts.append(max(-25.0, min(25.0, (ema21[-1] / slope_base - 1.0) * 700.0)))
        else:
            trend_parts.append(max(-35.0, min(35.0, (ema9[-1] - ema21[-1]) * 2500.0 / max(latest, 1.0))))
        ret20 = (latest / closes[-21] - 1.0) * 100.0
        trend_parts.append(max(-30.0, min(30.0, ret20 * 3.0)))
        recent = closes[-10:]
        higher_highs = sum(1 for a, b in zip(recent, recent[1:]) if b > a)
        lower_lows = sum(1 for a, b in zip(recent, recent[1:]) if b < a)
        trend_parts.append(max(-15.0, min(15.0, (higher_highs - lower_lows) * 2.5)))
        trend_score = max(-100.0, min(100.0, sum(trend_parts)))

        roc5 = (latest / closes[-6] - 1.0) * 100.0 if len(closes) >= 6 else 0.0
        roc10 = (latest / closes[-11] - 1.0) * 100.0 if len(closes) >= 11 else roc5
        prior5 = (closes[-6] / closes[-11] - 1.0) * 100.0 if len(closes) >= 11 else 0.0
        acceleration = roc5 - prior5
        momentum_score = max(-100.0, min(100.0, roc5 * 8.0 + roc10 * 3.0 + acceleration * 5.0))

        participation_score = None
        recent_volumes = volumes[-20:] if len(volumes) >= 20 else []
        if len(recent_volumes) == 20 and all(value is not None and value >= 0 for value in recent_volumes):
            avg20 = sum(float(value) for value in recent_volumes) / 20.0
            ratio = float(recent_volumes[-1]) / avg20 if avg20 > 0 else None
            if ratio is not None:
                direction_sign = 1.0 if roc5 >= 0 else -1.0
                participation_score = direction_sign * max(0.0, min(100.0, (ratio - 0.6) * 80.0))
        quality = min(100.0, len(closes) * 2.0)
        confluence = (
            max(-100.0, min(100.0, trend_score * 0.55 + momentum_score * 0.35 + participation_score * 0.10))
            if participation_score is not None else None
        )
        return {
            "trend_score": round(trend_score, 1),
            "momentum_score": round(momentum_score, 1),
            "participation_score": round(participation_score, 1) if participation_score is not None else None,
            "confluence": round(confluence, 1) if confluence is not None else None,
            "trend": "bullish" if trend_score > 8 else "bearish" if trend_score < -8 else "neutral",
            "momentum": "bullish" if momentum_score > 8 else "bearish" if momentum_score < -8 else "neutral",
            "evidence_quality": round(quality, 1),
            "evidence_bars": len(closes),
            "evidence_model": EVIDENCE_MODEL,
            "evidence_authority": self.authority,
            "evidence_authority_version": self.authority_version,
            "evidence_source_time": source_time,
            "participation_evidence_complete": participation_score is not None,
        }

    def project_direction(self, row: Mapping[str, Any]) -> dict[str, Any]:
        """Project the established browser semantics on the server.

        The atomic market snapshot decides whether price+breadth are authorized.
        We additionally require signed structural evidence.  Conviction remains
        the absolute confluence strength, matching the existing UI semantics.
        """
        if row.get("direction_authority_ready") is not True:
            return self._unavailable(str(row.get("direction_authority_reason") or "market-context gate unavailable"))
        trend_score = _num(row.get("trend_score"))
        momentum_score = _num(row.get("momentum_score"))
        confluence = _num(row.get("confluence"))
        trend_state = str(row.get("trend") or "").strip().lower()
        signed = trend_score if trend_score is not None else confluence if confluence is not None else momentum_score
        if signed is None:
            return self._unavailable("completed-daily signed direction evidence unavailable")
        if "bear" in trend_state or "down" in trend_state or "negative" in trend_state:
            direction = "Bearish"
        elif any(token in trend_state for token in ("neutral", "mixed", "flat", "sideways")):
            direction = "Neutral"
        elif any(token in trend_state for token in ("bull", "up", "positive")):
            direction = "Bullish"
        else:
            direction = "Bullish" if signed > 5 else "Bearish" if signed < -5 else "Neutral"
        raw_conviction = confluence if confluence is not None else trend_score if trend_score is not None else momentum_score
        conviction = None if raw_conviction is None else round(max(0.0, min(100.0, abs(raw_conviction))), 1)
        return {
            "direction": direction,
            "market_direction": direction,
            "direction_state": direction.upper(),
            "conviction": conviction,
            "direction_evidence_authority": self.authority,
            "direction_evidence_authority_version": self.authority_version,
            "direction_evidence_model": str(row.get("evidence_model") or EVIDENCE_MODEL),
        }

    def _unavailable(self, reason: str) -> dict[str, Any]:
        return {
            "direction": None,
            "market_direction": None,
            "direction_state": "UNAVAILABLE",
            "conviction": None,
            "direction_evidence_authority": self.authority,
            "direction_evidence_authority_version": self.authority_version,
            "direction_evidence_reason": reason,
        }


DEFAULT_INDEX_DIRECTION_EVIDENCE_AUTHORITY = IndexDirectionEvidenceAuthority()
