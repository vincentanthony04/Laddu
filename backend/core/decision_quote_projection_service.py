"""Isolated quote-to-decision projection workers.

Live quote continuity is latency-critical and must never wait on Model Paper,
counterfactual-learning or evidence side effects.  The primary worker therefore
only consumes quote deltas, refreshes the in-memory quote authority and enqueues
one coalesced side-effect batch.  A separate supervised worker owns all durable
learning/paper side effects.

This separation is intentionally fail-closed for trading authority: a stuck
side-effect worker can delay Model Paper/research evidence, but it cannot stall
Stock Intelligence, Product State or the live quote projection itself.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Dict
from core.persistent_research_history_service import PersistentResearchHistoryService


class DecisionQuoteProjectionService:
    VERSION = "decision-quote-projection-1.3.0-canonical-final-admission"

    def __init__(self, app: Any):
        self.app = app
        self.cursor = 0
        self.last_admission_at = 0.0
        self.last_result: Dict[str, Any] = {"state": "STARTING", "observed": 0}
        self.last_side_effect_result: Dict[str, Any] = {"state": "EXPECTED_IDLE", "observed": 0}
        self.last_research_side_effect_result: Dict[str, Any] = {"state": "EXPECTED_IDLE", "observed": 0}
        self._pending: Dict[str, Dict[str, Any]] = {}
        self._condition = threading.Condition()
        self._research_pending: Dict[str, Dict[str, Any]] = {}
        self._research_lock = threading.RLock()
        self._research_thread: threading.Thread | None = None

    def _enqueue_side_effects(self, accepted: Dict[str, Dict[str, Any]]) -> None:
        if not accepted:
            return
        with self._condition:
            # Coalesce by symbol.  The newest verified quote supersedes an older
            # pending quote; no durable decision/outcome is deleted here.
            self._pending.update({str(key): dict(value) for key, value in accepted.items()})
            self._condition.notify_all()

    def _take_pending(self, timeout: float = 0.5) -> Dict[str, Dict[str, Any]]:
        with self._condition:
            if not self._pending:
                self._condition.wait(timeout=max(0.05, float(timeout)))
            pending = dict(self._pending)
            self._pending.clear()
            return pending

    def run_once(self) -> Dict[str, Any]:
        payload = self.app.live_market.quotes.deltas_since(
            self.cursor, market_open=self.app.market_open(), max_age_sec=8.0, limit=1000
        )
        self.cursor = int(payload.get("cursor") or self.cursor)
        latest: Dict[str, Dict[str, Any]] = {}
        for row in payload.get("deltas") or []:
            if row.get("usable_for_promotion"):
                latest[str(row.get("symbol") or "").upper()] = dict(row)
        accepted = self.app._cache_live_quote_state(latest)
        self._enqueue_side_effects(accepted)
        self.last_result = {
            "state": "READY", "observed": len(accepted), "cursor": self.cursor,
            "side_effect_state": self.last_side_effect_result.get("state"),
            "time": time.time(), "version": self.VERSION,
        }
        return dict(self.last_result)

    def _research_side_effect_worker(self) -> None:
        """Best-effort research/evaluation marks isolated from Model Paper.

        Legacy/local evidence locks may be slow during heavy historical work.
        They are valuable research side effects but are not allowed to hold the
        live Model Paper/quote side-effect authority hostage.
        """
        while True:
            with self._research_lock:
                accepted = dict(self._research_pending)
                self._research_pending.clear()
            if not accepted:
                self.last_research_side_effect_result = {
                    "state": "EXPECTED_IDLE", "observed": 0, "time": time.time(), "version": self.VERSION
                }
                return
            errors = []
            try:
                self.app.counterfactual_learning.mark(accepted)
                self.app.evidence_score_validation.mark_quotes(accepted)
            except Exception as exc:
                errors.append(f"evidence:{type(exc).__name__}:{exc}"[:240])
                self.app.record_error("decision_quote_research_side_effect_evidence", str(exc))
            try:
                quant_paper = getattr(self.app.production_ranker, "quant_paper", None)
                if quant_paper is not None:
                    quant_paper.mark_quotes(accepted)
            except Exception as exc:
                errors.append(f"quant_paper:{type(exc).__name__}:{exc}"[:240])
                self.app.record_error("decision_quote_research_side_effect_quant", str(exc))
            try:
                PersistentResearchHistoryService(self.app.store, self.app.model_portfolio).mark_quotes(accepted)
            except Exception as exc:
                errors.append(f"persistent_research:{type(exc).__name__}:{exc}"[:240])
                self.app.record_error("decision_quote_persistent_research", str(exc))
            self.last_research_side_effect_result = {
                "state": "READY" if not errors else "READY_WITH_RESEARCH_SIDE_EFFECT_ERRORS",
                "observed": len(accepted), "errors": errors, "time": time.time(), "version": self.VERSION,
            }

    def _enqueue_research_side_effects(self, accepted: Dict[str, Dict[str, Any]]) -> None:
        if not accepted:
            return
        with self._research_lock:
            self._research_pending.update({str(key): dict(value) for key, value in accepted.items()})
            alive = bool(self._research_thread and self._research_thread.is_alive())
            if alive:
                return
            self._research_thread = threading.Thread(
                target=self._research_side_effect_worker,
                name="LadduDecisionQuoteResearchSideEffects", daemon=True,
            )
            self._research_thread.start()

    def _apply_side_effects(self, accepted: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
        # Critical lane: only production Model Paper admission is synchronous.
        # PostgreSQL repository calls are bounded by lock/statement timeouts.
        now_tick = time.monotonic()
        admit_final = bool(accepted) and (now_tick - self.last_admission_at >= 2.0)
        sync: Dict[str, Any] = {"ok": True, "observed": 0, "results": []}
        errors = []
        if accepted and admit_final:
            try:
                # R50: Model Paper admission may consume only the canonical
                # PostgreSQL decision authority.  The selected-signal ledger is a
                # projection/history surface and can lag or retain stale rows; it is
                # never an admission source.
                repo = getattr(self.app.store, "production_canonical_decision_repository", None)
                getter = getattr(repo, "active_decisions", None)
                final_rows = list(getter("all", limit=80) or []) if callable(getter) else []
                final_rows = [
                    dict(candidate) for candidate in final_rows
                    if str(candidate.get("publication_authority") or "").upper() in {"CAPITAL", "MODEL_PAPER"}
                    and str(candidate.get("canonical_state") or candidate.get("state") or "").upper()
                        in {"PREPARED", "TRIGGERED", "CONFIRMED", "WEAKENING"}
                    and str(candidate.get("decision_id") or "").strip()
                ]
                for candidate in final_rows:
                    candidate["authority"] = "POSTGRESQL_CANONICAL_DECISION"
                    candidate.setdefault("target", candidate.get("t1"))
                model_quotes = {
                    symbol: {
                        **dict(value),
                        "verified": value.get("identity_verified") is True,
                        "fresh": str(value.get("freshness_state") or "").lower() == "live" and value.get("stale") is not True,
                        "executable": value.get("usable_for_promotion") is True,
                    }
                    for symbol, value in accepted.items()
                }
                sync = self.app.model_portfolio.sync_final_signals(final_rows, model_quotes)
                self.last_admission_at = now_tick
            except Exception as exc:
                sync = {"ok": False, "error": str(exc)[:240], "results": []}
                errors.append(f"model_paper:{type(exc).__name__}:{exc}"[:240])
                self.app.record_error("decision_quote_side_effect_model_paper", str(exc))
        # Research/evaluation marks are deliberately isolated from the critical lane.
        self._enqueue_research_side_effects(accepted)
        result = {
            "state": "READY" if not errors else "READY_WITH_SIDE_EFFECT_ERRORS",
            "observed": len(accepted), "admission": sync, "errors": errors,
            "research_side_effect_state": self.last_research_side_effect_result.get("state"),
            "time": time.time(), "version": self.VERSION,
        }
        self.last_side_effect_result = result
        return dict(result)

    def status(self) -> Dict[str, Any]:
        with self._condition:
            pending = len(self._pending)
        with self._research_lock:
            research_pending = len(self._research_pending)
            research_inflight = bool(self._research_thread and self._research_thread.is_alive())
        return {**dict(self.last_result), "side_effects": dict(self.last_side_effect_result),
                "research_side_effects": dict(self.last_research_side_effect_result),
                "pending_side_effect_quotes": pending, "pending_research_side_effect_quotes": research_pending,
                "research_side_effect_inflight": research_inflight}

    def loop(self, sup=None, *, running_fn):
        """Latency-critical quote projection. Never performs durable side effects."""
        while running_fn() and (sup is None or sup.running):
            if sup:
                sup.beat("decision_quote_projection")
            try:
                result = self.run_once()
                market_open = bool(self.app.market_open())
                if sup:
                    sup.progress(
                        "decision_quote_projection",
                        token=f"{result.get('cursor')}:{result.get('observed')}",
                        stage="quote_cache_projection",
                        completed_units=int(result.get("cursor") or 0), total_units=None,
                        waiting_on=("market closed" if not market_open else None),
                        expected_idle=not market_open,
                    )
                time.sleep(0.20 if market_open else 2.0)
            except Exception as exc:
                self.app.record_error("decision_quote_projection", str(exc))
                time.sleep(1.0)

    def side_effect_loop(self, sup=None, *, running_fn):
        """Durable evidence/Model-Paper side effects, isolated from quote reads."""
        completed = 0
        while running_fn() and (sup is None or sup.running):
            if sup:
                sup.beat("decision_quote_side_effects")
            accepted = self._take_pending(timeout=0.5)
            if not accepted:
                if sup:
                    sup.progress(
                        "decision_quote_side_effects", token=f"idle:{completed}",
                        stage="expected_idle", completed_units=completed, total_units=None,
                        waiting_on="new verified quote batch", expected_idle=True,
                    )
                continue
            try:
                if sup:
                    with sup.heartbeat_guard("decision_quote_side_effects", interval_sec=1.0):
                        result = self._apply_side_effects(accepted)
                else:
                    result = self._apply_side_effects(accepted)
                completed += len(accepted)
                if sup:
                    sup.progress(
                        "decision_quote_side_effects",
                        token=f"{completed}:{result.get('observed')}:{result.get('state')}",
                        stage="evidence_and_model_paper_projection",
                        completed_units=completed, total_units=None,
                        waiting_on=None, expected_idle=False,
                    )
            except Exception as exc:
                self.app.record_error("decision_quote_side_effects", str(exc))
                if sup:
                    sup.progress(
                        "decision_quote_side_effects", token=f"error:{completed}:{time.time()}",
                        stage="side_effect_error", completed_units=completed, total_units=None,
                        waiting_on=str(exc)[:240], expected_idle=False,
                    )
                time.sleep(0.5)
