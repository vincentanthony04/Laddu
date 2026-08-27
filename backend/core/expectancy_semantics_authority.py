"""Canonical semantic lanes for Project Laddu expectancy / EV metrics.

The project intentionally uses several valid expectancy concepts.  This module
prevents them from being conflated in APIs, research gates or UI projections.
It owns terminology and units only; the underlying mathematics remain with the
existing governed calculation authorities.
"""
from __future__ import annotations

AUTHORITY_NAME = "ExpectancySemanticsAuthority"
AUTHORITY_VERSION = "1.0.0"

PROSPECTIVE_MODEL_EV = {
    "lane": "PROSPECTIVE_MODEL_EV",
    "units": "INR_EXPECTED_NET_PNL_FOR_DECLARED_QUANTITY",
    "owner": "ProductionDecisionMathService",
    "meaning": "Probability-weighted post-cost expected P&L from one frozen governed champion prediction.",
    "realized": False,
    "capital_gate_eligible": True,
}

MODEL_PAPER_REALIZED_EXPECTANCY = {
    "lane": "MODEL_PAPER_REALIZED_EXPECTANCY",
    "units": "INR_NET_PNL_PER_SETTLED_TRADE",
    "owner": "ModelPortfolioPerformanceService",
    "meaning": "Arithmetic mean of governed settled Model-Paper net rupee P&L.",
    "realized": True,
    "capital_gate_eligible": False,
}

WALK_FORWARD_NET_RETURN_EXPECTANCY = {
    "lane": "WALK_FORWARD_NET_RETURN_EXPECTANCY",
    "units": "FRACTIONAL_NET_RETURN_PER_TEST_OBSERVATION",
    "owner": "WalkForwardValidationService",
    "meaning": "Arithmetic mean of out-of-sample forward return after recorded costs.",
    "realized": True,
    "capital_gate_eligible": True,
}

FORWARD_SELECTION_EXPECTANCY = {
    "lane": "FORWARD_SELECTION_EXPECTANCY",
    "units": "NET_RETURN_BPS_PER_SETTLED_CANDIDATE",
    "owner": "SelectionResearchValidationService",
    "meaning": "Same-population forward selector mean net return in basis points, including recorded costs.",
    "realized": True,
    "capital_gate_eligible": True,
}

SIGNAL_ACCURACY_POINTS = {
    "lane": "SIGNAL_ACCURACY_POINTS",
    "units": "PRICE_POINTS",
    "owner": "SignalLedger/PerformanceJournal",
    "meaning": "Legacy/canonical signal directional outcome points for accuracy diagnostics only.",
    "realized": True,
    "capital_gate_eligible": False,
    "economic_performance_eligible": False,
}

LANES = {
    item["lane"]: item
    for item in (
        PROSPECTIVE_MODEL_EV,
        MODEL_PAPER_REALIZED_EXPECTANCY,
        WALK_FORWARD_NET_RETURN_EXPECTANCY,
        FORWARD_SELECTION_EXPECTANCY,
        SIGNAL_ACCURACY_POINTS,
    )
}


def lane(name: str) -> dict:
    key = str(name or "").strip().upper()
    if key not in LANES:
        raise ValueError(f"unknown expectancy lane {name!r}")
    return {"authority": AUTHORITY_NAME, "authority_version": AUTHORITY_VERSION, **LANES[key]}
