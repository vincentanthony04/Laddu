"""Canonical immutable market/sector snapshot authority.

Only exact identities from the canonical market-context registry can enter the
customer snapshot. Price, breadth and direction are published atomically for
one evidence session; an older/partial poll cannot erase stronger truth or
carry a direction into a newer price with missing breadth.
"""
from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime
from typing import Any, Dict, Iterable, Mapping

from core.heatmap_index_catalog import heatmap_index_identity
from core.market_clock import parse_timestamp

_LOCK = threading.RLock()
_ROWS: Dict[str, Dict[str, Any]] = {}
_LOADED_STORE_IDS: set[int] = set()
AUTHORITY_NAME = "MarketContextSnapshotAuthority"
AUTHORITY_VERSION = "1.1.0"
KV_KEY = "market_sector_snapshot:v107_context_v1"

BROAD_INDEX_NAMES = {
    "NIFTY 50", "NIFTY NEXT 50", "NIFTY 100", "NIFTY 200", "NIFTY 500",
    "NIFTY MIDCAP 100", "NIFTY SMALLCAP 100", "SENSEX", "INDIA VIX",
}
SECTOR_INDEX_TOKENS = (
    "BANK", "IT", "PHARMA", "AUTO", "METAL", "FMCG", "ENERGY",
    "REALTY", "MEDIA", "INFRA", "HEALTHCARE", "OIL & GAS",
    "FINANCIAL SERVICES", "CONSUMER DURABLES", "PSU", "PRIVATE BANK",
)

REQUIRED_MARKET_ROWS = (
    "NIFTY", "SENSEX", "VIX", "BANK", "IT", "AUTO", "FMCG", "PHARMA",
    "METAL", "REALTY", "ENERGY",
)


def _timestamp(row: Mapping[str, Any], *fields: str):
    for field in fields:
        parsed = parse_timestamp(row.get(field))
        if parsed is not None:
            return parsed
    return None


def _same_session(left: datetime | None, right: datetime | None) -> bool:
    if left is None or right is None:
        return False
    # Provider timestamps can be UTC or IST; exact date equality after parsing
    # is deliberately conservative. A mismatch suppresses direction.
    return left.date() == right.date()


def _identity_for(row: Mapping[str, Any]) -> Dict[str, Any] | None:
    for value in (
        row.get("instrument_key"), row.get("canonical_display_name"),
        row.get("display_name"), row.get("chart_query"), row.get("trading_symbol"),
        row.get("symbol"), row.get("name"),
    ):
        identity = heatmap_index_identity(str(value or ""))
        if identity:
            return identity
    return None


def market_group(row: Mapping[str, Any]) -> str:
    name = str(
        row.get("canonical_display_name") or row.get("display_name") or
        row.get("name") or row.get("symbol") or ""
    ).upper().strip()
    if name in BROAD_INDEX_NAMES:
        return "index"
    if name.startswith("NIFTY ") and any(token in name for token in SECTOR_INDEX_TOKENS):
        return "sector"
    return "index"


def required_identity_rows() -> list[Dict[str, Any]]:
    rows: list[Dict[str, Any]] = []
    for code in REQUIRED_MARKET_ROWS:
        identity = heatmap_index_identity(code)
        if not identity:
            continue
        display_name = str(identity["canonical_display_name"])
        group = market_group(identity)
        rows.append({
            **identity,
            "name": display_name,
            "symbol": identity["trading_symbol"],
            "market_group": group,
            "type": "Sector" if group == "sector" else "Index",
            "freshness_state": "unavailable",
            "freshness_reason": "verified completed-session price pending",
            "stale": True,
            "session_result_verified": False,
            "direction_authority_ready": False,
            "direction_authority_reason": "price-and-breadth snapshot pending",
            "source": "canonical_market_context_registry_v103",
            "market_context_authority": AUTHORITY_NAME,
            "market_context_authority_version": AUTHORITY_VERSION,
        })
    return rows


def _key(row: Mapping[str, Any]) -> str:
    return str(row.get("instrument_key") or "").upper().strip()


def _load_persisted(store: Any | None) -> None:
    if store is None or id(store) in _LOADED_STORE_IDS:
        return
    try:
        raw = store.get_kv(KV_KEY, {}) or {}
        persisted = raw.get("rows") if isinstance(raw, Mapping) else []
        for raw_row in persisted or []:
            if not isinstance(raw_row, Mapping):
                continue
            row = _canonicalize(raw_row)
            if not row:
                continue
            row = _enforce_atomic_direction(row)
            _ROWS[str(row["instrument_key"]).upper()] = row
    except Exception:
        pass
    finally:
        _LOADED_STORE_IDS.add(id(store))


def _persist(store: Any | None, rows: list[Dict[str, Any]]) -> None:
    if store is None:
        return
    try:
        store.set_kv(KV_KEY, {"snapshot_id": snapshot_id(rows), "rows": rows})
    except Exception:
        pass


def _canonicalize(raw: Mapping[str, Any]) -> Dict[str, Any] | None:
    identity = _identity_for(raw)
    if not identity:
        return None
    source = dict(raw)
    # Runtime rows must be resolved by the PostgreSQL instrument catalogue.
    # Persisted v102 rows already carry this proof. A static alias alone is not
    # sufficient to display a price or direction.
    if not bool(source.get("identity_resolved") or source.get("identity_verified")):
        return None
    actual_key = str(source.get("instrument_key") or identity.get("instrument_key") or "").strip()
    if not actual_key or not actual_key.upper().startswith(("NSE_INDEX|", "BSE_INDEX|")):
        return None
    actual_symbol = str(source.get("trading_symbol") or source.get("symbol") or identity["trading_symbol"]).strip()
    row = {**source, **identity}
    display = str(identity["canonical_display_name"])
    row.update({
        "instrument_key": actual_key,
        "name": display,
        "display_name": display,
        "canonical_display_name": display,
        "chart_query": display,
        "symbol": actual_symbol,
        "trading_symbol": actual_symbol,
        "identity_verified": True,
        "identity_resolved": True,
        "identity_authority": "postgresql_instrument_catalogue_plus_canonical_market_context_registry_v103",
    })
    row["market_group"] = market_group(row)
    row["type"] = "Sector" if row["market_group"] == "sector" else "Index"
    return row


