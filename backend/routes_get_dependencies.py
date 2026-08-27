"""Shared dependencies for Project Laddu GET route modules."""
from __future__ import annotations

import threading

import hashlib

import time

import json

from datetime import datetime, timezone

from pathlib import Path

from urllib.parse import unquote

from models import now_iso

from core.evidence_engine_service import EvidenceEngineService

from core.walk_forward_validation_service import WalkForwardValidationService

from core.institutional_outcome_service import InstitutionalOutcomeService

from core.ai_governance_service import AIGovernanceService

from core.storage_architecture_service import StorageArchitectureService

from core.india_cost_model import IndiaCashCostModel

from core.capital_readiness_service import CapitalReadinessService

from core.production_risk_authority_service import ProductionRiskAuthorityService

from core.production_mode_policy import UnsupportedProductionMode, require_production_mode, normalise_mode

from core.structural_trade_map_service import StructuralTradeMapService

from core.market_level_service import compute_levels_from_candles

from core.quote_integrity_service import classify_quote

from core.candle_freshness_service import CandleFreshnessService


from core.strategy_validation_status_service import StrategyValidationStatusService

from core.evidence_score_validation_service import EvidenceScoreValidationService

from core.market_radar_service import MarketRadarService

from core.simulation_robustness_service import SimulationRobustnessService

from core.model_challenger_governance_service import ModelChallengerGovernanceService

from core.selection_platform_service import SelectionPlatformService

from core.selection_research_validation_service import SelectionResearchValidationService

from core.selection_walk_forward_replay_service import SelectionWalkForwardReplayService

from core.forward_evidence_clock_service import ForwardEvidenceClockService

from core.improvement_review_service import ImprovementReviewService

from core.improvement_proposal_service import ImprovementProposalService

from core.nse_calibrated_challenger_service import NseCalibratedChallengerService, DEFAULT_HORIZON

from core.nse_cross_sectional_selector_service import feature_manifest, FEATURE_MANIFEST_HASH

from core.research_maturity_status_service import ResearchMaturityStatusService

from core.market_cycle_maturity_service import MarketCycleMaturityService

from core.product_maturity_service import ProductMaturityService

from core.level5_forward_maturity_service import Level5ForwardMaturityService

from core.decision_surface_reconciliation_service import DecisionSurfaceReconciliationService

from core.model_learning_audit_service import ModelLearningAuditService

from core.operational_evidence_integrity_service import OperationalEvidenceIntegrityService

from core.evidence_pipeline_status_service import EvidencePipelineStatusService

from core.quant_research_orchestrator_service import QuantResearchOrchestratorService

from core.quant_paper_activation_service import QuantPaperActivationService

from core.dual_desk_architecture_service import DualDeskArchitectureService

from core.model_tournament_service import ModelTournamentService

from core.active_research_method_registry import ActiveResearchMethodRegistry

from core.binding_mtf_contract_service import BindingMtfContractService

from core.portfolio_workspace_service import PortfolioWorkspaceService

from core.operator_capital_settings_service import OperatorCapitalSettingsService

from core.behavioural_pattern_service import analyze as analyze_behavioural_patterns

from config import TRADING_CAPITAL, APP_VERSION, BUILD_MARKER, PRODUCT_MODE, BROKER_ORDER_EXECUTION_ENABLED, FRONTEND_DIR, DATA_DIR

from market_layers import camarilla_levels, support_resistance_levels

from intelligence import (
    build_market_object,
    build_action_objects,
    build_action_object,
    build_delivery_object,
    build_fundamental_object,
    persist_action_objects,
    action_object_history,
)

def _flag(qs, key, default="false"):
    return str(qs.get(key, [default])[0]).lower() in ("1", "true", "yes")

def _qint(qs, key, default, min_val=None, max_val=None):
    """Safe int query-param read.

    v59: several handlers did `int(qs.get("days", ["10"])[0] or 10)`
    directly -- a garbage value like `?days=abc` raised ValueError inside
    the handler, which the outer do_GET try/except then reported as a
    generic 500 instead of the request just falling back to the default.
    r_daily_learning / r_trade_journal already had a local try/except
    version of this; this is that pattern promoted to a shared helper and
    applied everywhere a GET route parses an int off the query string.
    """
    try:
        val = int(qs.get(key, [str(default)])[0] or default)
    except (TypeError, ValueError):
        return default
    if min_val is not None:
        val = max(min_val, val)
    if max_val is not None:
        val = min(max_val, val)
    return val

from core.priority_pipeline_service import PriorityPipelineService
from core.canonical_evidence_snapshot_service import CanonicalEvidenceSnapshotService
from core.cross_plane_reconciliation_service import CrossPlaneReconciliationService
from core.level5_evidence_matrix_service import Level5EvidenceMatrixService
from core.ml_population_qualification_service import MLPopulationQualificationService
from core.level5_operational_proof_service import Level5OperationalProofService

# Wildcard route modules depend on this explicit export list.  Build it only
# after every service import, otherwise late-added route services disappear at
# runtime even though source imports and static tests still look valid.
__all__ = [name for name in globals() if not name.startswith("__")]
