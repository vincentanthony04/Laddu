from __future__ import annotations
import csv
import json
import re
from pathlib import Path
from typing import Any, Dict, Optional

from config import DATA_DIR
from models import now_iso
from core.fundamental_scoring_authority import DEFAULT_FUNDAMENTAL_SCORING_AUTHORITY
from core.fundamental_dimension_authority import DEFAULT_FUNDAMENTAL_DIMENSION_AUTHORITY
from core.sector_classification_authority import DEFAULT_SECTOR_CLASSIFICATION_AUTHORITY
from core.sector_fundamental_checklist_authority import (
    DEFAULT_SECTOR_FUNDAMENTAL_CHECKLIST_AUTHORITY, SCORED_TEMPLATES as SECTOR_TEMPLATES,
)


def _num(v):
    try:
        if v in (None, "", "NA", "N/A", "null"):
            return None
        return float(str(v).replace("%", "").replace(",", ""))
    except Exception:
        return None


def _score_range(v, good, bad, higher=True):
    if v is None:
        return None
    v = float(v)
    if higher:
        if v >= good: return 100
        if v <= bad: return 0
        return int((v - bad) / (good - bad) * 100)
    else:
        if v <= good: return 100
        if v >= bad: return 0
        return int((bad - v) / (bad - good) * 100)


# Sector-specific checklist/scoring ownership lives in SectorFundamentalChecklistAuthority.

def _resolve_sector(row: Dict[str, Any]) -> Optional[str]:
    """Compatibility facade over the canonical sector classification authority."""
    return DEFAULT_SECTOR_CLASSIFICATION_AUTHORITY.classify(
        row.get("sector"), row.get("industry")
    ).get("fundamental_sector")


def _sector_score(row: Dict[str, Any], sector: Optional[str]):
    """Compatibility facade over the versioned sector checklist authority."""
    result = DEFAULT_SECTOR_FUNDAMENTAL_CHECKLIST_AUTHORITY.evaluate(row, sector)
    return result.get("score"), list(result.get("metrics_used") or [])




# Authorized, file-based provider adapters. These are imports supplied by the
# user or produced from official exchange filings; Project Laddu does not
# scrape third-party websites. The canonical fundamentals.csv/json names keep
# backward compatibility, while the explicit names preserve source lineage.
FUNDAMENTAL_IMPORTS = (
    ("fundamentals.csv", "user_import", 30),
    ("fundamentals.json", "user_import", 30),
    ("screener_export.csv", "screener_authorized_export", 20),
    ("screener_export.json", "screener_authorized_export", 20),
    ("nse_bse_fundamentals.csv", "official_exchange_filing_import", 40),
    ("nse_bse_fundamentals.json", "official_exchange_filing_import", 40),
)


def _canonical_import_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower()).strip("_")


