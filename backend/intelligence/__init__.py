"""
Project Laddu — Intelligence Object layer (v61.0.0).

This package is the first slice of the "Action Object Oriented Intelligence
System" described in the v61 architecture note. It does NOT introduce a new
scoring engine and does NOT duplicate any existing scanner/indicator logic.

Every number in an ActionObject or MarketObject already exists somewhere in
the codebase today (EvidenceEngineService, institutional_signal_service,
reference_data_service breadth, decision_engine_service market_context,
fundamentals.py). What was missing was ONE stable shape that every consumer
(dashboard, cockpit, future engines) can read instead of re-deriving its own
opinion from scattered fields.

    build_market_object(...)  -> MarketObject   (index/regime/breadth/FII-DII)
    build_action_object(...)  -> ActionObject    (one stock's full intelligence)
    build_action_objects(...) -> List[ActionObject]

These are pure functions: dict/dataclass in, dict out. No I/O, no DB, no
network calls happen in this package -- callers (routes_get.py) fetch data
using the existing services and pass the results in.
"""
from __future__ import annotations

from intelligence.market_object import MarketObject, build_market_object
from intelligence.delivery_object import DeliveryObject, build_delivery_object
from intelligence.fundamental_object import FundamentalObject, build_fundamental_object
from intelligence.action_object import (
    ActionObject,
    build_action_object,
    build_action_objects,
)
from intelligence.persistence import persist_action_objects, action_object_history

__all__ = [
    "MarketObject",
    "build_market_object",
    "DeliveryObject",
    "build_delivery_object",
    "FundamentalObject",
    "build_fundamental_object",
    "ActionObject",
    "build_action_object",
    "build_action_objects",
    "persist_action_objects",
    "action_object_history",
]
