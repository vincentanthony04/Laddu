"""
DeliveryObject -- NSE delivery-volume pattern classification.

v61 architecture note, section 3 ("NSE Delivery Volume Intelligence"), asked
for exactly four labels: Accumulation / Distribution / Weak Rally / Panic
Selling, derived from price direction + volume direction + delivery
direction.

All three inputs already exist in `core.institutional_signal_service.analyze()`
(reached today via `LadduRuntime.delivery_context(symbol)`):
  * price direction   <- volatility.price_move_pct
  * volume direction   <- delivery.traded_qty_z20 (traded-quantity z-score vs 20d)
  * delivery direction <- delivery.pct_excess_20d (delivery% vs its own 20d mean)

This module adds NO new math and does NOT re-derive score/stage/dwap --
those stay owned by institutional_signal_service. It only names the
price/volume/delivery combination the way the architecture note asked for,
as a label sitting alongside (not replacing) the existing `stage`/`bias`.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Dict, Optional

CONTRACT_VERSION = "delivery-object-v1"

VOLUME_Z_THRESHOLD = 0.5   # traded_qty_z20 above this counts as "volume up"
DELIVERY_EXCESS_THRESHOLD = 0.0  # pct_excess_20d above this counts as "delivery up"


def _sign(value: Optional[float], threshold: float) -> Optional[bool]:
    if value is None:
        return None
    return value > threshold


def _classify(price_up: Optional[bool], volume_up: Optional[bool], delivery_up: Optional[bool]) -> str:
    if price_up is None or volume_up is None:
        return "insufficient_data"
    if price_up and volume_up and delivery_up:
        return "accumulation"
    if price_up and volume_up and delivery_up is False:
        return "distribution"
    if price_up and not volume_up:
        return "weak_rally"
    if not price_up and volume_up and delivery_up:
        return "panic_selling"
    return "unclassified"


CLASSIFICATION_MEANING = {
    "accumulation": "Price up, volume up, delivery up — possible institutional buying",
    "distribution": "Price up, volume up, delivery down — short-term selling into strength",
    "weak_rally": "Price up on falling volume — low conviction",
    "panic_selling": "Price down, volume up, delivery up — possible capitulation/absorption",
    "unclassified": "Price/volume/delivery combination does not match a named pattern",
    "insufficient_data": "Not enough delivery/candle history yet",
}


@dataclass(frozen=True)
class DeliveryObject:
    symbol: str
    ok: bool
    classification: str
    classification_meaning: str
    stage: Optional[str]
    bias: Optional[str]
    delivery_latest_pct: Optional[float]
    delivery_avg20_pct: Optional[float]
    delivery_pct_excess_20d: Optional[float]
    traded_qty_z20: Optional[float]
    price_move_pct: Optional[float]
    dwap: Optional[Dict[str, Any]]
    signals: Optional[Dict[str, Any]]
    coverage: Optional[Dict[str, Any]]
    source_model_version: Optional[str]
    contract_version: str = CONTRACT_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def build_delivery_object(delivery_context_result: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """delivery_context_result: the dict returned by
    core.institutional_signal_service.analyze() (reached via
    LadduRuntime.delivery_context(symbol) / app.delivery_context(symbol))."""
    result = delivery_context_result or {}
    delivery = result.get("delivery") or {}
    volatility = result.get("volatility") or {}
    ok = bool(result.get("ok"))

    price_up = _sign(volatility.get("price_move_pct"), 0.0)
    volume_up = _sign(delivery.get("traded_qty_z20"), VOLUME_Z_THRESHOLD)
    delivery_up = _sign(delivery.get("pct_excess_20d"), DELIVERY_EXCESS_THRESHOLD)

    classification = _classify(price_up, volume_up, delivery_up) if ok else "insufficient_data"
    obj = DeliveryObject(
        symbol=str(result.get("symbol") or ""),
        ok=ok,
        classification=classification,
        classification_meaning=CLASSIFICATION_MEANING[classification],
        stage=result.get("stage"),
        bias=result.get("bias"),
        delivery_latest_pct=delivery.get("latest_pct"),
        delivery_avg20_pct=delivery.get("average_20d_pct"),
        delivery_pct_excess_20d=delivery.get("pct_excess_20d"),
        traded_qty_z20=delivery.get("traded_qty_z20"),
        price_move_pct=volatility.get("price_move_pct"),
        dwap=result.get("dwap"),
        signals=result.get("signals"),
        coverage=result.get("coverage"),
        source_model_version=result.get("model_version"),
    )
    return obj.to_dict()
