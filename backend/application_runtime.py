from __future__ import annotations

from runtime_shared import *
from runtime_lifecycle import RuntimeLifecycleMixin
from runtime_discovery import RuntimeDiscoveryMixin
from runtime_readmodels import RuntimeReadModelsMixin
from runtime_symbol_intelligence import RuntimeSymbolIntelligenceMixin
from core.data_conveyor_runtime_service import DataConveyorRuntimeService
from core.workload_governor import WorkloadGovernor
from core.operations_control_service import OperationsControlService
from core.maturity_projection_service import MaturityProjectionService
from core.product_state_envelope_service import ProductStateEnvelopeService
from core.control_audit_writer import ControlAuditWriter
from core.model_paper_settlement_lineage_service import ModelPaperSettlementLineageService
from core.decision_quote_projection_service import DecisionQuoteProjectionService
from core.model_paper_settlement_reconciliation_service import ModelPaperSettlementReconciliationService
from core.signal_lifecycle_reconciliation_service import SignalLifecycleReconciliationService
from core.level5_learning_loop_service import Level5LearningLoopService
from core.derivatives_context_service import DerivativesContextService
from core.current_thesis_evidence_service import CurrentThesisEvidenceService
from core.production_lake_safety_manifest_service import ProductionLakeSafetyManifestService
from core.historical_alpha_scheduling_service import HistoricalAlphaSchedulingService
from core.research_control_projection_service import ResearchControlProjectionService
from core.http_latency_monitor import HttpLatencyMonitor
from core.trust_state_service import TrustStateService
from core.historical_pit_sweep_service import HistoricalPitSweepService


