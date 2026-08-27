"""Canonical sector classification for Project Laddu.

Acquisition providers may use different sector/industry labels.  This authority
translates those labels into two *separate* canonical meanings:

* ``fundamental_sector`` -- the sector family used by sector-specific
  fundamental overlays; and
* ``market_sector_key`` -- the NSE sector-index family used for market/sector
  context.

The two are deliberately not collapsed.  For example Banking/NBFC are distinct
fundamental models while both can consume financial-sector market context.
Unknown labels fail closed; no generic sector is guessed.
"""
from __future__ import annotations

import re
from typing import Any

AUTHORITY_NAME = "SectorClassificationAuthority"
AUTHORITY_VERSION = "1.0.0"


def _clean(value: Any) -> str:
    text = str(value or "").strip().lower().replace("&", " and ")
    text = re.sub(r"[_\-/]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


# Order matters: specific financial/energy families precede broad words.
_FUNDAMENTAL_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("banking", ("private bank", "psu bank", "banking", "banks", "bank")),
    ("nbfc", ("housing finance", "non banking finance", "non banking financial", "nbfc")),
    ("it", ("information technology", "software", "technology services", "it services")),
    ("pharma", ("pharmaceuticals", "pharmaceutical", "pharma", "healthcare")),
    ("fmcg", ("fast moving consumer goods", "fmcg", "consumer staples")),
    ("auto", ("automobile", "automobiles", "auto components", "auto ancillary", "automotive", "auto")),
    ("defence", ("defence", "defense", "aerospace and defence", "aerospace and defense")),
    ("cement", ("cement",)),
    ("steel", ("iron and steel", "steel", "metals", "metal")),
    ("renewable", ("renewable energy", "renewables")),
    ("power", ("power generation", "power transmission", "power", "utilities", "utility")),
    ("energy", ("oil and gas", "oil gas", "energy")),
    ("telecom", ("telecommunication", "telecom")),
    ("real_estate", ("real estate", "realty")),
    ("chemicals", ("specialty chemicals", "speciality chemicals", "chemicals", "chemical")),
    ("aviation", ("airlines", "airline", "aviation", "airport")),
)

_MARKET_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("PVTBANK", ("private bank",)),
    ("PSUBANK", ("psu bank", "public sector bank")),
    ("BANK", ("banking", "banks", "bank", "financial services")),
    ("AUTO", ("automobile", "automobiles", "auto components", "auto ancillary", "automotive", "auto", "ev")),
    ("IT", ("information technology", "software", "technology services", "it services")),
    ("PHARMA", ("pharmaceuticals", "pharmaceutical", "pharma")),
    ("HEALTHCARE", ("healthcare", "hospitals", "hospital")),
    ("FMCG", ("fast moving consumer goods", "fmcg", "consumer staples", "food products", "beverages")),
    ("METAL", ("iron and steel", "steel", "metals", "metal")),
    ("REALTY", ("real estate", "realty")),
    ("OILGAS", ("oil and gas", "oil gas")),
    ("ENERGY", ("renewable energy", "renewables", "power", "utilities", "utility", "energy")),
    ("CONSUMDUR", ("consumer durables", "consumer durable")),
    ("MEDIA", ("media", "entertainment")),
)


# This policy is consumed by the provider-independent fundamental dimension
# authority.  It is intentionally broader than fundamental_sector because
# several distinct sectors share the same balance-sheet economics.
_POLICY_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("asset_light_financial_services", ("registrar", "transfer agent", "exchange", "depository", "asset management", "broker", "capital market", "financial infrastructure")),
    ("banking", ("private bank", "psu bank", "banking", "banks", "bank")),
    ("nbfc", ("housing finance", "non banking finance", "non banking financial", "nbfc")),
    ("insurance", ("insurance",)),
    ("utilities_infrastructure", ("utility", "power", "energy", "infrastructure", "telecom")),
    ("asset_light_services", ("information technology", "software", "consulting", "technology services", "it services")),
    ("consumer", ("consumer", "fmcg", "retail", "food", "beverage")),
)


def _match(text: str, rules: tuple[tuple[str, tuple[str, ...]], ...]) -> str | None:
    if not text:
        return None
    # Exact match gets precedence over substring matching.
    for canonical, aliases in rules:
        if text == canonical or text in aliases:
            return canonical
    for canonical, aliases in rules:
        if any(alias in text for alias in aliases):
            return canonical
    return None


class SectorClassificationAuthority:
    authority = AUTHORITY_NAME
    authority_version = AUTHORITY_VERSION

    def classify(self, sector: Any = None, industry: Any = None) -> dict[str, Any]:
        raw_sector = _clean(sector)
        raw_industry = _clean(industry)
        text = raw_sector or raw_industry
        # If the broad sector is too generic, allow a more specific industry
        # to resolve the family before falling back to the broad label.
        candidates = [x for x in (raw_industry, raw_sector) if x]
        fundamental = next((_match(x, _FUNDAMENTAL_RULES) for x in candidates if _match(x, _FUNDAMENTAL_RULES)), None)
        market_key = next((_match(x, _MARKET_RULES) for x in candidates if _match(x, _MARKET_RULES)), None)
        policy = next((_match(x, _POLICY_RULES) for x in candidates if _match(x, _POLICY_RULES)), None) or "manufacturing_general"
        return {
            "authority": self.authority,
            "authority_version": self.authority_version,
            "raw_sector": raw_sector or None,
            "raw_industry": raw_industry or None,
            "fundamental_sector": fundamental,
            "market_sector_key": market_key,
            "fundamental_policy": policy,
            "classified": bool(fundamental or market_key),
            "state": "CLASSIFIED" if (fundamental or market_key) else "UNMAPPED",
            "policy": "deterministic provider-label normalization; unknown labels fail closed and are never guessed",
        }

    def fundamental_sector(self, value: Any, industry: Any = None) -> str | None:
        return self.classify(value, industry).get("fundamental_sector")

    def market_sector_key(self, value: Any, industry: Any = None) -> str:
        return str(self.classify(value, industry).get("market_sector_key") or "")

    def fundamental_policy(self, value: Any, industry: Any = None) -> str:
        return str(self.classify(value, industry).get("fundamental_policy") or "manufacturing_general")


DEFAULT_SECTOR_CLASSIFICATION_AUTHORITY = SectorClassificationAuthority()
