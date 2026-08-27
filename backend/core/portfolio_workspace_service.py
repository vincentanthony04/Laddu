"""Single cache-safe read model for the two-pane portfolio workstation."""
from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from typing import Any, Dict

from core.intraday_session_policy import IntradaySessionPolicy
from core.model_portfolio_performance_service import ModelPortfolioPerformanceService
from core.model_portfolio_service import ModelPortfolioService
from core.india_time import INDIA_TZ, as_india, trading_date_ist
from core.signal_ledger_continuity_service import SignalLedgerContinuityService
from core.quant_paper_activation_service import QuantPaperActivationService
from core.signal_age_authority import DEFAULT_SIGNAL_AGE_AUTHORITY
from core.today_entries_lifecycle_projection_service import TodayEntriesLifecycleProjectionService
from core.persistent_research_history_service import PersistentResearchHistoryService


def _payload(row: Dict[str, Any]) -> Dict[str, Any]:
    try:
        return json.loads(row.get("payload_json") or "{}")
    except Exception:
        return {}


def _num(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class PortfolioWorkspaceService:
    VERSION = "portfolio-workspace-projection-2.2.0-r49-independent-books"

    def __init__(
        self,
        store: Any,
        *,
        equity: float = 500_000.0,
        intraday_cap: float = 100_000.0,
        portfolio_service: ModelPortfolioService | None = None,
        repository: Any | None = None,
    ):
        self.store = store
        self.equity = float(equity)
        self.intraday_cap = float(intraday_cap)
        # Production must reuse the runtime's already-bound PostgreSQL model
        # portfolio authority. Constructing a fresh service without its
        # repository would silently fall back to SQLite and make a populated
        # production book look empty.
        self.portfolio = portfolio_service or ModelPortfolioService(
            store, equity=self.equity, intraday_cap=self.intraday_cap, repository=repository
        )
        bound_repository = repository or getattr(self.portfolio, "repository", None)
        self.repository = bound_repository
        self.performance = ModelPortfolioPerformanceService(store, repository=bound_repository)
        self.lifecycle_projection = TodayEntriesLifecycleProjectionService(bound_repository)
        self.continuity = SignalLedgerContinuityService(store)
        self.quant = QuantPaperActivationService(store)
        self.session = IntradaySessionPolicy()
        self.persistent_research = PersistentResearchHistoryService(store, self.portfolio)

    @staticmethod
    def _decision_blocker(row: Dict[str, Any]) -> str:
        explicit = row.get("qualification_blocker")
        if explicit:
            return str(explicit)
        blocked = row.get("promotion_blocked_by")
        if isinstance(blocked, dict) and blocked:
            return ", ".join(str(key) for key, value in blocked.items() if value) or "promotion gate"
        if isinstance(blocked, (list, tuple, set)) and blocked:
            return ", ".join(str(value) for value in blocked)
        if blocked:
            return str(blocked)
        risk = str(row.get("risk_admission_state") or "").strip()
        if risk and risk != "APPROVED_CAPITAL":
            return f"capital/risk gate: {risk}"
        invariants = row.get("final_promotion_invariants")
        if isinstance(invariants, dict) and invariants.get("passed") is not True:
            failed = [
                str(key).replace("_", " ")
                for key, value in invariants.items()
                if key != "passed" and value is False
            ]
            if failed:
                return "final gate: " + ", ".join(failed[:3])
        readiness = str(row.get("rank_readiness") or "").strip()
        if readiness and readiness.upper() != "READY":
            return f"rank readiness: {readiness}"
        if not (row.get("trade_map_valid") is True or str(row.get("level_status") or "").lower() == "valid"):
            return "entry/stop/target map not verified"
        status = str(row.get("status") or "UNKNOWN").upper()
        decision = str(row.get("decision") or "UNKNOWN").upper()
        if status not in {"PROMOTED", "SIGNAL_OPEN"} or decision not in {"TRADE", "ACCUMULATE"}:
            return f"decision {decision} / {status}"
        return "Final decision exists but no governed Model Paper admission is persisted"

    def _entry_diagnostics(
        self,
        final: list[Dict[str, Any]],
        research: list[Dict[str, Any]],
    ) -> Dict[str, Any]:
        trading_date = trading_date_ist()
        decisions: list[Dict[str, Any]] = []
        try:
            latest_fn = getattr(self.store, "latest_decisions", None)
            if callable(latest_fn):
                persisted = latest_fn("all", limit=500)
            else:
                # Narrow compatibility path for legacy/test stores that expose
                # only an SQLite connection.  Production Store always provides
                # latest_decisions(), which resolves to canonical PostgreSQL.
                rows = self.store.conn.execute(
                    "SELECT payload_json,created_at,mode FROM decisions "
                    "WHERE LOWER(mode) IN ('intraday','delivery') ORDER BY id DESC LIMIT 500"
                ).fetchall()
                persisted = []
                for row in rows:
                    payload = json.loads(row["payload_json"] or "{}")
                    stamp = str(row["created_at"] or "").strip()
                    payload.setdefault("mode", str(row["mode"] or "").lower())
                    payload.setdefault("created_at", stamp or None)
                    if stamp and not payload.get("trading_date"):
                        parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                        payload["trading_date"] = as_india(parsed).date().isoformat()
                    persisted.append(payload)
            for raw in persisted:
                payload = dict(raw or {})
                if str(payload.get("trading_date") or "")[:10] != trading_date:
                    continue
                payload["_persisted_at"] = payload.get("created_at") or payload.get("updated_at")
                decisions.append(payload)
                if len(decisions) >= 200:
                    break
        except Exception:
            decisions = []

        modes: Dict[str, Any] = {}
        for mode in ("intraday", "delivery"):
            mode_final = [row for row in final if row.get("mode") == mode]
            mode_research = [row for row in research if row.get("mode") == mode]
            mode_decisions = [row for row in decisions if str(row.get("mode") or "").lower() == mode]
            reasons = Counter(
                str(row.get("action") or row.get("continuity_reason") or "").strip()
                for row in mode_research
                if str(row.get("action") or row.get("continuity_reason") or "").strip()
            )
            reasons.update(self._decision_blocker(row) for row in mode_decisions)
            blockers = [
                {"reason": reason, "count": count}
                for reason, count in reasons.most_common(5)
            ]
            legacy_open = sum(row.get("book") == "LEGACY_SIGNAL_LEDGER" for row in mode_research)
            if mode_final:
                state = "FINAL_VISIBLE"
                explanation = f"{len(mode_final)} governed Model Paper row(s) visible."
            elif mode_research:
                state = "RESEARCH_ONLY"
                explanation = blockers[0]["reason"] if blockers else "Observed under Research; no governed admission."
            elif mode_decisions:
                state = "NO_PROMOTION"
                explanation = blockers[0]["reason"] if blockers else "Current decisions did not pass Final admission."
            else:
                state = "NO_CURRENT_DECISIONS"
                explanation = "No decision rows are persisted for the current IST trading date."
            modes[mode] = {
                "state": state,
                "final": len(mode_final),
                "research": len(mode_research),
                "legacy_open_evidence": legacy_open,
                "decisions_today": len(mode_decisions),
                "top_blockers": blockers,
                "explanation": explanation,
                "last_decision_at": mode_decisions[0].get("_persisted_at") if mode_decisions else None,
            }
        def _flag(row: Dict[str, Any], *keys: str) -> bool:
            for key in keys:
                value = row.get(key)
                if value is True:
                    return True
                if isinstance(value, str) and value.strip().upper() in {"TRUE", "YES", "READY", "PASS", "ELIGIBLE", "APPROVED"}:
                    return True
            return False

        def _nested_ok(row: Dict[str, Any], *paths: tuple[str, ...]) -> bool:
            for path in paths:
                value: Any = row
                for part in path:
                    if not isinstance(value, dict):
                        value = None
                        break
                    value = value.get(part)
                if value is True or (isinstance(value, str) and value.strip().upper() in {"TRUE", "YES", "READY", "PASS", "ELIGIBLE", "APPROVED", "FULL"}):
                    return True
            return False

        def _checkpoint(mode: str, lane: str) -> Dict[str, Any]:
            try:
                value = self.store.get_kv(f"scan_checkpoint:{mode}:{lane}", {}) or {}
                return dict(value) if isinstance(value, dict) else {}
            except Exception:
                return {}

        intraday_checkpoint = _checkpoint("intraday", "coverage")
        delivery_checkpoint = _checkpoint("delivery", "analysis")
        attempted = max(
            int(intraday_checkpoint.get("sweep_attempted") or intraday_checkpoint.get("sweep_scanned") or 0),
            int(delivery_checkpoint.get("sweep_scanned") or delivery_checkpoint.get("sweep_attempted") or 0),
            len(decisions),
        )
        universe = max(
            int(intraday_checkpoint.get("universe_size") or 0),
            int(delivery_checkpoint.get("universe_size") or 0),
            attempted,
        )
        identity_from_decisions = sum(
            _flag(row, "identity_verified")
            or _nested_ok(row, ("identity_contract", "ok"), ("instrument_identity", "ok"), ("selected_stock_truth", "identity_verified"))
            for row in decisions
        )
        identity_verified = max(
            int(intraday_checkpoint.get("sweep_verified") or 0),
            identity_from_decisions,
        )
        data_complete = sum(
            _flag(row, "data_complete")
            or str(row.get("rank_readiness") or "").upper() == "READY"
            or _nested_ok(row, ("selected_stock_truth", "data_status"), ("pipeline", "data_complete"))
            for row in decisions
        )
        quant_eligible = sum(
            _flag(row, "quant_eligible", "model_eligible", "production_model_eligible")
            or str(row.get("quant_state") or row.get("model_state") or "").upper()
            in {"ELIGIBLE", "PREDICTION_ACTIVE", "ACTIVE"}
            for row in decisions
        )
        engine_actionable = sum(
            _flag(row, "engine_actionable")
            or str(row.get("decision") or "").upper() in {"TRADE", "ACCUMULATE"}
            for row in decisions
        )
        evidence_ready = sum(
            _flag(row, "evidence_ready")
            or str(row.get("rank_readiness") or row.get("evidence_state") or "").upper() == "READY"
            for row in decisions
        )
        risk_reward_pass = sum(
            _flag(row, "risk_reward_pass", "rr_pass")
            or (
                (row.get("trade_map_valid") is True or str(row.get("level_status") or "").lower() == "valid")
                and str(row.get("structural_target_state") or "").upper() not in {"BLOCKED", "INVALID"}
            )
            for row in decisions
        )
        capital_approved = sum(
            str(row.get("risk_admission_state") or "").upper() == "APPROVED_CAPITAL"
            for row in decisions
        )
        duplicates_suppressed = sum(
            _flag(row, "duplicate_suppressed", "duplicate_blocked")
            or "DUPLICATE" in str(row.get("qualification_blocker") or row.get("promotion_blocked_by") or "").upper()
            for row in decisions
        )
        funnel = {
            "universe_size": universe,
            "universe_scanned": attempted,
            "identity_verified": identity_verified,
            "decisions_observed": len(decisions),
            "data_complete": data_complete,
            "quant_eligible": quant_eligible,
            "engine_actionable": engine_actionable,
            "evidence_ready": evidence_ready,
            "risk_reward_pass": risk_reward_pass,
            "capital_approved": capital_approved,
            "duplicates_suppressed": duplicates_suppressed,
            "final_admitted": len(final),
        }

        summary = " | ".join(
            f"{mode.title()}: {detail['explanation']}"
            for mode, detail in modes.items()
            if detail["state"] != "FINAL_VISIBLE"
        ) or "Governed Final entries are visible."
        return {
            "trading_date": trading_date,
            "modes": modes,
            "summary": summary,
            "funnel": funnel,
            "policy": (
                "Only governed Model Paper positions enter Final. Unlinked persisted "
                "signals remain visible under Research continuity."
            ),
        }

    @staticmethod
    def _position_row(row: Dict[str, Any], *, at: Any = None) -> Dict[str, Any]:
        source = _payload(row)
        ltp = _num(row.get("last_price"))
        prior = _num(source.get("previous_close") or source.get("close"))
        change = round(ltp - prior, 2) if ltp is not None and prior else _num(source.get("rupee_change"))
        change_pct = round(change / prior * 100, 2) if change is not None and prior else _num(source.get("change_pct"))
        age = DEFAULT_SIGNAL_AGE_AUTHORITY.measure(
            generated_at=row.get("generated_at") or source.get("generated_at") or source.get("decision_generated_at") or source.get("created_at"),
            opened_at=row.get("opened_at"),
            at=row.get("closed_at") or at or row.get("updated_at"),
            mode=row.get("mode"),
            approved_policy=(source.get("approved_age_risk_policy") if isinstance(source.get("approved_age_risk_policy"), dict) else None),
        )
        return {
            "row_id": row.get("position_id"),
            "rank": source.get("rank") or source.get("universe_rank"),
            "final_confidence": _num(source.get("final_confidence") or source.get("confidence") or source.get("score")),
            "book": "MODEL_PAPER",
            "segment": "final",
            "symbol": row.get("symbol"),
            "exchange": row.get("exchange"),
            "mode": row.get("mode"),
            "side": row.get("side"),
            "status": row.get("status"),
            "ltp": ltp,
            "rupee_change": change,
            "change_pct": change_pct,
            "entry": _num(row.get("original_entry")),
            "target": _num(row.get("original_target")),
            "original_stop": _num(row.get("original_stop")),
            "active_stop": _num(row.get("managed_stop")),
            "hit_status": row.get("hit_status"),
            "quantity": int(row.get("quantity") or 0),
            "capital": round(float(row.get("notional") or 0) + float(row.get("reserved_cost") or 0), 2),
            "gross_pnl": _num(row.get("gross_pnl")),
            "costs": _num(row.get("total_cost")),
            "net_pnl": _num(row.get("net_pnl")),
            "action": row.get("action"),
            "exit_reason": row.get("exit_reason"),
            "economic_outcome": row.get("economic_outcome"),
            "signal_outcome": row.get("signal_outcome"),
            "outcome": row.get("signal_outcome") or "OPEN",
            "generated_at": age.get("generated_at"),
            "opened_at": row.get("opened_at"),
            "updated_at": row.get("updated_at"),
            "closed_at": row.get("closed_at"),
            "signal_age": age,
            "generation_age_seconds": age.get("generation_age_seconds"),
            "open_age_seconds": age.get("open_age_seconds"),
            "decision_delay_seconds": age.get("decision_delay_seconds"),
            "generation_age_bucket": age.get("generation_age_bucket"),
            "open_age_bucket": age.get("open_age_bucket"),
            "decision_delay_bucket": age.get("decision_delay_bucket"),
            "age_attribution_state": age.get("age_attribution_state"),
            "age_bucket_policy_version": age.get("age_bucket_policy_version"),
            "current_thesis_state": row.get("current_thesis_state") or "NOT_REASSESSED",
            "latest_reassessment_at": row.get("latest_reassessment_at"),
            "latest_reassessment_reason": row.get("latest_reassessment_reason"),
            "reassessment_validation_scope": row.get("reassessment_validation_scope"),
            "latest_management_action": row.get("latest_management_action") or row.get("action"),
            "latest_management_reason": row.get("latest_management_reason"),
            "latest_management_at": row.get("latest_management_at"),
            "latest_management_hit_status": row.get("latest_management_hit_status"),
            "lifecycle_attribution_state": row.get("lifecycle_attribution_state") or "UNAVAILABLE",
            "lifecycle_attribution_authority": row.get("lifecycle_attribution_authority"),
            "lifecycle_projection_authority_version": row.get("lifecycle_projection_authority_version"),
            "chart_binding": {"symbol": row.get("symbol"), "mode": row.get("mode")},
        }

    @staticmethod
    def _research_row(row: Dict[str, Any]) -> Dict[str, Any]:
        source = _payload(row)
        side = str(source.get("side") or row.get("side") or "").upper().strip()
        entry = _num(source.get("entry") or source.get("planned_entry"))
        target = _num(
            source.get("target") or source.get("t1") or source.get("planned_t1")
        )
        stop = _num(
            source.get("sl") or source.get("stop") or source.get("planned_sl")
        )
        geometry_valid = bool(
            side == "LONG" and None not in (stop, entry, target) and stop < entry < target
            or side == "SHORT" and None not in (target, entry, stop) and target < entry < stop
        )
        missing = [
            label for label, value in (("entry", entry), ("target", target), ("stop", stop))
            if value is None
        ]
        disposition = str(row.get("disposition") or "").strip()
        return {
            "row_id": row.get("research_id"),
            "rank": source.get("rank") or source.get("universe_rank"),
            "final_confidence": _num(source.get("final_confidence") or source.get("confidence") or source.get("score")),
            "source_signal_id": row.get("source_signal_id"),
            "book": "RESEARCH_COUNTERFACTUAL",
            "segment": "research",
            "symbol": row.get("symbol"),
            "exchange": source.get("exchange") or "NSE",
            "mode": row.get("mode"),
            "side": side or None,
            "status": "RESEARCH" if geometry_valid else "NON_ACTIONABLE",
            "ltp": _num(row.get("observed_price") or source.get("ltp")),
            "rupee_change": _num(source.get("rupee_change")),
            "change_pct": _num(source.get("change_pct")),
            "entry": entry,
            "target": target,
            "original_stop": stop,
            "active_stop": stop,
            "trade_map_valid": geometry_valid,
            "trade_map_state": "RESEARCH_MAP_READY" if geometry_valid else "NON_ACTIONABLE_MAP_INCOMPLETE",
            "missing_trade_map_fields": missing,
            "hit_status": "NOT_ADMITTED",
            "quantity": 0,
            "capital": 0.0,
            "net_pnl": None,
            "action": disposition or (
                "RESEARCH MAP READY - AWAITING GOVERNED MODEL PAPER ADMISSION"
                if geometry_valid else
                "NON-ACTIONABLE - MISSING " + ", ".join(missing or ["valid LONG/SHORT geometry"])
            ),
            "outcome": disposition or "NOT_ADMITTED",
            "accuracy_state": "EXCLUDED_UNTIL_GOVERNED_OPEN_AND_SETTLED",
            "performance_state": "EXCLUDED_FROM_MODEL_PAPER_PERFORMANCE",
            "model_id": source.get("model_id"),
            "horizon": source.get("horizon"),
            "occurred_at": row.get("occurred_at"),
            "chart_binding": {"symbol": row.get("symbol"), "mode": row.get("mode")},
        }

    @staticmethod
    def _quant_position_row(row: Dict[str, Any]) -> Dict[str, Any]:
        source = row.get("payload") if isinstance(row.get("payload"), dict) else _payload(row)
        ltp = _num(row.get("last_price"))
        return {
            "row_id": f"quant:{row.get('position_id')}",
            "rank": source.get("population_rank") or source.get("rank"),
            "final_confidence": _num(source.get("model_score")),
            "book": "RESEARCH_PREDICTION_PAPER",
            "segment": "research",
            "included_in_final_economics": False,
            "economic_lane": "COUNTERFACTUAL_RESEARCH",
            "symbol": row.get("symbol"),
            "exchange": "NSE",
            "mode": row.get("mode"),
            "side": row.get("side"),
            "status": row.get("status"),
            "prediction_state": row.get("prediction_state"),
            "model_id": row.get("model_id"),
            "horizon": row.get("horizon"),
            "evaluation_objective": row.get("evaluation_objective"),
            "decision_weight": row.get("decision_weight"),
            "broker_execution_weight": 0.0,
            "ltp": ltp,
            "rupee_change": _num(source.get("rupee_change")),
            "change_pct": _num(source.get("change_pct")),
            "entry": _num(row.get("entry_price")),
            "target": _num(row.get("target_price")),
            "original_stop": _num(row.get("stop_price")),
            "active_stop": _num(row.get("managed_stop") or row.get("stop_price")),
            "trailing_state": row.get("trailing_state") or "ORIGINAL_STOP",
            "secured_profit": bool(row.get("secured_profit")),
            "hit_status": row.get("exit_reason") or (row.get("trailing_state") or "MONITORING" if row.get("status") == "OPEN" else "CLOSED"),
            "quantity": int(row.get("quantity") or 0),
            "capital": round(float(row.get("notional") or 0) + float(row.get("reserved_cost") or 0), 2),
            "gross_pnl": _num(row.get("gross_pnl")),
            "costs": _num(row.get("total_cost")),
            "net_pnl": _num(row.get("net_pnl")),
            "mfe_bps": _num(row.get("mfe_bps")),
            "mae_bps": _num(row.get("mae_bps")),
            "holding_seconds": int(row.get("holding_seconds") or 0),
            "trade_map_valid": all(
                _num(row.get(field)) is not None
                for field in ("entry_price", "target_price", "stop_price")
            ),
            "trade_map_state": "GOVERNED_OPEN_MAP" if row.get("status") == "OPEN" else "SETTLED_MAP",
            "accuracy_state": (
                "PENDING_SETTLEMENT_EXCLUDED"
                if row.get("status") == "OPEN"
                else "ELIGIBLE" if row.get("outcome") in {"WIN", "LOSS", "BREAKEVEN"}
                else "UNSCORABLE"
            ),
            "performance_state": (
                "OPEN_MTM_EXCLUDED"
                if row.get("status") == "OPEN"
                else "SETTLED_NET_OF_COSTS" if not row.get("unscorable")
                else "UNSCORABLE_EXCLUDED"
            ),
            "action": (
                "HOLD / MONITOR"
                if row.get("status") == "OPEN"
                else str(row.get("exit_reason") or "CLOSED").replace("_", " ")
            ),
            "exit_reason": row.get("exit_reason"),
            "economic_outcome": row.get("outcome"),
            "signal_outcome": (
                "SUCCESS" if row.get("outcome") == "WIN"
                else "FAILURE" if row.get("outcome") == "LOSS"
                else "UNSCORABLE" if row.get("unscorable") else None
            ),
            "outcome": row.get("outcome") or "OPEN",
            "opened_at": row.get("opened_at"),
            "updated_at": row.get("updated_at"),
            "closed_at": row.get("closed_at"),
            "chart_binding": {"symbol": row.get("symbol"), "mode": row.get("mode")},
        }

    def _combined_capital(self, quant_rows: list[Dict[str, Any]]) -> Dict[str, Any]:
        # AC-044/AC-072: Final capital/economics are owned only by canonical
        # PostgreSQL Model Paper.  Prediction/research paper rows may remain
        # visible for evaluation but can never consume Final capital or P&L.
        canonical = dict(self.portfolio.capital_summary() or {})
        canonical["authority"] = "POSTGRESQL_MODEL_PAPER_POSITIONS" if self.repository is not None else "MODEL_PAPER_COMPATIBILITY"
        canonical["research_prediction_paper_included"] = False
        canonical["research_prediction_paper_rows"] = len(quant_rows or [])
        return canonical

    def build(self, *, include_aux: bool = False, research_limit: int = 500) -> Dict[str, Any]:
        """Build the Model Paper workstation without allowing auxiliary evidence to mask the book.

        Canonical Final positions and persisted Research publications are independent
        read authorities.  Performance, automatic prediction paper, continuity and
        current-decision diagnostics are auxiliary evidence.  A failure in one of
        those auxiliary paths must never make valid persisted Model Paper rows appear
        to have disappeared.
        """
        sections: Dict[str, Dict[str, Any]] = {}

        def safe(name: str, fn, default):
            try:
                value = fn()
                sections[name] = {"state": "READY", "error": None}
                return value
            except Exception as exc:
                sections[name] = {"state": "UNAVAILABLE", "error": str(exc)[:300]}
                return default

        now_local = self.session.local()
        positions = safe("final_positions", lambda: self.portfolio.positions(), [])
        projected_positions = safe(
            "final_lifecycle_projection",
            lambda: self.lifecycle_projection.project(positions),
            positions,
        )
        final = []
        for row in projected_positions:
            try:
                final.append(self._position_row(row, at=(row.get("closed_at") or now_local)))
            except Exception as exc:
                sections.setdefault("final_row_projection", {"state": "PARTIAL", "errors": []})
                sections["final_row_projection"].setdefault("errors", []).append(str(exc)[:180])
        if "final_row_projection" not in sections:
            sections["final_row_projection"] = {"state": "READY", "error": None}

        research = safe(
            "research_publications",
            lambda: self.persistent_research.history(limit=max(1, min(int(research_limit), 1000))),
            [],
        )
        sections["research_row_projection"] = {"state": "READY", "error": None}
        research_performance = safe(
            "research_performance",
            lambda: self.persistent_research.performance(research),
            {"authority":"PERSISTENT_RESEARCH_COUNTERFACTUAL_ONLY","published":len(research),"active":len(research),"settled":0,"success":0,"failure":0,"accuracy_pct":None,"included_in_final_performance":False},
        )

        promoted_ids = {str(row.get("source_signal_id") or "") for row in positions}
        for row in research:
            if row.get("source_signal_id") and str(row["source_signal_id"]) in promoted_ids:
                row["action"] = "PROMOTED TO FINAL"
                row["outcome"] = "PROMOTED TO FINAL"

        # Auxiliary research/evaluation evidence is intentionally excluded from the
        # default browser request.  It can be requested explicitly for diagnostics,
        # but can never control visibility of persisted Final/Research rows.
        quant_positions: list[Dict[str, Any]] = []
        legacy_open: list[Dict[str, Any]] = []
        if include_aux:
            quant_positions = safe("research_prediction_paper", lambda: self.quant.positions(limit=500), [])
            for row in quant_positions:
                try:
                    research.append(self._quant_position_row(row))
                except Exception as exc:
                    sections.setdefault("research_prediction_row_projection", {"state": "PARTIAL", "errors": []})
                    sections["research_prediction_row_projection"].setdefault("errors", []).append(str(exc)[:180])
            if "research_prediction_row_projection" not in sections:
                sections["research_prediction_row_projection"] = {"state": "READY", "error": None}
            known_research_ids = {
                str(row.get("source_signal_id") or "")
                for row in research
                if row.get("source_signal_id")
            }
            legacy_open = safe(
                "legacy_signal_continuity",
                lambda: [
                    row for row in self.continuity.open_research_rows()
                    if str(row.get("source_signal_id") or "") not in promoted_ids
                    and str(row.get("source_signal_id") or "") not in known_research_ids
                ],
                [],
            )
            research.extend(legacy_open)
        else:
            sections["research_prediction_paper"] = {"state": "DEFERRED", "error": None}
            sections["legacy_signal_continuity"] = {"state": "DEFERRED", "error": None}

        configured_capital = {
            "initial_equity": self.equity,
            "equity": self.equity,
            "model_wallet": self.equity,
            "intraday_cap": self.intraday_cap,
            "authority": "OPERATOR_SETTINGS_FALLBACK",
            "state": "CONFIGURED_ONLY",
            "research_prediction_paper_included": False,
            "research_prediction_paper_rows": len(quant_positions),
        }
        capital = safe("capital", lambda: self._combined_capital(quant_positions), configured_capital)

        # Lightweight diagnostics are derived only from already-read persisted rows.
        # They therefore remain available even when current-decision/governance
        # diagnostics are busy or deferred.
        modes: Dict[str, Any] = {}
        for desk in ("intraday", "delivery"):
            desk_final = [row for row in final if str(row.get("mode") or "").lower() == desk]
            desk_research = [row for row in research if str(row.get("mode") or "").lower() == desk]
            open_final = [row for row in desk_final if str(row.get("status") or "").upper() == "OPEN"]
            if open_final:
                state = "FINAL_OPEN"
                explanation = f"{len(open_final)} open governed Model Paper row(s)."
            elif desk_final:
                state = "FINAL_HISTORY"
                explanation = f"{len(desk_final)} persisted governed Model Paper row(s), none currently open."
            elif desk_research:
                state = "RESEARCH_HISTORY"
                explanation = f"{len(desk_research)} persisted Research publication(s); no governed Final position."
            else:
                state = "NO_PERSISTED_ROWS"
                explanation = "No persisted Model Paper or Research row is currently visible for this desk."
            modes[desk] = {
                "state": state,
                "final": len(desk_final),
                "final_open": len(open_final),
                "research": len(desk_research),
                "top_blockers": [],
                "explanation": explanation,
            }
        entry_diagnostics: Dict[str, Any] = {
            "state": "PERSISTED_BOOK_ONLY",
            "modes": modes,
            "policy": "Current-decision and automatic-paper diagnostics are auxiliary; they cannot hide persisted Final/Research rows.",
        }

        performance: Dict[str, Any] = {
            "state": "DEFERRED",
            "policy": "Performance is served by its dedicated materialized endpoint and is not required to render Model Paper rows.",
        }
        if include_aux:
            detailed = safe("entry_diagnostics", lambda: self._entry_diagnostics(final, research), None)
            if isinstance(detailed, dict):
                entry_diagnostics = detailed
            quant_admission = safe("automatic_paper_admission", lambda: self.quant.admission_diagnostics(limit=30), [])
            entry_diagnostics["automatic_paper"] = {"attempts": len(quant_admission), "recent": quant_admission}
            performance = safe("performance", lambda: self.performance.report(), performance)
        else:
            sections["entry_diagnostics"] = {"state": "DEFERRED", "error": None}
            sections["automatic_paper_admission"] = {"state": "DEFERRED", "error": None}
            sections["performance"] = {"state": "DEFERRED", "error": None}

        core_states = [
            sections.get("final_positions", {}).get("state"),
            sections.get("research_publications", {}).get("state"),
        ]
        if all(value == "READY" for value in core_states):
            overall_state = "READY"
            ok = True
        elif any(value == "READY" for value in core_states):
            overall_state = "PARTIAL"
            ok = True
        else:
            overall_state = "UNAVAILABLE"
            ok = False

        final.sort(key=lambda row: str(row.get("opened_at") or row.get("closed_at") or row.get("updated_at") or ""), reverse=True)
        research.sort(key=lambda row: str(row.get("occurred_at") or row.get("opened_at") or row.get("updated_at") or ""), reverse=True)

        return {
            "ok": ok,
            "state": overall_state,
            "contract_version": self.VERSION,
            "read_contract": "INDEPENDENT_CANONICAL_BOOKS_AUXILIARY_FAIL_ISOLATED",
            "history_scope": "ALL_PERSISTED_ROWS",
            "model_paper_authority": "POSTGRESQL_MODEL_PAPER_POSITIONS" if getattr(self.portfolio, "repository", None) is not None else "SQLITE_COMPAT_MODEL_PAPER_POSITIONS",
            "research_publication_authority": "POSTGRESQL_RESEARCH_MODEL_PAPER_OBSERVATIONS" if getattr(self.portfolio, "repository", None) is not None else "SQLITE_COMPAT_MODEL_PAPER_RESEARCH",
            "execution_boundary": "AUTOMATIC_PAPER_SIMULATION_NO_BROKER_ORDERS",
            "sections": sections,
            "capital": capital,
            "session": self.session.at(),
            "final": final,
            "research": research,
            "counts": {
                "final": len(final),
                "final_open": sum(str(row.get("status") or "").upper() == "OPEN" for row in final),
                "final_closed": sum(str(row.get("status") or "").upper() == "CLOSED" for row in final),
                "research": len(research),
                "research_active": int(research_performance.get("active") or 0),
                "research_success": int(research_performance.get("success") or 0),
                "research_failure": int(research_performance.get("failure") or 0),
                "legacy_open_evidence": len(legacy_open),
            },
            "research_performance": research_performance,
            "performance": performance,
            "entry_diagnostics": entry_diagnostics,
            "books": {
                "model_paper": "all persisted governed positions; open and settled history remain visible",
                "research_counterfactual": "persistent Research publication history with separate counterfactual performance; never deleted by scanner rerank and excluded from Final/Model Paper performance",
                "research_prediction_paper": "auxiliary evaluation evidence only; excluded from Final capital, risk, Signal Accuracy and realized P&L",
                "legacy_signal_continuity": "auxiliary continuity evidence only; excluded from governed capital and rupee P&L",
                "manual_holdings": "separate existing position book; excluded from model performance",
            },
        }

