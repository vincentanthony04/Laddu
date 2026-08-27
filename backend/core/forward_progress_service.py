"""Read-only forward evidence stage accounting.

Counts come from persisted point-in-time population, feature, prediction, paper
and settlement ledgers.  Infrastructure readiness is never substituted for
model evidence.
"""
from __future__ import annotations

from typing import Any, Dict

from core.selection_platform_service import SelectionPlatformService
from core.candidate_population_service import CandidatePopulationService

FORWARD_PROGRESS_VERSION = "forward-progress-1.4.0-governance-authority-aware"


class ForwardProgressService:
    def __init__(self, store: Any):
        self.store = store
        self.platform = SelectionPlatformService(store)
        self.populations = CandidatePopulationService(store)

    def _count(self, sql: str, params=()) -> int:
        try:
            row = self.store.conn.execute(sql, tuple(params)).fetchone()
            return int((row[0] if row else 0) or 0)
        except Exception:
            return 0


    def _current_paper_counts(self, candidate_ids: set[str], desk: str) -> tuple[int, int]:
        """Count local Model-Paper rows joined by canonical candidate identity.

        The population itself may live in Governance PostgreSQL; therefore this
        method must never join through the retired local population tables.
        """
        if not candidate_ids:
            return 0, 0
        try:
            rows = self.store.conn.execute(
                """SELECT DISTINCT q.candidate_id,e.status
                     FROM quant_evaluation_positions e
                     JOIN quant_paper_predictions q ON q.prediction_id=e.prediction_id
                    WHERE e.mode=?""",
                (desk,),
            ).fetchall()
        except Exception:
            return 0, 0
        candidates: set[str] = set()
        settled: set[str] = set()
        for row in rows:
            candidate = str(row[0] or "").strip()
            if not candidate or candidate not in candidate_ids:
                continue
            candidates.add(candidate)
            if str(row[1] or "").upper() == "CLOSED":
                settled.add(candidate)
        return len(candidates), len(settled)

    def _population_ids(self, fingerprint: str, desk: str) -> set[str]:
        if not fingerprint:
            return set()
        try:
            return {
                str(row.get("candidate_id") or "")
                for row in self.populations.rows(fingerprint)
                if str(row.get("mode") or "").lower() == desk and row.get("candidate_id")
            }
        except Exception:
            return set()

    def _paper_admission_status(self, candidate_ids: set[str], desk: str) -> tuple[str, str, Dict[str, Any]]:
        """Return the governed no-open reason for the current population.

        A zero paper count is not one state. It can be a legitimate market/entry
        wait, a terminal no-selection result, an actionable data/risk blocker or
        a missing admission attempt. Reuse the research reconciliation authority
        so Forward Progress, OCC and Research cannot disagree about that edge.
        """
        from core.research_lifecycle_reconciliation_service import (
            ResearchLifecycleReconciliationService,
        )

        reconciliation = ResearchLifecycleReconciliationService(self.store)
        diagnostics = reconciliation.paper_admission_diagnostics(desk, candidate_ids)
        paper_model = reconciliation.paper_model_status(desk)
        diagnostics["paper_model"] = paper_model
        actionable = dict(diagnostics.get("actionable_blocker_counts") or {})
        expected = dict(diagnostics.get("expected_wait_blocker_counts") or {})
        terminal = dict(diagnostics.get("terminal_non_admission_counts") or {})

        def top_reason(values: Dict[str, int]) -> str:
            if not values:
                return ""
            reason, count = sorted(values.items(), key=lambda item: (-item[1], item[0]))[0]
            return f"{reason} ({count})"

        if actionable:
            return (
                "PAPER_ADMISSION_BLOCKED",
                "Model Paper admission blocked: " + top_reason(actionable),
                diagnostics,
            )
        if expected:
            return (
                "PAPER_ADMISSION_WAITING",
                "Model Paper admission waiting on governed market condition: "
                + top_reason(expected),
                diagnostics,
            )
        if terminal:
            return (
                "PAPER_NOT_SELECTED",
                "No Model Paper position is expected from this population: "
                + top_reason(terminal),
                diagnostics,
            )
        if int(diagnostics.get("evaluated_candidates") or 0) > 0:
            return (
                "PAPER_ADMISSION_PENDING",
                "Model Paper admission was evaluated but no open position or classified blocker was persisted.",
                diagnostics,
            )
        if not paper_model.get("available"):
            obs = int(paper_model.get("observations") or 0)
            days = int(paper_model.get("trading_days") or 0)
            if paper_model.get("shadow_evidence_ready"):
                return (
                    "PAPER_MODEL_TRAINING_BLOCKED",
                    f"Shadow paper-model evidence is sufficient ({obs} observations · {days} trading days) but no governed artifact is available.",
                    diagnostics,
                )
            return (
                "PAPER_MODEL_EVIDENCE_WAITING",
                f"Shadow paper model needs {obs}/{paper_model.get('shadow_min_observations', 100)} settled observations and {days}/{paper_model.get('shadow_min_trading_days', 20)} trading days.",
                diagnostics,
            )
        return (
            "PAPER_ADMISSION_PENDING",
            "Three-arm predictions exist; Model Paper admission has not yet been evaluated/persisted.",
            diagnostics,
        )

    def _desk(self, desk: str) -> Dict[str, Any]:
        summary = self.platform.latest_summary(desk)
        fingerprint = str(summary.get("population_fingerprint") or "").strip()
        population_rows = self.populations.rows(fingerprint) if fingerprint else []
        population_ids = {
            str(row.get("candidate_id") or "") for row in population_rows
            if row.get("candidate_id") and str(row.get("mode") or "").lower() == desk
        }
        population = len(population_ids)

        repo = getattr(self.store, "production_model_governance_read_repository", None) or getattr(self.store, "production_model_governance_repository", None)
        if repo is not None:
            feature_ids = {
                str(row.get("candidate_id") or "") for row in population_rows
                if row.get("candidate_id") and (
                    str(row.get("feature_snapshot_state") or "").upper() == "COMPLETE"
                    and str(row.get("feature_lineage_state") or "").upper() == "VERIFIED"
                )
            }
        else:
            feature_ids = self._population_feature_ids_local(fingerprint, desk) if fingerprint else set()
        features = len(population_ids & feature_ids)

        predictions = self.platform.predictions(fingerprint) if fingerprint else []
        arm_ids: Dict[str, set[str]] = {arm: set() for arm in ("heuristic", "quant", "hybrid")}
        for row in predictions:
            arm = str(row.get("arm") or "").lower()
            candidate = str(row.get("candidate_id") or "")
            if arm in arm_ids and candidate in population_ids:
                arm_ids[arm].add(candidate)
        heuristic, quant, hybrid = (len(arm_ids[arm]) for arm in ("heuristic", "quant", "hybrid"))

        paper, settled = self._current_paper_counts(population_ids, desk)
        admission: Dict[str, Any] = {
            "evaluated_candidates": 0, "states": {}, "blocker_counts": {},
            "actionable_blocker_counts": {}, "expected_wait_blocker_counts": {},
            "terminal_non_admission_counts": {}, "samples": [],
        }
        features_complete = bool(population and features == population)
        complete_three_arm = bool(
            features_complete and heuristic == quant == hybrid == population
        )
        if not population:
            state = "NOT_STARTED"
            blocker = "No analysed candidate population has reached immutable capture."
        elif not features_complete:
            state = "FEATURE_CAPTURE_INCOMPLETE"
            blocker = f"Persisted point-in-time features cover {features}/{population} candidates."
        elif not complete_three_arm:
            state = "INCOMPLETE_THREE_ARM_CAPTURE"
            blocker = (
                "Baseline, ML Challenger and Hybrid do not reconcile to the same "
                f"feature-complete population ({heuristic}/{quant}/{hybrid} of {population})."
            )
        elif paper == 0:
            state, blocker, admission = self._paper_admission_status(population_ids, desk)
        elif settled == 0:
            state = "MODEL_PAPER_ACTIVE"
            blocker = "Model Paper observations are open and awaiting future settlement."
        else:
            state = "SETTLEMENT_ACTIVE"
            blocker = "Forward outcomes are accumulating; maturity remains governed by the full horizon policy."
        return {
            "desk": desk, "state": state,
            "population_fingerprint": fingerprint or None,
            "population_candidates": population, "population_count": population,
            "feature_rows": features, "feature_capture_complete": features_complete,
            "heuristic_predictions": heuristic, "quant_predictions": quant,
            "hybrid_predictions": hybrid, "paper_predictions": paper,
            "settled_outcomes": settled, "same_population_three_arm": complete_three_arm,
            "paper_admission": admission, "blocker": blocker,
            "prediction_state": summary.get("prediction_state") or "MODEL_UNAVAILABLE",
            "decision_weight": summary.get("decision_weight") or 0.0,
            "evidence_authority": summary.get("authority") or ("GOVERNANCE_POSTGRESQL" if repo is not None else "LEGACY_SQLITE_READ_PROJECTION"),
        }

    def _population_feature_ids_local(self, fingerprint: str, desk: str) -> set[str]:
        try:
            return {
                str(row[0]) for row in self.store.conn.execute(
                    "SELECT DISTINCT candidate_id FROM quant_feature_snapshots WHERE population_fingerprint=? AND mode=?",
                    (fingerprint, desk),
                ).fetchall() if row and row[0]
            }
        except Exception:
            return set()

    def status(self) -> Dict[str, Any]:
        by_desk = {desk: self._desk(desk) for desk in ("delivery", "intraday")}
        return {
            "ok": True,
            "version": FORWARD_PROGRESS_VERSION,
            "by_desk": by_desk,
            "clock_start_eligible": all(row["same_population_three_arm"] for row in by_desk.values()),
            "production_change_allowed": False,
            "policy": "Counts are observational. Level 5 requires future settlement, purged walk-forward and governed champion lineage.",
        }
