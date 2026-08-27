"""Candle-derived market-structure intelligence, independent of runtime state."""
from __future__ import annotations

from typing import Any, Dict, List

from indicators import closes, support_resistance
from market_layers import market_structure, order_blocks, retest_zone, trendline, volume_profile, support_resistance_levels
from core.compression_expansion_authority import DEFAULT_COMPRESSION_EXPANSION_AUTHORITY
from core.pattern_evidence_service import DEFAULT_PATTERN_EVIDENCE_SERVICE
from core.chart_pattern_evidence_service import DEFAULT_CHART_PATTERN_EVIDENCE_SERVICE


class PriceActionIntelligenceService:
    def analyze(self, candles: List[Dict[str, Any]], mode: str) -> Dict[str, Any]:
        if not candles:
            return {"ok": False, "state": "pending", "reason": "candles unavailable"}
        sr_actionable = support_resistance(candles, min(len(candles), 220)) if len(candles) >= 20 else {}
        sr_structural = support_resistance_levels(candles, lookback=min(len(candles), 220)) if len(candles) >= 20 else {}
        structure = market_structure(candles)
        values = closes(candles)
        last = values[-1] if values else None
        previous_high = max((float(c["high"]) for c in candles[-40:-1] if c.get("high") is not None), default=None)
        previous_low = min((float(c["low"]) for c in candles[-40:-1] if c.get("low") is not None), default=None)
        liquidity_event = "none"
        try:
            final = candles[-1]
            if previous_low and final.get("low") is not None and float(final["low"]) < previous_low and last and last > previous_low:
                liquidity_event = "downside_liquidity_sweep_reclaim"
            elif previous_high and final.get("high") is not None and float(final["high"]) > previous_high and last and last < previous_high:
                liquidity_event = "upside_liquidity_sweep_reject"
        except (TypeError, ValueError, IndexError):
            liquidity_event = "pending"
        return {
            "ok": True, "mode": mode,
            "actionable_support": sr_actionable.get("support"),
            "actionable_resistance": sr_actionable.get("resistance"),
            "long_term_support": (sr_structural.get("major_support") or {}).get("price"),
            "long_term_resistance": (sr_structural.get("major_resistance") or {}).get("price"),
            "major_support_evidence": sr_structural.get("major_support"),
            "major_resistance_evidence": sr_structural.get("major_resistance"),
            "support_resistance_semantics": "actionable=nearest; long_term=ranked_major_structural",
            "structure": structure,
            "volume_profile": volume_profile(candles),
            "liquidity_event": liquidity_event,
            "order_flow_proxy": self.order_flow(candles),
            "trendline": trendline(candles),
            "order_blocks": order_blocks(candles),
            "retest_zone": retest_zone(candles, structure),
            "compression_expansion": DEFAULT_COMPRESSION_EXPANSION_AUTHORITY.evaluate(candles),
            "candle_patterns": DEFAULT_PATTERN_EVIDENCE_SERVICE.analyze(candles),
            "chart_patterns": DEFAULT_CHART_PATTERN_EVIDENCE_SERVICE.analyze(candles),
            "note": "Support/resistance, structure, trendline, order blocks, retest, compression/expansion, mechanical candle-pattern and chart-formation evidence are computed from persisted completed candle history, not generated. New C13 pattern/compression evidence is shadow-weighted until governed walk-forward validation.",
        }

    def order_flow(self, candles: List[Dict[str, Any]]) -> Dict[str, Any]:
        if len(candles) < 12:
            return {"state": "pending", "reason": "need more candles"}
        recent = candles[-10:]
        if not any(float(c.get("volume") or 0) for c in recent) or not any(c.get("close") is not None for c in recent):
            return {"state": "pending"}
        up_volume = down_volume = 0.0
        for candle in recent:
            volume, opened, closed = float(candle.get("volume") or 0), candle.get("open"), candle.get("close")
            if opened is not None and closed is not None and float(closed) >= float(opened):
                up_volume += volume
            else:
                down_volume += volume
        bias = "buying_pressure" if up_volume > down_volume * 1.25 else "selling_pressure" if down_volume > up_volume * 1.25 else "balanced"
        return {"state": bias, "up_volume": round(up_volume, 2), "down_volume": round(down_volume, 2)}
