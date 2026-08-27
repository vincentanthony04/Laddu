"""Point-in-time NSE institutional signal engine.

No signal is marked available until its minimum lookback exists.  All inputs
are persisted NSE delivery rows and daily OHLCV candles; no generated values.
"""
from __future__ import annotations

from datetime import datetime, timezone
import math
import statistics
from typing import Any, Dict, List, Optional

from core.compression_expansion_authority import DEFAULT_COMPRESSION_EXPANSION_AUTHORITY
from core.indicator_snapshot_authority import DEFAULT_INDICATOR_SNAPSHOT_AUTHORITY
from core.numeric_semantics import finite_number

MODEL_VERSION = "institutional-nse-1.4.0-canonical-atr-no-fallback"
MIN_DELIVERY_ROWS = 21
MIN_CANDLES = 35


def _f(v: Any) -> Optional[float]:
    return finite_number(v)


def _mean(xs: List[float]) -> Optional[float]:
    return statistics.mean(xs) if xs else None


def _z(value: Optional[float], baseline: List[float]) -> Optional[float]:
    if value is None or len(baseline) < 10:
        return None
    sd = statistics.pstdev(baseline)
    return 0.0 if sd == 0 and value == statistics.mean(baseline) else ((value - statistics.mean(baseline)) / sd if sd else None)


