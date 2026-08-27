"""Canonical compression -> expansion evidence from completed OHLCV candles.

This authority intentionally does *not* treat low ATR as bullish.  Compression is
only a setup state.  Directional evidence appears only when range/ATR expand and
price confirms structural release or a governed retest.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import statistics
from typing import Any, Dict, List, Optional

from core.indicator_snapshot_authority import true_ranges as canonical_true_ranges, wilder_series
from core.numeric_semantics import finite_number, nonnegative_number, positive_number

VERSION = "compression-expansion-authority-1.1.0-strict-finite-input"
MIN_ROWS = 24


def _f(value: Any) -> Optional[float]:
    return finite_number(value)


def _true_ranges(rows: List[Dict[str, Any]]) -> List[float]:
    clean = []
    for row in rows or []:
        values = {key: _f(row.get(key)) for key in ("high", "low", "close")}
        if None in values.values():
            continue
        clean.append(values)
    return canonical_true_ranges(clean)


def _wilder_atr(trs: List[float], period: int = 14) -> List[Optional[float]]:
    return wilder_series(list(trs or []), period)


def _mean(values: List[float]) -> Optional[float]:
    clean = [v for v in values if finite_number(v) is not None]
    return statistics.mean(clean) if clean else None


@dataclass(frozen=True)
class CompressionExpansionThresholds:
    compression_tr_atr: float = 0.72
    expansion_tr_atr: float = 1.05
    strong_expansion_tr_atr: float = 1.25
    positive_atr_slope: float = 0.0
    breakout_lookback: int = 20
    volume_participation_ratio: float = 1.20


class CompressionExpansionAuthority:
    version = VERSION

    def __init__(self, thresholds: CompressionExpansionThresholds | None = None) -> None:
        self.thresholds = thresholds or CompressionExpansionThresholds()

    def evaluate(self, candles: List[Dict[str, Any]]) -> Dict[str, Any]:
        rows = [dict(row) for row in candles or []]
        if len(rows) < MIN_ROWS:
            return {
                "ok": False,
                "state": "INSUFFICIENT_EVIDENCE",
                "direction": "neutral",
                "decision_usable": False,
                "production_weight": 0,
                "version": self.version,
                "required_rows": MIN_ROWS,
                "rows": len(rows),
            }

        # Never create a shortened mathematical series by silently dropping bad
        # candles. Every source bar used by compression/expansion must be valid.
        for index, row in enumerate(rows):
            o = positive_number(row.get("open"))
            h = positive_number(row.get("high"))
            l = positive_number(row.get("low"))
            c = positive_number(row.get("close"))
            v = nonnegative_number(row.get("volume"))
            if None in (o, h, l, c, v) or h < max(o, c, l) or l > min(o, c, h):
                return {
                    "ok": False, "state": "INVALID_SOURCE_EVIDENCE",
                    "direction": "neutral", "decision_usable": False,
                    "production_weight": 0, "version": self.version,
                    "invalid_row": index,
                }

        trs = _true_ranges(rows)
        atrs = _wilder_atr(trs, 14)
        if len(trs) != len(rows) or len(atrs) != len(rows) or atrs[-2] is None or atrs[-1] is None:
            return {
                "ok": False,
                "state": "INSUFFICIENT_ATR_HISTORY",
                "direction": "neutral",
                "decision_usable": False,
                "production_weight": 0,
                "version": self.version,
            }

        tr_now = trs[-1]
        tr_prev = trs[-2]
        atr_prior = float(atrs[-2])
        atr_now = float(atrs[-1])
        atr_prev2 = float(atrs[-3]) if len(atrs) >= 3 and atrs[-3] is not None else atr_prior
        tr_atr_ratio = tr_now / atr_prior if atr_prior > 0 else None
        prior_tr_mean = _mean(trs[-6:-1])
        tr_expansion_ratio = tr_now / prior_tr_mean if prior_tr_mean and prior_tr_mean > 0 else None
        atr_slope = (atr_now / atr_prior - 1.0) if atr_prior > 0 else None
        atr_acceleration = ((atr_now - atr_prior) - (atr_prior - atr_prev2)) / atr_prior if atr_prior > 0 else None

        lookback = min(self.thresholds.breakout_lookback, len(rows) - 1)
        prior_rows = rows[-(lookback + 1):-1]
        prior_high = max((_f(row.get("high")) for row in prior_rows if _f(row.get("high")) is not None), default=None)
        prior_low = min((_f(row.get("low")) for row in prior_rows if _f(row.get("low")) is not None), default=None)
        final = rows[-1]
        opened, high, low, close = (_f(final.get(key)) for key in ("open", "high", "low", "close"))
        previous = rows[-2]
        prev_close = _f(previous.get("close"))

        volumes = [_f(row.get("volume")) for row in rows[-21:-1]]
        volume_mean = _mean([v for v in volumes if v is not None])
        volume_now = _f(final.get("volume"))
        volume_ratio = volume_now / volume_mean if volume_now is not None and volume_mean and volume_mean > 0 else None

        recent_ratios: List[float] = []
        for offset in range(2, min(7, len(rows) - 14) + 1):
            atr_reference = atrs[-offset - 1]
            if atr_reference is not None and float(atr_reference) > 0:
                recent_ratios.append(trs[-offset] / float(atr_reference))
        prior_compression = bool(recent_ratios and min(recent_ratios) <= self.thresholds.compression_tr_atr)
        current_compression = bool(tr_atr_ratio is not None and tr_atr_ratio <= self.thresholds.compression_tr_atr)
        expansion = bool(
            tr_atr_ratio is not None
            and tr_atr_ratio >= self.thresholds.expansion_tr_atr
            and atr_slope is not None
            and atr_slope > self.thresholds.positive_atr_slope
        )
        strong_expansion = bool(tr_atr_ratio is not None and tr_atr_ratio >= self.thresholds.strong_expansion_tr_atr and expansion)
        bullish_break = bool(close is not None and prior_high is not None and close > prior_high)
        bearish_break = bool(close is not None and prior_low is not None and close < prior_low)
        upside_probe_failed = bool(high is not None and prior_high is not None and high > prior_high and close is not None and close <= prior_high)
        downside_probe_failed = bool(low is not None and prior_low is not None and low < prior_low and close is not None and close >= prior_low)

        # A retest is defined mechanically against the previous completed bar's
        # structural break, never inferred from future candles.
        prev_prior = rows[-(lookback + 2):-2] if len(rows) >= lookback + 2 else rows[:-2]
        prev_prior_high = max((_f(row.get("high")) for row in prev_prior if _f(row.get("high")) is not None), default=None)
        prev_prior_low = min((_f(row.get("low")) for row in prev_prior if _f(row.get("low")) is not None), default=None)
        prev_bull_break = bool(prev_close is not None and prev_prior_high is not None and prev_close > prev_prior_high)
        prev_bear_break = bool(prev_close is not None and prev_prior_low is not None and prev_close < prev_prior_low)
        bullish_retest_hold = bool(prev_bull_break and low is not None and prev_prior_high is not None and low <= prev_prior_high and close is not None and close >= prev_prior_high)
        bearish_retest_hold = bool(prev_bear_break and high is not None and prev_prior_low is not None and high >= prev_prior_low and close is not None and close <= prev_prior_low)

        direction = "neutral"
        state = "NORMAL"
        decision_usable = False
        if upside_probe_failed or downside_probe_failed:
            state = "FAILED_EXPANSION"
            direction = "bearish" if upside_probe_failed else "bullish"
            decision_usable = True
        elif bullish_retest_hold or bearish_retest_hold:
            state = "RETEST_HOLD"
            direction = "bullish" if bullish_retest_hold else "bearish"
            decision_usable = True
        elif expansion and (bullish_break or bearish_break):
            state = "BREAKOUT_CONFIRMED"
            direction = "bullish" if bullish_break else "bearish"
            decision_usable = True
        elif expansion:
            state = "EXPANSION_STARTING"
            direction = "bullish" if close is not None and opened is not None and close > opened else "bearish" if close is not None and opened is not None and close < opened else "neutral"
            decision_usable = False
        elif current_compression:
            participating = bool(volume_ratio is not None and volume_ratio >= self.thresholds.volume_participation_ratio)
            state = "ACCUMULATION_IN_COMPRESSION" if participating else "COMPRESSION"
            direction = "neutral"
            decision_usable = False
        elif prior_compression and tr_atr_ratio is not None and tr_atr_ratio > self.thresholds.compression_tr_atr:
            state = "COMPRESSION_RELEASING"

        return {
            "ok": True,
            "state": state,
            "direction": direction,
            "decision_usable": decision_usable,
            # Unvalidated new evidence is explicit shadow evidence. Existing
            # production score is not silently changed by introducing it.
            "production_weight": 0,
            "validation_state": "SHADOW_AWAITING_WALK_FORWARD_VALIDATION",
            "metrics": {
                "true_range": round(tr_now, 8),
                "previous_true_range": round(tr_prev, 8),
                "atr14_prior": round(atr_prior, 8),
                "atr14": round(atr_now, 8),
                "tr_atr_ratio": round(tr_atr_ratio, 6) if tr_atr_ratio is not None else None,
                "tr_expansion_ratio": round(tr_expansion_ratio, 6) if tr_expansion_ratio is not None else None,
                "atr_slope": round(atr_slope, 8) if atr_slope is not None else None,
                "atr_acceleration": round(atr_acceleration, 8) if atr_acceleration is not None else None,
                "volume_ratio_20": round(volume_ratio, 6) if volume_ratio is not None else None,
                "prior_high": prior_high,
                "prior_low": prior_low,
                "prior_compression": prior_compression,
                "strong_expansion": strong_expansion,
            },
            "evidence": {
                "current_compression": current_compression,
                "range_expansion": expansion,
                "bullish_breakout": bullish_break,
                "bearish_breakdown": bearish_break,
                "bullish_retest_hold": bullish_retest_hold,
                "bearish_retest_hold": bearish_retest_hold,
                "upside_failed_expansion": upside_probe_failed,
                "downside_failed_expansion": downside_probe_failed,
            },
            "semantics": "Narrow TR is setup evidence only; directional confirmation requires widening range/ATR plus structural release/retest.",
            "version": self.version,
            "observed_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        }


DEFAULT_COMPRESSION_EXPANSION_AUTHORITY = CompressionExpansionAuthority()
