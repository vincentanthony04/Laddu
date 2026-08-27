from __future__ import annotations

from datetime import datetime, timezone
import threading
from typing import Any, Mapping

from .config import DataPlaneSettings
from .postgres import PostgresAuthority
from .questdb import QuestDBMicroBatchWriter
from .hot_runtime_store import HotRuntimeMarketStateStore
from .instrument_repository import ProductionInstrumentRepository
from .model_governance_repository import ProductionModelGovernanceRepository
from .forward_evidence_governance_repository import ForwardEvidenceGovernanceRepository


class ProductionDataPlane:
    SERVICE_VERSION = "production-data-plane-1.2.0-interactive-read-capacity"

    def __init__(self, settings: DataPlaneSettings | None = None):
        self.settings = settings or DataPlaneSettings.from_env()
        self.operational = PostgresAuthority(self.settings.operational_dsn, role="operational", max_size=12)
        # Dedicated foreground read capacity. Interactive stock/search/status reads
        # must not queue behind scanner, backfill or research transactions.
        self.interactive = PostgresAuthority(self.settings.operational_dsn, role="interactive-read", min_size=2, max_size=8)
        self.governance = PostgresAuthority(self.settings.governance_dsn, role="governance", max_size=8)
        # Dedicated read-only governance capacity keeps browser/read-model and WFA
        # diagnostics from queuing behind publication/settlement writes.
        self.governance_read = PostgresAuthority(self.settings.governance_dsn, role="governance-read", min_size=1, max_size=6)
        self.instruments = ProductionInstrumentRepository(self.operational)
        self.interactive_instruments = ProductionInstrumentRepository(self.interactive)
        self.model_governance = ProductionModelGovernanceRepository(self.governance)
        self.model_governance_read = ProductionModelGovernanceRepository(self.governance_read)
        self.forward_evidence = ForwardEvidenceGovernanceRepository(self.governance)
        self.questdb = QuestDBMicroBatchWriter(
            self.settings.questdb_http_url,
            username=self.settings.questdb_username,
            password=self.settings.questdb_password,
            flush_ms=self.settings.questdb_flush_ms,
            batch_size=self.settings.questdb_batch_size,
        )
        self.runtime_market_state = HotRuntimeMarketStateStore(
            tick_sink=self.record_tick,
            bar_sink=self.record_bar,
            quality_sink=self.record_quality_event,
        )
        self._lock = threading.RLock()
        self._last_probe: dict[str, Any] | None = None

    def start(self) -> dict[str, Any]:
        if self.settings.mode == "test":
            return self.status(probe=False)
        if self.settings.require_operational:
            self.operational.open()
            self.interactive.open()
        if self.settings.require_governance:
            self.governance.open()
            self.governance_read.open()
        if self.settings.require_questdb:
            self.questdb.start()
        result = self.status(probe=True)
        blockers = result.get("blockers") or []
        if self.settings.mode == "production" and blockers:
            self.close()
            raise RuntimeError("PRODUCTION_DATA_PLANE_BLOCKED: " + "; ".join(blockers))
        return result

    def close(self) -> None:
        self.questdb.close()
        self.operational.close()
        self.interactive.close()
        self.governance.close()
        self.governance_read.close()

    def record_tick(self, row: Mapping[str, Any]) -> bool:
        if self.settings.mode != "production":
            return False
        return self.questdb.enqueue_tick(row)

    def record_bar(self, row: Mapping[str, Any]) -> bool:
        if self.settings.mode != "production":
            return False
        return self.questdb.enqueue_bar(row)

    def record_quality_event(self, row: Mapping[str, Any]) -> bool:
        if self.settings.mode != "production":
            return False
        return self.questdb.enqueue_quality_event(row)

    def status(self, *, probe: bool = False) -> dict[str, Any]:
        if probe:
            op = self.operational.probe(
                required_schemas=("core", "trading", "risk", "accounting", "integration", "market_data", "scanner", "runtime_control", "reference"),
                required_relations=(
                    "core.instruments", "core.securities", "core.listings", "core.universe_snapshots",
                    "market_data.coverage", "market_data.hydration_jobs", "scanner.scan_runs",
                    "trading.canonical_decisions", "trading.model_paper_positions", "trading.signal_lifecycle_events",
                    "risk.control_state", "accounting.journal_entries",
                    "integration.event_inbox", "integration.transactional_outbox",
                    "runtime_control.schema_migrations",
                    "reference.bulk_block_deals",
                    "reference.market_breadth_daily", "reference.reference_data_runs",
                    "reference.fundamentals_cache", "reference.earnings_calendar",
                    "trading.priority_symbols",
                    "trading.manual_trade_journal", "trading.outcome_learning",
                    "runtime_control.kv", "runtime_control.daily_learning",
                    "trading.desk_candidates", "trading.desk_candidate_events",
                    "trading.desk_runtime_checkpoints",
                ),
            )
            gov = self.governance.probe(
                required_schemas=("model_registry", "research", "deployment", "runtime_control"),
                required_relations=(
                    "model_registry.models", "research.predictions", "research.prediction_outcomes",
                    "research.experiments", "research.experiment_metrics",
                    "deployment.promotion_decisions", "deployment.assignments",
                    "runtime_control.schema_migrations",
                    "research.selector_populations", "research.selector_population_members",
                    "research.selector_arm_predictions", "research.selector_outcomes",
                    "research.forward_maturity_checkpoints", "research.training_validation_evidence",
                    "research.learning_findings", "research.rule_change_proposals",
                ),
            )
            gov_read = self.governance_read.probe(
                required_schemas=("model_registry", "research", "deployment", "runtime_control"),
                required_relations=("research.selector_population_members", "research.selector_outcomes", "research.training_validation_evidence", "runtime_control.schema_migrations"),
            )
            interactive = self.interactive.probe(required_schemas=("core", "market_data", "reference", "runtime_control"), required_relations=("core.instruments", "market_data.coverage", "reference.fundamentals_cache", "runtime_control.kv"))
            qdb = self.questdb.probe()
            instrument_proof: dict[str, Any] = {}
            if op.ok:
                try:
                    instrument_proof = self.instruments.proof().as_dict()
                except Exception as exc:
                    instrument_proof = {"error": f"{type(exc).__name__}: {exc}"[:300]}
            snapshot = {
                "operational_postgres": op.as_dict(),
                "interactive_postgres": interactive.as_dict(),
                "governance_postgres": gov.as_dict(),
                "governance_read_postgres": gov_read.as_dict(),
                "questdb": qdb.as_dict(),
                "instrument_authority": instrument_proof,
                "probed_at": datetime.now(timezone.utc).isoformat(),
            }
            with self._lock:
                self._last_probe = snapshot
        else:
            with self._lock:
                snapshot = dict(self._last_probe or {})
        blockers: list[str] = []
        for required, key in ((self.settings.require_operational, "operational_postgres"),
                              (self.settings.require_governance, "governance_postgres"),
                              (self.settings.require_governance, "governance_read_postgres"),
                              (self.settings.require_questdb, "questdb")):
            if required and not bool((snapshot.get(key) or {}).get("ok")):
                blockers.append(f"{key.upper()}_UNAVAILABLE")
        writer = self.questdb.status()
        if self.settings.require_questdb:
            if str(writer.get("state") or "").lower() == "degraded":
                blockers.append("QUESTDB_WRITER_DEGRADED")
            if int(writer.get("dropped") or 0) > 0:
                blockers.append("QUESTDB_DURABILITY_QUEUE_DROPPED_EVENTS")
            if float(writer.get("current_oldest_queue_age_ms") or 0.0) > self.settings.questdb_max_queue_age_ms:
                blockers.append("QUESTDB_DURABILITY_QUEUE_STALE")
        return {
            "service_version": self.SERVICE_VERSION,
            "mode": self.settings.mode,
            "production_ready": self.settings.mode == "production" and not blockers,
            "blockers": blockers,
            "planes": snapshot,
            "questdb_writer": writer,
            "authority_contract": {
                "hot_runtime": "in_process_memory",
                "operational": "dedicated_postgresql_cluster_with_isolated_interactive_read_pool",
                "interactive_read": "reserved_postgresql_pool_for_search_stock_report_and_operations",
                "postgres_recovery": "stable_logical_authority_with_verified_atomic_physical_pool_generation_swap",
                "market_time_series": "dedicated_questdb_service",
                "historical_analytical": "versioned_parquet_and_read_only_duckdb",
                "model_governance": "separate_postgresql_cluster",
                "governance_read": "dedicated_read_only_pool_for_research_ui_and_wfa_diagnostics",
                "legacy_sqlite": "migration_and_read_only_compatibility_only",
            },
        }
