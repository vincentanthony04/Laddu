"""Desk-aware scheduled-event admission policy."""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Dict, Mapping, Optional

from core.production_mode_policy import require_production_mode
from core.india_time import INDIA_TZ, india_now

EVENT_RISK_POLICY_VERSION = "event-risk-admission-1.0.0"


def _parse_date(value: Any) -> Optional[date]:
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in (None, "%d-%b-%Y", "%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00")) if fmt is None else datetime.strptime(text[:10], fmt)
            return parsed.date()
        except Exception:
            continue
    return None


def _decision_date(candidate: Mapping[str, Any]) -> date:
    raw = candidate.get("decision_as_of") or candidate.get("last_ai_validation")
    if raw:
        try:
            stamp = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            return stamp.astimezone(INDIA_TZ).date()
        except Exception:
            pass
    return india_now().date()


class EventRiskPolicyService:
    BLOCK_WINDOW_DAYS = {"intraday": 0, "delivery": 2}

    def evaluate(self, candidate: Mapping[str, Any]) -> Dict[str, Any]:
        mode = require_production_mode(candidate.get("mode"))
        risk = candidate.get("event_risk") or {}
        if not isinstance(risk, Mapping) or not bool(risk.get("flag")):
            return {
                "version": EVENT_RISK_POLICY_VERSION, "state": "CLEAR", "gate": "PASS",
                "policy": "scheduled-event policy may veto new promotion; it never changes direction or score",
            }
        raw_date = risk.get("nearest_event_date") or risk.get("event_date")
        event_date = _parse_date(raw_date)
        if event_date is None:
            return {
                "version": EVENT_RISK_POLICY_VERSION, "state": "FLAGGED_DATE_UNVERIFIED", "gate": "SHADOW",
                "nearest_event_date": raw_date,
                "reason": "event flag exists but date could not be normalised; surfaced for review",
                "policy": "unparseable event date cannot silently create a trade",
            }
        reference_date = _decision_date(candidate)
        days = (event_date - reference_date).days
        window = self.BLOCK_WINDOW_DAYS[mode]
        block = 0 <= days <= window
        return {
            "version": EVENT_RISK_POLICY_VERSION,
            "state": "BLOCKED_NEAR_EVENT" if block else "FLAGGED_OUTSIDE_BLOCK_WINDOW",
            "gate": "BLOCK" if block else "PASS",
            "nearest_event_date": event_date.isoformat(),
            "reference_date": reference_date.isoformat(),
            "days_until_event": days,
            "block_window_days": window,
            "reason": f"scheduled company event is {days} day(s) away" if block else "event is outside the desk block window",
            "policy": "Intraday blocks same-day scheduled events; Delivery blocks new entries within two calendar days",
        }