class LadduRuntime(
    RuntimeLifecycleMixin,
    RuntimeDiscoveryMixin,
    RuntimeReadModelsMixin,
    RuntimeSymbolIntelligenceMixin,
):
    """Composition root for the Project Laddu application runtime.

    Domain behaviour is owned by focused services and concern-specific mixins;
    this class owns construction, shared state and compatibility-facing events.
    """

    def __init__(self):
        self.production_data_plane = ProductionDataPlane()
        self.data_plane_startup = self.production_data_plane.start()
        self._production_data_plane_active = self.production_data_plane.settings.mode == "production"
        # Isolated test mode uses disposable local stores. The installed
        # launcher hard-requires the production authorities.
        runtime_owner = self.production_data_plane.runtime_market_state if self._production_data_plane_active else None
        instrument_owner = self.production_data_plane.instruments if self._production_data_plane_active else None
        event_owner = RuntimeEventBuffer(capacity=10_000) if self._production_data_plane_active else None
        self.store = Store(
            runtime_market_state=runtime_owner,
            production_instrument_repository=instrument_owner,
            runtime_event_buffer=event_owner,
        )
        self.research_governance_migration = {
            "ok": True, "state": "TEST_MODE_NOT_APPLICABLE",
            "count_verified": True, "hash_verified": True, "quarantine_verified": True,
            "authority": "TEST_MODE",
        }
        if self._production_data_plane_active:
            # Retained installs can carry a pre-four-plane market-lake manifest
            # with a blank operational-prune field. Repair only the no-prune
            # safety assertion at startup; reconciliation evidence is preserved
            # byte-for-semantics and is never fabricated here.
            self.production_lake_safety_manifest = ProductionLakeSafetyManifestService(DATA_DIR).ensure()
            # Foreground catalogue proof/search reads use the dedicated
            # interactive PostgreSQL pool; mutation/replacement authority stays
            # on the operational repository injected during Store construction.
            self.store.production_instrument_read_repository = self.production_data_plane.interactive_instruments
            self.store.production_canonical_decision_repository = ProductionCanonicalDecisionRepository(
                self.production_data_plane.operational,
                event_fn=self.event,
                read_authority=self.production_data_plane.interactive,
            )
            self.store.production_risk_repository = ProductionRiskRepository(
                self.production_data_plane.operational,
            )
            self.store.production_delivery_repository = DeliveryLakeRepository(DATA_DIR)
            self.store.production_candle_repository = CandleLakeRepository(DATA_DIR)
            self.store.production_market_time_series_repository = self.production_data_plane.questdb
            self.store.production_model_governance_repository = self.production_data_plane.model_governance
            self.store.production_model_governance_read_repository = self.production_data_plane.model_governance_read
            self.store.production_model_governance_required = True
            governance_repository = self.store.production_model_governance_repository
            if (
                governance_repository is self.store.conn
                or getattr(governance_repository, "authority", None) is None
                or not callable(getattr(governance_repository, "migrate_legacy_research_store", None))
            ):
                raise RuntimeError("PRODUCTION_RESEARCH_GOVERNANCE_COMPATIBILITY_CONNECTION_REJECTED")
            # Candidate-5: normal service startup never replays the retired
            # SQLite research store. The installer owns that one-time migration
            # while the old runtime is quiescent and writes an immutable
            # PostgreSQL completion checkpoint. Startup performs only a bounded
            # checkpoint/census verification so the HTTP listener cannot be
            # held hostage by historical row volume.
            self.research_governance_migration = governance_repository.legacy_research_migration_status(
                self.store
            )
            self.universe_authority_repository = OperationalUniverseRepository(
                self.production_data_plane.operational,
                read_authority=self.production_data_plane.interactive,
            )
            self.store.universe_authority_repository = self.universe_authority_repository
            self.store.production_kv_repository = ProductionKVRepository(
                self.production_data_plane.operational,
                read_authority=self.production_data_plane.interactive,
            )
            self.store.production_manual_watch_repository = ProductionManualWatchRepository(self.production_data_plane.operational)
            self.store.production_opportunity_memory_repository = ProductionOpportunityMemoryRepository(self.production_data_plane.operational, _desk_modes)
            self.store.production_reference_data_repository = ProductionReferenceDataRepository(
                self.production_data_plane.operational,
                read_authority=self.production_data_plane.interactive,
            )
            self.store.production_priority_repository = ProductionPriorityRepository(
                self.production_data_plane.operational, self.store.get_kv, self.store.set_kv
            )
            self.store.production_signal_ledger_repository = ProductionSignalLedgerRepository(
                self.production_data_plane.operational,
                self.store.production_canonical_decision_repository,
                self.event,
            )
            self.store.production_performance_repository = ProductionPerformanceJournalRepository(
                self.production_data_plane.operational
            )
        else:
            # Unit-test mode only. Installed launchers hard-require production,
            # and no customer/scanner/chart route may silently use a
            # compatibility database when PostgreSQL authority is unavailable.
            self.universe_authority_repository = None
        # Separate bounded recovery plane. Live decisions remain memory-first;
        # this store must never become a dependency of urgent risk evaluation.
        self.runtime_market_state = self.store.runtime_market_state
        try:
            self.ai_publication_recovery = AITrainingPublicationService(self.store).publish_pending(
                RUNTIME_DIR / "publication_outbox"
            )
        except Exception as exc:
            self.ai_publication_recovery = {"ok": False, "error": str(exc), "published": []}
        self.factor_dedup = FactorDedupService(self.store)
        self.factor_dedup_bootstrap = self.factor_dedup.apply_static_manifest()
        self.client = UpstoxClient(self.store, self.event)
        self.derivatives_context = DerivativesContextService(self.store, self.client, self.event)
        self.current_thesis_evidence = CurrentThesisEvidenceService(self)
        self.nse_delivery = NSEDeliveryClient(self.event)
        self.fundamentals = FundamentalStore()
        self.lock = threading.RLock()
        self.quote_blocked_until = 0.0
        self._instrument_key_cache: Dict[str, Any] = {}  # v35.9-fix: symbol -> (instrument_dict, cached_at)
        # Provider quote-capability quarantine for exact index tokens that
        # return deterministic HTTP 400. Historical/canonical index evidence
        # remains usable; only the unsupported REST quote retry is suppressed.
        self._index_quote_unavailable_until: Dict[str, float] = {}
        self.status = {
            "service": "starting",
            "started_at": now_iso(),
            "active_mode": "all",
            "auth": {"state": "unknown", "quote_ok": None, "historical_ok": None, "last_test": None, "message": "not tested"},
            "fast_lane": {"state": "idle", "scanned": 0, "promoted": 0, "rejected": 0, "last_run": None, "next_run": None, "note": "Intraday scanner summary", "production_policy_version": POLICY_VERSION},
            "deep_scan": {"state": "idle", "scanned": 0, "promoted": 0, "rejected": 0, "last_run": None, "next_run": None, "cursor": 0, "coverage": {}, "prepared_candidates": 0, "note": "Canonical Delivery scanner summary", "production_policy_version": POLICY_VERSION},
            "mode_scanners": {
                "index_levels": {"state": "idle", "last_run": None, "next_run": None, "levels_cached": 0},
                "intraday": {
                    "state": "idle", "scanned": 0, "promoted": 0, "rejected": 0, "last_run": None, "next_run": None,
                    "coverage": {
                        "state": "idle", "cursor": 0, "universe_size": 0, "sweep_number": 1,
                        "sweep_attempted": 0, "sweep_returned": 0, "sweep_verified": 0,
                        "sweep_missing": 0, "sweep_unverified": 0, "coverage_pct": 0.0,
                        "verified_pct": 0.0, "sweep_complete": False, "last_completed": None,
                    },
                    "analysis": {
                        "state": "idle", "cycle_scanned": 0, "cycle_promoted": 0,
                        "cycle_rejected": 0, "cycle_blocked": 0, "last_completed": None,
                    },
                    "promotion": {"state": "idle", "cycle_promoted": 0, "last_completed": None},
                },
                "delivery": {
                    "state": "idle", "scanned": 0, "promoted": 0, "rejected": 0, "last_run": None, "next_run": None, "cursor": 0,
                    "analysis": {
                        "state": "idle", "cursor": 0, "universe_size": 0, "sweep_number": 1,
                        "sweep_scanned": 0, "coverage_pct": 0.0, "sweep_complete": False,
                        "cycle_scanned": 0, "cycle_promoted": 0, "cycle_rejected": 0,
                        "last_completed": None,
                    },
                    "promotion": {"state": "idle", "cycle_promoted": 0, "last_completed": None},
                },
            },
            "quote_delta": {"state": "idle", "symbols": 0, "last_run": None, "served_from": None, "next_run": None},
            "opportunity_memory": {"state": "active", "count": 0, "last_update": None, "purpose": "remember potential stocks and prioritize rescans before breakout"},
            "last_price_refresh": None,
            "last_historical_fetch": None,
            "last_ai_validation": None,
            "api_errors": [],
            "last_fundamental_refresh": None,
            "last_delivery_refresh": None,
            "delivery_data_sync": {"state": "starting", "last_run": None, "last_report_date": None, "last_file": None, "source": "auto+nse_archive", "message": "waiting for bootstrap"},
            "last_market_data_maintenance": None,
            "storage_maintenance": {"state": "idle", "last_run": None, "message": "post-close bounded retention"},
            "market_layers": {"structure": "enabled", "volume_profile": "enabled", "participation": "enabled", "fundamentals": "mandatory_for_delivery"},
            "live_market_gateway": {"state": "starting", "connected": False},
            "production_data_plane": self.data_plane_startup,
            "quant_research_plane": build_research_plane_status(INSTALL_DIR),
            "research_governance_migration": dict(self.research_governance_migration),
        }
        # Independent, bounded health plane. HTTP health/pipeline probes read
        # this compact projection and never wait for analytical/database work.
        self.health_registry = RuntimeHealthRegistry(self.status)
        self._instrument_health_meta = {"loaded": False, "count": 0, "source": "warming", "cache_usable": False}
        self._canonical_universe = None
        self._universe_snapshots = {}
        self._scanner_snapshot_rows = {"delivery": [], "intraday": []}
        self._fundamental_health_meta = {"loaded": False, "ready": False, "state": "warming"}
        # One canonical stream authority supplies all live UI, chart, position
        # and risk consumers.  HTTP snapshots are a labelled fallback only.
        self.live_market = LiveMarketGateway(self.client.get_token, event_fn=self.event)
        self._interactive_live_symbols: Dict[str, tuple[str, float]] = {}
        self._live_plan_refreshed_at = 0.0
        self._heatmap_cache = []
        self._heatmap_cache_ts = 0.0
        self._coverage_quote_cache = {}
        # v65.26.24: HTTP Market Radar is a pure in-memory read. Projection,
        # persisted-LKG loading and any SQLite waits happen in its own worker.
        self._market_radar_persisted_rows = []
        self._market_radar_persisted_loaded = False
        self._market_radar_lock = threading.RLock()
        self._market_radar_snapshot_ts = 0.0
        self._market_radar_snapshot = {
            "ok": True, "counts": {}, "opportunities": [],
            "market_radar": {
                "coverage": 0, "verified_coverage": 0, "data_state": "warming",
                "reason": "Market Radar projection is warming up",
                "top_gainers": [], "top_losers": [], "volume_shockers": [],
                "intraday_trending": [], "delivery_trending": [], "fo_positioning": [],
                "empty_reasons": {}, "heatmap": [],
            },
            "heatmap": [], "projection_state": "warming",
            "projection_elapsed_ms": 0.0, "time": now_iso(),
        }
        # v51 (Cluster 7): _fund_api_cache/_fund_api_pending moved to reference_data;
        # see alias assignment right after ReferenceDataService is constructed below.
        # v51: cluster 3 extraction -- card cache + dashboard read-model now
        # owned by DashboardReadModelService. self.dashboard.* replaces the
        # old self._cards_cache / _cards_cache_mode / _cards_cache_ts /
        # _cards_refreshing / _last_cards_error attributes directly.
        self.dashboard = DashboardReadModelService(
            store=self.store, status=self.status, event=self.event,
            record_error=self.record_error, runtime_facade=self,
            app_version=APP_VERSION, running_fn=lambda: CONTROL.running,
        )
        # v61.4.1: cache is now keyed by mode (see dashboard_readmodel_service.py);
        # only "all" needs a startup seed since that's the only mode the
        # background loop populates before any request has come in.
        self.dashboard._cards_cache = {
            "all": {
                "selected": [], "final_signals": [], "active_positions": [], "decision_list": [], "watch_queue": [],
                "daily_performance": [], "trade_journal": [],
                "payload_policy": {"version": APP_VERSION, "detail": "safe empty cache at startup; background cards refresh isolated; discovery hydrates async; sector context only when stock-specific"},
                "cache_state": "starting", "time": now_iso()
            }
        }
        self._bad_historical_keys: dict[str, float] = {}  # v36.6: key -> expiry ts (TTL blacklist, not permanent)
        self._BAD_KEY_TTL = 900  # 15 min
        # v36.9.11: hard global cap on concurrent outbound Upstox connections.
        # Prior to this, the historical-refresh pool (3 workers), the deep-scan
        # prefetch pool (a fresh 5-worker pool spun up per batch), and live-quote
        # polling could all be in flight at the same moment -- especially right
        # after boot, when every watchlist symbol needs history at once. Windows
        # starts aborting sockets under that load (WinError 10053/10051), which is
        # what showed up as Stock Intelligence / Chart "timeouts". This semaphore
        # is shared by every code path that opens an Upstox connection, so no
        # matter how many logical thread pools exist, only NET_CONCURRENCY_CAP
        # real sockets are ever open to Upstox at the same time.
        # v77: Windows evidence showed 114 busy rejects and background starvation
        # with four sockets / three interactive reservations. Keep a hard bound,
        # but give the two scanner lanes and exact-gap workers usable capacity.
        self.NET_CONCURRENCY_CAP = 8
        # v37.0: RateController centralizes the concurrency cap and the
        # revalidation throttle below in one module (core/rate_controller.py)
        # instead of scattered instance attributes, so every future caller
        # is forced through the same choke point -- see that module's
        # docstring for why this mattered (WinError 10053 regressions).
        self.rate = RateController(net_concurrency_cap=self.NET_CONCURRENCY_CAP,
                                    revalidate_min_interval_sec=45.0,
                                    interactive_reserved=3)
        self.workload_governor = WorkloadGovernor(self)
        # v37.2: MarketDataService owns all historical/quote/mtf I/O and its
        # own locks/executors (see docs/SERVICE_CONTRACTS_v37_2.md, Cluster A).
        # LadduRuntime methods below are thin delegates so existing routes and
        # callers need zero changes.
        self.market_data = MarketDataService(self.store, self.client, self.rate,
                                              self.event, self.record_error,
                                              host=self, running_fn=lambda: CONTROL.running)
        self.first_useful_mode = FirstUsefulModeService(self)
        self.status["first_useful_mode"] = {"state": "initialising", "desk": "delivery", "time": now_iso()}
        # v37.4: Cluster B/C/D decoupling. Each service gets its own
        # ServiceLogger (separate log file, same shared events table) instead
        # of reaching back into LadduRuntime.event/record_error -- see
        # core/service_logger.py docstring. LadduRuntime methods below
        # (resolve/_first_instrument/_index_instrument_for_chart,
        # analyze_one/_apply_liquidity_gate, audit_open_signal_ledger) become
        # thin delegates, same pattern already used for market_data above.
        self._resolver_logger = ServiceLogger("instrument_resolver", self.store)
        self._engine_logger = ServiceLogger("engine_dispatch", self.store)
        self._ledger_logger = ServiceLogger("signal_ledger", self.store)
        self.instrument_resolver = InstrumentResolver(self.store, self.client, logger=self._resolver_logger)
        self.engine_dispatch = EngineDispatchService(ENGINES, self.market_data, logger=self._engine_logger)
        self.decision_engine = DecisionEngineService(logger=self._engine_logger)
        # Governed paper simulation follows the approved production Final
        # stream only. It has no broker client and therefore cannot place an
        # order even if a future route is misconfigured.
        self.operator_capital_settings = OperatorCapitalSettingsService(
            self.store, default_wallet=TRADING_CAPITAL, default_intraday_cap=100_000.0
        )
        self.operator_capital = self.operator_capital_settings.read()
        self.model_portfolio_repository = (
            ProductionModelPortfolioRepository(
                self.production_data_plane.operational,
                self.production_data_plane.governance,
                read_authority=self.production_data_plane.interactive,
            )
            if self._production_data_plane_active else None
        )
        self.store.production_model_portfolio_repository = self.model_portfolio_repository
        self.level5_learning_loop = Level5LearningLoopService(
            self.model_portfolio_repository,
            getattr(self.store, "production_model_governance_repository", None),
        )
        self.model_paper_settlement_lineage = (
            ModelPaperSettlementLineageService(self.store.production_canonical_decision_repository, self.event)
            if self._production_data_plane_active else None
        )
        self.market_open = is_india_market_open
        self.desk_runtime_repository = DeskRuntimeRepository(self.production_data_plane.operational) if self._production_data_plane_active else None
        self.model_portfolio = ModelPortfolioService(
            self.store,
            equity=float(self.operator_capital.get("model_wallet") or TRADING_CAPITAL),
            intraday_cap=float(self.operator_capital.get("intraday_exposure_ceiling") or 100_000.0),
            repository=self.model_portfolio_repository,
            settlement_sink=self.model_paper_settlement_lineage,
        )
        self.store.model_portfolio_service = self.model_portfolio
        self.settlement_reconciliation = ModelPaperSettlementReconciliationService(
            self.model_portfolio_repository, self.model_paper_settlement_lineage, self.event
        )
        self.signal_lifecycle_reconciliation = SignalLifecycleReconciliationService(
            self.model_portfolio_repository, self.event
        )
        self.intraday_candidate_authority = DeskCandidateScannerAuthority(self, "intraday", self.desk_runtime_repository)
        self.delivery_candidate_authority = DeskCandidateScannerAuthority(self, "delivery", self.desk_runtime_repository)
        self.intraday_lifecycle_authority = DeskPositionLifecycleAuthority(self, "intraday", self.desk_runtime_repository)
        self.delivery_lifecycle_authority = DeskPositionLifecycleAuthority(self, "delivery", self.desk_runtime_repository)
        self.decision_quote_projection = DecisionQuoteProjectionService(self)
        # Dedicated PostgreSQL is the transactional operational authority.
        # Position mutations append to the same PostgreSQL transaction outbox;
        # accepted market observations go to QuestDB and historical projections
        # go to Parquet. DuckDB never owns live decisions or positions.
        if self.production_data_plane.settings.mode == "production":
            self.analytical_projection = PostgresParquetProjectionService(
                self.production_data_plane.operational,
                event_fn=self.event,
            )
        else:
            self.analytical_projection = AnalyticalProjectionService(
                self.store, event_fn=self.event
            )
        def _record_accepted_market_observation(row):
            # One accepted stream observation fans out to two independent
            # persistence planes: immutable analytical tick projection and the
            # bounded canonical runtime bar/quote store. Neither may block the
            # in-memory quote authority or grant trading authority.
            # The hot runtime owner updates memory and enqueues both ticks and
            # canonical bars to QuestDB. Do not enqueue a second copy here.
            self.status["production_data_plane"] = self.production_data_plane.status(probe=False)
            try:
                self.analytical_projection.record_tick(row)
            except Exception as exc:
                self.event("WARN", "analytical_projection", "Accepted tick projection failed", {"error": str(exc)[:180]})
            try:
                self.runtime_market_state.ingest_market_observation(dict(row or {}))
            except Exception as exc:
                self.event("WARN", "canonical_bar_plane", "Accepted tick bar update failed", {"error": str(exc)[:180]})
        self.live_market.accepted_observation_fn = _record_accepted_market_observation
        self.status["analytical_projection"] = self.analytical_projection.status()
        self.status["canonical_bar_plane"] = self.runtime_market_state.canonical_bar_health()
        self.decision_ledger = DecisionLedger(self.store, logger=self._ledger_logger)
        self.research_libraries = ResearchLibraryRegistry()
        self.research_adapter = ResearchAdapter(BASE, store=self.store, logger=self._ledger_logger)
        self.historical_alpha_scheduling = HistoricalAlphaSchedulingService(self.store)
        self._research_result_cache: Dict[str, tuple[float, Dict[str, Any]]] = {}
        self._research_pending: set[str] = set()
        self._delivery_context_cache: Dict[str, tuple[float, Dict[str, Any]]] = {}
        # v51 (Cluster 7): _fund_prefetch_pool moved to reference_data; see alias below.
        self.evidence_score_validation = EvidenceScoreValidationService(self.store); self.selection_outcome_settlement = SelectionOutcomeSettlementService(self.store)
        self.level5_forward_maturity = Level5ForwardMaturityService(
            self.store,
            governance_repository=getattr(self.production_data_plane, "forward_evidence", None),
            model_governance_repository=getattr(self.production_data_plane, "model_governance", None),
            build_version=APP_VERSION,
        )
        self.evidence_snapshots = CanonicalEvidenceSnapshotService(self)
        self.cross_plane_reconciliation = CrossPlaneReconciliationService(self)
        self.priority_pipeline = PriorityPipelineService(self)
        self.data_conveyor = DataConveyorRuntimeService(self, data_dir=DATA_DIR, install_dir=INSTALL_DIR)
        # v99: one event-driven control authority observes data, jobs and Level-5
        # blockers. It may execute only bounded, allow-listed recovery playbooks.
        self.control_event_bus = ControlEventBus(capacity=2500)
        self.control_audit_writer = ControlAuditWriter(self.store, capacity=512)
        self.autonomic_controller = AutonomicControlPlane(self, self.control_event_bus, audit_writer=self.control_audit_writer)
        self.forward_evidence_lifecycle = ForwardEvidenceLifecycleService(
            self.store,
            governance_repository=getattr(self.production_data_plane, "forward_evidence", None),
            maturity_service=self.level5_forward_maturity,
        )
        self.production_ranker = ProductionRankingService(
            self.store, runtime_status=self.status, evidence_validation=self.evidence_score_validation
        )
        self.counterfactual_learning = CounterfactualLearningService(self.store)
        self.outcome_learning = OutcomeLearningService(self.store)
        # v37.5 Phase 2/3: reference data (delivery %, bulk/block deals,
        # market breadth). Own loop, own cadence (once/day),
        # own failure domain -- never shares a thread pool with live quotes.
        self.reference_data = ReferenceDataService(self.store, event=self.event, record_error=self.record_error,
                                                     client=self.client, rate=self.rate, fundamentals=self.fundamentals, host=self)
        # v51 (Cluster 7): fundamentals cache/pending-set/prefetch-pool now live
        # on reference_data; keep these as aliases so any other code in this
        # file that still reaches self._fund_api_cache etc. directly keeps working.
        self._fund_api_cache = self.reference_data._fund_api_cache
        self._fund_api_pending = self.reference_data._fund_api_pending
        self._fund_prefetch_pool = self.reference_data._fund_prefetch_pool
        self.http_latency_monitor = HttpLatencyMonitor()
        self.trust_state_service = TrustStateService(self)
        self.historical_pit_sweep = HistoricalPitSweepService(self)
        self.system_health_service = SystemHealthService(self)
        self.operator_read_models = OperatorReadModelService(self)
        self.research_control_projection = ResearchControlProjectionService(self)
        # Heavy Level-5/product evidence is projected on its own supervised
        # worker. The autonomic controller and Operations HTTP paths consume
        # only this last-known immutable snapshot.
        self.maturity_projection = MaturityProjectionService(self)
        self.operations_control = OperationsControlService(self)
        self.product_state_envelope = ProductStateEnvelopeService(self)
        self.product_readiness_service = ProductReadinessService(self, market_open_fn=is_india_market_open)
        self._last_reference_run_date = None
        ## Retired derivative-chain capability is not constructed or polled.
        # own short-TTL cache -- never on the live tick loop.
        # v37.5 Phase 6: earnings/board-meeting calendar -- read-only event-risk
        # input, own daily cadence, never blocks/overrides a promotion by itself.
        self.earnings_calendar = EarningsCalendarService(self.store, event=self.event, record_error=self.record_error)
        self._last_earnings_run_date = None
        # v37.1: RateController now owns two internal pools (interactive/
        # background) instead of one flat semaphore; there is no longer a
        # single _net_gate to alias. Use self.rate.net_slot(priority=...).
        # v36.9.9: throttle how often a passive "stale_while_revalidate" background
        # check is even dispatched per (instrument_key, interval). Previously every
        # single request (health check, chart poll, market-intelligence call, quote
        # tick) independently called _schedule_historical_refresh, which re-checked
        # candle_coverage() and submitted a threadpool job on every hit even when
        # nothing had changed since the last check a few hundred ms earlier. Under
        # load (Stock Intelligence + Chart Desk both polling the same symbol) this
        # produced a burst of redundant background jobs contending for the same
        # SQLite rows/Upstox connection right as the foreground request also needed
        # to respond within the frontend's timeout -- a likely contributor to the
        # WinError 10053 client-disconnects seen right after symbol selection.
        # Manual/forced refreshes and genuine cache-miss fetches always bypass this.
        self._hist_revalidate_at: Dict[tuple, float] = {}  # unused now, kept only so any stray external reference doesn't AttributeError
        self._HIST_REVALIDATE_MIN_INTERVAL_SEC = 45.0
        # v37.0: Supervisor owns every background daemon loop -- restarts on
        # crash with backoff, tracks per-loop heartbeats. See core/supervisor.py.
        # Loops are registered here but only started in start(), same as before.
        self.supervisor = Supervisor(event_fn=self.event)

        def _recover_scanner(mode):
            def handler(context):
                # Clean Core deterministic analysis has no disposable worker
                # generation in either desk. Recovery therefore reconciles the
                # immutable desk checkpoint and queues/coalesces a new governed
                # scan; it never calls a generation-rotation API that is
                # structurally unsupported and would convert a safe action into
                # a false 409 failure.
                desk = "intraday" if str(mode).lower() == "intraday" else "delivery"

                def work():
                    try:
                        self.freeze_authoritative_universe()
                        self.scan_orchestration._ensure_checkpoint_reconciled(desk)
                        self.scan_orchestration.request_scan(desk)
                    except Exception as exc:
                        self.record_error(f"{desk}_scanner_recovery", str(exc)[:240])

                from core.background_repair_dispatcher import for_app as repair_dispatcher_for_app
                submitted = repair_dispatcher_for_app(self).submit(
                    f"scanner-recovery:{desk}", work
                )
                accepted = bool(submitted.accepted or submitted.state == "COALESCED")
                return {
                    "ok": accepted, "accepted": accepted, "verified": False,
                    "state": (
                        f"{desk.upper()}_AUTHORITY_RECONCILE_AND_SCAN_ACCEPTED"
                        if accepted else f"{desk.upper()}_RECOVERY_CAPACITY_DEFERRED"
                    ),
                    "dispatcher_state": submitted.state,
                    "recovery_kind": "AUTHORITATIVE_CHECKPOINT_AND_SCAN",
                }
            return handler

        def _recover_read_models(context):
            result = self.operator_read_models.refresh()
            self.dashboard.refresh_cards_cache("all")
            return {"ok": str(result.get("state") or "").lower() in {"ready", "warming"}, "state": result.get("state"), "action": "read models and card cache refreshed"}

        def _recover_card_cache(context):
            # Recovery dispatch must never block the autonomic controller for
            # minutes while a dashboard projection rebuilds.  Refresh each cache
            # in the background and let the controller verify a changed business
            # token before calling the recovery successful.
            refreshing = dict(getattr(self.dashboard, "_cards_refreshing", {}) or {})
            if any(bool(refreshing.get(mode)) for mode in ("all", "delivery", "intraday")):
                return {
                    "ok": True, "accepted": True, "verified": False,
                    "state": "CARD_CACHE_REFRESH_ALREADY_IN_FLIGHT",
                    "modes": {mode: bool(refreshing.get(mode)) for mode in ("all", "delivery", "intraday")},
                }

            def work():
                for mode in ("all", "delivery", "intraday"):
                    try:
                        self.dashboard.refresh_cards_cache(mode)
                    except Exception as exc:
                        self.record_error("card_cache_recovery", f"{mode}: {str(exc)[:180]}")

            threading.Thread(target=work, name="LadduRecoverCardCache", daemon=True).start()
            return {
                "ok": True, "accepted": True, "verified": False,
                "state": "CARD_CACHE_REFRESH_ACCEPTED",
                "modes": {"all": "scheduled", "delivery": "scheduled", "intraday": "scheduled"},
            }

        def _recover_data_conveyor(context):
            inflight = dict(self.data_conveyor.cycle_inflight() or {})
            snapshot = dict((context or {}).get("snapshot") or {})
            reason = " | ".join(
                str(value) for value in (
                    (context or {}).get("reason"), snapshot.get("waiting_on"), snapshot.get("stage")
                ) if value
            )
            plan = self.data_conveyor.recovery_plan(inflight, reason)
            lanes = list(plan.get("lanes") or [])
            if not lanes:
                # An equivalent lane already running is an idempotent accepted
                # state, never a recovery failure and never circuit-breaker fuel.
                return {
                    "ok": True, "accepted": False, "verified": False,
                    "state": plan.get("state"), "inflight": inflight,
                    "idempotent": True,
                }
            for lane in lanes:
                target = self.data_conveyor.run_research_once if lane == "research" else self.data_conveyor.run_official_once
                threading.Thread(
                    target=target,
                    name=f"LadduControllerDataConveyor-{lane}",
                    daemon=True,
                ).start()
            return {
                "ok": True, "accepted": True, "verified": False,
                "state": "RECOVERY_LANE_ACCEPTED", "lanes": lanes,
                "inflight": inflight,
            }

        def _recover_priority(context):
            result = dict(self.priority_pipeline.recover_stale() or {})
            return {"ok": int(result.get("blocked") or 0) == 0, **result}

        def _recover_index_levels(context):
            def work():
                self.scan_orchestration.run_index_level_scan()
                self.operator_read_models.refresh()
                self.dashboard.refresh_cards_cache("all")
            threading.Thread(target=work, name="LadduRecoverIndexLevels", daemon=True).start()
            return {"ok": True, "state": "INDEX_SNAPSHOT_REBUILD_ACCEPTED"}

        def _recover_instrument_bootstrap(context):
            # Recovery is catalogue-focused and nonblocking. Never launch another
            # synchronous universe freeze beside a stale worker generation.
            try:
                meta = dict(self.client._cached_instrument_meta("instrument-recovery") or {})
                stats = dict(meta.get("universe_stats") or {})
                revision_current = meta.get("universe_revision") == ACTIVE_UNIVERSE_REVISION
                loaded = bool(meta.get("loaded") and int(meta.get("count") or 0) > 0 and revision_current and int(stats.get("derivatives") or 0) == 0)
                if loaded:
                    try:
                        self.instrument_resolver.clear_negative_cache()
                        self._refresh_live_subscription_plan(force=True)
                    except Exception:
                        pass
                    return {"ok": True, "state": "INSTRUMENT_AUTHORITY_ALREADY_READY", "count": int(meta.get("count") or 0)}
                self.client.refresh_instruments_background(force=not revision_current)
                return {"ok": True, "state": "INSTRUMENT_AUTHORITY_REFRESH_ACCEPTED", "count": int(meta.get("count") or 0)}
            except Exception as exc:
                return {"ok": False, "state": "INSTRUMENT_AUTHORITY_RECOVERY_FAILED", "error": str(exc)[:240]}

        def _recover_forward_evidence(context):
            if self.forward_evidence_lifecycle.cycle_inflight():
                return {"ok": False, "state": "FORWARD_EVIDENCE_CYCLE_ALREADY_IN_FLIGHT"}
            def work():
                self.forward_evidence_lifecycle.run_once(limit=80)
            threading.Thread(target=work, name="LadduRecoverForwardEvidence", daemon=True).start()
            return {"ok": True, "state": "FORWARD_EVIDENCE_CYCLE_ACCEPTED"}

        def _recover_operations_projection(context):
            # A second refresh thread cannot repair a blocked first refresh and
            # would only add contention. Let the controller expose the in-flight
            # blocker until the original generation completes or fails.
            if self.operations_control.refresh_inflight():
                return {"ok": False, "state": "OPERATIONS_PROJECTION_ALREADY_IN_FLIGHT"}
            threading.Thread(target=self.operations_control.refresh, name="LadduRecoverOperationsProjection", daemon=True).start()
            return {"ok": True, "state": "OPERATIONS_PROJECTION_REFRESH_ACCEPTED"}

        self.supervisor.register("intraday_scanner", lambda sup: self.intraday_candidate_authority.loop(sup, running_fn=lambda: CONTROL.running), stale_after_sec=60, progress_stale_after_sec=180, recover_fn=_recover_scanner("intraday"))
        self.supervisor.register("intraday_coverage", lambda sup: self.scan_orchestration.intraday_coverage_loop(sup, running_fn=lambda: CONTROL.running), stale_after_sec=75, progress_stale_after_sec=240)
        self.supervisor.register("delivery_scanner", lambda sup: self.delivery_candidate_authority.loop(sup, running_fn=lambda: CONTROL.running), stale_after_sec=120, progress_stale_after_sec=150, recover_fn=_recover_scanner("delivery"))
        self.supervisor.register("delivery_coverage", lambda sup: self.scan_orchestration.delivery_coverage_loop(sup, running_fn=lambda: CONTROL.running), stale_after_sec=60, progress_stale_after_sec=90, recover_fn=_recover_scanner("delivery"))
        self.supervisor.register("index_levels", lambda sup: self.scan_orchestration.index_levels_loop(sup, running_fn=lambda: CONTROL.running), stale_after_sec=90, progress_stale_after_sec=300, recover_fn=_recover_index_levels)
        self.supervisor.register("market_heat", lambda sup: self.scan_orchestration.heat_loop(sup, running_fn=lambda: CONTROL.running), stale_after_sec=180)
        self.supervisor.register("market_radar_projection", lambda sup: self.scan_orchestration.market_radar_loop(sup, running_fn=lambda: CONTROL.running), stale_after_sec=120)
        self.supervisor.register(
            "settlement_reconciliation",
            lambda sup: self.settlement_reconciliation.loop(sup, running_fn=lambda: CONTROL.running),
            stale_after_sec=90, progress_stale_after_sec=180, safety_class="LEDGER_AUTHORITY",
        )
        self.supervisor.register(
            "signal_lifecycle_reconciliation",
            lambda sup: self.signal_lifecycle_reconciliation.loop(sup, running_fn=lambda: CONTROL.running),
            stale_after_sec=90, progress_stale_after_sec=180, safety_class="LEDGER_AUTHORITY",
        )
        self.supervisor.register("live_market_stream", lambda sup: self.live_market.run(sup, running_fn=lambda: CONTROL.running, market_open_fn=is_india_market_open), stale_after_sec=45)
        self.supervisor.register("decision_quote_projection", lambda sup: self.decision_quote_projection.loop(sup, running_fn=lambda: CONTROL.running), stale_after_sec=45, progress_stale_after_sec=90, safety_class="SAFE_COMPONENT")
        self.supervisor.register("decision_quote_side_effects", lambda sup: self.decision_quote_projection.side_effect_loop(sup, running_fn=lambda: CONTROL.running), stale_after_sec=45, progress_stale_after_sec=180, safety_class="SAFE_COMPONENT")
        self.supervisor.register("intraday_lifecycle", lambda sup: self.intraday_lifecycle_authority.loop(sup, running_fn=lambda: CONTROL.running), stale_after_sec=20, safety_class="RISK_AUTHORITY")
        self.supervisor.register("delivery_lifecycle", lambda sup: self.delivery_lifecycle_authority.loop(sup, running_fn=lambda: CONTROL.running), stale_after_sec=120, safety_class="RISK_AUTHORITY")
        self.supervisor.register("quote_delta", lambda sup: self.quote_delta_loop(sup), stale_after_sec=60)
        self.supervisor.register("analytical_projection", lambda sup: self.analytical_projection.run(sup, running_fn=lambda: CONTROL.running), stale_after_sec=90)
        self.supervisor.register("operator_read_models", lambda sup: self.operator_read_models.run(sup, running_fn=lambda: CONTROL.running), stale_after_sec=120, progress_stale_after_sec=180, recover_fn=_recover_read_models)
        self.supervisor.register("research_control_projection", lambda sup: self.research_control_projection.run(sup, running_fn=lambda: CONTROL.running), stale_after_sec=240, progress_stale_after_sec=420)
        self.supervisor.register("priority_pipeline_recovery", lambda sup: self.priority_pipeline.recovery_loop(sup, running_fn=lambda: CONTROL.running), stale_after_sec=150, progress_stale_after_sec=240, recover_fn=_recover_priority)
        self.supervisor.register("delivery_data_sync", lambda sup: self.delivery_data_loop(sup), stale_after_sec=max(1200, NSE_DELIVERY_REFRESH_SECONDS * 2))
        self.supervisor.register("instrument_bootstrap", lambda sup: self.instrument_bootstrap(sup), stale_after_sec=600, progress_stale_after_sec=900, recover_fn=_recover_instrument_bootstrap)
        self.supervisor.register("card_cache", lambda sup: self.dashboard.card_cache_loop(sup), stale_after_sec=90, progress_stale_after_sec=180, recover_fn=_recover_card_cache)
        self.supervisor.register("reference_data_daily", lambda sup: self.reference_data_loop(sup), stale_after_sec=3700)
        self.supervisor.register("earnings_calendar_daily", lambda sup: self.earnings_calendar_loop(sup), stale_after_sec=3700)
        self.supervisor.register("daily_learning", lambda sup: self.daily_learning_loop(sup), stale_after_sec=3700)
        self.supervisor.register("forward_evidence_lifecycle", lambda sup: self.forward_evidence_lifecycle.loop(sup, running_fn=lambda: CONTROL.running), stale_after_sec=420, progress_stale_after_sec=900, recover_fn=_recover_forward_evidence)
        self.supervisor.register("deep_history_backfill", lambda sup: self.deep_history_backfill_loop(sup), stale_after_sec=7200, progress_stale_after_sec=7800)
        self.supervisor.register("historical_pit_enrichment", lambda sup: self.historical_pit_sweep.loop(sup, running_fn=lambda: CONTROL.running), stale_after_sec=180, progress_stale_after_sec=7200)
        self.supervisor.register("data_conveyor", lambda sup: self.data_conveyor.loop(sup, running_fn=lambda: CONTROL.running), stale_after_sec=300, progress_stale_after_sec=900, recover_fn=_recover_data_conveyor)
        self.supervisor.register("storage_maintenance", lambda sup: self.storage_maintenance_loop(sup), stale_after_sec=3700, safety_class="DATABASE_AUTHORITY")
        self.supervisor.register("maturity_projection", lambda sup: self.maturity_projection.run(sup, running_fn=lambda: CONTROL.running), stale_after_sec=180, progress_stale_after_sec=360)
        self.supervisor.register("operations_projection", lambda sup: self.operations_control.run(sup, running_fn=lambda: CONTROL.running), stale_after_sec=30, progress_stale_after_sec=60, recover_fn=_recover_operations_projection)
        self.supervisor.register("product_state_envelope", lambda sup: self.product_state_envelope.run(sup, running_fn=lambda: CONTROL.running), stale_after_sec=30, progress_stale_after_sec=90)
        self.supervisor.register("control_audit_writer", lambda sup: self.control_audit_writer.run(sup, running_fn=lambda: CONTROL.running), stale_after_sec=90, progress_stale_after_sec=180)
        self.supervisor.register("autonomic_controller", lambda sup: self.autonomic_controller.loop(sup, running_fn=lambda: CONTROL.running), stale_after_sec=45, progress_stale_after_sec=75)
        # v68: startup is physically phased. Critical market/risk/identity
        # workers start first; scanner/read-model workers wait for HTTP and the
        # focused identity authority; bulk delivery/research/retention workers
        # start last. This prevents bulk writes from competing with startup,
        # health, search, risk or live-market recovery.
        self._startup_critical_workers = (
            "live_market_stream", "intraday_lifecycle", "delivery_lifecycle",
            "quote_delta", "instrument_bootstrap",
        )
        self._startup_operational_workers = (
            "intraday_scanner", "intraday_coverage", "delivery_scanner", "delivery_coverage",
            "index_levels", "market_heat", "market_radar_projection",
            "settlement_reconciliation", "signal_lifecycle_reconciliation", "operator_read_models", "research_control_projection", "priority_pipeline_recovery", "card_cache",
            "maturity_projection", "operations_projection", "product_state_envelope", "control_audit_writer", "autonomic_controller", "decision_quote_projection", "decision_quote_side_effects",
        )
        self._startup_bulk_workers = (
            "analytical_projection", "delivery_data_sync", "reference_data_daily",
            "earnings_calendar_daily", "daily_learning", "forward_evidence_lifecycle", "deep_history_backfill",
            "historical_pit_enrichment", "data_conveyor", "storage_maintenance",
        )
        phased = set(self._startup_critical_workers + self._startup_operational_workers + self._startup_bulk_workers)
        registered = set(self.supervisor.registered_names)
        if phased != registered:
            raise RuntimeError(
                "STARTUP_PHASE_REGISTRY_MISMATCH: missing="
                + ",".join(sorted(registered - phased))
                + "; unknown=" + ",".join(sorted(phased - registered))
            )
        self._http_ready_event = threading.Event()
        self._universe_freeze_lock = threading.Lock()
        self.status["startup_phases"] = {
            "version": "startup-phase-coordinator-1.1.0",
            "state": "STARTING",
            "required_phases": ["http", "critical", "operational"],
            "optional_phases": ["bulk"],
            "required_complete": False,
            "optional_complete": False,
            "http": {"state": "PENDING"},
            "critical": {"state": "PENDING", "workers": list(self._startup_critical_workers)},
            "operational": {"state": "PENDING", "workers": list(self._startup_operational_workers)},
            "bulk": {"state": "PENDING", "workers": list(self._startup_bulk_workers), "installation_blocking": False},
        }
        # v65.27.0: executable architecture fitness check. Startup fails before
        # any worker starts if config, policy, engine registry or worker wiring
        # drift apart. This closes the recurring "declared mode, missing dispatch"
        # bug class rather than adding another one-off regression test.
        self.production_topology = validate_production_topology(
            production_modes=PRODUCTION_MODES,
            policy_modes=POLICIES.keys(),
            engine_modes=ENGINES.keys(),
            refresh_modes=MODE_REFRESH_SECONDS.keys(),
            worker_names=self.supervisor.registered_names,
        )
        self.status["production_topology"] = self.production_topology
        self.health_registry.publish_runtime(self.status, state="fresh")
        self._level_cache: Dict[str, Dict[str, Any]] = {}
        self._level_cache_ts = 0.0
        self.event("INFO", "runtime", f"Project Laddu {APP_VERSION} starting", {"install_dir": str(INSTALL_DIR)})
        _cleanup_old_logs()

    @property
    def scan_orchestration(self) -> "ScanOrchestrationService":
        """Lazily constructed so tests that bypass __init__ (object.__new__(LadduRuntime))
        to unit-test extracted logic in isolation still work without full runtime setup."""
        svc = self.__dict__.get("_scan_orchestration")
        if svc is None:
            svc = ScanOrchestrationService(self, logger=getattr(self, "_engine_logger", None))
            self.__dict__["_scan_orchestration"] = svc
        return svc

    def _is_bad_key(self, key: str) -> bool:
        exp = self._bad_historical_keys.get(key)
        if exp is None:
            return False
        if time.time() >= exp:
            self._bad_historical_keys.pop(key, None)
            return False
        return True

    def event(self, level: str, module: str, message: str, detail: Dict[str, Any] | None = None) -> None:
        try:
            self.store.event(level, module, message, detail or {})
        except Exception:
            pass
        log_line(f"{level} [{module}] {message} {json.dumps(detail or {})}")

    def record_error(self, module: str, error: str, endpoint: str | None = None) -> None:
        item = {"time": now_iso(), "module": module, "error": error}
        if endpoint:
            item["endpoint"] = endpoint
        err = str(error)
        # v60: this whole read-modify-write (api_errors trim+append, nested
        # auth.state) now happens under self.lock -- previously unguarded,
        # and record_error is called from every background loop thread on
        # any API error, concurrently with HTTP handler threads reading
        # self.status to build health responses.
        with self.lock:
            self.status["api_errors"] = (self.status.get("api_errors") or [])[-7:] + [item]
            if "429" in err or "Too Many Requests" in err:
                if module in ("quote", "index", "fast_lane", "search"):
                    self.quote_blocked_until = time.time() + (180 if is_india_market_open() else 600)
                    self.status["auth"]["state"] = "quote_rate_limited"
                if module in ("historical", "deep_scan", "mtf_trend"):
                    self.market_data.hist_blocked_until = time.time() + (300 if is_india_market_open() else 900)
                    self.status["auth"]["state"] = "historical_rate_limited"
            if "403" in err:
                if module in ("quote", "index", "fast_lane"):
                    self.quote_blocked_until = time.time() + 300
                    self.status["auth"]["state"] = "quote_api_blocked"
                if module in ("historical", "deep_scan"):
                    self.market_data.hist_blocked_until = time.time() + 300
                    self.status["auth"]["state"] = "historical_api_blocked"
            self.health_registry.publish_runtime(self.status, state="fresh")


APP = LadduRuntime()
