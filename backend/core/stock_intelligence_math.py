"""Pure scoring primitives for the Stock Intelligence card.

This module deliberately contains no network, database, runtime, or UI access.
All scores are deterministic from the supplied evidence and expose coverage so
missing data cannot be mistaken for neutral or complete evidence.
"""
from __future__ import annotations

from core.production_mode_policy import require_production_mode
from core.indicator_snapshot_authority import DEFAULT_INDICATOR_SNAPSHOT_AUTHORITY, true_ranges, wilder_series, ema as canonical_ema, rsi_series as canonical_rsi_series

from math import isfinite
from typing import Any, Dict, Iterable, List, Optional, Tuple


def _num(value: Any) -> Optional[float]:
    try:
        number = float(value)
        return number if isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, float(value)))


def _linear(value: Optional[float], bad: float, good: float, *, higher: bool = True) -> Optional[float]:
    """Continuous 0..100 score between evidence thresholds."""
    if value is None or good == bad:
        return None
    if higher:
        return _clamp((value - bad) * 100.0 / (good - bad))
    return _clamp((bad - value) * 100.0 / (bad - good))


def ema(values: Iterable[float], period: int) -> Optional[float]:
    return canonical_ema([float(v) for v in values], period)

def wilder_rsi(values: Iterable[float], period: int = 14) -> Optional[float]:
    series = canonical_rsi_series([float(v) for v in values], period)
    return series[-1] if series else None

def wilder_atr(candles: Iterable[Dict[str, Any]], period: int = 14) -> Optional[float]:
    """Compatibility projection of canonical Wilder ATR.

    Production callers must consume IndicatorSnapshotAuthority; this wrapper
    exists only for older callers/tests and delegates to the same canonical
    primitive rather than maintaining a second volatility definition.
    """
    clean: List[Dict[str, float]] = []
    for raw in candles or []:
        high, low, close = _num(raw.get("high")), _num(raw.get("low")), _num(raw.get("close"))
        if high is None or low is None or close is None:
            continue
        clean.append({"high": high, "low": low, "close": close})
    if not clean:
        return None
    series = wilder_series(true_ranges(clean), period)
    return series[-1] if series else None


def canonical_mode(mode: Any) -> str:
    return require_production_mode(mode)


def mtf_score(rows: Iterable[Dict[str, Any]], mode: Any) -> Dict[str, Any]:
    """Coverage-aware multi-timeframe score.

    Each timeframe's existing directional_score is bounded to [-6,+6] and
    linearly mapped to [0,100]. Pending frames contribute no evidence and their
    missing weight is reported; weights are never silently reassigned to the
    remaining frames.
    """
    desk = canonical_mode(mode)
    weights = MTF_WEIGHTS[desk]
    by_tf = {str(row.get("tf") or ""): row for row in (rows or [])}
    weighted_points = 0.0
    weighted_signed = 0.0
    resolved_weight = 0.0
    resolved: List[str] = []
    missing: List[str] = []
    details: List[Dict[str, Any]] = []
    for tf, weight in weights.items():
        row = by_tf.get(tf) or {}
        direction = _num(row.get("directional_score"))
        state = str(row.get("state") or "pending").lower()
        if row.get("usable_for_live_confirmation") is False:
            direction = None
            state = "pending"
        if direction is None and state in {"bullish", "neutral", "bearish"}:
            # State-only legacy rows are lower-resolution evidence than rows
            # carrying the component sum. Map them to a moderate directional
            # value instead of an unjustified extreme +/-6.
            direction = 3.0 if state == "bullish" else -3.0 if state == "bearish" else 0.0
        if direction is None or state == "pending":
            missing.append(tf)
            details.append({"tf": tf, "weight": weight, "state": "pending", "score": None})
            continue
        direction = max(-6.0, min(6.0, direction))
        score = 50.0 + direction * (50.0 / 6.0)
        weighted_points += score * weight
        weighted_signed += direction * weight
        resolved_weight += weight
        resolved.append(tf)
        details.append({"tf": tf, "weight": weight, "state": state, "directional_score": direction, "score": round(score, 1)})
    coverage = resolved_weight / sum(weights.values()) if weights else 0.0
    # Four frames and at least 75% of configured desk weight are required.
    # This prevents one or two dominant higher-weight frames from being shown
    # as broad multi-timeframe confirmation.
    ready = len(resolved) >= 4 and coverage >= 0.75
    raw = weighted_points / resolved_weight if resolved_weight else None
    # Shrink partial evidence towards neutral rather than renormalising it to an
    # unjustifiably extreme value.
    adjusted = 50.0 + ((raw - 50.0) * coverage) if raw is not None else None
    signed = weighted_signed / resolved_weight if resolved_weight else None
    observed_bias = "LONG" if signed is not None and signed >= 1.0 else "SHORT" if signed is not None and signed <= -1.0 else "NEUTRAL"
    # A partial collection may describe the observed direction, but it cannot
    # own the canonical desk bias. This prevents one available frame from
    # turning the entire Stock Intelligence card bullish or bearish.
    bias = observed_bias if ready else "NEUTRAL"
    return {
        "score": round(adjusted, 1) if ready and adjusted is not None else None,
        "raw_score": round(raw, 1) if raw is not None else None,
        "coverage": round(coverage, 3),
        "resolved": resolved,
        "missing": missing,
        "required_frames": 4,
        "ready": ready,
        "bias": bias,
        "observed_bias": observed_bias,
        "signed_alignment": round(signed, 3) if signed is not None else None,
        "weights": weights,
        "details": details,
    }