def _enforce_atomic_direction(row: Dict[str, Any]) -> Dict[str, Any]:
    """Fail closed unless price and *governed* breadth describe one session.

    Breadth counts are allowed to remain visible when partial/fallback so an
    operator can diagnose coverage, but those counts may not authorize a
    directional/conviction projection.  This closes the historical state where
    the UI could show 24 advances / 23 declines while simultaneously claiming
    that breadth was unavailable.
    """
    quote_time = _timestamp(row, "price_source_time", "source_time", "provider_timestamp", "timestamp", "updated_at")
    has_breadth = all(row.get(field) is not None for field in ("advances", "declines", "unchanged"))
    breadth_time = _timestamp(row, "breadth_source_time", "breadth_timestamp", "breadth_updated_at")
    governed_breadth = bool(row.get("breadth_authority_ready") or row.get("breadth_decision_usable"))
    if has_breadth and breadth_time is None:
        # Missing evidence time remains diagnostic-only.  Do not silently
        # inherit the quote timestamp and manufacture session alignment.
        breadth_time = None
    aligned = bool(has_breadth and governed_breadth and _same_session(quote_time, breadth_time))
    row["market_context_authority"] = AUTHORITY_NAME
    row["market_context_authority_version"] = AUTHORITY_VERSION
    row["direction_authority_ready"] = bool(aligned and row.get("ltp") is not None)
    if row["direction_authority_ready"]:
        row["direction_authority_reason"] = "price and governed complete breadth share one evidence session"
        row["direction_source_time"] = (breadth_time or quote_time).isoformat() if (breadth_time or quote_time) else None
        from core.index_direction_evidence_authority import DEFAULT_INDEX_DIRECTION_EVIDENCE_AUTHORITY
        projected = DEFAULT_INDEX_DIRECTION_EVIDENCE_AUTHORITY.project_direction(row)
        row.update(projected)
        if row.get("direction") is None:
            row["direction_authority_ready"] = False
            row["direction_authority_reason"] = projected.get("direction_evidence_reason") or "signed direction evidence unavailable"
    else:
        if not has_breadth:
            reason = "breadth unavailable"
        elif not governed_breadth:
            reason = str(row.get("breadth_reason") or row.get("reason") or "breadth is partial, fallback, or not governed")
        elif breadth_time is None:
            reason = "breadth evidence timestamp unavailable"
        else:
            reason = "price and breadth timestamps are not aligned"
        row["direction_authority_reason"] = reason
        for field in ("direction", "market_direction", "regime", "conviction", "confidence", "confidence_score"):
            row[field] = None
        row["direction_state"] = "UNAVAILABLE"
    return row


def stable_market_snapshot(rows: Iterable[Mapping[str, Any]], *, store: Any | None = None) -> list[Dict[str, Any]]:
    with _LOCK:
        _load_persisted(store)
        # Required identities are a validation roster, not synthetic customer
        # rows. Only catalogue-resolved runtime rows (or previously persisted
        # verified rows under this v102 key) enter the visible snapshot.
        incoming = [dict(row or {}) for row in (rows or [])]
        for raw in incoming:
            row = _canonicalize(raw)
            if not row:
                continue  # unresolved aliases never enter the governed UI
            key = str(row["instrument_key"]).upper()
            prior = dict(_ROWS.get(key) or {})
            prior_time = _timestamp(prior, "source_time", "provider_timestamp", "timestamp", "updated_at")
            next_time = _timestamp(row, "source_time", "provider_timestamp", "timestamp", "updated_at")
            # Never allow an older or empty incoming row to replace a newer
            # verified quote. Identity fields always come from the registry.
            merged = dict(prior)
            for field, value in row.items():
                if value not in (None, "", [], {}) or field not in merged:
                    merged[field] = value
            if prior_time and (next_time is None or prior_time > next_time):
                for field in (
                    "ltp", "last_price", "price", "change", "change_pct",
                    "percent_change", "previous_close", "source_time",
                    "provider_timestamp", "timestamp", "freshness_state",
                    "freshness_reason", "stale", "advances", "declines",
                    "unchanged", "breadth_source_time", "breadth_timestamp",
                ):
                    if prior.get(field) not in (None, ""):
                        merged[field] = prior[field]
            merged.update({k: v for k, v in row.items() if k in {
                "instrument_key", "trading_symbol", "chart_query", "display_name",
                "canonical_display_name", "exchange", "segment", "instrument_type",
                "identity_verified", "identity_authority", "market_group", "type",
            }})
            merged = _enforce_atomic_direction(merged)
            _ROWS[key] = merged
        output = [dict(_ROWS[key]) for key in sorted(_ROWS)]
        _persist(store, output)
        return output


def snapshot_id(rows: Iterable[Mapping[str, Any]]) -> str:
    material = [{
        "key": _key(row),
        "price": row.get("ltp") or row.get("last_price") or row.get("price"),
        "change_pct": row.get("change_pct") or row.get("percent_change"),
        "source_time": row.get("source_time") or row.get("timestamp"),
        "breadth_source_time": row.get("breadth_source_time"),
        "direction_ready": row.get("direction_authority_ready"),
        "group": row.get("market_group"),
    } for row in rows]
    return hashlib.sha256(json.dumps(material, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:24]