def _normalise_import_row(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize common authorized-export headings into Laddu's schema.

    Unknown columns are retained verbatim so sector-specific extensions remain
    available. No value is invented and no undated export is treated as
    point-in-time safe.
    """
    row = dict(raw or {})
    by_key = {_canonical_import_key(k): v for k, v in row.items()}
    aliases = {
        "symbol": ("symbol", "trading_symbol", "ticker", "nse_code", "bse_code"),
        "effective_date": ("effective_date", "as_of_date", "report_date", "filing_date", "results_date"),
        "roe": ("roe", "return_on_equity", "return_on_equity_percentage"),
        "roce": ("roce", "return_on_capital_employed", "return_on_capital_employed_percentage"),
        "operating_margin": ("operating_margin", "opm", "opm_percentage", "operating_profit_margin"),
        "sales_growth": ("sales_growth", "revenue_growth", "sales_growth_3years", "sales_growth_5years"),
        "profit_growth": ("profit_growth", "pat_growth", "profit_growth_3years", "profit_growth_5years"),
        "eps_growth": ("eps_growth", "eps_growth_3years", "eps_growth_5years"),
        "debt_to_equity": ("debt_to_equity", "debt_equity"),
        "interest_coverage": ("interest_coverage", "interest_coverage_ratio"),
        "cash_flow_margin": ("cash_flow_margin", "fcf_margin", "free_cash_flow_margin"),
        "pe": ("pe", "pe_ratio", "stock_p_e", "price_to_earnings"),
        "pb": ("pb", "pb_ratio", "price_to_book_value", "price_to_book"),
        "promoter_pledge": ("promoter_pledge", "pledged_percentage", "promoter_pledge_percentage"),
        "promoter_holding": ("promoter_holding", "promoter_holding_percentage"),
        "fii_holding": ("fii_holding", "fii_holding_percentage"),
        "dii_holding": ("dii_holding", "dii_holding_percentage"),
        "sector": ("sector",),
        "industry": ("industry",),
    }
    for target, candidates in aliases.items():
        if row.get(target) not in (None, ""):
            continue
        for candidate in candidates:
            if candidate in by_key and by_key[candidate] not in (None, ""):
                row[target] = by_key[candidate]
                break
    return row


class FundamentalStore:
    """User/local fundamentals provider.

    Accepted files under ProgramData\\ProjectLaddu\\data:
    - fundamentals.csv
    - fundamentals.json

    This module never invents fundamentals. If no row exists, Delivery stays blocked.

    Universal checklist scored for every sector: sales growth, profit growth,
    EPS growth, operating margin, ROE, ROCE, debt/equity, free cash flow,
    valuation (PE/PB). Promoter holding, promoter pledge direction, FII/DII
    holding, and intrinsic value / margin of safety are carried through as
    informational fields (see 'context' in the result) rather than scored --
    there's no universal good/bad band for a holding percentage or an MoS
    figure without a computed intrinsic value input, so scoring them would be
    inventing a threshold not backed by data.

    On top of that, a sector-specific block (FMCG/Banking/NBFC/IT/Pharma/Auto/
    Defence/Cement/Steel/Renewable) is scored from SECTOR_TEMPLATES when the
    row carries a 'sector' or 'industry' field and the relevant metrics are
    present. It contributes 25% of the final score; the universal checklist
    remains 75%. Unmapped sectors, or rows missing sector-specific fields,
    fall back to the universal score only -- unchanged from before.
    """
    def __init__(self):
        self.rows: Dict[str, list[Dict[str, Any]]] = {}
        self.loaded_at: Optional[str] = None
        self.errors = []
        self._source_signature = None

    @staticmethod
    def _current_source_signature():
        signature = []
        for filename, _source_name, _priority in FUNDAMENTAL_IMPORTS:
            path = DATA_DIR / filename
            try:
                stat = path.stat()
                signature.append((filename, int(stat.st_mtime_ns), int(stat.st_size)))
            except FileNotFoundError:
                continue
            except OSError:
                signature.append((filename, -1, -1))
        return tuple(signature)

    def load(self, force: bool = False) -> Dict[str, Any]:
        signature = self._current_source_signature()
        if self.loaded_at and not force and signature == self._source_signature:
            return self.status()
        self.rows = {}
        self.errors = []
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        for filename, source_name, provider_priority in FUNDAMENTAL_IMPORTS:
            path = DATA_DIR / filename
            if not path.exists():
                continue
            try:
                if path.suffix.lower() == ".csv":
                    with path.open("r", encoding="utf-8-sig", newline="") as f:
                        rows = list(csv.DictReader(f))
                else:
                    data = json.loads(path.read_text(encoding="utf-8"))
                    if isinstance(data, dict):
                        data = data.get("rows") or data.get("data") or []
                    rows = [r for r in data if isinstance(r, dict)]
                for raw in rows:
                    row = _normalise_import_row(raw)
                    row["_source"] = source_name
                    row["_source_file"] = filename
                    row["_provider_priority"] = provider_priority
                    self._add_row(row)
            except Exception as exc:
                self.errors.append({"file": str(path), "error": str(exc)})
        self._source_signature = signature
        self.loaded_at = now_iso()
        return self.status()

    def _add_row(self, r: Dict[str, Any]) -> None:
        sym = str(r.get("symbol") or r.get("trading_symbol") or r.get("ticker") or "").upper().strip()
        if not sym:
            return
        row = dict(r)
        row["effective_date"] = str(r.get("effective_date") or r.get("as_of_date") or r.get("report_date") or "")[:10]
        self.rows.setdefault(sym, []).append(row)
        self.rows[sym].sort(key=lambda x: (x.get("effective_date") or "9999-12-31", int(x.get("_provider_priority") or 0)))

    def status(self) -> Dict[str, Any]:
        sources = sorted({str(row.get("_source") or "user_import") for rows in self.rows.values() for row in rows})
        return {
            "loaded": bool(self.rows), "count": sum(len(v) for v in self.rows.values()),
            "symbols": len(self.rows), "last_refresh": self.loaded_at,
            "source": "+".join(sources) if sources else "none", "sources": sources,
            "point_in_time": True, "errors": self.errors,
            "accepted_imports": [name for name, _source, _priority in FUNDAMENTAL_IMPORTS],
            "template": str(DATA_DIR / "fundamentals_template.csv"),
            "sectors_supported": list(DEFAULT_SECTOR_FUNDAMENTAL_CHECKLIST_AUTHORITY.supported_sectors),
            "sectors_scored": list(DEFAULT_SECTOR_FUNDAMENTAL_CHECKLIST_AUTHORITY.scored_sectors),
        }

    def score(self, instrument: Dict[str, Any], as_of: Optional[str] = None) -> Dict[str, Any]:
        symbol = str(instrument.get("trading_symbol") or "").upper().strip()
        candidates = self.rows.get(symbol) or []
        cutoff = str(as_of or now_iso())[:10]
        dated = [r for r in candidates if r.get("effective_date") and r.get("effective_date") <= cutoff]
        row = dated[-1] if dated else None
        if row is None:
            undated = [r for r in candidates if not r.get("effective_date")]
            if undated:
                row = undated[-1]
            elif candidates:
                next_effective = min((r.get("effective_date") for r in candidates if r.get("effective_date")), default=None)
                return {
                    "ok": False, "symbol": symbol, "state": "not_yet_effective", "score": None,
                    "reason": f"No fundamental filing was effective on or before {cutoff}.",
                    "source": str((candidates[0] if candidates else {}).get("_source") or "user_import"), "point_in_time_safe": True,
                    "as_of": cutoff, "next_effective_date": next_effective,
                }
        if not row:
            return {
                "ok": False, "symbol": symbol, "state": "missing", "score": None,
                "reason": "Fundamental row missing; add a point-in-time fundamentals, authorized Screener export, or NSE/BSE filing import to the data folder",
                "source": "none", "as_of": cutoff,
            }

        row_source = str(row.get("_source") or "user_import")
        row_source_file = str(row.get("_source_file") or "fundamentals.csv/json")
        roe = _num(row.get("roe")); roce = _num(row.get("roce")); margin = _num(row.get("operating_margin") or row.get("margin"))
        sales_g = _num(row.get("sales_growth") or row.get("revenue_growth")); profit_g = _num(row.get("profit_growth") or row.get("pat_growth")); eps_g = _num(row.get("eps_growth"))
        debt = _num(row.get("debt_to_equity") or row.get("debt_equity")); ic = _num(row.get("interest_coverage")); cf = _num(row.get("cash_flow_margin") or row.get("fcf_margin"))
        pe = _num(row.get("pe") or row.get("pe_ratio")); pb = _num(row.get("pb") or row.get("pb_ratio")); pledge = _num(row.get("promoter_pledge"))

        # One provider-independent authority owns the meaning of Quality,
        # Growth, Safety and Valuation.  The local filing/import adapter owns
        # only field translation and point-in-time evidence lineage.
        sector_classification = DEFAULT_SECTOR_CLASSIFICATION_AUTHORITY.classify(row.get("sector"), row.get("industry"))
        sector = sector_classification.get("fundamental_sector")
        raw_metrics = dict(row)
        raw_metrics.update({
            "roe": roe, "roce": roce, "operating_margin": margin,
            "cash_flow_margin": cf, "sales_growth": sales_g,
            "profit_growth": profit_g, "eps_growth": eps_g,
            "debt_to_equity": debt, "interest_coverage": ic,
            "promoter_pledge": pledge, "pe": pe, "pb": pb,
        })
        normalized = DEFAULT_FUNDAMENTAL_DIMENSION_AUTHORITY.normalize(raw_metrics, sector=sector)
        dimensions = dict(normalized["dimensions"])
        quality = dimensions.get("quality"); growth = dimensions.get("growth")
        safety = dimensions.get("safety"); valuation = dimensions.get("valuation")
        metric_counts = dict(normalized["dimension_counts"])
        metric_capacity = dict(normalized["dimension_capacity"])
        minimum_counts = dict(normalized["minimum_counts"])
        total_metrics = int(normalized["resolved_metric_count"])
        total_capacity = int(normalized["metric_capacity"])
        coverage_ratio = round(total_metrics / total_capacity, 3) if total_capacity else 0.0
        available = {name: value for name, value in dimensions.items() if value is not None}
        coverage = {
            "ratio": coverage_ratio,
            "metric_count": total_metrics,
            "metric_capacity": total_capacity,
            "dimension_counts": metric_counts,
            "dimension_capacity": metric_capacity,
            "minimum_counts": minimum_counts,
            "available": sorted(available),
            "missing": sorted(set(dimensions) - set(available)),
            "dimension_authority": normalized["authority"],
            "dimension_authority_version": normalized["authority_version"],
        }
        if not row.get("effective_date"):
            return {
                "ok": False, "symbol": symbol, "state": "undated", "score": None,
                "quality": quality, "growth": growth, "safety": safety, "valuation": valuation,
                "coverage": coverage,
                "reason": "Fundamental row has no effective_date/as_of_date/report_date; point-in-time safety is required.",
                "source": row_source, "source_file": row_source_file, "point_in_time_safe": False, "as_of": cutoff, "raw": row,
            }
        insufficient = list(normalized["insufficient_dimensions"])
        if insufficient:
            return {
                "ok": False, "symbol": symbol, "state": "incomplete", "score": None,
                "quality": quality, "growth": growth, "safety": safety, "valuation": valuation,
                "coverage": coverage,
                "reason": "Fundamental evidence is incomplete under the canonical dimension authority; missing values receive no neutral points.",
                "insufficient_dimensions": insufficient,
                "fundamental_dimension_authority": normalized["authority"],
                "fundamental_dimension_authority_version": normalized["authority_version"],
                "source": row_source, "source_file": row_source_file, "point_in_time_safe": True, "as_of": cutoff, "raw": row,
            }

        # The store owns point-in-time evidence normalization only.  Final
        # fundamental scoring is one versioned authority shared by every
        # provider adapter so a score has one meaning regardless of source.
        sector_checklist = DEFAULT_SECTOR_FUNDAMENTAL_CHECKLIST_AUTHORITY.evaluate(row, sector)
        sector_score = sector_checklist.get("score")
        sector_metrics_used = list(sector_checklist.get("metrics_used") or [])
        canonical = DEFAULT_FUNDAMENTAL_SCORING_AUTHORITY.score_dimensions(
            dimensions, sector_score=sector_score, sector=sector
        )
        score = canonical["score"]
        state = canonical["state"]
        universal_score = canonical["universal_score"]

        context = {
            "promoter_holding": _num(row.get("promoter_holding")),
            "promoter_pledge_pct": pledge,
            "fii_holding": _num(row.get("fii_holding")),
            "dii_holding": _num(row.get("dii_holding")),
            "intrinsic_value": _num(row.get("intrinsic_value")),
            "margin_of_safety_pct": _num(row.get("margin_of_safety_pct")),
        }
        context = {k: v for k, v in context.items() if v is not None}

        return {
            "ok": True, "symbol": symbol, "state": state, "score": score,
            "quality": quality, "growth": growth, "safety": safety, "valuation": valuation,
            "universal_score": universal_score,
            "coverage": coverage,
            "score_method": {**canonical["score_method"], "minimum_metric_counts": minimum_counts},
            "fundamental_dimension_authority": normalized["authority"],
            "fundamental_dimension_authority_version": normalized["authority_version"],
            "fundamental_scoring_authority": canonical["authority"],
            "fundamental_scoring_authority_version": canonical["authority_version"],
            "sector": sector, "sector_score": sector_score, "sector_metrics_used": sector_metrics_used,
            "sector_checklist": sector_checklist,
            "sector_checklist_authority": sector_checklist["authority"],
            "sector_checklist_authority_version": sector_checklist["authority_version"],
            "sector_classification_authority": sector_classification["authority"],
            "sector_classification_authority_version": sector_classification["authority_version"],
            "market_sector_key": sector_classification.get("market_sector_key"),
            "debt_to_equity": debt, "pe": pe, "pb": pb, "roe": roe, "roce": roce,
            "context": context,
            "source": row_source, "source_file": row_source_file, "effective_date": row.get("effective_date") or None, "point_in_time_safe": bool(row.get("effective_date")), "as_of": cutoff, "raw": row,
            "reason": f"Quality {quality}; Growth {growth}; Safety {safety}; Valuation {valuation}" + (f"; Sector({sector}) {sector_score}" if sector_score is not None else ""),
        }
