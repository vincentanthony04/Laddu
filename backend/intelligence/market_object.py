"""
MarketObject -- the regime context every ActionObject is judged against.

Source data (all already computed elsewhere, this module only reshapes it):
  * regime         <- routes_get._evidence_regime(app)  (index change-pct mean)
  * breadth        <- app.store.get_latest_market_breadth(universe)
                       (advances/declines/unchanged, from
                       core.reference_data_service.compute_market_breadth)

Market-wide FII/FPI and DII cash-market flow is supplied by the dedicated
ReferenceDataService when available.  It is provisional, aggregate market
context and must never be interpreted as stock-specific institutional identity.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Optional

CONTRACT_VERSION = "market-object-v2"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _num(value: Any) -> Optional[float]:
    try:
        if value in (None, "", "—"):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _breadth_state(advances: Optional[int], declines: Optional[int]) -> str:
    if advances is None or declines is None or (advances + declines) == 0:
        return "pending"
    total = advances + declines
    ratio = advances / total
    if ratio >= 0.60:
        return "positive"
    if ratio <= 0.40:
        return "negative"
    return "mixed"


def _regime_from_mean(mean_change_pct: Optional[float]) -> str:
    if mean_change_pct is None:
        return "pending"
    if mean_change_pct >= 0.15:
        return "trending_up"
    if mean_change_pct <= -0.15:
        return "trending_down"
    return "range_bound"


@dataclass(frozen=True)
class MarketObject:
    trend_state: str                       # supportive | hostile | neutral | pending
    regime: str                            # trending_up | trending_down | range_bound | pending
    mean_index_change_pct: Optional[float]
    index_count: int
    breadth_state: str                     # positive | mixed | negative | pending
    advances: Optional[int]
    declines: Optional[int]
    unchanged: Optional[int]
    breadth_universe: Optional[str]
    institutional_bias: str                # aggregate FII/DII regime or not_available
    risk_state: str                        # low | medium | high, derived from breadth+trend agreement
    institutional_flow_state: str = "UNAVAILABLE"
    institutional_flow_as_of: Optional[str] = None
    institutional_flow_provisional: bool = True
    institutional_flow_scope: Optional[str] = None
    fii_net_5d_crore: Optional[float] = None
    dii_net_5d_crore: Optional[float] = None
    as_of: str = field(default_factory=_now)
    contract_version: str = CONTRACT_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _risk_state(trend_state: str, breadth_state: str) -> str:
    """Conflict between index trend and breadth = higher risk (v61 note:
    'A bullish breakout in weak market: confidence reduced')."""
    if trend_state == "pending" or breadth_state == "pending":
        return "medium"
    agree = (
        (trend_state == "supportive" and breadth_state == "positive")
        or (trend_state == "hostile" and breadth_state == "negative")
    )
    conflict = (
        (trend_state == "supportive" and breadth_state == "negative")
        or (trend_state == "hostile" and breadth_state == "positive")
    )
    if conflict:
        return "high"
    if agree:
        return "low"
    return "medium"


def build_market_object(
    regime: Optional[Dict[str, Any]] = None,
    breadth: Optional[Dict[str, Any]] = None,
    institutional_flow: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """regime: output of routes_get._evidence_regime(app), e.g.
        {"state": "supportive", "mean_index_change_pct": 0.32, "index_count": 16}
    breadth: output of app.store.get_latest_market_breadth(universe), e.g.
        {"advances": 140, "declines": 90, "unchanged": 10, "universe": "NIFTY250_CORE"}
    Both are optional and independently missing-tolerant -- a cold-start
    system should return a "pending" MarketObject, never crash the caller.
    """
    regime = regime or {}
    breadth = breadth or {}
    institutional_flow = institutional_flow or {}

    trend_state = str(regime.get("state") or "pending")
    mean_change = _num(regime.get("mean_index_change_pct"))
    index_count = int(regime.get("index_count") or 0)

    advances = breadth.get("advances")
    declines = breadth.get("declines")
    unchanged = breadth.get("unchanged")
    advances = int(advances) if advances is not None else None
    declines = int(declines) if declines is not None else None
    unchanged = int(unchanged) if unchanged is not None else None
    breadth_state = _breadth_state(advances, declines)

    flow_state = str(institutional_flow.get("state") or "UNAVAILABLE").upper()
    flow_available = flow_state == "AVAILABLE"
    flow_regime = str(institutional_flow.get("regime") or "not_available") if flow_available else "not_available"
    fii = institutional_flow.get("fii_fpi") if isinstance(institutional_flow.get("fii_fpi"), dict) else {}
    dii = institutional_flow.get("dii") if isinstance(institutional_flow.get("dii"), dict) else {}

    obj = MarketObject(
        trend_state=trend_state,
        regime=_regime_from_mean(mean_change),
        mean_index_change_pct=mean_change,
        index_count=index_count,
        breadth_state=breadth_state,
        advances=advances,
        declines=declines,
        unchanged=unchanged,
        breadth_universe=breadth.get("universe"),
        institutional_bias=flow_regime,
        risk_state=_risk_state(trend_state, breadth_state),
        institutional_flow_state=flow_state,
        institutional_flow_as_of=institutional_flow.get("latest_trade_date"),
        institutional_flow_provisional=bool(institutional_flow.get("provisional", True)),
        institutional_flow_scope=institutional_flow.get("market_scope"),
        fii_net_5d_crore=_num(fii.get("net_5d_crore")),
        dii_net_5d_crore=_num(dii.get("net_5d_crore")),
    )
    return obj.to_dict()
