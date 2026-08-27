"""Portfolio-fit projection for research candidates.

This is an additive adapter around ProductionRiskAuthorityService.  It does not
create a second risk engine, alter evidence, or place orders.  It exposes the
candidate's marginal portfolio heat, concentration and expected economic impact
for research comparison before any manual position confirmation.
"""
from __future__ import annotations

import math
from typing import Any, Dict, Mapping, Optional

from core.production_risk_authority_service import ProductionRiskAuthorityService

ASSESSMENT_VERSION = "portfolio-candidate-assessment-1.0.0"


def _num(value: Any) -> Optional[float]:
    try:
        out = float(value)
        return out if math.isfinite(out) else None
    except (TypeError, ValueError):
        return None


class PortfolioCandidateAssessmentService:
    def __init__(self, store: Any, runtime_status: Optional[Dict[str, Any]] = None):
        self.authority = ProductionRiskAuthorityService(store, runtime_status=runtime_status)

    def assess(self, candidate: Mapping[str, Any]) -> Dict[str, Any]:
        payload = dict(candidate or {})
        report = self.authority.evaluate(payload, persist=False)
        current = dict(report.get("current_portfolio") or {})
        projected = dict(report.get("projected_portfolio") or {})
        sizing = dict(report.get("sizing") or {})
        expected_bps = _num(
            payload.get("expected_net_return_bps")
            if payload.get("expected_net_return_bps") is not None
            else payload.get("shadow_expected_net_return_bps")
        )
        notional = _num(sizing.get("notional")) or 0.0
        risk_cash = _num(sizing.get("risk_cash")) or 0.0
        expected_profit = notional * expected_bps / 10000.0 if expected_bps is not None else None
        expected_profit_to_risk = expected_profit / risk_cash if expected_profit is not None and risk_cash > 0 else None
        marginal = {
            "open_positions": int(projected.get("open_positions") or 0) - int(current.get("open_positions") or 0),
            "notional": round((_num(projected.get("portfolio_notional")) or 0.0) - (_num(current.get("portfolio_notional")) or 0.0), 2),
            "portfolio_heat_pct": round((_num(projected.get("portfolio_heat_pct")) or 0.0) - (_num(current.get("portfolio_heat_pct")) or 0.0), 6),
            "sector_exposure_pct": _num(projected.get("sector_exposure_pct")),
            "symbol_exposure_pct": _num(projected.get("symbol_exposure_pct")),
            "highly_correlated_positions": int((report.get("correlation") or {}).get("highly_correlated_count") or 0),
            "expected_net_return_bps": None if expected_bps is None else round(expected_bps, 6),
            "expected_net_profit_at_risk_size": None if expected_profit is None else round(expected_profit, 2),
            "expected_profit_to_stop_risk": None if expected_profit_to_risk is None else round(expected_profit_to_risk, 6),
        }
        return {
            "ok": report.get("admission_state") != "BLOCKED",
            "version": ASSESSMENT_VERSION,
            "symbol": report.get("symbol"),
            "mode": report.get("mode"),
            "portfolio_fit_state": report.get("admission_state"),
            "marginal_contribution": marginal,
            "current_portfolio": current,
            "projected_portfolio": projected,
            "sizing": sizing,
            "correlation": report.get("correlation"),
            "hard_blocks": list(report.get("hard_blocks") or []),
            "capital_blocks": list(report.get("capital_blocks") or []),
            "warnings": list(report.get("warnings") or []),
            "authority": "RISK_ASSESSMENT_ONLY",
            "production_score_change_allowed": False,
            "order_placement": False,
            "policy": "Uses the canonical production risk authority; portfolio fit cannot increase evidence or place an order.",
        }
