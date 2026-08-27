"""Continuous reassessment of an open Model-Paper thesis.

The service compares the frozen entry/stop/target geometry with the current
verified market snapshot.  It never creates a new direction or widens risk.
"""
from __future__ import annotations

import json
from typing import Any, Mapping

from core.signal_age_authority import DEFAULT_SIGNAL_AGE_AUTHORITY


class LiveThesisReassessmentService:
    authority = "LiveThesisReassessmentService"
    authority_version = "2.0.0"
    states = ("VALID", "WEAKENING", "INVALIDATED")

    @staticmethod
    def _payload(row: Mapping[str, Any]) -> dict[str, Any]:
        raw = row.get("payload") if isinstance(row.get("payload"), Mapping) else row.get("payload_json")
        if isinstance(raw, Mapping):
            return dict(raw)
        try:
            value = json.loads(str(raw or "{}"))
            return dict(value) if isinstance(value, Mapping) else {}
        except Exception:
            return {}

    @classmethod
    def evaluate(cls, row: Mapping[str, Any], quote: Mapping[str, Any], *, at: Any = None, thesis_evidence: Mapping[str, Any] | None = None) -> dict[str, Any]:
        side = str(row.get("side") or "").upper()
        long = side == "LONG"
        price = float(quote.get("ltp", quote.get("price")))
        entry = float(row.get("entry_price") or row.get("original_entry"))
        stop = float(row.get("managed_stop") or row.get("original_stop"))
        original_stop = float(row.get("original_stop"))
        target = float(row.get("original_target"))
        payload = cls._payload(row)
        generated_at = payload.get("generated_at") or payload.get("decision_generated_at") or payload.get("created_at")
        age = DEFAULT_SIGNAL_AGE_AUTHORITY.measure(
            generated_at=generated_at,
            opened_at=row.get("opened_at"),
            at=at,
            mode=row.get("mode"),
            approved_policy=payload.get("approved_age_risk_policy") if isinstance(payload.get("approved_age_risk_policy"), Mapping) else None,
        )

        reasons: list[str] = []
        state = "VALID"
        stop_breached = price <= stop if long else price >= stop
        original_stop_breached = price <= original_stop if long else price >= original_stop
        explicit_invalidated = quote.get("invalidated") is True or str(quote.get("thesis_state") or "").upper() == "INVALIDATED"
        trend = str(quote.get("trend_state") or quote.get("mtf_state") or "").upper()
        opposite_trend = (long and "BEAR" in trend) or ((not long) and "BULL" in trend)
        explicit_weakening = quote.get("weakening") is True or str(quote.get("thesis_state") or "").upper() == "WEAKENING"

        if stop_breached or original_stop_breached or explicit_invalidated:
            state = "INVALIDATED"
            if stop_breached:
                reasons.append("MANAGED_STOP_BREACHED")
            if original_stop_breached:
                reasons.append("ORIGINAL_THESIS_STOP_BREACHED")
            if explicit_invalidated:
                reasons.append("CURRENT_SNAPSHOT_INVALIDATED")
        elif explicit_weakening or opposite_trend:
            state = "WEAKENING"
            if explicit_weakening:
                reasons.append("CURRENT_SNAPSHOT_WEAKENING")
            if opposite_trend:
                reasons.append("OPPOSITE_TREND_STATE")
        evidence = dict(thesis_evidence or {})
        full_thesis_ready = evidence.get("full_thesis_ready") is True
        contradictions = [str(item) for item in (evidence.get("contradictions") or []) if str(item).strip()]
        if state == "VALID" and contradictions:
            state = "WEAKENING"
            reasons.extend(f"CURRENT_{item.upper()}_CONTRADICTS_FROZEN_THESIS" for item in contradictions)
        elif state == "VALID" and full_thesis_ready:
            reasons.append("FROZEN_THESIS_REVALIDATED_AGAINST_CURRENT_CANONICAL_EVIDENCE")
        elif state == "VALID":
            reasons.append("PRICE_GEOMETRY_VALID_FULL_THESIS_EVIDENCE_INCOMPLETE")

        initial_r = abs(entry - original_stop)
        adverse_r = ((entry - price) if long else (price - entry)) / initial_r if initial_r > 0 else 0.0
        favorable_to_target = ((price - entry) if long else (entry - price)) / abs(target - entry) if target != entry else 0.0
        return {
            "state": state,
            "reasons": reasons,
            "price": price,
            "entry": entry,
            "original_stop": original_stop,
            "managed_stop": stop,
            "target": target,
            "adverse_r": round(adverse_r, 6),
            "target_progress": round(favorable_to_target, 6),
            "signal_age": age,
            "current_thesis_evidence": evidence,
            "full_thesis_validated": bool(full_thesis_ready),
            "validation_scope": "FULL_THESIS" if full_thesis_ready else "PRICE_GEOMETRY_OR_PARTIAL_EVIDENCE",
            "authority": cls.authority,
            "authority_version": cls.authority_version,
            "policy": "reassess original frozen thesis against current verified price plus canonical current evidence; missing domains cannot be called full-thesis validation; never create direction or widen risk",
        }


DEFAULT_LIVE_THESIS_REASSESSMENT = LiveThesisReassessmentService()
