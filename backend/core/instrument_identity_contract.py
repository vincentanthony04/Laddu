"""Exact symbol/instrument identity contract shared by charts and research.

Suggestion/search endpoints may return fuzzy matches.  Data and decision
endpoints may not: a direct IDEA request must resolve to trading_symbol IDEA
and one exact instrument_key across every component.
"""
from __future__ import annotations

from typing import Any, Dict, Mapping

AUTHORITY_NAME = "InstrumentIdentityAuthority"
AUTHORITY_VERSION = "1.1.0"

INDEX_SYMBOL_ALIASES = {
    # Broad indices and operator-facing short names.
    "NIFTY": "NIFTY 50", "NIFTY50": "NIFTY 50", "NIFTY 50": "NIFTY 50",
    "BANK": "NIFTY BANK", "BANKNIFTY": "NIFTY BANK", "NIFTY BANK": "NIFTY BANK",
    "SENSEX": "SENSEX", "BSE SENSEX": "SENSEX",
    "FINNIFTY": "NIFTY FINANCIAL SERVICES",
    "NIFTY FIN SERVICE": "NIFTY FINANCIAL SERVICES",
    "NIFTY FIN SERVICES": "NIFTY FINANCIAL SERVICES",
    "NIFTY FINANCIAL SERVICES": "NIFTY FINANCIAL SERVICES",
    "NIFTY NEXT 50": "NIFTY NEXT 50",
    "INDIA VIX": "INDIA VIX",

    # Sector aliases used by Chart Desk, Market Trends and verifier probes.
    "IT": "NIFTY IT", "NIFTY IT": "NIFTY IT",
    "PHARMA": "NIFTY PHARMA", "NIFTY PHARMA": "NIFTY PHARMA",
    "AUTO": "NIFTY AUTO", "NIFTY AUTO": "NIFTY AUTO",
    "METAL": "NIFTY METAL", "NIFTY METAL": "NIFTY METAL",
    "FMCG": "NIFTY FMCG", "NIFTY FMCG": "NIFTY FMCG",
    "PSUBANK": "NIFTY PSU BANK", "PSU BANK": "NIFTY PSU BANK", "NIFTY PSU BANK": "NIFTY PSU BANK",
    "PVTBANK": "NIFTY PRIVATE BANK", "PRIVATE BANK": "NIFTY PRIVATE BANK",
    "NIFTY PVT BANK": "NIFTY PRIVATE BANK", "NIFTY PRIVATE BANK": "NIFTY PRIVATE BANK",
    "REALTY": "NIFTY REALTY", "NIFTY REALTY": "NIFTY REALTY",
    "ENERGY": "NIFTY ENERGY", "NIFTY ENERGY": "NIFTY ENERGY",
    "OILGAS": "NIFTY OIL & GAS", "NIFTY OIL AND GAS": "NIFTY OIL & GAS",
    "NIFTY OIL & GAS": "NIFTY OIL & GAS",
    "HEALTHCARE": "NIFTY HEALTHCARE", "NIFTY HEALTHCARE INDEX": "NIFTY HEALTHCARE",
    "NIFTY HEALTHCARE": "NIFTY HEALTHCARE",
    "CONSUMDUR": "NIFTY CONSUMER DURABLES",
    "NIFTY CONSUMER DURABLE": "NIFTY CONSUMER DURABLES",
    "NIFTY CONSUMER DURABLES": "NIFTY CONSUMER DURABLES",
    "MEDIA": "NIFTY MEDIA", "NIFTY MEDIA": "NIFTY MEDIA",
}

# Backward-compatible private name for older imports/tests.
_INDEX_ALIASES = INDEX_SYMBOL_ALIASES


def normalize_symbol(value: Any) -> str:
    text = " ".join(str(value or "").strip().upper().replace("_", " ").split())
    return INDEX_SYMBOL_ALIASES.get(text, text)


def identity_contract(requested_symbol: Any, instrument: Mapping[str, Any] | None) -> Dict[str, Any]:
    inst = dict(instrument or {})
    requested = normalize_symbol(requested_symbol)
    resolved = normalize_symbol(inst.get("trading_symbol") or inst.get("symbol") or inst.get("name"))
    key = str(inst.get("instrument_key") or "").strip()
    ok = bool(requested and resolved == requested and key)
    return {
        "ok": ok,
        "requested_symbol": requested,
        "resolved_symbol": resolved,
        "instrument_key": key,
        "exchange": str(inst.get("exchange") or inst.get("segment") or ""),
        "reason": "exact instrument identity verified" if ok else (
            "instrument identity missing" if not key else f"resolved {resolved or 'unknown'} instead of {requested or 'unknown'}"
        ),
        "authority": AUTHORITY_NAME,
        "authority_version": AUTHORITY_VERSION,
        "version": "instrument-identity-contract-1.1.0",
    }


def same_instrument(left: Mapping[str, Any] | None, right: Mapping[str, Any] | None) -> bool:
    left_key = str((left or {}).get("instrument_key") or "").strip()
    right_key = str((right or {}).get("instrument_key") or "").strip()
    return bool(left_key and right_key and left_key == right_key)


def canonical_listing_identity(listing: Any) -> Dict[str, Any]:
    """Verify one canonical universe listing before desk eligibility uses it.

    This is intentionally provider-independent: a canonical listing must carry
    an exact cash exchange, provider key, symbol and INE security identity.
    The universe builder already rejects fuzzy/out-of-scope rows; this proof
    prevents downstream code from replacing that fact with a literal True.
    """
    exchange = str(getattr(listing, "exchange", "") or "").strip().upper()
    segment = str(getattr(listing, "segment", "") or "").strip().upper()
    symbol = str(getattr(listing, "symbol", "") or "").strip().upper()
    key = str(getattr(listing, "provider_instrument_key", "") or "").strip()
    isin = str(getattr(listing, "isin", "") or "").strip().upper()
    canonical = bool(getattr(listing, "canonical", False))
    expected_segment = f"{exchange}_EQ" if exchange in {"NSE", "BSE"} else ""
    ok = bool(
        canonical
        and exchange in {"NSE", "BSE"}
        and segment == expected_segment
        and symbol
        and key
        and isin.startswith("INE")
        and len(isin) == 12
    )
    return {
        "authority": AUTHORITY_NAME,
        "authority_version": AUTHORITY_VERSION,
        "ok": ok,
        "exchange": exchange,
        "segment": segment,
        "symbol": symbol,
        "instrument_key": key,
        "isin": isin,
        "reason": "canonical cash-equity identity verified" if ok else "canonical listing identity incomplete or inconsistent",
    }
