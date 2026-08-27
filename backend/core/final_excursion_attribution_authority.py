"""Compatibility read projection over CanonicalTradeLifecycleAuthority excursion math."""
from __future__ import annotations
from typing import Any, Mapping
from core.canonical_trade_lifecycle_authority import DEFAULT_CANONICAL_TRADE_LIFECYCLE_AUTHORITY


class FinalExcursionAttributionAuthority:
    authority = DEFAULT_CANONICAL_TRADE_LIFECYCLE_AUTHORITY.authority
    authority_version = DEFAULT_CANONICAL_TRADE_LIFECYCLE_AUTHORITY.authority_version

    @classmethod
    def enrich(cls, record: Mapping[str, Any]) -> dict[str, Any]:
        return DEFAULT_CANONICAL_TRADE_LIFECYCLE_AUTHORITY.enrich_settlement(record)


DEFAULT_FINAL_EXCURSION_ATTRIBUTION_AUTHORITY = FinalExcursionAttributionAuthority()