def _directional_rsi_score(value: Optional[float], bias: str) -> Optional[float]:
    if value is None:
        return None
    # Moderate trend-zone RSI is rewarded; exhaustion is not. Mirrored for
    # short bias. The points are deliberately continuous -- the previous
    # piecewise formula jumped from ~100 to 0 exactly at RSI 35.
    if bias == "SHORT":
        value = 100.0 - value
    value = _clamp(value)
    points = ((0.0, 0.0), (25.0, 0.0), (35.0, 40.0), (50.0, 80.0),
              (60.0, 100.0), (70.0, 60.0), (80.0, 0.0), (100.0, 0.0))
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        if value <= x1:
            if x1 == x0:
                return y1
            ratio = (value - x0) / (x1 - x0)
            return _clamp(y0 + ratio * (y1 - y0))
    return 0.0


def technical_score(candles: Iterable[Dict[str, Any]], mode: Any, bias: str) -> Dict[str, Any]:
    """Score one desk direction from aligned, completed OHLCV evidence.

    Invalid rows are removed as rows, not independently per column. This keeps
    close, high/low, volume and timestamps on the same observations and avoids
    a common silent error where volume[-1] belongs to a different candle than
    close[-1].
    """
    normalised: List[Dict[str, Any]] = []
    for candle in candles or []:
        close = _num(candle.get("close"))
        if close is None:
            continue
        normalised.append({
            **candle,
            "close": close,
            "open": _num(candle.get("open")),
            "high": _num(candle.get("high")),
            "low": _num(candle.get("low")),
            "volume": _num(candle.get("volume")),
        })
    closes = [float(c["close"]) for c in normalised]
    if len(closes) < 20:
        return {"score": None, "raw_score": None, "coverage": 0.0, "ready": False, "reason": f"need at least 20 aligned completed candles; got {len(closes)}", "evidence": [], "values": {}}

    desk = canonical_mode(mode)
    side = "SHORT" if str(bias or "").upper() == "SHORT" else "LONG"
    last = closes[-1]
    indicator_snapshot = DEFAULT_INDICATOR_SNAPSHOT_AUTHORITY.calculate(normalised)
    indicator_metrics = dict(indicator_snapshot.get("metrics") or {})
    e20 = indicator_metrics.get("ema20")
    e50 = indicator_metrics.get("ema50")
    rv = indicator_metrics.get("rsi14")
    # ATR is owned by IndicatorSnapshotAuthority; Stock Intelligence must not
    # recompute a same-labelled value through a parallel implementation.
    atr = indicator_metrics.get("atr14")
    lookback = 5 if desk == "intraday" else 20
    momentum = ((last / closes[-1 - lookback]) - 1.0) * 100.0 if len(closes) > lookback and closes[-1 - lookback] else None

    volume_ratio = None
    # Current volume and its comparison window must be the same contiguous
    # aligned candles. Do not compress away missing values and change dates.
    if len(normalised) >= 21:
        recent_volumes = [row.get("volume") for row in normalised[-21:]]
        if all(value is not None for value in recent_volumes):
            base = sum(float(value) for value in recent_volumes[:-1]) / 20.0
            if base > 0:
                volume_ratio = float(recent_volumes[-1]) / base

    components: List[Tuple[str, float, Optional[float], str]] = []
    if e20 is not None:
        aligned = last >= e20 if side == "LONG" else last <= e20
        components.append(("price_vs_ema20", 18.0, 100.0 if aligned else 0.0, f"Price {'above' if last >= e20 else 'below'} EMA20"))
    if e20 is not None and e50 is not None:
        aligned = e20 >= e50 if side == "LONG" else e20 <= e50
        separation = abs(e20 - e50) / max(abs(e50), 1e-9) * 100.0
        structure = 50.0 + min(50.0, separation * 20.0) if aligned else max(0.0, 50.0 - separation * 20.0)
        components.append(("ema_structure", 22.0, structure, f"EMA20 {'above' if e20 >= e50 else 'below'} EMA50 by {separation:.2f}%"))
    components.append(("rsi14", 18.0, _directional_rsi_score(rv, side), f"Primary-frame RSI14 {rv:.2f}" if rv is not None else "RSI14 unavailable"))
    if momentum is not None:
        signed_momentum = momentum if side == "LONG" else -momentum
        threshold = 1.2 if desk == "intraday" else 6.0
        components.append(("momentum", 17.0, _linear(signed_momentum, -threshold, threshold), f"{lookback}-bar momentum {momentum:+.2f}%"))
    if volume_ratio is not None:
        components.append(("volume", 12.0, _linear(volume_ratio, 0.55, 1.35), f"Volume {volume_ratio:.2f}x prior-20 average"))
    if atr is not None and last:
        atr_pct = atr / abs(last) * 100.0
        optimum = 0.65 if desk == "intraday" else 2.5
        distance = abs(atr_pct - optimum) / max(optimum, 1e-9)
        vol_quality = _clamp(100.0 - distance * 55.0)
        components.append(("volatility", 13.0, vol_quality, f"ATR14 {atr:.2f} ({atr_pct:.2f}% of price)"))

    available_weight = sum(weight for _, weight, score, _ in components if score is not None)
    raw = (sum(weight * float(score) for _, weight, score, _ in components if score is not None) / available_weight) if available_weight else None
    coverage = available_weight / 100.0
    adjusted = 50.0 + ((raw - 50.0) * coverage) if raw is not None else None
    # EMA50 is a declared core component, therefore 50 aligned bars are the
    # minimum for a final technical score. Shorter series remain a transparent
    # partial model rather than silently switching to different EMA periods.
    core_ready = len(closes) >= 50 and e20 is not None and e50 is not None and rv is not None
    ready = core_ready and coverage >= 0.70
    return {
        "score": round(adjusted, 1) if ready and adjusted is not None else None,
        "partial_score": round(adjusted, 1) if adjusted is not None else None,
        "raw_score": round(raw, 1) if raw is not None else None,
        "coverage": round(coverage, 3),
        "available_weight": available_weight,
        "aligned_candle_count": len(normalised),
        "ready": ready,
        "bias": side,
        "evidence": [text for _, _, score, text in components if score is not None],
        "components": {name: {"weight": weight, "score": round(score, 1) if score is not None else None} for name, weight, score, _ in components},
        "values": {
            "last_close": round(last, 4), "ema20": round(e20, 4) if e20 is not None else None,
            "ema50": round(e50, 4) if e50 is not None else None, "rsi14": round(rv, 2) if rv is not None else None,
            "momentum_pct": round(momentum, 3) if momentum is not None else None,
            "volume_ratio": round(volume_ratio, 3) if volume_ratio is not None else None,
            "atr14": round(atr, 4) if atr is not None else None,
        },
        "reason": None if ready else "technical evidence is incomplete; EMA20/EMA50 requires 50 aligned completed candles",
    }


