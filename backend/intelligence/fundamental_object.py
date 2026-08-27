"""
FundamentalObject -- point-in-time fundamental score plus quarterly momentum.

`fundamentals.FundamentalStore.score()` already produces a point-in-time
quality/growth/safety/valuation score from the latest row on file (no new
math is added here for that part -- see `latest` fields below).

What was missing, per the v61 architecture note section 5 ("Quarterly
Fundamental Object" / "Business Momentum"), is a read across the last few
quarterly rows the *same* store already holds (`FundamentalStore.rows[symbol]`,
sorted oldest-first by `effective_date`). This module reads that existing
list and reports whether revenue/profit growth is rising, falling, or flat
across the quarters on file -- it does not fetch or invent any new data
source; if fewer than two dated rows exist, it says so rather than guessing.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional

CONTRACT_VERSION = "fundamental-object-v1"


def _num(v: Any) -> Optional[float]:
    try:
        if v in (None, "", "NA", "N/A", "null"):
            return None
        return float(str(v).replace("%", "").replace(",", ""))
    except Exception:
        return None


def _trend_label(values: List[float]) -> str:
    if len(values) < 2:
        return "insufficient_data"
    diffs = [b - a for a, b in zip(values, values[1:])]
    if all(d > 0.01 for d in diffs):
        return "improving"
    if all(d < -0.01 for d in diffs):
        return "declining"
    if all(abs(d) <= 0.01 for d in diffs):
        return "flat"
    return "mixed"


def _quarterly_series(rows: List[Dict[str, Any]], field_names: tuple) -> List[Dict[str, Any]]:
    series = []
    for row in rows or []:
        if not row.get("effective_date"):
            continue
        value = None
        for name in field_names:
            value = _num(row.get(name))
            if value is not None:
                break
        if value is not None:
            series.append({"effective_date": row.get("effective_date"), "value": value})
    return series


@dataclass(frozen=True)
class FundamentalObject:
    symbol: str
    ok: bool
    state: Optional[str]
    score: Optional[float]
    quality: Optional[float]
    growth: Optional[float]
    safety: Optional[float]
    valuation: Optional[float]
    effective_date: Optional[str]
    quarters_on_file: int
    revenue_growth_series: List[Dict[str, Any]]
    profit_growth_series: List[Dict[str, Any]]
    revenue_momentum: str      # improving | declining | flat | mixed | insufficient_data
    profit_momentum: str
    business_momentum: str     # rollup of the two series above
    reason: Optional[str]
    source: Optional[str]
    contract_version: str = CONTRACT_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _rollup_momentum(revenue_momentum: str, profit_momentum: str) -> str:
    labels = {revenue_momentum, profit_momentum}
    if "insufficient_data" in labels and len(labels) == 1:
        return "insufficient_data"
    if labels <= {"improving", "insufficient_data"} and "improving" in labels:
        return "improving"
    if labels <= {"declining", "insufficient_data"} and "declining" in labels:
        return "declining"
    if labels <= {"flat", "insufficient_data"} and "flat" in labels:
        return "flat"
    return "mixed"


def build_fundamental_object(
    latest_score: Optional[Dict[str, Any]],
    quarterly_rows: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """latest_score: output of fundamentals.FundamentalStore.score(instrument)
    (the point-in-time quality/growth/safety/valuation result).
    quarterly_rows: FundamentalStore.rows.get(symbol) -- the raw list of
    dated rows for that symbol, oldest-first, as already stored by
    FundamentalStore._add_row(). Optional; last few quarters used, most
    recent 4 by effective_date.
    """
    latest_score = latest_score or {}
    quarterly_rows = sorted((quarterly_rows or []), key=lambda r: r.get("effective_date") or "")[-4:]

    revenue_series = _quarterly_series(quarterly_rows, ("revenue_growth", "sales_growth"))
    profit_series = _quarterly_series(quarterly_rows, ("profit_growth", "pat_growth"))
    revenue_momentum = _trend_label([r["value"] for r in revenue_series])
    profit_momentum = _trend_label([r["value"] for r in profit_series])

    obj = FundamentalObject(
        symbol=str(latest_score.get("symbol") or ""),
        ok=bool(latest_score.get("ok")),
        state=latest_score.get("state"),
        score=latest_score.get("score"),
        quality=latest_score.get("quality"),
        growth=latest_score.get("growth"),
        safety=latest_score.get("safety"),
        valuation=latest_score.get("valuation"),
        effective_date=latest_score.get("effective_date"),
        quarters_on_file=len(quarterly_rows),
        revenue_growth_series=revenue_series,
        profit_growth_series=profit_series,
        revenue_momentum=revenue_momentum,
        profit_momentum=profit_momentum,
        business_momentum=_rollup_momentum(revenue_momentum, profit_momentum),
        reason=latest_score.get("reason"),
        source=latest_score.get("source"),
    )
    return obj.to_dict()
