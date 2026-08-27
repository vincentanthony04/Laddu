"""Versioned sector-specific fundamental checklist authority.

The authority separates *evidence integration* from *scoring*. Existing Laddu
sector bands are preserved exactly. Sector families that are required for
research completeness but do not yet have validated scoring bands are
EVIDENCE_ONLY: their metrics are captured and surfaced, but they contribute
zero points to the canonical fundamental score until a separately versioned
band is approved. This avoids manufacturing edge by inventing thresholds.
"""
from __future__ import annotations

from typing import Any, Mapping

AUTHORITY_NAME = "SectorFundamentalChecklistAuthority"
AUTHORITY_VERSION = "1.0.0"

# Existing established Laddu bands, preserved byte-for-byte in meaning:
# (good, bad, higher_is_better).
SCORED_TEMPLATES: dict[str, dict[str, tuple[float, float, bool]]] = {
    "fmcg": {
        "volume_growth": (6, 0, True),
        "gross_margin": (45, 25, True),
        "ebitda_margin": (18, 8, True),
    },
    "banking": {
        "nim": (4.0, 2.0, True),
        "casa_ratio": (45, 20, True),
        "gross_npa": (1.5, 6, False),
        "net_npa": (0.5, 3, False),
        "pcr": (75, 40, True),
        "credit_growth": (15, 5, True),
        "deposit_growth": (12, 4, True),
        "cost_to_income": (42, 60, False),
        "roa": (1.3, 0.4, True),
    },
    "nbfc": {
        "aum_growth": (18, 5, True),
        "nim": (7, 3, True),
        "gnpa": (2, 6, False),
        "nnpa": (1, 3, False),
        "credit_cost": (1.5, 4, False),
        "collection_efficiency": (98, 90, True),
        "crar": (20, 15, True),
        "roa": (2.5, 1, True),
    },
    "it": {
        "cc_growth": (10, 0, True),
        "attrition_rate": (12, 25, False),
        "ebit_margin": (24, 14, True),
        "fcf_margin": (18, 8, True),
        "dividend_payout": (40, 5, True),
    },
    "pharma": {
        "rnd_pct_of_sales": (8, 2, True),
        "export_growth": (12, 0, True),
        "ebitda_margin": (24, 12, True),
    },
    "auto": {
        "volume_growth": (10, -5, True),
        "ev_sales_growth": (25, 0, True),
        "ebitda_margin": (14, 6, True),
    },
    "defence": {
        "order_book_growth": (15, 0, True),
        "execution_ratio": (90, 60, True),
        "ebitda_margin": (18, 8, True),
    },
    "cement": {
        "capacity_utilization": (85, 60, True),
        "volume_growth": (8, -2, True),
        "ebitda_per_ton": (1200, 400, True),
    },
    "steel": {
        "capacity_utilization": (85, 60, True),
        "ebitda_per_ton": (10000, 3000, True),
    },
    "renewable": {
        "mw_installed_growth": (25, 0, True),
        "order_book_growth": (20, 0, True),
    },
}

# Required research checklists with no newly invented scoring thresholds.
# These fields may be supplied by authorized filings/imports/providers. Missing
# fields stay missing; no value is imputed.
EVIDENCE_ONLY_CHECKLISTS: dict[str, tuple[str, ...]] = {
    "energy": ("production_growth", "realization_growth", "reserve_replacement_ratio", "ebitda_margin"),
    "power": ("capacity_growth", "plant_load_factor", "receivable_days", "regulated_equity_growth"),
    "telecom": ("arpu", "subscriber_growth", "ebitda_margin", "capex_intensity", "net_debt_ebitda"),
    "real_estate": ("presales_growth", "collections_growth", "net_debt_equity", "inventory_turnover", "roce"),
    "chemicals": ("volume_growth", "capacity_utilization", "ebitda_margin", "export_growth"),
    "aviation": ("load_factor", "yield_growth", "rask", "cask", "net_debt_ebitda"),
}


def _number(value: Any) -> float | None:
    try:
        if value in (None, "", "NA", "N/A", "null"):
            return None
        return float(str(value).replace("%", "").replace(",", ""))
    except (TypeError, ValueError):
        return None


def _score_range(value: float | None, good: float, bad: float, higher: bool) -> float | None:
    if value is None:
        return None
    if higher:
        if value >= good:
            return 100.0
        if value <= bad:
            return 0.0
        return (value - bad) / (good - bad) * 100.0
    if value <= good:
        return 100.0
    if value >= bad:
        return 0.0
    return (bad - value) / (bad - good) * 100.0


class SectorFundamentalChecklistAuthority:
    authority = AUTHORITY_NAME
    authority_version = AUTHORITY_VERSION

    @property
    def supported_sectors(self) -> tuple[str, ...]:
        return tuple(sorted(set(SCORED_TEMPLATES) | set(EVIDENCE_ONLY_CHECKLISTS)))

    @property
    def scored_sectors(self) -> tuple[str, ...]:
        return tuple(sorted(SCORED_TEMPLATES))

    def evaluate(self, row: Mapping[str, Any], sector: str | None) -> dict[str, Any]:
        family = str(sector or "").strip().lower()
        scored = SCORED_TEMPLATES.get(family)
        checklist = tuple(scored.keys()) if scored else EVIDENCE_ONLY_CHECKLISTS.get(family, ())
        if not checklist:
            return {
                "authority": self.authority,
                "authority_version": self.authority_version,
                "sector": family or None,
                "state": "NOT_MAPPED",
                "score": None,
                "metrics_used": [],
                "metrics_present": {},
                "missing_metrics": [],
                "scoring_influence": False,
                "reason": "no governed sector-specific checklist for this family",
            }
        present = {name: _number(row.get(name)) for name in checklist}
        present = {name: value for name, value in present.items() if value is not None}
        missing = [name for name in checklist if name not in present]
        if not scored:
            return {
                "authority": self.authority,
                "authority_version": self.authority_version,
                "sector": family,
                "state": "EVIDENCE_ONLY" if present else "EVIDENCE_PENDING",
                "score": None,
                "metrics_used": list(present),
                "metrics_present": present,
                "missing_metrics": missing,
                "scoring_influence": False,
                "reason": "sector checklist integrated; no validated scoring band, so evidence cannot change canonical score",
            }
        parts: list[float] = []
        used: list[str] = []
        for field, (good, bad, higher) in scored.items():
            value = present.get(field)
            if value is None:
                continue
            score = _score_range(value, good, bad, higher)
            if score is not None:
                parts.append(float(score))
                used.append(field)
        return {
            "authority": self.authority,
            "authority_version": self.authority_version,
            "sector": family,
            "state": "SCORED" if parts else "EVIDENCE_PENDING",
            "score": round(sum(parts) / len(parts), 1) if parts else None,
            "metrics_used": used,
            "metrics_present": present,
            "missing_metrics": missing,
            "scoring_influence": bool(parts),
            "reason": "established Laddu sector bands applied only to supplied metrics" if parts else "sector fields not present; universal score remains authoritative",
        }


DEFAULT_SECTOR_FUNDAMENTAL_CHECKLIST_AUTHORITY = SectorFundamentalChecklistAuthority()
