"""Atomic breadth projection from point-in-time membership and quote evidence.

Decision-usable breadth is deliberately stricter than a diagnostic count:
* membership must come from the governed point-in-time authority;
* every constituent must have a resolved change and provider/session timestamp;
* every constituent quote must describe one trading session; and
* when an index-price evidence timestamp is supplied, constituent breadth must
  describe that same session.

Partial/fallback counts remain useful diagnostics but may never authorize market
Direction/Conviction.
"""
from __future__ import annotations

from typing import Any, Mapping
import math

from core.market_clock import parse_timestamp

AUTHORITY_NAME = "IndexBreadthAuthority"
AUTHORITY_VERSION = "1.2.0-finite-fresh-atomic"


class IndexBreadthAuthority:
    authority = AUTHORITY_NAME
    authority_version = AUTHORITY_VERSION

    @staticmethod
    def _quote_for(symbol: str, quotes: Mapping[str, Mapping[str, Any]]) -> Mapping[str, Any] | None:
        key = str(symbol).upper().strip()
        if key in quotes:
            return quotes[key]
        for qkey, row in quotes.items():
            if str(qkey).upper().split("|")[-1].strip() == key:
                return row
            if str(row.get("symbol") or row.get("trading_symbol") or "").upper().strip() == key:
                return row
        return None

    @staticmethod
    def _quote_time(row: Mapping[str, Any] | None):
        if not row:
            return None
        for field in ("source_time", "provider_timestamp", "timestamp", "updated_at", "last_refresh"):
            parsed = parse_timestamp(row.get(field))
            if parsed is not None:
                return parsed
        return None

    def compute(
        self,
        membership: Mapping[str, Any],
        quotes: Mapping[str, Mapping[str, Any]],
        *,
        source_time: str | None = None,
    ) -> dict[str, Any]:
        """Return diagnostic breadth and fail-closed decision readiness.

        ``source_time`` is the index-price evidence timestamp when available.  It
        is *not* copied into breadth evidence; breadth_source_time is derived from
        actual constituent quote timestamps.
        """
        symbols = [str(item).upper().strip() for item in membership.get("symbols") or [] if str(item).strip()]
        advances = declines = unchanged = resolved = 0
        missing: list[str] = []
        missing_timestamps: list[str] = []
        quote_times = []
        for symbol in symbols:
            row = self._quote_for(symbol, quotes)
            change = None if row is None else row.get("change_pct", row.get("percent_change"))
            if change is None:
                missing.append(symbol)
                continue
            try:
                value = float(change)
            except (TypeError, ValueError):
                missing.append(symbol)
                continue
            if not math.isfinite(value):
                missing.append(symbol)
                continue
            resolved += 1
            if value > 0:
                advances += 1
            elif value < 0:
                declines += 1
            else:
                unchanged += 1
            quote_time = self._quote_time(row)
            if quote_time is None:
                missing_timestamps.append(symbol)
            else:
                quote_times.append(quote_time)

        eligible = len(symbols)
        complete = bool(eligible) and resolved == eligible
        governed_membership = bool(membership.get("decision_usable"))
        timestamps_complete = bool(eligible) and len(quote_times) == eligible and not missing_timestamps
        quote_session_dates = sorted({stamp.date().isoformat() for stamp in quote_times})
        one_quote_session = timestamps_complete and len(quote_session_dates) == 1
        price_time = parse_timestamp(source_time)
        price_session_date = price_time.date().isoformat() if price_time else None
        quote_session_date = quote_session_dates[0] if len(quote_session_dates) == 1 else None
        price_session_aligned = bool(
            one_quote_session
            and (price_session_date is None or quote_session_date == price_session_date)
        )
        # A same-date check is not enough for live breadth.  Use the index
        # evidence time when supplied, otherwise the newest constituent quote,
        # and require a tight, causal constituent window around that snapshot.
        reference_time = price_time or (max(quote_times) if quote_times else None)
        max_quote_age_seconds = 120.0
        quote_ages = [
            (reference_time - stamp).total_seconds()
            for stamp in quote_times
        ] if reference_time is not None else []
        causal_timestamps = bool(
            timestamps_complete
            and quote_ages
            and all(0.0 <= age <= max_quote_age_seconds for age in quote_ages)
        )
        quote_skew_seconds = (
            (max(quote_times) - min(quote_times)).total_seconds()
            if len(quote_times) >= 2 else 0.0 if quote_times else None
        )
        decision_usable = bool(
            complete
            and governed_membership
            and timestamps_complete
            and one_quote_session
            and price_session_aligned
            and causal_timestamps
        )
        breadth_ratio = (advances / resolved) if resolved else None
        breadth_source_time = max(quote_times).isoformat(timespec="seconds") if quote_times else None

        if decision_usable:
            reason = "official point-in-time membership and complete same-session constituent evidence"
        elif not governed_membership:
            reason = "official point-in-time membership unavailable"
        elif not complete:
            reason = "constituent evidence incomplete"
        elif not timestamps_complete:
            reason = "constituent quote timestamps incomplete"
        elif not one_quote_session:
            reason = "constituent quotes span multiple sessions"
        elif not price_session_aligned:
            reason = "constituent breadth session is not aligned to index price session"
        elif not causal_timestamps:
            reason = "constituent quote freshness/causality exceeds live breadth tolerance"
        else:
            reason = "breadth evidence unavailable"

        return {
            "authority": self.authority,
            "authority_version": self.authority_version,
            "membership_authority": membership.get("authority"),
            "membership_authority_version": membership.get("authority_version"),
            "membership_source": membership.get("source"),
            "membership_date": membership.get("membership_date"),
            "eligible_population": eligible,
            "resolved_population": resolved,
            "coverage_pct": round((resolved / eligible) * 100.0, 2) if eligible else 0.0,
            "advances": advances,
            "declines": declines,
            "unchanged": unchanged,
            "breadth_ratio": round(breadth_ratio, 6) if breadth_ratio is not None else None,
            "breadth_complete": complete,
            "breadth_authority_ready": decision_usable,
            "breadth_decision_usable": decision_usable,
            "decision_usable": decision_usable,
            "breadth_source_time": breadth_source_time,
            "price_source_time": price_time.isoformat(timespec="seconds") if price_time else source_time,
            "quote_session_date": quote_session_date,
            "price_session_date": price_session_date,
            "quote_session_dates": quote_session_dates,
            "quote_timestamps_complete": timestamps_complete,
            "quote_session_aligned": price_session_aligned,
            "quote_freshness_causal": causal_timestamps,
            "max_quote_age_seconds": max_quote_age_seconds,
            "observed_quote_ages_seconds": [round(age, 3) for age in quote_ages],
            "quote_skew_seconds": round(quote_skew_seconds, 3) if quote_skew_seconds is not None else None,
            "missing_symbols": missing[:50],
            "missing_timestamp_symbols": missing_timestamps[:50],
            "state": "READY" if decision_usable else "PARTIAL" if resolved else "UNAVAILABLE",
            "reason": reason,
            "breadth_reason": reason,
        }


DEFAULT_INDEX_BREADTH_AUTHORITY = IndexBreadthAuthority()
