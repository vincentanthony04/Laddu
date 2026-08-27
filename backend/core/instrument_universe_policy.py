"""Binding active-instrument policy for Project Laddu.

Current production scope is deliberately narrow:
- NSE cash equities are primary.
- BSE cash equities are retained only when the same security is not available on NSE.
- NSE/BSE cash indices are reference identities for market trend, structure,
  breadth and sector-rotation context.
- Derivatives are a future capability and are never admitted into the active
  search/scanner/readiness universe in this revision.

The policy is pure and deterministic so it can be exercised before any SQLite
write.  A refresh either produces one fully filtered catalogue or keeps the
previous usable catalogue; it never incrementally mixes provider-wide rows
with the active universe.
"""
from __future__ import annotations

from collections import Counter
from typing import Any, Dict, Iterable, List, Tuple

ACTIVE_UNIVERSE_REVISION = "nse-first-bse-fallback-ordinary-equity-v69.8.0"

_EQ_SEGMENTS = {"NSE_EQ", "BSE_EQ"}
_INDEX_SEGMENTS = {"NSE_INDEX", "BSE_INDEX"}
_DERIVATIVE_MARKERS = ("FO", "FUT", "OPT", "CD", "MCX")
_DISALLOWED_TYPES = {
    "CE", "PE", "FUT", "FUTIDX", "FUTSTK", "OPTIDX", "OPTSTK",
    "BOND", "NCD", "DEBT", "MF", "MUTUAL_FUND", "COMMODITY", "CURRENCY",
}

# Exchange series are not interchangeable.  The legacy v67.5.2 defect used
# the NSE EQ/BE allow-list for BSE and therefore rejected every real BSE row.
# These sets are intentionally conservative and represent ordinary listed
# company equity / SME / trade-to-trade series only.
_NSE_EQUITY_SERIES = {"EQ", "BE"}
_BSE_EQUITY_SERIES = {"A", "B", "X", "XT", "T"}

# Explicit BSE non-stock series observed in the provider catalogue.  F and G
# are fixed-income / government-security identities.  E/IF/P/R are not
# admitted until a separately approved product policy exists for them.
_BSE_NON_EQUITY_SERIES = {"F", "G", "E", "IF", "P", "R"}

_ETF_NAME_MARKERS = (
    " EXCHANGE TRADED FUND", " ETF", "ETF ", "NIFTYBEES", "BANKBEES",
    "GOLDBEES", "JUNIORBEES", "LIQUIDBEES", "SILVERBEES",
)
_DEBT_NAME_MARKERS = (
    "DEBENTURE", " NCD", "BOND", "TREASURY", "T-BILL", "TBILL",
    "G-SEC", "GOVERNMENT SECURITY", "STATE DEVELOPMENT LOAN", " SDL",
)
_FUND_NAME_MARKERS = (
    "MUTUAL FUND", "ASSET MANAGEMENT", "AMC LTD", "AMC LIMITED",
    "LIQUID FUND", "INDEX FUND", "OVERNIGHT FUND", "GILT FUND",
    "GROWTH FUND", "INCOME FUND", "BALANCED ADVANTAGE FUND",
)
_FUND_SECURITY_TYPES = {"MF", "MUTUAL_FUND", "ETF", "FUND", "SCHEME"}


def _upper(value: Any) -> str:
    return str(value or "").strip().upper()


_INDEX_SEARCH_ALIASES = {"NIFTY", "NIFTY50", "NIFTY 50", "SENSEX", "BSE SENSEX", "BANK", "BANKNIFTY", "NIFTY BANK", "FINNIFTY", "NIFTY NEXT 50", "INDIA VIX"}

def is_index_search_query(value: Any) -> bool:
    query = _upper(value).replace("_", " ")
    return query in _INDEX_SEARCH_ALIASES or query.startswith("NIFTY ")


def _segment(row: Dict[str, Any]) -> str:
    segment = _upper(row.get("segment"))
    if segment:
        return segment
    exchange = _upper(row.get("exchange"))
    key = _upper(row.get("instrument_key"))
    if exchange in ("NSE", "BSE") and key.startswith(exchange + "_INDEX|"):
        return exchange + "_INDEX"
    if exchange in ("NSE", "BSE") and key.startswith(exchange + "_EQ|"):
        return exchange + "_EQ"
    return exchange


def is_derivative_or_out_of_scope(row: Dict[str, Any]) -> bool:
    segment = _segment(row)
    instrument_type = _upper(row.get("instrument_type"))
    option_type = _upper(row.get("option_type"))
    expiry = _upper(row.get("expiry"))
    strike = row.get("strike")
    if option_type in {"CE", "PE"}:
        return True
    if instrument_type in _DISALLOWED_TYPES:
        return True
    if any(marker in segment for marker in _DERIVATIVE_MARKERS):
        return True
    # Cash equities and indices must not carry a derivative identity.
    if expiry:
        return True
    try:
        if strike not in (None, "") and float(strike) != 0.0:
            return True
    except (TypeError, ValueError):
        return True
    return False


