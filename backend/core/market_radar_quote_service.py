"""Verified Market Trends quote hydration and cache projection.

The broad V3 LTP lane discovers movement cheaply.  This service selects a
balanced visible shortlist, upgrades it through timestamped full quotes, and
merges only stronger identity/freshness observations into the radar cache.
It never creates trade authority.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Callable, Dict, Iterable, List, Mapping, MutableMapping

from core.india_time import india_now
from core.quote_integrity_service import classify_quote


class MarketRadarQuoteService:
    @staticmethod
    def display_shortlist(
        batch: List[Dict[str, Any]],
        quotes: List[Dict[str, Any]],
        *,
        limit: int = 24,
    ) -> List[Dict[str, Any]]:
        """Select sign-balanced radar leaders for exact-token hydration."""
        by_symbol = {
            str(row.get("trading_symbol") or row.get("symbol") or "").upper().strip(): row
            for row in (batch or [])
        }
        usable = [
            row for row in (quotes or [])
            if str(row.get("symbol") or row.get("trading_symbol") or "").upper().strip() in by_symbol
            and row.get("ltp") is not None
        ]
        gainers = sorted(
            (row for row in usable if float(row.get("change_pct") or 0.0) > 0),
            key=lambda row: float(row.get("change_pct") or 0.0),
            reverse=True,
        )[:6]
        losers = sorted(
            (row for row in usable if float(row.get("change_pct") or 0.0) < 0),
            key=lambda row: float(row.get("change_pct") or 0.0),
        )[:6]
        active = sorted(
            usable,
            key=lambda row: (
                float(row.get("activity_score") or 0.0),
                abs(float(row.get("change_pct") or 0.0)),
            ),
            reverse=True,
        )
        selected, seen = [], set()
        for row in gainers + losers + active:
            symbol = str(row.get("symbol") or row.get("trading_symbol") or "").upper().strip()
            instrument = by_symbol.get(symbol)
            if symbol and symbol not in seen and instrument and instrument.get("instrument_key"):
                selected.append(instrument)
                seen.add(symbol)
            if len(selected) >= max(1, limit):
                break
        return selected

    @classmethod
    def fetch_display_quotes(
        cls,
        client: Any,
        batch: List[Dict[str, Any]],
        discovery_quotes: List[Dict[str, Any]],
        *,
        enrich: Callable[[List[Dict[str, Any]]], List[Dict[str, Any]]],
    ) -> List[Dict[str, Any]]:
        """Upgrade visible leaders through the provider full-quote endpoint."""
        full_quote_fn = getattr(client, "full_quotes", None)
        if not discovery_quotes or not callable(full_quote_fn):
            return []
        instruments = cls.display_shortlist(batch, discovery_quotes)
        if not instruments:
            return []
        try:
            rows = full_quote_fn(instruments, persist=False) or []
        except TypeError:
            rows = full_quote_fn(instruments) or []
        return enrich(rows)

    @staticmethod
    def classify_row(
        row: Dict[str, Any],
        *,
        market_open: bool,
        now: datetime | None = None,
    ) -> Dict[str, Any]:
        integrity = classify_quote(
            row,
            now=now or india_now(),
            market_open=market_open,
            max_live_age_sec=45.0,
        )
        source_time = integrity.get("source_time")
        state = str(integrity.get("state") or "unverified")
        return dict(
            row,
            source_time=source_time,
            freshness_state=state,
            freshness_reason=integrity.get("reason"),
            stale=state not in {"live", "closed_market"},
            identity_verified=bool(integrity.get("identity_verified")),
            usable_for_promotion=False,
            provider_timestamp_verified=bool(integrity.get("provider_timestamp_verified")),
            freshness=(
                f"{state.replace('_', ' ')} @ {source_time}"
                if source_time
                else f"{state.replace('_', ' ')} · provider timestamp unavailable"
            ),
        )

    @classmethod
    def classify_rows(
        cls,
        discovery_quotes: Iterable[Dict[str, Any]],
        display_quotes: Iterable[Dict[str, Any]],
        *,
        market_open: bool,
        now: datetime | None = None,
    ) -> List[Dict[str, Any]]:
        return [
            cls.classify_row(row, market_open=market_open, now=now)
            for row in [*(discovery_quotes or []), *(display_quotes or [])]
        ]

    @staticmethod
    def by_symbol(rows: Iterable[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
        return {
            str(row.get("symbol") or row.get("trading_symbol") or "").upper().strip(): row
            for row in rows
            if str(row.get("symbol") or row.get("trading_symbol") or "").strip()
        }

    @staticmethod
    def merge_cache(
        cache: MutableMapping[str, Dict[str, Any]],
        rows: Iterable[Dict[str, Any]],
        *,
        seen_at: str,
        max_entries: int = 2500,
    ) -> None:
        """Merge discovery/full rows without downgrading verified observations."""
        for row in rows:
            symbol = str(row.get("symbol") or row.get("trading_symbol") or "").upper().strip()
            if not symbol:
                continue
            previous = dict(cache.get(symbol) or {})
            current_verified = str(row.get("freshness_state") or "") in {"live", "closed_market"}
            previous_verified = str(previous.get("freshness_state") or "") in {"live", "closed_market"}
            if current_verified or not previous or not previous_verified:
                cache[symbol] = dict(previous, **row, _coverage_seen_at=seen_at)
        overflow = max(0, len(cache) - max_entries)
        for old_key in list(cache)[:overflow]:
            cache.pop(old_key, None)
