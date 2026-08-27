from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List

from core.canonical_decision_repository import CanonicalDecisionRepository
from core.decision_write_dedup_filter import DecisionWriteDedupFilter
from core.historical_data_service import HistoricalDataReadinessService
from core.canonical_admission_policy import evaluate_canonical_admission


class StoreDecisionPipelineMixin:
    """Decision-pipeline façade kept out of the legacy Store god-object.

    The host Store supplies ``conn``, ``write_lock``, ``event`` and
    ``_signal_repo``.  Keeping the orchestration here preserves the public Store
    API while enforcing the architecture no-growth budget on storage.py.
    """

    def historical_readiness(self, instrument_key: str, interval: str = "1d", target_years: int = 10) -> Dict[str, Any]:
        return HistoricalDataReadinessService(self.conn).readiness(instrument_key, interval, target_years)

    def _canonical_decision_repo(self) -> CanonicalDecisionRepository:
        production = getattr(self, "production_canonical_decision_repository", None)
        if production is not None:
            return production
        return CanonicalDecisionRepository(self.conn, self.write_lock, self.event, ensure_schema=False)

    def save_decision(self, decision: Dict[str, Any]) -> None:
        working = dict(decision or {})
        working.setdefault(
            "generated_at",
            working.get("decision_generated_at") or working.get("decision_as_of")
            or datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        )
        if DecisionWriteDedupFilter.reject_unsupported_mode(working):
            self.event("WARN", "canonical_decision", "Unsupported production mode suppressed", {
                "symbol": working.get("symbol"), "mode": working.get("mode"),
            })
            return

        from core.production_mode_policy import require_production_mode
        working["mode"] = require_production_mode(working.get("mode"))
        admission = evaluate_canonical_admission(working)
        if admission.normalized_side:
            working["side"] = admission.normalized_side
        if not admission.allowed:
            status = str(working.get("status") or working.get("canonical_state") or "").upper()
            action = str(working.get("decision") or working.get("decision_action") or "").upper()
            hard_rejected = status in {"REJECTED", "BLOCKED", "INVALIDATED"} or action in {"REJECT", "AVOID", "AVOID_LONG"}
            research_like = (not hard_rejected) and (
                status in {"", "WAIT", "WATCH", "WATCHING", "RESEARCH", "PREPARING", "WAITING_FOR_CONFIRMATION", "UNDER_REVIEW"}
                or action in {"", "WAIT", "WATCH", "RESEARCH"}
            )
            if hard_rejected and working.get("symbol"):
                try:
                    retire = getattr(self, "retire_scanner_candidate", None)
                    retired = retire(working.get("symbol"), working.get("mode")) if callable(retire) else {}
                    self.event("INFO", "research_candidate", "Failed scanner candidate moved off active surfaces", {
                        "symbol": working.get("symbol"), "mode": working.get("mode"),
                        "status": status, "decision": action, "retired": retired,
                    })
                except Exception as exc:
                    self.event("WARN", "research_candidate", "Failed scanner candidate retirement deferred", {
                        "symbol": working.get("symbol"), "mode": working.get("mode"), "error": str(exc),
                    })
            if research_like and working.get("symbol"):
                watch = dict(working)
                watch.update({
                    "status": "WATCH",
                    "decision": "WATCH",
                    "canonical_write_suppressed": True,
                    "canonical_suppression_reason": admission.reason,
                    "source": watch.get("source") or "scanner_research",
                })
                try:
                    upsert_watch = getattr(self, "upsert_manual_watch", None)
                    if callable(upsert_watch):
                        upsert_watch(watch, source="scanner_research")
                    upsert_memory = getattr(self, "upsert_opportunity_memory", None)
                    if callable(upsert_memory):
                        upsert_memory(watch, source="scanner_research")
                except Exception as exc:
                    self.event("WARN", "research_candidate", "Research candidate projection failed", {
                        "symbol": working.get("symbol"), "mode": working.get("mode"), "error": str(exc),
                    })
            self.event("INFO", "canonical_decision", "Non-admitted scanner/research row kept outside canonical decisions", {
                "symbol": working.get("symbol"), "mode": working.get("mode"),
                "status": status, "decision": action, "reason": admission.reason,
            })
            return

        repo = self._canonical_decision_repo()
        production_authority = bool(getattr(repo, "production_authority", False))

        try:
            if production_authority:
                # PostgreSQL owns idempotency, active-thesis exclusion and state
                # transitions. Never enter the legacy SQLite writer lock or run
                # a stale SQLite dedup query on the production decision path.
                canonical = repo.record(working)
            else:
                with self.write_lock:
                    if DecisionWriteDedupFilter.suppress_duplicate_stale(self.conn, working):
                        self.event("DEBUG", "canonical_decision", "Duplicate stale decision suppressed before canonical mutation", {
                            "symbol": working.get("symbol"), "mode": working.get("mode"),
                        })
                        return
                    canonical = repo.record(working)
                    self._signal_repo().save_decision({**working, **{
                        "decision_id": canonical.get("decision_id"),
                        "thesis_id": canonical.get("thesis_id"),
                        "signal_id": canonical.get("signal_id") or canonical.get("decision_id"),
                        "canonical_state": canonical.get("state"),
                        "canonical_contract_version": canonical.get("contract_version"),
                        "publication_authority": canonical.get("publication_authority"),
                        "execution_authority": canonical.get("execution_authority"),
                    }})
        except Exception as exc:
            self.event("ERROR", "canonical_decision", "Canonical decision write failed", {
                "symbol": working.get("symbol"), "mode": working.get("mode"), "error": str(exc),
            })
            raise

        working.update({
            "decision_id": canonical.get("decision_id"),
            "thesis_id": canonical.get("thesis_id"),
            "signal_id": canonical.get("signal_id") or canonical.get("decision_id"),
            "canonical_state": canonical.get("state"),
            "canonical_contract_version": canonical.get("contract_version"),
            "publication_authority": canonical.get("publication_authority"),
            "execution_authority": canonical.get("execution_authority"),
        })

        lifecycle_repo = getattr(self, "production_model_portfolio_repository", None)
        append_lifecycle = getattr(lifecycle_repo, "append_signal_lifecycle_event", None)
        if callable(append_lifecycle):
            try:
                append_lifecycle({
                    "signal_id": working.get("signal_id") or working.get("decision_id"),
                    "decision_id": working.get("decision_id"),
                    "position_id": None,
                    "event_type": "GENERATED",
                    "thesis_state": "VALID",
                    "occurred_at": working.get("generated_at"),
                    "mode": working.get("mode"),
                    "payload": {
                        "generated_at": working.get("generated_at"),
                        "symbol": working.get("symbol"),
                        "exchange": working.get("exchange"),
                        "mode": working.get("mode"),
                        "side": working.get("side"),
                        "entry": working.get("entry") or working.get("planned_entry"),
                        "target": working.get("target") or working.get("t1"),
                        "stop": working.get("sl") or working.get("stop"),
                        "score": working.get("rank_score") if working.get("rank_score") is not None else working.get("score"),
                        "model_version": working.get("model_version"),
                        "policy_version": working.get("policy_version"),
                        "pipeline_version": working.get("pipeline_version"),
                        "frozen_evidence_hash": canonical.get("frozen_evidence_hash"),
                    },
                })
            except Exception as exc:
                self.event("WARN", "signal_lifecycle", "Generated lifecycle evidence append failed", {
                    "decision_id": working.get("decision_id"), "error": str(exc),
                })

        from core.model_portfolio_bridge_service import ModelPortfolioBridgeService
        ModelPortfolioBridgeService.observe_store_decision(self, working)

    def canonical_decision(self, decision_id: str) -> Dict[str, Any]:
        return self._canonical_decision_repo().get(decision_id)

    def canonical_decision_events(self, decision_id: str) -> List[Dict[str, Any]]:
        return self._canonical_decision_repo().events(decision_id)

    def canonical_today_entries(self, mode: str = "all", trading_date: str = "", limit: int = 100) -> List[Dict[str, Any]]:
        return self._canonical_decision_repo().today_entries(mode, trading_date or None, limit)
