"""Evidence-based research-platform maturity status.

The status deliberately avoids self-issued numeric scores.  It reports whether
selection, calibration, replay, lifecycle and portfolio components are wired and
what evidence thresholds remain.  It is diagnostic-only and does not influence
candidate selection.
"""
from __future__ import annotations

from typing import Any, Dict, List

from core.nse_calibrated_challenger_service import NseCalibratedChallengerService
from core.selection_platform_service import SelectionPlatformService
from core.selection_research_validation_service import SelectionResearchValidationService

STATUS_VERSION = "research-maturity-status-1.1.0-explicit-shadow-state"
DELIVERY_HORIZONS = ("5d", "10d", "20d", "60d")


class ResearchMaturityStatusService:
    def __init__(self, store: Any):
        self.store = store

    def _table_count(self, table: str, where: str = "", params=()) -> int:
        try:
            row = self.store.conn.execute(f"SELECT COUNT(*) FROM {table} {where}", tuple(params)).fetchone()
            return int(row[0] if row else 0)
        except Exception:
            return 0

    def _validation(self, mode: str, horizon: str) -> Dict[str, Any]:
        try:
            report = SelectionResearchValidationService(self.store).report(mode=mode, horizon=horizon)
            return {
                "state": report.get("state"),
                "readiness": report.get("readiness"),
                "arms": report.get("arms"),
                "production_change_allowed": False,
            }
        except Exception as exc:
            return {"state": "UNAVAILABLE", "error": str(exc), "production_change_allowed": False}

    def status(self) -> Dict[str, Any]:
        platform = SelectionPlatformService(self.store)
        calibrated = NseCalibratedChallengerService(self.store)
        desks = {}
        for mode, horizons in (("intraday", ("session",)), ("delivery", DELIVERY_HORIZONS)):
            horizon_status = {}
            for horizon in horizons:
                horizon_status[horizon] = {
                    "validation": self._validation(mode, horizon),
                    "calibrated_challenger": calibrated.status(mode=mode, horizon=horizon),
                }
            desks[mode] = {
                "selection_platform": platform.latest_summary(mode),
                "horizons": horizon_status,
            }
        portfolio = getattr(self.store, "model_portfolio_service", None)
        try:
            open_positions = len(portfolio.open_positions()) if portfolio is not None else 0
        except Exception:
            open_positions = 0
        closed_learning = self._table_count("position_learning_ledger")
        candidate_outcomes = self._table_count("selector_candidate_outcomes")
        populations = self._table_count("candidate_populations")
        predictions = self._table_count("shadow_selector_predictions")
        eligible_models = self._table_count("shadow_calibrated_models", "WHERE state='SHADOW_MODEL_ELIGIBLE'")
        if predictions and eligible_models:
            maturity_state = "SHADOW_VALIDATION_ACTIVE"
        elif predictions:
            maturity_state = "SHADOW_ACTIVE_AWAITING_STATISTICAL_VALIDATION"
        else:
            maturity_state = "MODEL_UNAVAILABLE_AWAITING_EVIDENCE"
        component_states: List[Dict[str, Any]] = [
            {"component": "point_in_time_candidate_population", "state": "WIRED" if populations else "AWAITING_DATA", "count": populations},
            {"component": "three_arm_shadow_selection", "state": "WIRED" if predictions else "AWAITING_DATA", "count": predictions},
            {"component": "immutable_selector_outcomes", "state": "WIRED" if candidate_outcomes else "AWAITING_DATA", "count": candidate_outcomes},
            {"component": "governed_calibrated_models", "state": "VALIDATION_ELIGIBLE" if eligible_models else "AWAITING_EVIDENCE", "count": eligible_models},
            {"component": "canonical_model_paper_portfolio", "state": "WIRED", "open_positions": open_positions},
            {"component": "closed_learning_ledger", "state": "WIRED" if closed_learning else "AWAITING_CLOSURES", "count": closed_learning},
            {"component": "portfolio_risk_authority", "state": "WIRED"},
            {"component": "broker_order_execution", "state": "DISABLED_BY_CONTRACT"},
        ]
        return {
            "ok": True,
            "version": STATUS_VERSION,
            "maturity_state": maturity_state,
            "selection_edge": "NOT_YET_STATISTICALLY_VALIDATED",
            "production_selector": "FROZEN_HEURISTIC_BASELINE",
            "challenger_authority": "SHADOW_ONLY",
            "prediction_state": "SHADOW_ACTIVE" if predictions else "MODEL_UNAVAILABLE",
            "decision_weight": 0.0,
            "product_claim": "Professional research architecture; profitability remains unverified until evidence thresholds pass.",
            "components": component_states,
            "desks": desks,
            "lifecycle": {
                "open_positions": open_positions,
                "closed_learning_records": closed_learning,
                "automatic_paper_execution": True,
            },
            "acceptance_thresholds": {
                "diagnostic_report": {"settled": 100, "trading_days": 60},
                "model_review": {"settled": 300, "trading_days": 126, "regimes": 3},
                "shadow_review": {"settled": 300, "trading_days": 126, "regimes": 3},
                "prediction_activation": "AUTOMATIC_WHEN_ARTIFACT_AND_DATA_GATES_PASS",
            },
            "numeric_maturity_score": None,
            "policy": "No self-issued 9.x score; evidence and installed-workflow results are reported separately.",
        }
