from __future__ import annotations

from datetime import datetime, timezone
import json
from typing import Any, Callable, Dict, List, Mapping, Optional

from core.outcome_learning_service import attribute_outcome, learning_features
from core.production_mode_policy import require_production_mode
from core.signal_ledger_repository import SignalLedgerRepository
from models import now_iso
from .canonical_decision_repository import ProductionCanonicalDecisionRepository
from .postgres import PostgresAuthority


class ProductionSignalLedgerRepository(SignalLedgerRepository):
    """Canonical PostgreSQL decision ledger projected through the legacy API.

    There is deliberately no second production `signal_ledger` table.  Every
    Today Entry, selected row, lifecycle update and outcome resolves to the same
    canonical decision record and append-only event stream.
    """

    production_authority = True

    def __init__(
        self,
        operational: PostgresAuthority,
        canonical: ProductionCanonicalDecisionRepository,
        event_fn: Callable[..., None],
    ):
        # Do not call the SQLite parent constructor.  We inherit only its pure
        # lifecycle mathematics and formatting helpers.
        self.operational = operational
        self.canonical = canonical
        self._event = event_fn

    @staticmethod
    def _payload(row: Mapping[str, Any]) -> Dict[str, Any]:
        raw = row.get("payload_json") if isinstance(row, Mapping) else None
        if isinstance(raw, Mapping):
            return dict(raw)
        if isinstance(raw, str):
            try:
                value = json.loads(raw)
                return dict(value) if isinstance(value, Mapping) else {}
            except Exception:
                return {}
        return dict(row or {})

    @staticmethod
    def _publication_risk(row: Mapping[str, Any]) -> str:
        existing = str(row.get("risk_admission_state") or "").upper()
        if existing:
            return existing
        return "APPROVED_CAPITAL" if str(row.get("publication_authority") or "").upper() == "CAPITAL" else "APPROVED_RESEARCH_ONLY"

    def _project_open(self, source: Mapping[str, Any]) -> Dict[str, Any]:
        row = dict(source or {})
        payload = dict(row)
        mode = require_production_mode(row.get("mode"))
        result = str(row.get("result") or "OPEN").upper()
        status = "OPEN"
        row.update({
            "payload_json": json.dumps(payload, default=str),
            "signal_id": row.get("signal_id") or row.get("decision_id"),
            "signal_status": status,
            "status": "SIGNAL_OPEN",
            "result": result,
            "decision": row.get("decision") or row.get("decision_action"),
            "target_stage": row.get("target_stage") or self._target_stage_label(result, status, bool(row.get("t1_hit")), payload),
            "stage_remarks": row.get("stage_remarks") or self._stage_remarks(bool(row.get("t1_hit")), row.get("sl"), payload),
            "selected_lifecycle": "same_session_only" if mode == "intraday" else "persistent_until_success_fail_exit_or_invalidation",
            "validation_policy": row.get("validation_policy") or "canonical PostgreSQL lifecycle on identity-verified prices",
            "publication_authority": row.get("publication_authority") or "MODEL_PAPER",
            "risk_admission_state": self._publication_risk(row),
        })
        return row

    def save_decision(self, decision: Dict[str, Any]) -> None:
        self.canonical.record(decision)

    def latest_decisions(self, mode: str = "all", limit: int = 50) -> List[Dict[str, Any]]:
        return self.canonical.latest_decisions(mode, limit)

    def selected_signals(self, mode: str = "all", limit: int = 50) -> List[Dict[str, Any]]:
        return [self._project_open(row) for row in self.canonical.active_decisions(mode, limit)]

    def open_signal_rows(self, limit: int = 500) -> List[Dict[str, Any]]:
        return self.selected_signals("all", limit)

    # v103 execution-authority fence -------------------------------------------------
    # The legacy SignalLedgerRepository API remains readable for compatibility,
    # but it is no longer allowed to mutate production execution outcomes.
    # ModelPaperSettlementLineageService is the only writer that may close a
    # canonical decision from actual Model Paper economics.

    def settle_signal_by_id(
        self, signal_id: str, status: str, result: str, exit_price: Any, pnl: Any,
        proof: Optional[Dict[str, Any]] = None,
    ) -> None:
        raise RuntimeError(
            "LEGACY_SIGNAL_SETTLEMENT_DISABLED_USE_MODEL_PAPER_POSITION_LIFECYCLE"
        )

    def refresh_open_signals_from_quotes(self, quotes: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
        return {
            "updated": 0, "closed": 0, "closed_symbols": [], "changes": [],
            "state": "DISABLED",
            "reason": "quote-driven canonical settlement removed in v103; desk Model Paper lifecycle is authoritative",
            "authority": "POSTGRESQL_MODEL_PAPER_POSITIONS",
        }

    def cancel_invalid_carry_shorts(self) -> int:
        # Invalid production modes are rejected by governed migration/admission.
        # A read-model refresh must never mutate retained decisions.
        return 0

    def expire_fast_desk_signals(self, reason: str = "trade_window_expired") -> int:
        # Intraday expiry/mandatory flat is owned by DeskPositionLifecycleAuthority.
        return 0
