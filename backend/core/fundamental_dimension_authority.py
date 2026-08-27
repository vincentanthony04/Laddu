"""Canonical source-independent normalization of fundamental evidence.

Providers own acquisition and field translation only.  This authority owns the
meaning of the four Laddu dimensions (Quality, Growth, Safety, Valuation) so a
score cannot change meaning merely because evidence came from a local filing
import instead of the Upstox fundamentals API.

Missing metrics are never imputed.  All returned component evidence is
versioned and auditable.  Financial companies use economically appropriate
safety inputs (ROA/ROE) rather than industrial debt/CFO assumptions.
"""
from __future__ import annotations

from typing import Any, Mapping

from core.sector_classification_authority import DEFAULT_SECTOR_CLASSIFICATION_AUTHORITY

AUTHORITY_NAME = "FundamentalDimensionAuthority"
AUTHORITY_VERSION = "1.0.0"
MINIMUM_COUNTS = {"quality": 2, "growth": 2, "safety": 2, "valuation": 1}


def _num(value: Any) -> float | None:
    try:
        if value in (None, "", "NA", "N/A", "null"):
            return None
        return float(str(value).replace("%", "").replace(",", ""))
    except (TypeError, ValueError):
        return None


def _first(metrics: Mapping[str, Any], *names: str) -> float | None:
    for name in names:
        value = _num(metrics.get(name))
        if value is not None:
            return value
    return None


def _score_range(value: float | None, good: float, bad: float, higher: bool = True) -> float | None:
    if value is None:
        return None
    value = float(value)
    if higher:
        if value >= good:
            return 100.0
        if value <= bad:
            return 0.0
        return round((value - bad) / (good - bad) * 100.0, 1)
    if value <= good:
        return 100.0
    if value >= bad:
        return 0.0
    return round((bad - value) / (bad - good) * 100.0, 1)


def _avg(values: list[float | None]) -> float | None:
    clean = [float(value) for value in values if value is not None]
    return round(sum(clean) / len(clean), 1) if clean else None


def _sector_policy(sector: str | None) -> str:
    """Compatibility facade over the canonical sector classification authority."""
    return DEFAULT_SECTOR_CLASSIFICATION_AUTHORITY.fundamental_policy(sector)


def _relative_value(company: float | None, sector: float | None, absolute_good: float, absolute_bad: float) -> float | None:
    if company is None:
        return None
    if sector is not None and sector > 0:
        return _score_range(company / sector, 0.85, 1.55, higher=False)
    return _score_range(company, absolute_good, absolute_bad, higher=False)