def combine_scores(mode: Any, technical: Optional[float], mtf: Optional[float], fundamental: Optional[float], *, fundamental_applicable: bool = True) -> Dict[str, Any]:
    """Combine desk evidence without promoting unknown inputs.

    ``model_score`` is a labelled diagnostic estimate with unknown inputs held
    at neutral. ``final_score`` exists only when every mandatory input is
    present. ``display_score`` preserves a bounded numeric UI value for legacy
    clients, but is capped below the trade threshold while evidence is missing.
    """
    desk = canonical_mode(mode)
    if desk == "intraday" or not fundamental_applicable:
        weights = {"technical": 0.55, "mtf": 0.45}
    else:
        weights = {"technical": 0.30, "mtf": 0.25, "fundamental": 0.45}
    inputs = {"technical": technical, "mtf": mtf, "fundamental": fundamental}
    mandatory = list(weights)
    missing = [name for name in mandatory if inputs.get(name) is None]
    partial = sum((float(inputs.get(name)) if inputs.get(name) is not None else 50.0) * weight for name, weight in weights.items())
    ready = not missing
    final = sum(float(inputs[name]) * weight for name, weight in weights.items()) if ready else None
    display = final if final is not None else min(partial, 69.0)
    return {
        "model_score": round(partial, 1),
        "display_score": round(display, 1),
        "final_score": round(final, 1) if final is not None else None,
        "ready": ready,
        "missing": missing,
        "weights": weights,
        "inputs": inputs,
        # Keep the established value for complete contracts. On partial
        # contracts the additional mathematical policy field is authoritative.
        "missing_policy": "excluded_and_weights_renormalized" if ready else "unknown_neutral_partial_final_withheld",
        "mathematical_policy": "fixed desk weights; unknown=50 only in labelled partial model; final confidence withheld; partial display capped at 69",
    }

