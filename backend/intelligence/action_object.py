"""
ActionObject -- one stock's full intelligence, in one stable shape.

This is a RESHAPE, not a new score. Every field below is read out of the
EvidenceDecision dict that core.evidence_engine_service.EvidenceEngineService
already produces (routes_get.r_evidence_today -> EvidenceEngineService.build_today).
Nothing here re-computes technical/institutional/fundamental scores.

Why this exists (v61 architecture note, "Action Object Oriented Intelligence
System"): today each engine's opinion about a stock is scattered across
`components[]`, `institutional_signals`, `fundamental_score`, `dwap`,
`relative_volume`, etc. Every UI/consumer has to know the shape of all of
that to answer "is this stock actionable and why". ActionObject collapses
it into the eight named states from the architecture note:

    market_state, trend_state, volume_state, institutional_state,
    fundamental_state, technical_state, risk_state, action_state

READINESS_TO_ACTION intentionally does not guess long/short direction --
EvidenceDecision does not carry a `side` field, so inventing BUY/SELL here
would be fabricating data the underlying engine never asserted.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

CONTRACT_VERSION = "action-object-v1"

READINESS_TO_ACTION = {
    "READY": "READY_TO_ACT",
    "WATCH": "WATCH",
    "EXTENDED": "EXTENDED_AVOID_CHASE",
    "AVOID": "IGNORE",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _component_by_name(components: List[Dict[str, Any]], *name_fragments: str) -> Optional[Dict[str, Any]]:
    for c in components or []:
        name = str(c.get("name") or "").lower()
        if any(frag.lower() in name for frag in name_fragments):
            return c
    return None


def _strength_label(component: Optional[Dict[str, Any]]) -> str:
    if not component or not component.get("available"):
        return "unavailable"
    points = float(component.get("points") or 0)
    maximum = float(component.get("max_points") or 1) or 1
    ratio = points / maximum
    if ratio >= 0.75:
        return "strong"
    if ratio >= 0.45:
        return "moderate"
    return "weak"


def _institutional_state(evidence: Dict[str, Any], delivery_object: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    component = _component_by_name(evidence.get("components"), "institutional participation")
    out = {
        "strength": _strength_label(component),
        "stage": evidence.get("institutional_stage"),
        "signals": evidence.get("institutional_signals"),
        "reason": (component or {}).get("reason"),
    }
    if delivery_object:
        out["delivery_classification"] = delivery_object.get("classification")
        out["delivery_classification_meaning"] = delivery_object.get("classification_meaning")
        out["delivery_pct_excess_20d"] = delivery_object.get("delivery_pct_excess_20d")
        out["traded_qty_z20"] = delivery_object.get("traded_qty_z20")
    return out


def _fundamental_state(evidence: Dict[str, Any], fundamental_object: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    out = {
        "strength": _strength_label(
            {"available": evidence.get("fundamental_score") is not None,
             "points": evidence.get("fundamental_score") or 0, "max_points": 100}
        ) if evidence.get("fundamental_score") is not None else "unavailable",
        "score": evidence.get("fundamental_score"),
        "state": evidence.get("fundamental_state"),
        "weight_pct": evidence.get("fundamental_weight_pct"),
    }
    if fundamental_object:
        out["business_momentum"] = fundamental_object.get("business_momentum")
        out["revenue_momentum"] = fundamental_object.get("revenue_momentum")
        out["profit_momentum"] = fundamental_object.get("profit_momentum")
        out["quarters_on_file"] = fundamental_object.get("quarters_on_file")
    return out


def _technical_state(evidence: Dict[str, Any]) -> Dict[str, Any]:
    component = _component_by_name(
        evidence.get("components"), "technical confirmation", "intraday technical", "intraday setup"
    )
    tech_score = evidence.get("technical_score")
    return {
        "strength": _strength_label(component) if tech_score is None else _strength_label(
            {"available": True, "points": tech_score, "max_points": 100}
        ),
        "score": tech_score,
        "reason": (component or {}).get("reason"),
        "market_structure_state": evidence.get("freshness_state"),
    }


def _volume_state(evidence: Dict[str, Any]) -> Dict[str, Any]:
    rel_vol = evidence.get("relative_volume")
    if rel_vol is None:
        label = "unavailable"
    elif rel_vol >= 2.5:
        label = "strong_expansion"
    elif rel_vol >= 1.5:
        label = "expansion"
    elif rel_vol >= 0.8:
        label = "normal"
    else:
        label = "contraction"
    return {"relative_volume": rel_vol, "label": label, "dwap": evidence.get("dwap")}


def _market_state(evidence: Dict[str, Any], market: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    component = _component_by_name(evidence.get("components"), "market regime")
    out = {
        "regime_component_strength": _strength_label(component),
        "sector": evidence.get("sector"),
        "sector_index": evidence.get("sector_index"),
        "sector_change_pct": evidence.get("sector_change_pct"),
    }
    if market:
        out["trend_state"] = market.get("trend_state")
        out["regime"] = market.get("regime")
        out["breadth_state"] = market.get("breadth_state")
        out["market_risk_state"] = market.get("risk_state")
    return out


def _risk_state(evidence: Dict[str, Any], market: Optional[Dict[str, Any]]) -> str:
    readiness = str(evidence.get("readiness") or "")
    conflicts = evidence.get("conflicts") or []
    market_risk = (market or {}).get("risk_state")
    if readiness == "AVOID" or len(conflicts) >= 2:
        return "high"
    if market_risk == "high" or conflicts:
        return "elevated"
    if readiness == "READY" and (market_risk in (None, "low")):
        return "low"
    return "medium"


def _reasons(evidence: Dict[str, Any]) -> List[str]:
    reasons = [evidence.get("thesis")] if evidence.get("thesis") else []
    for c in evidence.get("components") or []:
        if c.get("available") and c.get("reason") and float(c.get("points") or 0) >= 0.6 * float(c.get("max_points") or 1):
            reasons.append(f"{c.get('name')}: {c.get('reason')}")
    return reasons[:6]


@dataclass(frozen=True)
class ActionObject:
    symbol: str
    exchange: str
    mode: str
    market_state: Dict[str, Any]
    trend_state: str
    volume_state: Dict[str, Any]
    institutional_state: Dict[str, Any]
    fundamental_state: Dict[str, Any]
    technical_state: Dict[str, Any]
    risk_state: str
    action_state: Dict[str, Any]
    observed_at: str
    contract_version: str = CONTRACT_VERSION
    source_contract_version: Optional[str] = None
    source_model_version: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def build_action_object(
    evidence: Dict[str, Any],
    market: Optional[Dict[str, Any]] = None,
    delivery_object: Optional[Dict[str, Any]] = None,
    fundamental_object: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """evidence: one item from EvidenceEngineService.build_today(...)["opportunities"]
    (an EvidenceDecision.to_dict()). market: output of build_market_object(), optional.
    delivery_object/fundamental_object: outputs of build_delivery_object() /
    build_fundamental_object(), optional -- when supplied they enrich
    institutional_state/fundamental_state with the pattern classification
    and quarterly momentum those two objects add; omitting them (the
    default) keeps the exact evidence-only output from earlier versions.
    """
    readiness = str(evidence.get("readiness") or "WATCH")
    obj = ActionObject(
        symbol=str(evidence.get("symbol") or ""),
        exchange=str(evidence.get("exchange") or "NSE"),
        mode=str(evidence.get("mode") or ""),
        market_state=_market_state(evidence, market),
        trend_state=str((market or {}).get("trend_state") or evidence.get("freshness_state") or "pending"),
        volume_state=_volume_state(evidence),
        institutional_state=_institutional_state(evidence, delivery_object),
        fundamental_state=_fundamental_state(evidence, fundamental_object),
        technical_state=_technical_state(evidence),
        risk_state=_risk_state(evidence, market),
        action_state={
            "action": READINESS_TO_ACTION.get(readiness, "WATCH"),
            "readiness": readiness,
            "confidence": evidence.get("confidence"),
            "evidence_score": evidence.get("evidence_score"),
            "entry": evidence.get("entry"),
            "stop": evidence.get("stop"),
            "target": evidence.get("target"),
            "rr": evidence.get("rr"),
            "ltp": evidence.get("ltp"),
            "waiting_for": evidence.get("waiting_for"),
            "invalidation_reason": evidence.get("invalidation_reason"),
            "reasons": _reasons(evidence),
            "conflicts": evidence.get("conflicts") or [],
        },
        observed_at=evidence.get("observed_at") or _now(),
        source_contract_version=evidence.get("contract_version"),
        source_model_version=evidence.get("model_version"),
    )
    return obj.to_dict()


def build_action_objects(evidence_today: Dict[str, Any], market: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """evidence_today: full return value of EvidenceEngineService.build_today(...)."""
    return [build_action_object(item, market=market) for item in (evidence_today.get("opportunities") or [])]
