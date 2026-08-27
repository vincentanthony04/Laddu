from __future__ import annotations
import copy
import json
import csv
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as _FutTimeout
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

from config import DEFAULT_PORT, FRONTEND_DIR, INSTALL_DIR, LOG_DIR, DATA_DIR, RUNTIME_DIR, RUNTIME_DB_PATH, MAX_FASTLANE, DEEP_SCAN_BATCH, MAX_PROMOTED_PER_SECTOR, MODE_REFRESH_SECONDS, MAX_INTRADAY, INTRADAY_PRIORITY_LANE, INTRADAY_COVERAGE_LANE, NSE_DELIVERY_AUTO_DOWNLOAD, NSE_DELIVERY_LOOKBACK_DAYS, NSE_DELIVERY_REFRESH_SECONDS, APP_VERSION, BIND_HOST, MAX_REQUEST_BODY_BYTES, TRADING_CAPITAL
from storage import Store, INTELLIGENCE_SCAN_SYMBOLS, NIFTY50_CORE, NIFTY250_CORE, NEXT50_CORE, NIFTY250_EXTRA, _desk_modes

# How long a "new entry" stays visible on the dashboard after it drops out
# of the current cycle's top-N ranking, before being fully removed. Keeps
# entries from vanishing instantly on a single borderline-score refresh.
STICKY_TTL_SECONDS = 90
from laddu_upstox_rest_client import UpstoxClient, UpstoxApiError
from nse_delivery_client import NSEDeliveryClient
from engines import ENGINES
from models import now_iso
from fundamentals import FundamentalStore
from market_layers import market_structure, volume_profile, orb_context, heat_strip_context, sector_hint_from_symbol, trendline, order_blocks, retest_zone, support_resistance_levels, derive_prev_day_ohlc, camarilla_levels
from indicators import closes, ema, rsi, adx, support_resistance
from session_candles import candle_is_closed, latest_closed_age_seconds, interval_minutes, candle_datetime
from core.rate_controller import RateController, SlotBusy
from core.market_data_service import MarketDataService
from core.first_useful_mode_service import FirstUsefulModeService
from core.candle_freshness_service import CandleFreshnessService
from core.quote_integrity_service import (
    classify_quote, newer_quote, revalidate_cached_quote, visible_market_leader_symbols,
)
from core.live_market_gateway import LiveMarketGateway
from core.analytical_projection_service import AnalyticalProjectionService
from core.data_plane import ProductionDataPlane, PostgresParquetProjectionService, ProductionCanonicalDecisionRepository, ProductionRiskRepository, DeliveryLakeRepository, ProductionKVRepository, ProductionManualWatchRepository, ProductionOpportunityMemoryRepository, ProductionReferenceDataRepository, ProductionPriorityRepository, ProductionSignalLedgerRepository, ProductionPerformanceJournalRepository
from core.data_plane.model_portfolio_repository import ProductionModelPortfolioRepository
from core.data_plane.candle_lake_repository import CandleLakeRepository
from core.data_plane.desk_runtime_repository import DeskRuntimeRepository
from core.desk_runtime_authority import DeskCandidateScannerAuthority, DeskPositionLifecycleAuthority
from core.supervisor import Supervisor
from core.autonomic_control_plane import AutonomicControlPlane, ControlEventBus
from core.production_topology import validate_production_topology
from core.market_level_service import compute_levels_from_candles
from core.range_compression_rule_service import RangeCompressionRuleService
from core.service_logger import ServiceLogger
from core.instrument_resolver import InstrumentResolver
from core.instrument_identity_contract import INDEX_SYMBOL_ALIASES, canonical_listing_identity
from core.engine_dispatch_service import EngineDispatchService
from core.production_mode_policy import POLICY_VERSION, PRODUCTION_MODES, POLICIES, UnsupportedProductionMode, require_production_mode
from core.signal_ledger_service import SignalLedgerService
from core.model_portfolio_service import ModelPortfolioService
from core.operator_capital_settings_service import OperatorCapitalSettingsService
from core.dashboard_readmodel_service import DashboardReadModelService
from actionability import is_actionable_signal
from delivery_timeframes import delivery_timeframe_context
from core.reference_data_service import ReferenceDataService
from core.system_health_service import SystemHealthService
from core.runtime_health_registry import RuntimeHealthRegistry, bounded_runtime_health_snapshot
from core.startup_phase_contract import apply_startup_phase_update, startup_phase_summary
from core.operator_read_model_service import OperatorReadModelService
from core.product_readiness_service import ProductReadinessService
from core.research_plane_contract import build_research_plane_status
from core.instrument_universe_policy import ACTIVE_UNIVERSE_REVISION, is_index_search_query
from core.earnings_calendar_service import EarningsCalendarService
from decision_ledger import DecisionLedger
from research_libraries import ResearchLibraryRegistry
import routes_get as _routes_get
import routes_post as _routes_post

