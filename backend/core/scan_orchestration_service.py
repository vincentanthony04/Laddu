"""Composition root for scanner orchestration.

Worker lifecycle, coverage, desk execution, fast-lane processing and discovery
are owned by focused mixins. This module preserves the stable service import.
"""
from __future__ import annotations

from core.scan_orchestration_dependencies import *  # noqa: F401,F403
from core.scan_orchestration_lifecycle import ScanLifecycleMixin
from core.scan_orchestration_coverage import ScanCoverageMixin
from core.scan_orchestration_modes import ScanModeExecutionMixin
from core.scan_orchestration_fast_lane import ScanFastLaneMixin
from core.scan_orchestration_discovery import ScanDiscoveryMixin
from core.desk_analysis_executor_router import DeskAnalysisExecutorRouter


class ScanOrchestrationService(
    ScanLifecycleMixin,
    ScanCoverageMixin,
    ScanModeExecutionMixin,
    ScanFastLaneMixin,
    ScanDiscoveryMixin,
):
    def __init__(self, host, logger=None):
            self.host = host
            self.logger = logger
            self.lanes = ScanLaneCoordinator(publish=self._publish_lane_status)
            self.market_radar_projection = MarketRadarProjectionService(host)
            self.opening_intelligence = OpeningIntelligenceService()
            analysis_fn = getattr(host, "scanner_analyze_compute", None)
            if not callable(analysis_fn):
                analysis_fn = lambda *args, **kwargs: None
            self.analysis_executor = DeskAnalysisExecutorRouter(analysis_fn, enforce_local_input=True)
            store = getattr(host, "store", None)
            self.checkpoints = ScanCheckpointService(store, event=getattr(host, "event", None)) if (
                store is not None and callable(getattr(store, "get_kv", None)) and callable(getattr(store, "set_kv", None))
            ) else None
            # Scanner progress is restored only after the immutable PostgreSQL desk
            # snapshots exist.  Restoring earlier allowed legacy 1,927/2,382-stock
            # counters to leak into the current 1,220/167 desk cards.
            self._checkpoint_reconciled: Dict[str, str] = {}
            if isinstance(getattr(host, "status", None), dict) and getattr(host, "lock", None) is not None:
                self._publish_scanner_progress("intraday")
                self._publish_scanner_progress("delivery")
