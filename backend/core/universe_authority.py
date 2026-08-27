"""Canonical security, listing, lifecycle, and immutable universe authority.

This module is deliberately independent from provider catalogues and scanner
workers.  A provider row is reference input only.  The output is one canonical
ordinary-equity listing per ISIN, with NSE preferred and BSE admitted only when
no eligible NSE listing exists.  Indices are returned in a separate context
collection and can never enter a desk snapshot.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from enum import Enum
import hashlib
import json
import re
from typing import Any, Iterable, Mapping, Sequence
from uuid import NAMESPACE_URL, uuid5


UNIVERSE_RULE_VERSION = "pl-universe-69.8.0"


class LifecycleState(str, Enum):
    ANNOUNCED = "ANNOUNCED"
    PRELISTED = "PRELISTED"
    ACTIVE_UNVERIFIED = "ACTIVE_UNVERIFIED"
    DATA_ACCUMULATING = "DATA_ACCUMULATING"
    DELIVERY_ELIGIBLE = "DELIVERY_ELIGIBLE"
    INTRADAY_ELIGIBLE = "INTRADAY_ELIGIBLE"
    SUSPENDED = "SUSPENDED"
    DELISTED = "DELISTED"


class LifecycleEventType(str, Enum):
    NEW_LISTING = "NEW_LISTING"
    SUSPENDED = "SUSPENDED"
    RESUMED = "RESUMED"
    DELISTED = "DELISTED"
    SYMBOL_CHANGED = "SYMBOL_CHANGED"
    COMPANY_NAME_CHANGED = "COMPANY_NAME_CHANGED"
    SERIES_CHANGED = "SERIES_CHANGED"
    PRIMARY_LISTING_CHANGED = "PRIMARY_LISTING_CHANGED"
    INSTRUMENT_KEY_CHANGED = "INSTRUMENT_KEY_CHANGED"


_NSE_ORDINARY_SERIES = {"EQ", "BE"}
_NSE_INTRADAY_SERIES = {"EQ"}
_BSE_ORDINARY_GROUPS = {"A", "B", "X", "XT", "T"}
_BSE_INTRADAY_GROUPS = {"A", "B", "X"}
_INDEX_SEGMENTS = {"NSE_INDEX", "BSE_INDEX"}
_SME_MARKERS = {"SM", "ST", "M", "MT", "MS"}
_UNSAFE_GROUPS = {"BZ", "Z", "ZP", "TS"}
_DISALLOWED_TYPES = {
    "ETF", "MF", "MUTUAL_FUND", "FUND", "SCHEME", "REIT", "INVIT",
    "PREFERENCE", "PREF", "WARRANT", "RIGHTS", "RE", "PARTLY_PAID",
    "PP", "BOND", "NCD", "DEBT", "GSEC", "T-BILL", "FUT", "FUTSTK",
    "FUTIDX", "CE", "PE", "OPTSTK", "OPTIDX",
}
_NAME_EXCLUSIONS = re.compile(
    r"\b(ETF|EXCHANGE TRADED FUND|MUTUAL FUND|REIT|INVIT|WARRANT|RIGHTS ENTITLEMENT|"
    r"PARTLY PAID|PREFERENCE SHARE|DEBENTURE|BOND|NCD|GOVERNMENT SECURITY|T[ -]?BILL)\b",
    re.IGNORECASE,
)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _upper(value: Any) -> str:
    return _text(value).upper()


def _segment(row: Mapping[str, Any]) -> str:
    value = _upper(row.get("segment"))
    if value:
        return value
    exchange = _upper(row.get("exchange"))
    key = _upper(row.get("instrument_key") or row.get("provider_instrument_key"))
    if exchange in {"NSE", "BSE"} and "INDEX" in key:
        return f"{exchange}_INDEX"
    if exchange in {"NSE", "BSE"}:
        return f"{exchange}_EQ"
    return ""


def _series(row: Mapping[str, Any]) -> str:
    return _upper(row.get("series") or row.get("group") or row.get("instrument_type"))


def _stable_id(kind: str, *parts: str) -> str:
    return str(uuid5(NAMESPACE_URL, "project-laddu/" + kind + "/" + "/".join(parts)))


@dataclass(frozen=True)
class Security:
    security_id: str
    isin: str
    company_id: str
    security_type: str
    share_class: str
    face_value: float | None
    lifecycle_state: str


@dataclass(frozen=True)
class Listing:
    listing_id: str
    security_id: str
    exchange: str
    segment: str
    symbol: str
    series: str
    provider_instrument_key: str
    display_name: str
    effective_from: str
    effective_to: str | None
    listing_state: str
    canonical: bool
    isin: str


@dataclass(frozen=True)
class Exclusion:
    provider_instrument_key: str
    symbol: str
    reason: str
    detail: str = ""


@dataclass(frozen=True)
class LifecycleEvent:
    event_type: str
    security_id: str
    listing_id: str
    previous: Mapping[str, Any] | None
    current: Mapping[str, Any] | None


@dataclass(frozen=True)
class CanonicalUniverse:
    securities: tuple[Security, ...]
    canonical_listings: tuple[Listing, ...]
    listing_aliases: tuple[Listing, ...]
    market_context: tuple[Mapping[str, Any], ...]
    exclusions: tuple[Exclusion, ...]
    rule_version: str = UNIVERSE_RULE_VERSION

    def listing_by_security(self) -> dict[str, Listing]:
        return {row.security_id: row for row in self.canonical_listings}


@dataclass(frozen=True)
class UniverseSnapshot:
    snapshot_id: str
    effective_date: str
    desk: str
    security_ids: tuple[str, ...]
    listing_ids: tuple[str, ...]
    rule_version: str
    content_hash: str
    inclusion_reasons: Mapping[str, tuple[str, ...]]
    exclusion_reasons: Mapping[str, tuple[str, ...]]
    population_count: int
    created_at: str

    def __post_init__(self) -> None:
        if self.population_count != len(self.security_ids):
            raise ValueError("snapshot population_count must equal immutable security_ids")
        if len(self.security_ids) != len(set(self.security_ids)):
            raise ValueError("one security may appear only once in a snapshot")
        if len(self.listing_ids) != self.population_count:
            raise ValueError("snapshot requires exactly one listing per security")


def classify_reference_row(row: Mapping[str, Any]) -> tuple[str, str]:
    """Return ``(kind, reason)`` where kind is equity/index/excluded."""
    segment = _segment(row)
    series = _series(row)
    security_type = _upper(
        row.get("security_type") or row.get("security_class") or row.get("asset_class")
    )
    isin = _upper(row.get("isin"))
    name = _text(row.get("name") or row.get("display_name"))
    key = _text(row.get("instrument_key") or row.get("provider_instrument_key"))
    symbol = _text(row.get("trading_symbol") or row.get("symbol"))
    state = _upper(row.get("listing_state") or row.get("status") or "ACTIVE")

    if segment in _INDEX_SEGMENTS:
        return "index", "READ_ONLY_MARKET_CONTEXT"
    if not key or not symbol:
        return "excluded", "IDENTITY_MISSING"
    if segment not in {"NSE_EQ", "BSE_EQ"}:
        return "excluded", "OUTSIDE_CASH_EQUITY_SEGMENT"
    if not isin or not isin.startswith("INE"):
        return "excluded", "INVALID_OR_NON_EQUITY_ISIN"
    if security_type in _DISALLOWED_TYPES or _NAME_EXCLUSIONS.search(name):
        return "excluded", "EXCLUDED_INSTRUMENT_TYPE"
    if series in _SME_MARKERS:
        return "excluded", "SME_LISTING"
    if series in _UNSAFE_GROUPS:
        return "excluded", "UNSAFE_SERIES"
    if segment == "NSE_EQ" and series not in _NSE_ORDINARY_SERIES:
        return "excluded", "UNSUPPORTED_NSE_SERIES"
    if segment == "BSE_EQ" and series not in _BSE_ORDINARY_GROUPS:
        return "excluded", "UNSUPPORTED_BSE_GROUP"
    if state in {"SUSPENDED", "DELISTED", "INACTIVE"}:
        return "excluded", state
    if row.get("expiry") not in (None, "") or row.get("option_type") not in (None, ""):
        return "excluded", "DERIVATIVE_IDENTITY"
    try:
        if row.get("strike") not in (None, "") and float(row.get("strike")) != 0.0:
            return "excluded", "DERIVATIVE_IDENTITY"
    except (TypeError, ValueError):
        return "excluded", "INVALID_STRIKE_IDENTITY"
    return "equity", "ELIGIBLE_ORDINARY_EQUITY"


def _listing(row: Mapping[str, Any], *, canonical: bool, effective_date: str) -> Listing:
    isin = _upper(row.get("isin"))
    exchange = "NSE" if _segment(row).startswith("NSE") else "BSE"
    symbol = _upper(row.get("trading_symbol") or row.get("symbol"))
    security_id = _stable_id("security", isin)
    return Listing(
        listing_id=_stable_id("listing", isin, exchange, symbol),
        security_id=security_id,
        exchange=exchange,
        segment=_segment(row),
        symbol=symbol,
        series=_series(row),
        provider_instrument_key=_text(row.get("instrument_key") or row.get("provider_instrument_key")),
        display_name=_text(row.get("name") or row.get("display_name") or symbol),
        effective_from=_text(row.get("effective_from") or effective_date),
        effective_to=_text(row.get("effective_to")) or None,
        listing_state=_upper(row.get("listing_state") or row.get("status") or "ACTIVE"),
        canonical=canonical,
        isin=isin,
    )


def build_canonical_universe(
    rows: Iterable[Mapping[str, Any]], *, effective_date: date | str | None = None
) -> CanonicalUniverse:
    day = str(effective_date or date.today())
    by_isin: dict[str, list[Mapping[str, Any]]] = {}
    market_context: list[Mapping[str, Any]] = []
    exclusions: list[Exclusion] = []
    for raw in rows:
        row = dict(raw or {})
        kind, reason = classify_reference_row(row)
        if kind == "index":
            market_context.append(row)
            continue
        if kind == "excluded":
            exclusions.append(Exclusion(
                _text(row.get("instrument_key") or row.get("provider_instrument_key")),
                _upper(row.get("trading_symbol") or row.get("symbol")), reason,
            ))
            continue
        by_isin.setdefault(_upper(row.get("isin")), []).append(row)

    securities: list[Security] = []
    canonical: list[Listing] = []
    aliases: list[Listing] = []
    for isin in sorted(by_isin):
        candidates = sorted(
            by_isin[isin],
            key=lambda row: (
                0 if _segment(row) == "NSE_EQ" else 1,
                _upper(row.get("trading_symbol") or row.get("symbol")),
                _text(row.get("instrument_key") or row.get("provider_instrument_key")),
            ),
        )
        winner = candidates[0]
        selected = _listing(winner, canonical=True, effective_date=day)
        canonical.append(selected)
        for loser in candidates[1:]:
            alias = _listing(loser, canonical=False, effective_date=day)
            aliases.append(alias)
            exclusions.append(Exclusion(
                alias.provider_instrument_key, alias.symbol,
                "CROSS_EXCHANGE_DUPLICATE" if alias.exchange != selected.exchange else "DUPLICATE_LISTING",
                f"canonical_listing_id={selected.listing_id}",
            ))
        securities.append(Security(
            security_id=selected.security_id,
            isin=isin,
            company_id=_stable_id("company", isin),
            security_type="ORDINARY_EQUITY",
            share_class="ORDINARY",
            face_value=(float(winner.get("face_value")) if winner.get("face_value") not in (None, "") else None),
            lifecycle_state=LifecycleState.DATA_ACCUMULATING.value,
        ))

    return CanonicalUniverse(
        securities=tuple(securities),
        canonical_listings=tuple(canonical),
        listing_aliases=tuple(aliases),
        market_context=tuple(sorted(market_context, key=lambda row: _upper(row.get("trading_symbol") or row.get("name")))),
        exclusions=tuple(sorted(exclusions, key=lambda row: (row.reason, row.symbol, row.provider_instrument_key))),
    )


def freeze_snapshot(
    universe: CanonicalUniverse,
    *,
    desk: str,
    effective_date: date | str,
    eligibility: Mapping[str, Mapping[str, Any]] | None = None,
) -> UniverseSnapshot:
    """Freeze one desk population; eligibility data is keyed by security_id.

    Delivery requires identity, acceptable price, completed daily coverage and
    liquidity.  Intraday adds live-spread/frequency readiness and stricter
    exchange series.  Missing evidence is an exclusion, never a silent pass.
    """
    desk_name = _upper(desk)
    if desk_name not in {"DELIVERY", "INTRADAY"}:
        raise ValueError("desk must be Delivery or Intraday")
    evidence = dict(eligibility or {})
    included_security: list[str] = []
    included_listing: list[str] = []
    inclusion: dict[str, tuple[str, ...]] = {}
    exclusion: dict[str, tuple[str, ...]] = {}

    for listing in universe.canonical_listings:
        row = dict(evidence.get(listing.security_id) or {})
        reasons: list[str] = []
        if listing.exchange == "NSE" and listing.series not in _NSE_ORDINARY_SERIES:
            reasons.append("UNSUPPORTED_SERIES")
        if listing.exchange == "BSE" and listing.series not in _BSE_ORDINARY_GROUPS:
            reasons.append("UNSUPPORTED_GROUP")
        if desk_name == "INTRADAY":
            if listing.exchange == "NSE" and listing.series not in _NSE_INTRADAY_SERIES:
                reasons.append("NOT_INTRADAY_SERIES")
            if listing.exchange == "BSE" and listing.series not in _BSE_INTRADAY_GROUPS:
                reasons.append("NOT_INTRADAY_GROUP")
        if row:
            if row.get("eligible") is False:
                reasons.append(str(row.get("eligibility_reason") or "DESK_ELIGIBILITY_NOT_MET"))
            if row.get("identity_verified") is False:
                reasons.append("IDENTITY_UNVERIFIED")
            if row.get("suspended"):
                reasons.append("SUSPENDED")
            if row.get("price") is not None and float(row["price"]) < float(row.get("min_price") or 20.0):
                reasons.append("PRICE_BELOW_FLOOR")
            if row.get("avg_turnover") is not None and float(row["avg_turnover"]) < float(row.get("min_turnover") or 50_000_000.0):
                reasons.append("LIQUIDITY_BELOW_FLOOR")
            if row.get("coverage_state") not in (None, "ACCEPTED", "REPAIRED"):
                reasons.append("DATA_COVERAGE_NOT_ACCEPTED")
            if desk_name == "INTRADAY" and row.get("spread_bps") is not None and float(row["spread_bps"]) > float(row.get("max_spread_bps") or 35.0):
                reasons.append("SPREAD_TOO_WIDE")
        if reasons:
            exclusion[listing.security_id] = tuple(sorted(set(reasons)))
            continue
        included_security.append(listing.security_id)
        included_listing.append(listing.listing_id)
        inclusion[listing.security_id] = (
            "CANONICAL_NSE_LISTING" if listing.exchange == "NSE" else "GENUINE_BSE_ONLY_FALLBACK",
            "ORDINARY_EQUITY",
            f"{desk_name}_RULES_PASSED",
        )

    digest_input = {
        "effective_date": str(effective_date), "desk": desk_name,
        "rule_version": universe.rule_version,
        "security_ids": included_security, "listing_ids": included_listing,
        "inclusion_reasons": inclusion, "exclusion_reasons": exclusion,
    }
    content_hash = hashlib.sha256(
        json.dumps(digest_input, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    snapshot_id = f"{str(effective_date)}:{desk_name}:{content_hash[:16]}"
    return UniverseSnapshot(
        snapshot_id=snapshot_id,
        effective_date=str(effective_date),
        desk=desk_name,
        security_ids=tuple(included_security),
        listing_ids=tuple(included_listing),
        rule_version=universe.rule_version,
        content_hash=content_hash,
        inclusion_reasons=inclusion,
        exclusion_reasons=exclusion,
        population_count=len(included_security),
        created_at=datetime.now(timezone.utc).isoformat(),
    )


def lifecycle_diff(previous: CanonicalUniverse, current: CanonicalUniverse) -> tuple[LifecycleEvent, ...]:
    old = previous.listing_by_security()
    new = current.listing_by_security()
    events: list[LifecycleEvent] = []
    for security_id in sorted(set(old) | set(new)):
        before, after = old.get(security_id), new.get(security_id)
        if before is None and after is not None:
            events.append(LifecycleEvent(LifecycleEventType.NEW_LISTING.value, security_id, after.listing_id, None, after.__dict__))
            continue
        if before is not None and after is None:
            events.append(LifecycleEvent(LifecycleEventType.DELISTED.value, security_id, before.listing_id, before.__dict__, None))
            continue
        assert before is not None and after is not None
        checks = (
            ("symbol", LifecycleEventType.SYMBOL_CHANGED),
            ("display_name", LifecycleEventType.COMPANY_NAME_CHANGED),
            ("series", LifecycleEventType.SERIES_CHANGED),
            ("provider_instrument_key", LifecycleEventType.INSTRUMENT_KEY_CHANGED),
            ("exchange", LifecycleEventType.PRIMARY_LISTING_CHANGED),
        )
        for attribute, event_type in checks:
            if getattr(before, attribute) != getattr(after, attribute):
                events.append(LifecycleEvent(event_type.value, security_id, after.listing_id, before.__dict__, after.__dict__))
        if before.listing_state == "SUSPENDED" and after.listing_state == "ACTIVE":
            events.append(LifecycleEvent(LifecycleEventType.RESUMED.value, security_id, after.listing_id, before.__dict__, after.__dict__))
        elif before.listing_state == "ACTIVE" and after.listing_state == "SUSPENDED":
            events.append(LifecycleEvent(LifecycleEventType.SUSPENDED.value, security_id, after.listing_id, before.__dict__, after.__dict__))
    return tuple(events)