class FundamentalDimensionAuthority:
    authority = AUTHORITY_NAME
    authority_version = AUTHORITY_VERSION
    minimum_counts = MINIMUM_COUNTS

    def normalize(
        self,
        metrics: Mapping[str, Any],
        *,
        sector: str | None = None,
        sector_benchmarks: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        row = dict(metrics or {})
        benchmarks = dict(sector_benchmarks or {})
        policy = _sector_policy(sector or row.get("sector") or row.get("industry"))

        roe = _first(row, "roe")
        roce = _first(row, "roce")
        roa = _first(row, "roa")
        operating_margin = _first(row, "operating_margin", "margin", "ebit_margin", "ebitda_margin")
        cash_flow_margin = _first(row, "cash_flow_margin", "fcf_margin")

        if policy in {"banking", "nbfc"}:
            quality_named = {
                "roe": _score_range(roe, 16, 6),
                "roa": _score_range(roa, 1.5, 0.35),
            }
        else:
            quality_named = {
                "roe": _score_range(roe, 18, 6),
                "roce": _score_range(roce, 20, 7),
                "roa": _score_range(roa, 10, 2),
                "operating_margin": _score_range(operating_margin, 22, 6),
                "cash_flow_margin": _score_range(cash_flow_margin, 12, -5),
            }

        sales_growth = _first(row, "sales_growth", "revenue_growth")
        profit_growth = _first(row, "profit_growth", "pat_growth")
        eps_growth = _first(row, "eps_growth")
        growth_named = {
            "sales_growth": _score_range(sales_growth, 15, 0),
            "profit_growth": _score_range(profit_growth, 18, -5),
            "eps_growth": _score_range(eps_growth, 15, -5),
        }

        debt = _first(row, "debt_to_equity", "debt_equity")
        interest_coverage = _first(row, "interest_coverage")
        pledge = _first(row, "promoter_pledge", "promoter_pledge_pct")
        assets = _first(row, "total_assets", "total_asset", "assets")
        liabilities = _first(row, "total_liabilities", "total_liability", "liabilities")
        positive_cfo_ratio = _first(row, "positive_cfo_ratio", "positive_cfo_pct")
        balance_equity_ratio_score = None
        if assets is not None and assets > 0 and liabilities is not None:
            balance_equity_ratio_score = max(0.0, min(100.0, round((1.0 - liabilities / assets) * 140.0, 1)))
        cfo_score = None if positive_cfo_ratio is None else max(0.0, min(100.0, positive_cfo_ratio))
        if policy in {"banking", "nbfc"}:
            safety_named = {
                "roa": _score_range(roa, 1.5, 0.35),
                "roe": _score_range(roe, 15, 5),
            }
        else:
            safety_named = {
                "debt_to_equity": _score_range(debt, 0.4, 2.0, higher=False),
                "interest_coverage": _score_range(interest_coverage, 6, 1.5),
                "promoter_pledge": _score_range(pledge, 0, 25, higher=False),
                "balance_equity_ratio": balance_equity_ratio_score,
                "positive_cfo_ratio": cfo_score,
            }

        pe = _first(row, "pe", "pe_ratio", "p_e")
        pb = _first(row, "pb", "pb_ratio", "p_b")
        ev = _first(row, "ev_ebitda", "ev_ebitda_ratio")
        sector_pe = _first(benchmarks, "pe", "p_e")
        sector_pb = _first(benchmarks, "pb", "p_b")
        sector_ev = _first(benchmarks, "ev_ebitda")
        if policy in {"banking", "nbfc"}:
            valuation_named = {
                "pe": _relative_value(pe, sector_pe, 18, 45),
                "pb": _relative_value(pb, sector_pb, 2.5, 8),
            }
        else:
            valuation_named = {
                "pe": _relative_value(pe, sector_pe, 18, 60),
                "pb": _relative_value(pb, sector_pb, 3, 12),
                "ev_ebitda": _relative_value(ev, sector_ev, 8, 30),
            }

        groups = {
            "quality": quality_named,
            "growth": growth_named,
            "safety": safety_named,
            "valuation": valuation_named,
        }
        component_scores = {
            name: {metric: score for metric, score in values.items() if score is not None}
            for name, values in groups.items()
        }
        counts = {name: len(values) for name, values in component_scores.items()}
        capacities = {name: len(values) for name, values in groups.items()}
        dimensions = {
            name: _avg(list(values.values())) if values else None
            for name, values in component_scores.items()
        }
        insufficient = [
            name for name, minimum in self.minimum_counts.items()
            if counts.get(name, 0) < minimum
        ]
        raw_metric_count = sum(counts.values())
        required_count = sum(self.minimum_counts.values())
        return {
            "ok": not insufficient,
            "state": "READY" if not insufficient else "INCOMPLETE",
            "authority": self.authority,
            "authority_version": self.authority_version,
            "sector_policy": policy,
            "dimensions": dimensions,
            "component_scores": component_scores,
            "dimension_counts": counts,
            "dimension_capacity": capacities,
            "minimum_counts": dict(self.minimum_counts),
            "resolved_metric_count": raw_metric_count,
            "metric_capacity": sum(capacities.values()),
            "minimum_metric_count": required_count,
            "missing_dimensions": [name for name, value in dimensions.items() if value is None],
            "insufficient_dimensions": insufficient,
            "policy": "provider-independent normalization; missing evidence receives no neutral points and weights are never renormalized",
        }


DEFAULT_FUNDAMENTAL_DIMENSION_AUTHORITY = FundamentalDimensionAuthority()
