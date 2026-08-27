"""Canonical resolvable identities for governed market and sector indices.

Aliases are accepted only as lookup inputs. Every emitted row uses the exact
canonical display name and provider instrument key below. Unknown aliases are
not allowed into the customer market/sector table.
"""
from __future__ import annotations

from typing import Any, Dict


def _row(key: str, symbol: str, display: str, *, exchange: str = "NSE") -> Dict[str, Any]:
    segment = f"{exchange}_INDEX"
    return {
        "instrument_key": key,
        "trading_symbol": symbol,
        "chart_query": display,
        "display_name": display,
        "canonical_display_name": display,
        "exchange": exchange,
        "segment": segment,
        "instrument_type": "INDEX",
        "identity_verified": True,
        "identity_authority": "canonical_market_context_registry_v102",
    }


HEATMAP_INDEX_IDENTITIES: Dict[str, Dict[str, Any]] = {
    "NIFTY": _row("NSE_INDEX|Nifty 50", "NIFTY", "NIFTY 50"),
    "NXT50": _row("NSE_INDEX|Nifty Next 50", "NIFTYNXT50", "NIFTY NEXT 50"),
    "N100": _row("NSE_INDEX|Nifty 100", "NIFTY100", "NIFTY 100"),
    "N200": _row("NSE_INDEX|Nifty 200", "NIFTY200", "NIFTY 200"),
    "N500": _row("NSE_INDEX|Nifty 500", "NIFTY500", "NIFTY 500"),
    "SENSEX": _row("BSE_INDEX|SENSEX", "SENSEX", "SENSEX", exchange="BSE"),
    "VIX": _row("NSE_INDEX|India VIX", "INDIA VIX", "INDIA VIX"),
    "BANK": _row("NSE_INDEX|Nifty Bank", "BANKNIFTY", "NIFTY BANK"),
    "MIDCAP": _row("NSE_INDEX|Nifty Midcap 100", "NIFTYMIDCAP100", "NIFTY MIDCAP 100"),
    "SMALLCAP": _row("NSE_INDEX|Nifty Smallcap 100", "NIFTYSMALLCAP100", "NIFTY SMALLCAP 100"),
    "IT": _row("NSE_INDEX|Nifty IT", "NIFTYIT", "NIFTY IT"),
    "PHARMA": _row("NSE_INDEX|Nifty Pharma", "NIFTYPHARMA", "NIFTY PHARMA"),
    "AUTO": _row("NSE_INDEX|Nifty Auto", "NIFTYAUTO", "NIFTY AUTO"),
    "METAL": _row("NSE_INDEX|Nifty Metal", "NIFTYMETAL", "NIFTY METAL"),
    "FMCG": _row("NSE_INDEX|Nifty FMCG", "NIFTYFMCG", "NIFTY FMCG"),
    "PSUBANK": _row("NSE_INDEX|Nifty PSU Bank", "NIFTYPSUBANK", "NIFTY PSU BANK"),
    "PVTBANK": _row("NSE_INDEX|Nifty Private Bank", "NIFTYPVTBANK", "NIFTY PRIVATE BANK"),
    "REALTY": _row("NSE_INDEX|Nifty Realty", "NIFTYREALTY", "NIFTY REALTY"),
    "ENERGY": _row("NSE_INDEX|Nifty Energy", "NIFTYENERGY", "NIFTY ENERGY"),
    "OILGAS": _row("NSE_INDEX|Nifty Oil & Gas", "NIFTYOILANDGAS", "NIFTY OIL & GAS"),
    "HEALTHCARE": _row("NSE_INDEX|Nifty Healthcare Index", "NIFTYHEALTHCARE", "NIFTY HEALTHCARE"),
    "CONSUMDUR": _row("NSE_INDEX|Nifty Consumer Durables", "NIFTYCONSUMERDURABLES", "NIFTY CONSUMER DURABLES"),
    "MEDIA": _row("NSE_INDEX|Nifty Media", "NIFTYMEDIA", "NIFTY MEDIA"),
}


_ALIAS_TO_CODE: Dict[str, str] = {}
for code, row in HEATMAP_INDEX_IDENTITIES.items():
    candidates = {
        code,
        str(row["instrument_key"]),
        str(row["trading_symbol"]),
        str(row["display_name"]),
        str(row["chart_query"]),
    }
    if code == "NIFTY":
        candidates.update({"NIFTY 50", "NIFTY50"})
    if code == "VIX":
        candidates.update({"INDIA VIX", "INDIAVIX"})
    if code == "BANK":
        candidates.update({"NIFTY BANK", "BANK NIFTY"})
    if code == "CONSUMDUR":
        candidates.update({"CONSUMER", "NIFTY CONSUMER DURABLE"})
    for candidate in candidates:
        _ALIAS_TO_CODE[str(candidate).upper().strip()] = code


def heatmap_index_identity(name: str) -> Dict[str, Any] | None:
    code = _ALIAS_TO_CODE.get(str(name or "").upper().strip())
    row = HEATMAP_INDEX_IDENTITIES.get(code or "")
    return dict(row) if row else None


def canonical_index_code(name: str) -> str | None:
    return _ALIAS_TO_CODE.get(str(name or "").upper().strip())


def canonical_index_rows() -> list[Dict[str, Any]]:
    """Return one row per exact provider identity, without alias duplicates."""
    return [dict(row, catalog_code=code) for code, row in HEATMAP_INDEX_IDENTITIES.items()]
