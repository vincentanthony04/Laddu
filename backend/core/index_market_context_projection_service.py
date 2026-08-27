"""Background-only index membership/breadth projection.

This service deliberately performs **no provider/network I/O**.  It joins:
1) already-resolved index price rows,
2) point-in-time official NSE constituent membership, and
3) already-acquired constituent quote memory.

Static constituent lists are accepted only as continuity diagnostics.  They can
produce visible breadth counts but never decision-usable Direction/Conviction.
"""
from __future__ import annotations

from datetime import date
import time
from typing import Any, Mapping, Sequence

from core.heatmap_index_catalog import heatmap_index_identity
from core.index_breadth_authority import IndexBreadthAuthority
from core.index_membership_authority import IndexMembershipAuthority
from core.market_clock import parse_timestamp

AUTHORITY_NAME = "IndexMarketContextProjectionService"
AUTHORITY_VERSION = "1.0.0"


class IndexMarketContextProjectionService:
    authority = AUTHORITY_NAME
    authority_version = AUTHORITY_VERSION

    def __init__(
        self,
        *,
        membership_authority: IndexMembershipAuthority | None = None,
        breadth_authority: IndexBreadthAuthority | None = None,
        membership_cache_seconds: float = 900.0,
    ):
        self.membership = membership_authority or IndexMembershipAuthority()
        self.breadth = breadth_authority or IndexBreadthAuthority()
        self.membership_cache_seconds = max(1.0, float(membership_cache_seconds))
        self._membership_cache: dict[tuple[str, str, tuple[str, ...]], tuple[float, dict[str, Any]]] = {}

    @staticmethod
    def _canonical_index_name(row: Mapping[str, Any]) -> str | None:
        for value in (
            row.get("canonical_display_name"), row.get("display_name"), row.get("chart_query"),
            row.get("name"), row.get("trading_symbol"), row.get("instrument_key"),
        ):
            identity = heatmap_index_identity(str(value or ""))
            if identity:
                return str(identity.get("canonical_display_name") or identity.get("display_name") or "").upper().strip() or None
        return None

    @staticmethod
    def _source_time(row: Mapping[str, Any]) -> str | None:
        for field in ("source_time", "provider_timestamp", "timestamp", "updated_at", "last_refresh"):
            value = row.get(field)
            if value not in (None, ""):
                return str(value)
        return None

    @staticmethod
    def _quote_map(quotes: Mapping[str, Mapping[str, Any]] | Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
        if isinstance(quotes, Mapping):
            return {str(key): dict(value) for key, value in quotes.items() if isinstance(value, Mapping)}
        result: dict[str, dict[str, Any]] = {}
        for raw in quotes or ():
            if not isinstance(raw, Mapping):
                continue
            row = dict(raw)
            key = str(row.get("symbol") or row.get("trading_symbol") or row.get("instrument_key") or "").upper().strip()
            if key:
                result[key] = row
        return result

    def _membership_snapshot(self, index_name: str, as_of: str, fallback: Sequence[str]) -> dict[str, Any]:
        fallback_key = tuple(sorted({str(symbol).upper().strip() for symbol in fallback if str(symbol).strip()}))
        key = (index_name, as_of, fallback_key)
        now = time.monotonic()
        cached = self._membership_cache.get(key)
        if cached and now - cached[0] <= self.membership_cache_seconds:
            return dict(cached[1])
        snapshot = self.membership.snapshot(index_name, as_of, fallback_symbols=fallback_key).as_dict()
        self._membership_cache[key] = (now, dict(snapshot))
        if len(self._membership_cache) > 256:
            stale = sorted(self._membership_cache.items(), key=lambda item: item[1][0])[:64]
            for stale_key, _ in stale:
                self._membership_cache.pop(stale_key, None)
        return snapshot

    def enrich_rows(
        self,
        rows: Sequence[Mapping[str, Any]],
        constituent_quotes: Mapping[str, Mapping[str, Any]] | Sequence[Mapping[str, Any]],
        *,
        as_of: date | str,
        fallback_constituents: Mapping[str, Sequence[str]] | None = None,
    ) -> list[dict[str, Any]]:
        quotes = self._quote_map(constituent_quotes)
        fallback_constituents = fallback_constituents or {}
        default_as_of = as_of.isoformat() if isinstance(as_of, date) else str(as_of)[:10]
        output: list[dict[str, Any]] = []
        for raw in rows or ():
            row = dict(raw)
            index_name = self._canonical_index_name(row)
            exchange = str(row.get("exchange") or "").upper().strip()
            price_source_time = self._source_time(row)
            price_ts = parse_timestamp(price_source_time)
            evidence_date = price_ts.date().isoformat() if price_ts else default_as_of
            row["index_market_context_projection_authority"] = self.authority
            row["index_market_context_projection_version"] = self.authority_version

            # NSE constituent membership is the only governed membership plane
            # currently implemented.  BSE/SENSEX and India VIX remain explicit
            # non-actionable states rather than borrowing NSE constituents.
            if not index_name or exchange == "BSE" or index_name == "INDIA VIX":
                row.update({
                    "membership_authority": "NONE" if exchange == "BSE" else "NOT_APPLICABLE",
                    "membership_state": "UNAVAILABLE" if exchange == "BSE" else "NOT_APPLICABLE",
                    "membership_decision_usable": False,
                    "breadth_authority_ready": False,
                    "breadth_decision_usable": False,
                    "breadth_reason": (
                        "governed BSE constituent membership authority not available"
                        if exchange == "BSE" else "constituent breadth not applicable to this index"
                    ),
                })
                output.append(row)
                continue

            fallback = fallback_constituents.get(index_name) or ()
            membership = self._membership_snapshot(index_name, evidence_date, fallback)
            breadth = self.breadth.compute(membership, quotes, source_time=price_source_time)
            row.update({
                "membership_authority": membership.get("authority"),
                "membership_authority_version": membership.get("authority_version"),
                "membership_source": membership.get("source"),
                "membership_state": membership.get("state"),
                "membership_date": membership.get("membership_date"),
                "membership_decision_usable": membership.get("decision_usable"),
                "membership_population": membership.get("eligible_population"),
                "membership_content_hashes": membership.get("content_hashes"),
                **breadth,
            })
            output.append(row)
        return output