def is_cash_equity(row: Dict[str, Any]) -> bool:
    segment = _segment(row)
    if segment not in _EQ_SEGMENTS or is_derivative_or_out_of_scope(row):
        return False
    instrument_type = _upper(row.get("instrument_type"))
    if segment == "NSE_EQ":
        if instrument_type not in _NSE_EQUITY_SERIES:
            return False
    elif segment == "BSE_EQ":
        if instrument_type in _BSE_NON_EQUITY_SERIES:
            return False
        if instrument_type not in _BSE_EQUITY_SERIES:
            return False
    else:
        return False
    symbol = _upper(row.get("trading_symbol"))
    name = _upper(row.get("name"))
    isin = _upper(row.get("isin"))
    security_type = _upper(row.get("security_type") or row.get("asset_class") or row.get("security_class"))
    if not symbol:
        return False
    # Indian mutual-fund scheme ISINs use the INF prefix. The installed
    # v67.5.3 probe proved that BSE stock-series codes alone can still carry
    # Nippon India Mutual Fund schemes, so asset identity is a second mandatory
    # gate rather than a name-only heuristic.
    if isin.startswith("INF") or security_type in _FUND_SECURITY_TYPES:
        return False
    if any(marker in name for marker in _FUND_NAME_MARKERS):
        return False
    if any(marker in name or marker in symbol for marker in _DEBT_NAME_MARKERS):
        return False
    # Equity ETFs are not active stock-search identities in the current scope.
    if any(marker in name or marker in symbol for marker in _ETF_NAME_MARKERS):
        return False
    return True


def is_cash_index(row: Dict[str, Any]) -> bool:
    segment = _segment(row)
    instrument_type = _upper(row.get("instrument_type"))
    if segment not in _INDEX_SEGMENTS:
        return False
    if is_derivative_or_out_of_scope(row):
        return False
    return instrument_type in {"", "INDEX"}


def _identity(row: Dict[str, Any]) -> str:
    """Cross-exchange identity used to suppress BSE duplicates.

    ISIN is authoritative.  When a provider row omits ISIN, symbol is used as
    a conservative fallback so an ordinary dual listing does not appear twice.
    """
    isin = _upper(row.get("isin"))
    if isin:
        return "ISIN:" + isin
    return "SYMBOL:" + _upper(row.get("trading_symbol"))



def allowed_equity_series() -> Dict[str, List[str]]:
    """Return the binding exchange-specific stock-series policy."""
    return {
        "NSE_EQ": sorted(_NSE_EQUITY_SERIES),
        "BSE_EQ": sorted(_BSE_EQUITY_SERIES),
    }

def build_active_universe(rows: Iterable[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """Return deterministic NSE-first/BSE-only/index catalogue and counts."""
    nse_equities: Dict[str, Dict[str, Any]] = {}
    bse_candidates: Dict[str, Dict[str, Any]] = {}
    indices: Dict[str, Dict[str, Any]] = {}
    rejected = Counter()

    for raw in rows:
        row = dict(raw or {})
        key = _upper(row.get("instrument_key"))
        if not key:
            rejected["missing_key"] += 1
            continue
        segment = _segment(row)
        row["segment"] = segment
        if segment.startswith("NSE"):
            row["exchange"] = "NSE"
        elif segment.startswith("BSE"):
            row["exchange"] = "BSE"

        if is_cash_index(row):
            indices[key] = row
            continue
        if not is_cash_equity(row):
            rejected["out_of_scope"] += 1
            continue

        identity = _identity(row)
        if segment == "NSE_EQ":
            nse_equities.setdefault(identity, row)
        elif segment == "BSE_EQ":
            bse_candidates.setdefault(identity, row)

    # BSE rows are admitted only when the same ISIN/symbol is absent on NSE.
    bse_only = {
        identity: row for identity, row in bse_candidates.items()
        if identity not in nse_equities
    }

    ordered_nse = sorted(nse_equities.values(), key=lambda r: (_upper(r.get("trading_symbol")), _upper(r.get("instrument_key"))))
    ordered_bse = sorted(bse_only.values(), key=lambda r: (_upper(r.get("trading_symbol")), _upper(r.get("instrument_key"))))
    ordered_indices = sorted(indices.values(), key=lambda r: (_segment(r), _upper(r.get("trading_symbol")), _upper(r.get("instrument_key"))))
    active = ordered_nse + ordered_bse + ordered_indices

    stats = {
        "nse_equities": len(ordered_nse),
        "bse_only_equities": len(ordered_bse),
        "indices": len(ordered_indices),
        "active_total": len(active),
        "bse_duplicates_suppressed": max(0, len(bse_candidates) - len(bse_only)),
        "provider_rows_rejected": int(sum(rejected.values())),
    }
    return active, stats
