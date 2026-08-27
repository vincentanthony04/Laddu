"""Evidence-gated Ultra Scalp lens for the Intraday desk.

Ultra Scalp is not a third production mode.  It is a fail-closed Intraday lens
that uses only already-available local quote/depth/candle evidence.  It never
performs provider I/O and never places broker orders.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable


class UltraScalpService:
    VERSION = "ultra-scalp-lens-1.0.0-model-paper-only"

    @staticmethod
    def _num(value: Any) -> float | None:
        try:
            return float(value)
        except Exception:
            return None

    @classmethod
    def evaluate(
        cls,
        *,
        symbol: str,
        mode: str,
        quote: Dict[str, Any] | None,
        depth: Dict[str, Any] | None,
        mtf: Iterable[Dict[str, Any]] | None,
        market_context: Dict[str, Any] | None = None,
        estimated_roundtrip_cost_pct: float = 0.15,
    ) -> Dict[str, Any]:
        quote = dict(quote or {})
        depth = dict(depth or {})
        market_context = dict(market_context or {})
        mode = str(mode or "delivery").lower()
        blockers: list[str] = []
        if mode != "intraday":
            blockers.append("Ultra Scalp is available only inside the Intraday desk")

        ltp = cls._num(quote.get("ltp"))
        quote_state = str(quote.get("freshness_state") or quote.get("state") or "").lower()
        if ltp is None:
            blockers.append("Verified live price is unavailable")
        if quote_state not in {"live", "current", "fresh"}:
            blockers.append("Quote is not live")
        quote_age_sec = cls._num(quote.get("age_seconds") or quote.get("quote_age_seconds"))
        if quote_age_sec is not None and quote_age_sec > 5.0:
            blockers.append(f"Quote age {quote_age_sec:.1f}s exceeds ultra-scalp limit 5s")

        depth_state = str(depth.get("state") or "").upper()
        depth_actionable = depth.get("actionable")
        if depth_actionable is False or depth_state in {"CLOSED", "STALE", "UNAVAILABLE", "MISSING"}:
            blockers.append("Market depth is not live and actionable")

        spread_pct = cls._num(depth.get("spread_pct"))
        if spread_pct is None:
            spread_bps = cls._num(depth.get("spread_bps"))
            if spread_bps is not None:
                spread_pct = spread_bps / 100.0
        if spread_pct is None:
            bid = cls._num(depth.get("best_bid"))
            ask = cls._num(depth.get("best_ask"))
            if bid and ask and ltp:
                spread_pct = max(0.0, (ask - bid) * 100.0 / ltp)
        if spread_pct is None:
            blockers.append("Bid/ask spread evidence is unavailable")
        elif spread_pct > 0.18:
            blockers.append(f"Spread {spread_pct:.2f}% exceeds ultra-scalp limit 0.18%")

        buy_qty = cls._num(depth.get("buy_qty") or depth.get("buy_quantity") or depth.get("total_buy_qty"))
        sell_qty = cls._num(depth.get("sell_qty") or depth.get("sell_quantity") or depth.get("total_sell_qty"))
        depth_total = (buy_qty or 0.0) + (sell_qty or 0.0)
        if depth_total <= 0:
            blockers.append("Market depth is unavailable")
        imbalance = None
        if depth_total > 0:
            imbalance = ((buy_qty or 0.0) - (sell_qty or 0.0)) / depth_total

        frames = {str(row.get("tf") or row.get("timeframe") or ""): row for row in (mtf or []) if isinstance(row, dict)}
        fast = next((frames.get(key) for key in ("1m", "3m", "5m") if frames.get(key)), None)
        fast_state = str((fast or {}).get("state") or "").lower()
        if fast is None or fast_state in {"", "missing", "pending", "unavailable", "stale"}:
            blockers.append("Fast timeframe confirmation is unavailable")

        sector_state = str(market_context.get("sector_direction") or market_context.get("direction") or "").lower()
        index_state = str(market_context.get("index_direction") or market_context.get("market_direction") or "").lower()
        if not sector_state and not index_state:
            blockers.append("Index/sector impulse is unavailable")

        expected_move_pct = max(0.12, min(0.45, abs(cls._num((fast or {}).get("momentum_pct")) or 0.22)))
        post_cost_pct = expected_move_pct - float(estimated_roundtrip_cost_pct or 0.15) - float(spread_pct or 0.0)
        if post_cost_pct <= 0.05:
            blockers.append("Expected move does not clear spread, slippage and charges")

        side = "FLAT"
        if not blockers:
            state_text = f"{fast_state} {sector_state} {index_state}".lower()
            if imbalance is not None and imbalance >= 0.08 and not any(token in state_text for token in ("bear", "down", "short")):
                side = "LONG"
            elif imbalance is not None and imbalance <= -0.08 and not any(token in state_text for token in ("bull", "up", "long")):
                side = "SHORT"
            else:
                blockers.append("Order-book imbalance and directional impulse are not aligned")

        eligible = not blockers and side in {"LONG", "SHORT"}
        entry = ltp if eligible else None
        target = stop = None
        if eligible and ltp is not None:
            target_delta = ltp * expected_move_pct / 100.0
            stop_delta = max(ltp * 0.08 / 100.0, ltp * float(spread_pct or 0.0) / 100.0 * 1.5)
            if side == "LONG":
                target, stop = round(ltp + target_delta, 2), round(ltp - stop_delta, 2)
            else:
                target, stop = round(ltp - target_delta, 2), round(ltp + stop_delta, 2)

        return {
            "version": cls.VERSION,
            "mode": "intraday",
            "lens": "ultra_scalp",
            "broker_authority": "NONE",
            "paper_only": True,
            "symbol": str(symbol or "").upper(),
            "state": "ELIGIBLE" if eligible else "NOT_ELIGIBLE",
            "eligible": eligible,
            "side": side,
            "entry": entry,
            "target": target,
            "stop": stop,
            "maximum_holding_minutes": 15 if eligible else None,
            "spread_pct": round(spread_pct, 4) if spread_pct is not None else None,
            "depth_imbalance": round(imbalance, 4) if imbalance is not None else None,
            "expected_move_pct": round(expected_move_pct, 3),
            "estimated_roundtrip_cost_pct": round(float(estimated_roundtrip_cost_pct or 0.15), 3),
            "expected_post_cost_edge_pct": round(post_cost_pct, 3),
            "metrics": {
                "spread_pct": round(spread_pct, 4) if spread_pct is not None else None,
                "depth_imbalance": round(imbalance, 4) if imbalance is not None else None,
                "quote_state": quote_state or "unavailable",
                "quote_age_seconds": round(quote_age_sec, 3) if quote_age_sec is not None else None,
                "depth_state": depth_state or "unavailable",
                "depth_actionable": bool(depth_actionable),
                "fast_timeframe_state": fast_state or "unavailable",
            },
            "plan": {
                "side": side,
                "entry": entry,
                "target": target,
                "stop": stop,
                "post_cost_edge_pct": round(post_cost_pct, 3),
                "maximum_holding_minutes": 15 if eligible else None,
            },
            "blockers": blockers,
            "summary": "Ultra Scalp evidence aligned; Automatic Model Paper only" if eligible else "; ".join(blockers),
            "reason": "Ultra Scalp evidence aligned" if eligible else "; ".join(blockers),
        }
