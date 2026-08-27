"""Canonical separation of signal accuracy and Model Paper economics.

Project Laddu intentionally retains three different evidence classes:

* canonical decision/settlement lineage -> signal accuracy;
* governed Model Paper positions -> rupee economics after execution costs;
* pre-governed signal ledger -> historical continuity in price points only.

This authority keeps those lanes explicit so a points-only outcome can never
be interpreted as rupee P&L, profit factor or Level-5 economic evidence.
"""
from __future__ import annotations

from typing import Any, Dict

from core.decision_lifecycle_read_model_service import DecisionLifecycleReadModelService
from core.model_portfolio_performance_service import ModelPortfolioPerformanceService
from core.model_paper_settlement_parity_service import ModelPaperSettlementParityService
from core.follow_through_projection_service import FollowThroughProjectionService


class PerformanceEvidenceAuthority:
    NAME = "PerformanceEvidenceAuthority"
    VERSION = "1.0.0"

    def __init__(self, app: Any):
        self.app = app
        self.performance = ModelPortfolioPerformanceService(
            app.store,
            repository=getattr(app, "model_portfolio_repository", None),
        )

    def report(self, *, mode: str = "all") -> Dict[str, Any]:
        lifecycle = DecisionLifecycleReadModelService(self.app).status(mode=mode, limit=5000)
        if lifecycle.get("ok") is not True:
            return {
                "ok": False,
                "state": "UNAVAILABLE",
                "authority": self.NAME,
                "authority_version": self.VERSION,
                "blocked_authority": lifecycle.get("authority") or "POSTGRESQL_CANONICAL_DECISIONS",
                "error": lifecycle.get("error") or lifecycle.get("state") or "CANONICAL_LIFECYCLE_UNAVAILABLE",
                "signal_accuracy": lifecycle,
                "model_paper_performance": None,
                "settlement_parity": None,
                "fallback_used": False,
                "policy": "critical performance evidence fails closed when canonical lifecycle authority is unavailable",
            }
        # Follow-through is a separate observational projection over the same
        # immutable settled decision ids.  It may remain PENDING without
        # weakening or rewriting the settlement result.
        lifecycle = FollowThroughProjectionService(self.app).enrich_lifecycle(lifecycle)
        economics = self.performance.report()
        model_repo = getattr(self.app, "model_portfolio_repository", None)
        canonical_repo = getattr(self.app.store, "production_canonical_decision_repository", None)
        parity = ModelPaperSettlementParityService(model_repo, canonical_repo).report() if model_repo is not None and canonical_repo is not None else {
            "ok": False, "state": "TARGET_OR_NONPRODUCTION_PENDING",
            "authority": "ModelPaperSettlementParityAuthority", "authority_version": "1.0.0",
            "checked": 0, "mismatches": [],
        }
        return {
            "ok": True,
            "authority": self.NAME,
            "authority_version": self.VERSION,
            "signal_accuracy": lifecycle,
            "model_paper_performance": economics,
            "settlement_parity": parity,
            "continuity": economics.get("continuity") or {},
            "lanes": {
                "signal_accuracy": {
                    "authority": lifecycle.get("authority"),
                    "units": "OUTCOME_COUNTS_AND_PERCENT",
                    "requires_model_paper_settlement_lineage": True,
                    "currency_pnl_allowed": False,
                },
                "model_paper_economics": {
                    "authority": economics.get("authority"),
                    "units": economics.get("units"),
                    "requires_quantity_and_costs": True,
                    "currency_pnl_allowed": True,
                },
                "legacy_signal_continuity": {
                    "authority": "SIGNAL_LEDGER_CONTINUITY",
                    "units": "PRICE_POINTS_ONLY",
                    "currency_pnl_allowed": False,
                    "included_in_level5_economics": False,
                },
            },
            "policy": (
                "Signal accuracy and Model Paper rupee performance share the same settled "
                "decision/position lineage but remain distinct metric lanes. Legacy points-only "
                "history is continuity evidence and never contributes to economic performance."
            ),
        }