ROUTES_GET = _routes_get.ROUTES
ROUTES_POST = _routes_post.ROUTES
match_prefix_get = _routes_get.match_prefix
from research_adapter import ResearchAdapter
from core.institutional_signal_service import analyze as analyze_institutional_signal
from core.institutional_outcome_service import InstitutionalOutcomeService
from core.production_ranking_service import ProductionRankingService
from core.factor_dedup_service import FactorDedupService
from core.ai_training_publication_service import AITrainingPublicationService
from core.runtime_market_state_store import RuntimeMarketStateStore
from core.runtime_event_buffer import RuntimeEventBuffer
from core.counterfactual_learning_service import CounterfactualLearningService
from core.evidence_score_validation_service import EvidenceScoreValidationService; from core.selection_outcome_settlement_service import SelectionOutcomeSettlementService
from core.forward_evidence_lifecycle_service import ForwardEvidenceLifecycleService
from core.level5_forward_maturity_service import Level5ForwardMaturityService
from core.priority_pipeline_service import PriorityPipelineService
from core.canonical_evidence_snapshot_service import CanonicalEvidenceSnapshotService
from core.cross_plane_reconciliation_service import CrossPlaneReconciliationService
from core.outcome_learning_service import OutcomeLearningService
from core.candidate_discovery_service import CandidateDiscoveryService
from core.intraday_scan_funnel import IntradayScanFunnel
from core.price_action_intelligence_service import PriceActionIntelligenceService
from core.decision_engine_service import DecisionEngineService
from core.scan_orchestration_service import ScanOrchestrationService
from core.heatmap_index_catalog import heatmap_index_identity
from core.universe_authority_repository import OperationalUniverseRepository
from core.universe_authority import CanonicalUniverse, build_canonical_universe, freeze_snapshot, lifecycle_diff
# v39.0: Research Intelligence Ledger + live evidence sync.
# Every Stock Intelligence decision must expose auditable evidence, factors, contradictions and replayability.
# v38.2: Card-contract proof model. Every main cockpit card must declare
# its storage source, intelligence rule, empty-state rule and failure rule.
# This is intentionally served through /api/card-contracts so the UI/reviewer
# can audit the model instead of relying on verbal assurance.
from reference_catalog import (
    CARD_CONTRACTS, FINAL_FALLBACK_INSTRUMENTS, FINAL_INDEX_UNIVERSE,
    FINAL_INDEX_CONSTITUENTS, FINAL_INDEX_ALIAS, SECTOR_INDEX_KEY_MAP,
    SECTOR_INDEX_LABEL, final_fallback_instrument, fallback_instrument_matches, normalize_sector_key,
    final_heatmap_payload, final_index_stocks_payload, final_journal_summary_payload,
)  # moved out of main.py in v65.9.5 cleanup -- see reference_catalog.py

PORT = int(os.environ.get("PROJECT_LADDU_PORT", "8086"))

from core.runtime_control import CONTROL
from core.runtime_primitives import (
    IST, india_now, is_india_market_open, minutes_to_close,
    _parse_candle_ist_date, candle_staleness, _parse_ts_datetime,
    quote_freshness_guard, symbolKey_py, mode_uses_history_without_live,
)
from core.runtime_logging import log_line, _cleanup_old_logs

__all__ = [name for name in globals() if not name.startswith("__")]
