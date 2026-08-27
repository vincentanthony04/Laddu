"""Compact HTTP projection for the precomputed Market Radar authority.

The background MarketRadarProjectionService owns the complete diagnostic
snapshot. This module only shapes that immutable snapshot for HTTP consumers;
it performs no persistence, provider, ranking or trading work.
"""
from __future__ import annotations

from typing import Any, Dict, Mapping


COMPACT_CONTRACT = "market-radar-compact-http-1.0.0"
FULL_CONTRACT = "market-radar-full-diagnostic-1.0.0"


def project_market_radar_http(snapshot: Mapping[str, Any] | None, *, full: bool = False) -> Dict[str, Any]:
    source = dict(snapshot or {})
    if full:
        return {**source, "payload_contract": FULL_CONTRACT}

    radar = dict(source.get("market_radar") or {})
    return {
        "ok": source.get("ok") is not False,
        "counts": dict(source.get("counts") or {}),
        "opportunities": list(source.get("opportunities") or []),
        "next_session_watchlist": list(source.get("next_session_watchlist") or []),
        "research_candidates": list(source.get("research_candidates") or []),
        "publication_policy": source.get("publication_policy"),
        "market_radar": {
            key: radar.get(key)
            for key in (
                "coverage", "verified_coverage", "verified_coverage_pct",
                "data_state", "reason", "empty_reasons",
            )
            if key in radar
        },
        "projection_state": source.get("projection_state"),
        "projection_elapsed_ms": source.get("projection_elapsed_ms"),
        "price_refresh_contract": dict(source.get("price_refresh_contract") or {}),
        "time": source.get("time"),
        "payload_contract": COMPACT_CONTRACT,
    }