def analyze(symbol: str, delivery_rows: List[Dict[str, Any]], candles: List[Dict[str, Any]]) -> Dict[str, Any]:
    # Storage returns delivery newest-first and candles oldest-first.
    delivery = sorted((dict(r) for r in delivery_rows or []), key=lambda r: str(r.get("trade_date") or ""), reverse=True)
    daily = sorted((dict(c) for c in candles or []), key=lambda c: str(c.get("timestamp") or c.get("ts") or ""))
    coverage = {
        "delivery_rows": len(delivery), "required_delivery_rows": MIN_DELIVERY_ROWS,
        "candle_rows": len(daily), "required_candle_rows": MIN_CANDLES,
        "delivery_first": delivery[-1].get("trade_date") if delivery else None,
        "delivery_last": delivery[0].get("trade_date") if delivery else None,
        "candle_first": (daily[0].get("timestamp") if daily else None),
        "candle_last": (daily[-1].get("timestamp") if daily else None),
    }
    if len(delivery) < MIN_DELIVERY_ROWS or len(daily) < MIN_CANDLES:
        return {
            "ok": False, "state": "collecting_evidence", "bias": "neutral", "score": 0,
            "stage": "Unclassified", "signals": {"hidden_accumulation": False, "volume_climax": False, "absorption": False},
            "coverage": coverage, "model_version": MODEL_VERSION,
            "summary": f"Need {MIN_DELIVERY_ROWS} delivery days and {MIN_CANDLES} daily candles; have {len(delivery)} and {len(daily)}.",
        }

    # Institutional evidence is never repaired by silently dropping malformed
    # source rows.  The required 21 delivery observations and candle history
    # must be finite and internally valid before any z-score/ATR/stage math.
    for index, row in enumerate(delivery[:MIN_DELIVERY_ROWS]):
        deliverable = _f(row.get("deliverable_qty"))
        delivery_pct = _f(row.get("delivery_pct"))
        traded = _f(row.get("traded_qty"))
        if (
            deliverable is None or deliverable < 0
            or traded is None or traded < 0
            or delivery_pct is None or not (0.0 <= delivery_pct <= 100.0)
            or deliverable > traded
        ):
            return {
                "ok": False, "state": "invalid_evidence", "bias": "neutral", "score": 0,
                "stage": "Unclassified", "signals": {"hidden_accumulation": False, "volume_climax": False, "absorption": False},
                "coverage": coverage, "model_version": MODEL_VERSION,
                "summary": f"Invalid/non-finite delivery evidence at required row {index}.",
            }
    for index, row in enumerate(daily):
        o, h, l, c = (_f(row.get(name)) for name in ("open", "high", "low", "close"))
        if None in (o, h, l, c) or o <= 0 or h <= 0 or l <= 0 or c <= 0 or h < max(o, c, l) or l > min(o, c, h):
            return {
                "ok": False, "state": "invalid_evidence", "bias": "neutral", "score": 0,
                "stage": "Unclassified", "signals": {"hidden_accumulation": False, "volume_climax": False, "absorption": False},
                "coverage": coverage, "model_version": MODEL_VERSION,
                "summary": f"Invalid/non-finite OHLC evidence at candle row {index}.",
            }

    latest = delivery[0]
    qty_now = _f(latest.get("deliverable_qty"))
    pct_now = _f(latest.get("delivery_pct"))
    traded_now = _f(latest.get("traded_qty"))
    qty_base = [x for x in (_f(r.get("deliverable_qty")) for r in delivery[1:21]) if x is not None]
    pct_base = [x for x in (_f(r.get("delivery_pct")) for r in delivery[1:21]) if x is not None]
    traded_base = [x for x in (_f(r.get("traded_qty")) for r in delivery[1:21]) if x is not None]
    qty_z, pct_z, traded_z = _z(qty_now, qty_base), _z(pct_now, pct_base), _z(traded_now, traded_base)

    indicator_snapshot = DEFAULT_INDICATOR_SNAPSHOT_AUTHORITY.calculate(daily[-70:])
    if indicator_snapshot.get("state") != "READY" or indicator_snapshot.get("decision_usable") is not True:
        return {
            "ok": False, "state": "invalid_evidence", "bias": "neutral", "score": 0,
            "stage": "Unclassified", "signals": {"hidden_accumulation": False, "volume_climax": False, "absorption": False},
            "coverage": coverage, "model_version": MODEL_VERSION,
            "summary": "Canonical indicator evidence unavailable for institutional ATR.",
        }
    atr_series = list((indicator_snapshot.get("series") or {}).get("atr14") or [])
    atrs = [value for value in atr_series if _f(value) is not None]
    atr_now = _f((indicator_snapshot.get("metrics") or {}).get("atr14"))
    atr_base = atrs[-21:-1] if len(atrs) >= 21 else atrs[:-1]
    atr_mean = _mean(atr_base)
    if atr_now is None or atr_now <= 0 or atr_mean is None or atr_mean <= 0:
        return {
            "ok": False, "state": "invalid_evidence", "bias": "neutral", "score": 0,
            "stage": "Unclassified", "signals": {"hidden_accumulation": False, "volume_climax": False, "absorption": False},
            "coverage": coverage, "model_version": MODEL_VERSION,
            "summary": "Canonical ATR evidence is incomplete/non-positive; institutional volatility math unavailable.",
        }
    atr_ratio = atr_now / atr_mean
    compression_expansion = DEFAULT_COMPRESSION_EXPANSION_AUTHORITY.evaluate(daily)
    compression_state = str(compression_expansion.get("state") or "NORMAL").upper() if compression_expansion.get("ok") is True else "UNAVAILABLE"
    close_now = _f(daily[-1].get("close"))
    close_prev = _f(daily[-2].get("close"))
    ret_pct = ((close_now / close_prev) - 1) * 100 if close_now and close_prev else None
    atr_pct = atr_now / close_now * 100 if atr_now is not None and close_now else None
    quiet_ratio = abs(ret_pct) / atr_pct if ret_pct is not None and atr_pct else None
    pct_avg = _mean(pct_base)

    # v43.1: the original gate (`atr_ratio < 1.0`, i.e. "volatility currently
    # contracting vs its own 20d average") tested as *anti-predictive* on its
    # own (-0.10% mean fwd-5d vs +0.16% baseline) and actively harmful when
    # combined with the volume-z condition (-0.44%), on real NSE production
    # data -- see VALIDATION_FINDINGS_2026-07-18.md section 2. Low ATR turned
    # out to catch stocks stalling/dying as often as it caught genuine
    # coiling-before-breakout. Replaced with a simple trend-floor: price not
    # in a confirmed 20d downtrend. This tested closer to neutral (not
    # harmful) rather than a proven positive edge on its own -- the
    # continuous `delivery_pct_excess` score below is the piece with
    # confirmed cross-period signal (IR 0.5-1.0+ on a wide, liquid universe;
    # collapses on a narrow one -- see sections 1/5/13/14) and should be
    # preferred over this boolean gate for any new ranking/promotion logic.
    close_20d_ago = _f(daily[-21].get("close")) if len(daily) >= 21 else None
    trend_not_down = bool(close_now is not None and close_20d_ago is not None and close_now >= close_20d_ago)
    delivery_pct_excess = (pct_now - pct_avg) if (pct_now is not None and pct_avg is not None) else None

    hidden = bool(qty_z is not None and qty_z >= 2.0 and trend_not_down and pct_now is not None and pct_avg is not None and pct_now > pct_avg)
    absorption = bool(qty_z is not None and qty_z >= 1.5 and quiet_ratio is not None and quiet_ratio <= 0.65 and pct_now is not None and pct_avg is not None and pct_now >= pct_avg)
    volume_climax = bool(traded_z is not None and traded_z >= 2.0 and qty_z is not None and qty_z >= 1.5)

    # Delivery-weighted average price and an ATR-derived support zone.
    by_date = {str(r.get("trade_date"))[:10]: r for r in delivery[:20]}
    weighted, weight = 0.0, 0.0
    for c in daily[-35:]:
        day = str(c.get("timestamp") or c.get("ts") or "")[:10]
        row, close = by_date.get(day), _f(c.get("close"))
        q = _f((row or {}).get("deliverable_qty"))
        if close is not None and q is not None and q > 0:
            weighted += close * q; weight += q
    dwap = weighted / weight if weight else None
    zone_half = atr_now * 0.5 if atr_now is not None else None

    closes = [x for x in (_f(c.get("close")) for c in daily[-21:]) if x is not None]
    trend_return = ((closes[-1] / closes[0]) - 1) * 100 if len(closes) >= 2 and closes[0] else None
    above_dwap = bool(close_now is not None and dwap is not None and close_now >= dwap)
    distribution = bool(qty_z is not None and qty_z >= 1.5 and ret_pct is not None and atr_pct is not None and atr_pct > 0 and ret_pct < -0.5 * atr_pct)
    if volume_climax:
        stage = "Climax"
    elif distribution:
        stage = "Distribution"
    elif hidden and above_dwap and (trend_return or 0) > 0:
        stage = "Institutional Trend"
    elif hidden:
        stage = "Confirmed Accumulation"
    elif absorption or (qty_z is not None and qty_z >= 1.0 and compression_state in {"COMPRESSION", "ACCUMULATION_IN_COMPRESSION"}):
        stage = "Silent Accumulation"
    elif compression_state in {"BREAKOUT_CONFIRMED", "RETEST_HOLD"} and above_dwap and (trend_return or 0) >= 0:
        stage = "Expansion Confirmation"
    elif above_dwap and (trend_return or 0) > 2:
        stage = "Markup"
    elif compression_state == "COMPRESSION":
        stage = "Compression"
    else:
        stage = "Dormant"

    score = 0.0
    score += max(0, min(30, (qty_z or 0) * 12))
    score += 15 if pct_now is not None and pct_avg is not None and pct_now > pct_avg else 0
    # Compression/expansion is currently SHADOW_ONLY (production_weight=0).
    # It may be displayed and measured but must not alter production score until
    # its exact contract earns empirical qualification.
    compression_points = 0
    score += 15 if absorption else 0
    score += 10 if above_dwap else 0
    score += 10 if volume_climax else 0
    score += 5 if (trend_return or 0) > 0 else 0
    score = int(round(max(0, min(100, score))))
    bias = "distribution" if distribution else "accumulation" if score >= 60 else "supportive" if score >= 35 else "neutral"
    return {
        "ok": True, "state": "ready", "symbol": symbol.upper(), "bias": bias, "score": score, "stage": stage,
        "price": close_now, "signal_date": str(latest.get("trade_date") or "")[:10],
        "signals": {"hidden_accumulation": hidden, "volume_climax": volume_climax, "absorption": absorption},
        "delivery": {"latest_pct": pct_now, "average_20d_pct": round(pct_avg, 2) if pct_avg is not None else None,
                     "deliverable_qty": qty_now, "deliverable_qty_z20": round(qty_z, 3) if qty_z is not None else None,
                     "traded_qty_z20": round(traded_z, 3) if traded_z is not None else None,
                     "pct_excess_20d": round(delivery_pct_excess, 3) if delivery_pct_excess is not None else None},
        "volatility": {"atr14": round(atr_now, 4) if atr_now is not None else None,
                       "indicator_authority": indicator_snapshot.get("authority"),
                       "indicator_authority_version": indicator_snapshot.get("authority_version"),
                       "atr_vs_20d_average": round(atr_ratio, 4) if atr_ratio is not None else None,
                       "price_move_pct": round(ret_pct, 3) if ret_pct is not None else None,
                       "price_move_to_atr": round(quiet_ratio, 3) if quiet_ratio is not None else None,
                       "compression_expansion": compression_expansion,
                       "compression_production_points": compression_points},
        "dwap": {"value": round(dwap, 2) if dwap is not None else None,
                 "support_low": round(dwap-zone_half, 2) if dwap is not None and zone_half is not None else None,
                 "support_high": round(dwap+zone_half, 2) if dwap is not None and zone_half is not None else None,
                 "above": above_dwap},
        "coverage": coverage, "model_version": MODEL_VERSION,
        "formula": "delivery quantity/delivery-percent participation + DWAP/absorption context; compression/expansion remains shadow-only with zero score influence until qualified",
        "observed_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "summary": (
            f"{stage}: delivery {pct_now:.1f}% vs 20D {pct_avg:.1f}%; "
            f"quantity z {'unavailable' if qty_z is None else format(qty_z, '.2f')}; "
            f"ATR ratio {'unavailable' if atr_ratio is None else format(atr_ratio, '.2f')}."
        ),
    }
