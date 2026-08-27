from __future__ import annotations

from typing import Any, Dict, Optional

from core.candidate_eligibility_authority import DEFAULT_CANDIDATE_ELIGIBILITY_AUTHORITY

# Compatibility export for older callers/tests.  The set is owned by the
# canonical authority; this module contains no independent eligibility math.
GOOD_FUNDAMENTALS = DEFAULT_CANDIDATE_ELIGIBILITY_AUTHORITY.good_fundamentals


def _safe_float(value: Any) -> float:
    """Legacy helper retained for import compatibility only."""
    return DEFAULT_CANDIDATE_ELIGIBILITY_AUTHORITY._number(value)


def is_actionable_signal(
    decision: Dict[str, Any],
    market_open: Optional[bool] = None,
    *,
    require_final_authority: bool = True,
) -> bool:
    """Compatibility facade over CandidateEligibilityAuthority v1.0.0."""
    return DEFAULT_CANDIDATE_ELIGIBILITY_AUTHORITY.evaluate(
        decision,
        market_open=market_open,
        require_final_authority=require_final_authority,
        research_only=False,
    )["eligible"]


def is_publishable_research_signal(decision: Dict[str, Any], market_open: Optional[bool] = None) -> bool:
    """Research-only publication eligibility; never grants capital authority."""
    return DEFAULT_CANDIDATE_ELIGIBILITY_AUTHORITY.evaluate(
        decision,
        market_open=market_open,
        require_final_authority=True,
        research_only=True,
    )["eligible"]
