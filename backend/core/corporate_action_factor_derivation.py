"""Deterministic corporate-action factor derivation from official NSE terms.

No factor is inferred from a price jump. Structural factors are derived only when
published terms are sufficient. Unresolved structural actions stay fail-closed for
the affected instrument while zero-action instruments remain independently eligible.
"""
from __future__ import annotations

import math
import re
from typing import Any, Mapping

VERSION = "corporate-action-factor-derivation-1.0.0-pl42"


def _text(value: Any) -> str:
    return " ".join(str(value or "").upper().replace("₹", " RS ").split())


def _positive(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def classify_action(purpose: Any, action_type: Any = None) -> str:
    explicit = _text(action_type)
    source = explicit + " " + _text(purpose)
    if any(token in source for token in ("DEMERGER", "DE-MERGER", "MERGER", "AMALGAMATION", "CAPITAL REDUCTION", "REDUCTION OF CAPITAL")):
        return "UNRESOLVED_STRUCTURAL"
    if "BONUS" in source:
        return "BONUS"
    if any(token in source for token in ("SPLIT", "SUB-DIVISION", "SUB DIVISION", "SUBDIVISION")):
        return "SPLIT"
    if any(token in source for token in ("CONSOLIDATION", "CONSOLIDATE")):
        return "CONSOLIDATION"
    if "RIGHT" in source:
        return "RIGHTS"
    if any(token in source for token in ("DIVIDEND", "INTEREST", "CASH DISTRIBUTION")):
        return "CASH_ONLY"
    if "BUYBACK" in source or "BUY BACK" in source:
        return "CASH_ONLY"
    return "OTHER"


def _ratio(text: str) -> tuple[float, float] | None:
    """Return (new_or_entitlement, existing) for common A:B wording."""
    patterns = (
        r"(?:RATIO\s*(?:OF)?\s*)?(\d+(?:\.\d+)?)\s*[:/]\s*(\d+(?:\.\d+)?)",
        r"(\d+(?:\.\d+)?)\s+(?:EQUITY\s+)?SHARES?\s+FOR\s+EVERY\s+(\d+(?:\.\d+)?)",
        r"(\d+(?:\.\d+)?)\s+FOR\s+(\d+(?:\.\d+)?)",
    )
    for pattern in patterns:
        m = re.search(pattern, text, flags=re.I)
        if m:
            a, b = _positive(m.group(1)), _positive(m.group(2))
            if a and b:
                return a, b
    return None


def _face_value_transition(text: str) -> tuple[float, float] | None:
    # Examples: FV Rs 10 to Rs 2; face value from 10/- to 2/-.
    patterns = (
        r"(?:FACE\s*VALUE|FV)[^0-9]{0,20}(\d+(?:\.\d+)?)\D{1,25}(?:TO|INTO)[^0-9]{0,20}(\d+(?:\.\d+)?)",
        r"FROM[^0-9]{0,15}(\d+(?:\.\d+)?)\D{1,25}TO[^0-9]{0,15}(\d+(?:\.\d+)?)",
    )
    for pattern in patterns:
        m = re.search(pattern, text, flags=re.I)
        if m:
            old, new = _positive(m.group(1)), _positive(m.group(2))
            if old and new:
                return old, new
    return None


def _rights_issue_price(text: str, face_value: float | None = None) -> float | None:
    direct_patterns = (
        r"(?:ISSUE\s*PRICE|AT|@)\s*(?:RS\.?\s*)?(\d+(?:\.\d+)?)",
        r"RS\.?\s*(\d+(?:\.\d+)?)\s*PER\s*(?:EQUITY\s*)?SHARE",
    )
    for pattern in direct_patterns:
        m = re.search(pattern, text, flags=re.I)
        if m:
            value = _positive(m.group(1))
            if value:
                return value
    premium = re.search(r"PREMIUM[^0-9]{0,20}(\d+(?:\.\d+)?)", text, flags=re.I)
    if premium and face_value:
        p = _positive(premium.group(1))
        if p:
            return float(face_value) + p
    return None


def derive_factors(
    *, purpose: Any, action_type: Any = None, face_value: Any = None,
    pre_ex_close: Any = None, explicit_price_factor: Any = None,
    explicit_volume_factor: Any = None,
) -> dict[str, Any]:
    """Return deterministic price/volume factors or an explicit unresolved state."""
    explicit_pf, explicit_vf = _positive(explicit_price_factor), _positive(explicit_volume_factor)
    kind = classify_action(purpose, action_type)
    text = _text(purpose)
    if explicit_pf and explicit_vf:
        return {"ok": True, "state": "EXPLICIT_SOURCE_FACTORS", "action_type": kind,
                "price_factor": explicit_pf, "volume_factor": explicit_vf,
                "authority_version": VERSION}
    if kind == "CASH_ONLY" or kind == "OTHER":
        # Cash distributions do not change share basis. Unknown non-structural rows
        # are recorded as neutral only when they do not advertise structural terms.
        structural_words = ("SPLIT", "BONUS", "RIGHT", "CONSOLID", "MERGER", "DEMERGER", "CAPITAL")
        if kind == "OTHER" and any(word in text for word in structural_words):
            return {"ok": False, "state": "UNRESOLVED_STRUCTURAL_TERMS", "action_type": kind,
                    "authority_version": VERSION}
        return {"ok": True, "state": "NO_SHARE_BASIS_ADJUSTMENT", "action_type": "OTHER",
                "price_factor": 1.0, "volume_factor": 1.0, "authority_version": VERSION}
    if kind == "UNRESOLVED_STRUCTURAL":
        return {"ok": False, "state": "UNRESOLVED_STRUCTURAL_ACTION", "action_type": kind,
                "authority_version": VERSION}
    if kind == "BONUS":
        ratio = _ratio(text)
        if not ratio:
            return {"ok": False, "state": "BONUS_RATIO_MISSING", "action_type": kind, "authority_version": VERSION}
        bonus, existing = ratio
        return {"ok": True, "state": "DERIVED_FROM_OFFICIAL_TERMS", "action_type": "BONUS",
                "price_factor": existing / (existing + bonus), "volume_factor": (existing + bonus) / existing,
                "authority_version": VERSION}
    if kind in {"SPLIT", "CONSOLIDATION"}:
        transition = _face_value_transition(text)
        if transition:
            old, new = transition
            return {"ok": True, "state": "DERIVED_FROM_OFFICIAL_TERMS", "action_type": kind,
                    "price_factor": new / old, "volume_factor": old / new,
                    "authority_version": VERSION}
        ratio = _ratio(text)
        if ratio:
            new_shares, old_shares = ratio
            # For N new shares for M old shares, current-basis historical price scales M/N.
            return {"ok": True, "state": "DERIVED_FROM_OFFICIAL_TERMS", "action_type": kind,
                    "price_factor": old_shares / new_shares, "volume_factor": new_shares / old_shares,
                    "authority_version": VERSION}
        return {"ok": False, "state": "SHARE_BASIS_TERMS_MISSING", "action_type": kind, "authority_version": VERSION}
    if kind == "RIGHTS":
        ratio = _ratio(text)
        pre = _positive(pre_ex_close)
        fv = _positive(face_value)
        issue = _rights_issue_price(text, fv)
        if not ratio or not pre or not issue:
            return {"ok": False, "state": "RIGHTS_TERMS_OR_PRE_EX_CLOSE_MISSING", "action_type": kind,
                    "authority_version": VERSION}
        rights, existing = ratio
        terp = (existing * pre + rights * issue) / (existing + rights)
        return {"ok": True, "state": "DERIVED_FROM_OFFICIAL_TERMS_AND_PRE_EX_CLOSE", "action_type": "RIGHTS",
                "price_factor": terp / pre, "volume_factor": (existing + rights) / existing,
                "authority_version": VERSION, "pre_ex_close": pre, "issue_price": issue}
    return {"ok": False, "state": "UNSUPPORTED_ACTION", "action_type": kind, "authority_version": VERSION}
