"""Shared dependencies and pure helpers for scanner orchestration."""
import re
import threading
import time
from math import ceil, log10
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, time as dtime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional
from actionability import is_actionable_signal
from config import MAX_FASTLANE, DEEP_SCAN_BATCH, MAX_PROMOTED_PER_SECTOR, MODE_REFRESH_SECONDS, MAX_INTRADAY, INTRADAY_PRIORITY_LANE, INTRADAY_COVERAGE_LANE, INTRADAY_COVERAGE_OPEN_SECONDS, INTRADAY_COVERAGE_CLOSED_SECONDS, INTRADAY_QUOTE_BATCH, INTRADAY_SCREEN_SHORTLIST, INTRADAY_DEEP_ANALYSIS, INTRADAY_SCAN_BUDGET_SEC
from core.bounded_analysis_executor import BoundedDeskAnalysisExecutor
from core.deterministic_analysis_executor import DeterministicDeskAnalysisExecutor
from core.candidate_discovery_service import CandidateDiscoveryService
from core.selection_fairness_service import SelectionFairnessService, FAIRNESS_VERSION
from core.intelligent_universe_screening_service import IntelligentUniverseScreeningService, SCREENING_VERSION
from core.intraday_scan_funnel import IntradayScanFunnel
from core.opening_intelligence_service import OpeningIntelligenceService
from core.market_radar_projection_service import MarketRadarProjectionService
from core.scan_lane_coordinator import ScanLaneCoordinator
from core.scan_checkpoint_service import ScanCheckpointService, CHECKPOINT_VERSION, PROGRESS_CONTRACT_VERSION
from core.market_level_service import compute_levels_from_candles
from core.production_mode_policy import POLICY_VERSION, UnsupportedProductionMode, require_production_mode
from core.production_ranking_service import RANKING_VERSION
from core.quant_scan_capture_service import record_quant_scan_cycle
from core.operational_evidence_integrity_service import attach_evidence_integrity
from core.market_radar_quote_service import MarketRadarQuoteService
from core.instrument_universe_policy import ACTIVE_UNIVERSE_REVISION
from engines import ENGINES
from models import now_iso
from storage import INTELLIGENCE_SCAN_SYMBOLS, NIFTY50_CORE, NIFTY250_CORE, NEXT50_CORE
IST = timezone(timedelta(hours=5, minutes=30))
def india_now() -> datetime:
    """Return India Standard Time explicitly; do not depend on Windows/server local timezone."""
    return datetime.now(IST)
def is_india_market_open() -> bool:
    from core.market_clock import is_india_market_open as canonical_market_open
    return canonical_market_open(india_now())
def mode_uses_history_without_live(mode: str) -> bool:
    return str(mode or "").lower().strip() == "delivery"

from core.production_mode_policy import require_production_mode
from core.production_mode_policy import normalise_mode


def symbolKey_py(v: Any) -> str:
    return re.sub(r"\s+", " ", str(v or "").strip().upper())

__all__ = [name for name in globals() if not name.startswith("__")]
