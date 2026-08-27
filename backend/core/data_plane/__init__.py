"""Production data-plane adapters for Project Laddu v68.

The live application owns no hidden fallback from PostgreSQL/QuestDB to the
legacy SQLite file. Compatibility mode is explicit and is never reported as
production-operational.
"""
from .config import DataPlaneSettings
from .coordinator import ProductionDataPlane

__all__ = ["DataPlaneSettings", "ProductionDataPlane"]

from .hot_runtime_store import HotRuntimeMarketStateStore
from .projection import PostgresParquetProjectionService
from .canonical_decision_repository import ProductionCanonicalDecisionRepository
from .risk_repository import ProductionRiskRepository
from .delivery_lake_repository import DeliveryLakeRepository
from .instrument_repository import ProductionInstrumentRepository, InstrumentAuthorityProof
from .model_governance_repository import ProductionModelGovernanceRepository
from .forward_evidence_governance_repository import ForwardEvidenceGovernanceRepository
from .kv_repository import ProductionKVRepository
from .manual_watch_repository import ProductionManualWatchRepository
from .opportunity_memory_repository import ProductionOpportunityMemoryRepository
from .reference_data_repository import ProductionReferenceDataRepository
from .priority_repository import ProductionPriorityRepository
from .signal_ledger_repository import ProductionSignalLedgerRepository
from .performance_journal_repository import ProductionPerformanceJournalRepository
