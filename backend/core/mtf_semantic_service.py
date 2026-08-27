"""Composite completed-candle MTF intelligence for all chart timeframes.

A timeframe colour is the bounded composite of trend, momentum,
participation, structure and data quality. The same raw timeframe result is
used by Intraday and Delivery; desk aggregation is applied elsewhere.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import math
from typing import Any, Dict, Iterable, List, Mapping

from core.india_time import INDIA_TZ
from core.indicator_snapshot_authority import DEFAULT_INDICATOR_SNAPSHOT_AUTHORITY
from core.numeric_semantics import finite_number

FRAME_ROSTER = ("1m", "3m", "5m", "15m", "30m", "1H", "4H", "1D", "1W")
ANALYTICAL_FRAME_ROSTER = FRAME_ROSTER + ("1M",)
FRAME_WEIGHTS = {
    "1m": .04, "3m": .05, "5m": .08, "15m": .12, "30m": .13,
    "1H": .14, "4H": .16, "1D": .17, "1W": .08, "1M": .03,
}
FRAME_SECONDS = {
    "1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800,
    "1H": 3600, "4H": 14400, "1D": 86400, "1W": 604800, "1M": 2678400,
}
SEMANTIC_MODEL = "laddu-composite-mtf-2.0.0"


def _number(value: Any) -> float | None:
    return finite_number(value)


def _clip(value: float, low: float = -100.0, high: float = 100.0) -> float:
    return max(low, min(high, float(value)))


def _time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        out = value
    elif isinstance(value, (int, float)):
        out = datetime.fromtimestamp(float(value) / (1000.0 if value > 10_000_000_000 else 1.0), timezone.utc)
    else:
        try:
            out = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except Exception:
            return None
    if out.tzinfo is None:
        out = out.replace(tzinfo=INDIA_TZ)
    return out.astimezone(timezone.utc)


# All EMA/RSI/ATR/DMI/MACD/Supertrend mathematics are owned by
# IndicatorSnapshotAuthority. MTF semantics consumes its versioned snapshot and
# deliberately contains no duplicate indicator implementation.

def _explicitly_completed(raw: Dict[str, Any], timestamp: datetime, tf: str, current: datetime) -> bool:
    if raw.get("session_partial") is True or raw.get("pattern_eligible") is False:
        return False
    if raw.get("forming") is True or raw.get("is_closed") is False:
        return False
    if raw.get("is_closed") is True:
        return True
    period_end = _time(raw.get("period_end") or raw.get("bar_end"))
    if period_end is not None:
        return period_end <= current
    return timestamp + timedelta(seconds=FRAME_SECONDS[tf]) <= current


def _state(score: float, confidence: float) -> str:
    if confidence < 30:
        return "LOW_CONFIDENCE"
    if score >= 62:
        return "STRONG_BULLISH"
    if score >= 22:
        return "BULLISH"
    if score <= -62:
        return "STRONG_BEARISH"
    if score <= -22:
        return "BEARISH"
    if abs(score) >= 8:
        return "TRANSITION"
    return "NEUTRAL_MIXED"


class MtfSemanticService:
    MIN_CANDLES = 55

    @staticmethod
    def _pending(tf: str, reason: str, state: str = "PENDING", last_completed_at: str | None = None) -> Dict[str, Any]:
        return {
            "tf": tf, "state": state, "direction": 0, "strength": 0, "score": 0,
            "desk_directional_score": None, "desk_directional_score_scale": "-6_to_+6_from_composite",
            "trend_score": 0, "momentum_score": 0, "participation_score": 0,
            "participation_direction": 0, "structure_score": 0, "quality_score": 0,
            "composite_score": 0, "confidence": 0, "coverage": 0.0,
            "components": {"trend": None, "momentum": None, "participation": None, "structure": None, "quality": None},
            "metrics": {}, "last_completed_at": last_completed_at, "freshness": state,
            "reason": reason, "semantic_model": SEMANTIC_MODEL,
        }

    def evaluate_frame(
        self,
        tf: str,
        candles: Iterable[Dict[str, Any]],
        *,
        as_of: datetime | None = None,
        stale_after_seconds: float | None = None,
    ) -> Dict[str, Any]:
        if tf not in ANALYTICAL_FRAME_ROSTER:
            raise ValueError(f"unsupported timeframe {tf}")
        current = as_of or datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=INDIA_TZ)
        current = current.astimezone(timezone.utc)
        rows: List[Dict[str, Any]] = []
        for raw in candles or []:
            timestamp = _time(raw.get("timestamp", raw.get("time", raw.get("date"))))
            values = {name: _number(raw.get(name)) for name in ("open", "high", "low", "close", "volume")}
            if timestamp is None or any(values[name] is None for name in ("open", "high", "low", "close")):
                continue
            if not _explicitly_completed(raw, timestamp, tf, current):
                continue
            rows.append({**values, "timestamp": timestamp, "source": raw.get("source"), "is_closed": True})
        rows.sort(key=lambda row: row["timestamp"])
        last_at = rows[-1]["timestamp"] if rows else None
        last_text = last_at.astimezone(INDIA_TZ).isoformat(timespec="seconds") if last_at else None
        if len(rows) < self.MIN_CANDLES:
            return self._pending(tf, f"need {self.MIN_CANDLES} completed candles; have {len(rows)}", last_completed_at=last_text)
        age = (current - last_at).total_seconds() if last_at else float("inf")
        threshold = stale_after_seconds if stale_after_seconds is not None else max(15 * 60, FRAME_SECONDS[tf] * 4)
        if as_of is not None and age > threshold:
            return self._pending(tf, f"last completed candle is {int(age)}s old", state="STALE", last_completed_at=last_text)

        closes = [row["close"] for row in rows]
        indicator_snapshot = DEFAULT_INDICATOR_SNAPSHOT_AUTHORITY.calculate(rows)
        indicator_metrics = dict(indicator_snapshot.get("metrics") or {})
        indicator_series = dict(indicator_snapshot.get("series") or {})
        ema9s = list(indicator_series.get("ema9") or [])
        ema21s = list(indicator_series.get("ema21") or [])
        ema50s = list(indicator_series.get("ema50") or [])
        rsi_values = list(indicator_series.get("rsi14") or [])
        ema9, ema21, ema50 = indicator_metrics.get("ema9"), indicator_metrics.get("ema21"), indicator_metrics.get("ema50")
        latest = closes[-1]
        required = {
            "ema9": _number(indicator_metrics.get("ema9")),
            "ema21": _number(indicator_metrics.get("ema21")),
            "ema50": _number(indicator_metrics.get("ema50")),
            "atr14": _number(indicator_metrics.get("atr14")),
            "plus_di14": _number(indicator_metrics.get("plus_di14")),
            "minus_di14": _number(indicator_metrics.get("minus_di14")),
            "adx14": _number(indicator_metrics.get("adx14")),
            "rsi14": _number(indicator_metrics.get("rsi14")),
            "macd": _number(indicator_metrics.get("macd")),
            "macd_signal": _number(indicator_metrics.get("macd_signal")),
            "macd_hist": _number(indicator_metrics.get("macd_hist")),
            "supertrend_value": _number(indicator_metrics.get("supertrend_value")),
        }
        missing = [name for name, value in required.items() if value is None]
        st_raw = indicator_metrics.get("supertrend_direction")
        st_num = _number(st_raw)
        if st_num not in {-1.0, 1.0}:
            missing.append("supertrend_direction")
        series_ready = (
            len(ema9s) >= 6 and len(ema21s) >= 6 and len(ema50s) >= 6 and len(rsi_values) >= 4
            and all(_number(series[-1]) is not None and _number(series[-6]) is not None for series in (ema9s, ema21s, ema50s))
            and _number(rsi_values[-1]) is not None and _number(rsi_values[-4]) is not None
        )
        if not series_ready:
            missing.append("indicator_series_history")
        if required["atr14"] is not None and required["atr14"] <= 0:
            missing.append("atr14_positive")
        if missing:
            pending = self._pending(
                tf,
                "canonical indicator evidence incomplete: " + ", ".join(sorted(set(missing))),
                state="UNAVAILABLE",
                last_completed_at=last_text,
            )
            pending["metrics"] = {name: value for name, value in required.items()}
            pending["indicator_authority"] = indicator_snapshot.get("authority")
            pending["indicator_authority_version"] = indicator_snapshot.get("authority_version")
            return pending

        ema9, ema21, ema50 = required["ema9"], required["ema21"], required["ema50"]
        atr14 = required["atr14"]
        pdi, mdi, adx = required["plus_di14"], required["minus_di14"], required["adx14"]
        rsi14 = required["rsi14"]
        rsi_slope = _number(rsi_values[-1]) - _number(rsi_values[-4])
        macd_line, macd_signal, macd_hist = required["macd"], required["macd_signal"], required["macd_hist"]
        st_direction, st_value = int(st_num), required["supertrend_value"]

        stack = 0.0
        if None not in (ema9, ema21, ema50):
            if latest > ema9 > ema21 > ema50:
                stack = 55.0
            elif latest < ema9 < ema21 < ema50:
                stack = -55.0
            else:
                stack = _clip(((latest - ema21) / atr14) * 18.0, -28, 28)
                stack += 14.0 if ema9 > ema21 else -14.0
                stack += 10.0 if ema21 > ema50 else -10.0
        slope = 0.0
        for series, weight in ((ema9s, 8.0), (ema21s, 9.0), (ema50s, 8.0)):
            if series[-1] is not None and series[-6] is not None:
                slope += _clip((series[-1] - series[-6]) / atr14 * weight, -weight, weight)
        dmi = 0.0
        if pdi is not None and mdi is not None and adx is not None:
            dmi = (1.0 if pdi >= mdi else -1.0) * min(20.0, max(4.0, adx * .5))
        trend_score = _clip(stack * .62 + slope + dmi + st_direction * 10.0)

        rsi_component = _clip((rsi14 - 50.0) * 1.25 + rsi_slope * 1.5, -35, 35)
        macd_component = _clip((macd_hist / atr14) * 120.0, -28, 28)
        roc5 = ((latest / closes[-6]) - 1.0) * 100.0 if closes[-6] else 0.0
        roc10 = ((latest / closes[-11]) - 1.0) * 100.0 if closes[-11] else 0.0
        velocity = _clip(roc5 * 8.0, -24, 24)
        acceleration = _clip((roc5 - roc10 / 2.0) * 6.0, -13, 13)
        body = rows[-1]["close"] - rows[-1]["open"]
        displacement = _clip(body / atr14 * 12.0, -10, 10)
        momentum_score = _clip(rsi_component + macd_component + velocity + acceleration + displacement)

        recent, previous = rows[-8:], rows[-16:-8]
        hh = max(row["high"] for row in recent) > max(row["high"] for row in previous)
        hl = min(row["low"] for row in recent) > min(row["low"] for row in previous)
        lh = max(row["high"] for row in recent) < max(row["high"] for row in previous)
        ll = min(row["low"] for row in recent) < min(row["low"] for row in previous)
        structure_score = 60.0 if hh and hl else -60.0 if lh and ll else 18.0 if hh else -18.0 if ll else 0.0
        lookback_high = max(row["high"] for row in rows[-31:-1])
        lookback_low = min(row["low"] for row in rows[-31:-1])
        if latest > lookback_high:
            structure_score += min(35.0, (latest - lookback_high) / atr14 * 20.0)
        elif latest < lookback_low:
            structure_score -= min(35.0, (lookback_low - latest) / atr14 * 20.0)
        structure_score = _clip(structure_score)

        volumes = [row["volume"] for row in rows if row.get("volume") is not None and row.get("volume") >= 0]
        rvol = None
        participation_score = participation_direction = 0.0
        if len(volumes) >= 21:
            average = sum(volumes[-21:-1]) / 20.0
            rvol = volumes[-1] / average if average > 0 else None
            if rvol is not None:
                participation_score = _clip(35.0 + (rvol - 1.0) * 45.0, 0, 100)
                participation_direction = participation_score * (1.0 if body >= 0 else -1.0)

        coverage_ratio = min(1.0, len(rows) / 120.0)
        volume_ratio = min(1.0, len(volumes) / len(rows)) if rows else 0.0
        freshness_ratio = 1.0 if as_of is None else max(0.0, 1.0 - age / max(threshold * 2.0, 1.0))
        quality_score = _clip(coverage_ratio * 52.0 + volume_ratio * 23.0 + freshness_ratio * 25.0, 0, 100)
        composite = _clip(trend_score * .40 + momentum_score * .29 + structure_score * .19 + participation_direction * .12)
        agreement = 1.0 - min(1.0, abs(trend_score - momentum_score) / 160.0)
        confidence = _clip(quality_score * (.66 + .34 * agreement), 0, 100)
        state = _state(composite, confidence)
        direction = 1 if composite >= 12 and confidence >= 30 else -1 if composite <= -12 and confidence >= 30 else 0
        strength = round(min(100.0, abs(composite) * (.72 + confidence / 360.0)))
        ema_state = (
            "BULLISH_STACK" if None not in (ema9, ema21, ema50) and ema9 > ema21 > ema50
            else "BEARISH_STACK" if None not in (ema9, ema21, ema50) and ema9 < ema21 < ema50
            else "MIXED"
        )
        return {
            "tf": tf, "state": state, "direction": direction, "strength": strength,
            "score": round(composite, 2), "composite_score": round(composite, 2),
            "desk_directional_score": round(round(_clip(composite, -100.0, 100.0), 2) * 0.06, 4),
            "desk_directional_score_scale": "-6_to_+6_from_composite",
            "trend_score": round(trend_score, 2), "momentum_score": round(momentum_score, 2),
            "participation_score": round(participation_score, 2),
            "participation_direction": round(participation_direction, 2),
            "structure_score": round(structure_score, 2), "quality_score": round(quality_score, 2),
            "confidence": round(confidence, 2), "coverage": round(coverage_ratio, 4),
            "components": {
                "trend": round(trend_score, 2), "momentum": round(momentum_score, 2),
                "participation": round(participation_score, 2), "structure": round(structure_score, 2),
                "quality": round(quality_score, 2),
            },
            "metrics": {
                "ema9": round(ema9, 4) if ema9 is not None else None,
                "ema21": round(ema21, 4) if ema21 is not None else None,
                "ema50": round(ema50, 4) if ema50 is not None else None,
                "ema_state": ema_state,
                "rsi14": round(rsi14, 2) if rsi14 is not None else None,
                "rsi_slope": round(rsi_slope, 2),
                "macd": round(macd_line, 4) if macd_line is not None else None,
                "macd_signal": round(macd_signal, 4) if macd_signal is not None else None,
                "macd_hist": round(macd_hist, 4) if macd_hist is not None else None,
                "adx14": round(adx, 2) if adx is not None else None,
                "plus_di": round(pdi, 2) if pdi is not None else None,
                "minus_di": round(mdi, 2) if mdi is not None else None,
                "atr14": round(atr14, 4), "roc5_pct": round(roc5, 3),
                "rvol20": round(rvol, 3) if rvol is not None else None,
                "supertrend_direction": st_direction,
                "supertrend_value": round(st_value, 4) if st_value is not None else None,
            },
            "last_completed_at": last_text, "freshness": "CURRENT",
            "reason": "composite trend + momentum + participation + structure + quality",
            "semantic_model": SEMANTIC_MODEL,
            "indicator_authority": "IndicatorSnapshotAuthority",
            "indicator_authority_version": indicator_snapshot.get("authority_version"),
        }

    def evaluate_all(
        self,
        frame_candles: Mapping[str, Iterable[Dict[str, Any]]],
        *,
        as_of: datetime | None = None,
        include_monthly: bool = False,
    ) -> Dict[str, Any]:
        roster = ANALYTICAL_FRAME_ROSTER if include_monthly else FRAME_ROSTER
        frames = {tf: self.evaluate_frame(tf, frame_candles.get(tf) or [], as_of=as_of) for tf in roster}
        counts = {"bullish": 0, "bearish": 0, "neutral": 0, "pending": 0}
        available_weight = weighted = confidence_weighted = 0.0
        roster_weight = sum(FRAME_WEIGHTS[tf] for tf in roster)
        for tf, row in frames.items():
            state = str(row.get("state") or "")
            if "BULLISH" in state:
                counts["bullish"] += 1
            elif "BEARISH" in state:
                counts["bearish"] += 1
            elif state in {"NEUTRAL_MIXED", "TRANSITION", "LOW_CONFIDENCE"}:
                counts["neutral"] += 1
            else:
                counts["pending"] += 1
            coverage_value = _number(row.get("coverage"))
            composite_value = _number(row.get("composite_score"))
            confidence_value = _number(row.get("confidence"))
            if coverage_value is not None and coverage_value > 0 and composite_value is not None and confidence_value is not None:
                weight = FRAME_WEIGHTS[tf]
                available_weight += weight
                weighted += weight * composite_value
                confidence_weighted += weight * confidence_value
        coverage = round(available_weight / max(roster_weight, 1e-9), 4)
        alignment = round(weighted / max(roster_weight, 1e-9), 2)
        confidence = round(confidence_weighted / max(available_weight, 1e-9), 2) if available_weight else 0.0
        return {
            "frames": frames, "counts": counts, "coverage": coverage,
            "weighted_alignment": alignment, "confidence": confidence,
            "full_confluence": bool(coverage == 1.0 and (counts["bullish"] == len(roster) or counts["bearish"] == len(roster))),
            "roster": list(roster), "display_roster": list(FRAME_ROSTER),
            "analytical_roster": list(ANALYTICAL_FRAME_ROSTER), "semantic_model": SEMANTIC_MODEL,
        }
