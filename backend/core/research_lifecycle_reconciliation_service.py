"""Exact reconciliation for the Research / Model Paper lifecycle.

This is the read authority for the active shadow pipeline. It proves each
candidate's immutable population, point-in-time features, three prediction
arms, Model Paper monitoring, settlement, Research ledger attribution and
performance attribution. It never promotes a model or contaminates the
production Signal Ledger.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
from typing import Any, Dict, Iterable

from core.selection_platform_service import SelectionPlatformService
from core.candidate_population_service import CandidatePopulationService
from core.runtime_primitives import is_india_market_open

SERVICE_VERSION = "research-lifecycle-reconciliation-2.4.0-learned-model-truth"


class ResearchLifecycleReconciliationService:
    def __init__(self, store: Any):
        self.store = store

    def _exists(self, table: str) -> bool:
        try:
            return bool(self.store.conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone())
        except Exception:
            return False

    def _ids(self, sql: str, params: Iterable[Any] = ()) -> set[str]:
        try:
            return {str(row[0]) for row in self.store.conn.execute(sql, tuple(params)).fetchall() if row and row[0]}
        except Exception:
            return set()

    @staticmethod
    def _payload_candidate(value: Any) -> str:
        try:
            row = json.loads(value or "{}") if not isinstance(value, dict) else value
            return str(row.get("candidate_id") or "").strip()
        except Exception:
            return ""

    def _paper_ids(self, desk: str, *, closed: bool | None = None) -> set[str]:
        if not self._exists("quant_evaluation_positions"):
            return set()
        where = "e.mode=?"
        params: list[Any] = [desk]
        if closed is True:
            where += " AND e.status='CLOSED'"
        elif closed is False:
            where += " AND e.status='OPEN'"
        # Prefer the immutable candidate_id on the prediction ledger. Legacy
        # positions that predate that join still fall back to payload lineage.
        try:
            if self._exists("quant_paper_predictions"):
                rows = self.store.conn.execute(
                    f"""SELECT e.prediction_id,e.payload_json,q.candidate_id
                          FROM quant_evaluation_positions e
                          LEFT JOIN quant_paper_predictions q
                            ON q.prediction_id=e.prediction_id
                         WHERE {where}""",
                    tuple(params),
                ).fetchall()
            else:
                rows = self.store.conn.execute(
                    f"SELECT prediction_id,payload_json,NULL FROM quant_evaluation_positions e WHERE {where}",
                    tuple(params),
                ).fetchall()
        except Exception:
            return set()
        out = set()
        for row in rows:
            joined = str(row[2] or "").strip() if len(row) > 2 else ""
            candidate = joined or self._payload_candidate(row[1])
            out.add(candidate or str(row[0] or ""))
        return {value for value in out if value}




    def _paper_model_status(self, desk: str) -> Dict[str, Any]:
        """Return the governed paper-ranker training/availability state."""
        shadow_min_observations = 100
        shadow_min_days = 20
        if self._exists("shadow_lightgbm_models"):
            try:
                raw = self.store.conn.execute(
                    """SELECT model_id,horizon,state,observations,trading_days,regimes,created_at
                         FROM shadow_lightgbm_models
                        WHERE mode=? ORDER BY created_at DESC,model_id DESC LIMIT 1""",
                    (desk,),
                ).fetchone()
            except Exception:
                raw = None
            shadow_row: Dict[str, Any] = {}
            if raw:
                row = dict(raw) if hasattr(raw, "keys") else {
                    "model_id": raw[0], "horizon": raw[1], "state": raw[2],
                    "observations": raw[3], "trading_days": raw[4], "regimes": raw[5],
                    "created_at": raw[6],
                }
                shadow_row = row
                model_state = str(row.get("state") or "").upper()
                usable_shadow_states = {"SHADOW_MODEL_ELIGIBLE", "ACTIVE_VALIDATION", "ACTIVE_PRODUCTION"}
                if str(row.get("model_id") or "").strip() and model_state in usable_shadow_states:
                    return {
                        "state": "AVAILABLE", "available": True,
                        "admission_authority": "LEARNED_SHADOW_RANKER",
                        "bootstrap_available": True, "learned_model_available": True,
                        "model_id": row.get("model_id"), "horizon": row.get("horizon"),
                        "model_state": row.get("state"),
                        "observations": int(row.get("observations") or 0),
                        "trading_days": int(row.get("trading_days") or 0),
                        "regimes": int(row.get("regimes") or 0),
                        "created_at": row.get("created_at"),
                        "shadow_min_observations": shadow_min_observations,
                        "shadow_min_trading_days": shadow_min_days,
                        "production_min_trading_days": 126,
                        "production_influence": 0.0,
                    }
        else:
            shadow_row = {}
        latest_training: Dict[str, Any] = {}
        if self._exists("quant_research_cycles"):
            try:
                cycle = self.store.conn.execute(
                    """SELECT result_json,completed_at FROM quant_research_cycles
                        WHERE mode=? ORDER BY completed_at DESC LIMIT 1""",
                    (desk,),
                ).fetchone()
            except Exception:
                cycle = None
            if cycle:
                raw_result = cycle["result_json"] if hasattr(cycle, "keys") else cycle[0]
                try:
                    result = json.loads(raw_result or "{}")
                except Exception:
                    result = {}
                trainings = []
                for horizon, payload in dict(result.get("horizons") or {}).items():
                    training = dict((payload or {}).get("lightgbm_training") or {})
                    if training:
                        trainings.append((str(horizon), training))
                if trainings:
                    horizon, training = max(
                        trainings,
                        key=lambda item: (
                            int(item[1].get("trading_days") or 0),
                            int(item[1].get("observations") or 0),
                        ),
                    )
                    latest_training = {
                        "horizon": horizon,
                        "training_state": training.get("state"),
                        "observations": int(training.get("observations") or 0),
                        "trading_days": int(training.get("trading_days") or 0),
                        "regimes": int(training.get("regimes") or 0),
                        "training_error": training.get("error") or training.get("reason"),
                    }
        if shadow_row and not latest_training:
            latest_training = {
                "horizon": shadow_row.get("horizon"),
                "training_state": shadow_row.get("state"),
                "observations": int(shadow_row.get("observations") or 0),
                "trading_days": int(shadow_row.get("trading_days") or 0),
                "regimes": int(shadow_row.get("regimes") or 0),
            }
        observations = int(latest_training.get("observations") or 0)
        trading_days = int(latest_training.get("trading_days") or 0)
        evidence_ready = observations >= shadow_min_observations and trading_days >= shadow_min_days
        # Initial Model-Paper evidence must not depend on a learned ranker that
        # itself requires Model-Paper settlements.  The governed deterministic
        # bootstrap scorer is always available for SHADOW / Model-Paper-only
        # admission with zero production influence.  Learned-model readiness is
        # reported separately and never weakens its evidence thresholds.
        return {
            "state": "BOOTSTRAP_AVAILABLE_TRAINING_REQUIRED" if evidence_ready else "BOOTSTRAP_AVAILABLE_COLLECTING_SHADOW_EVIDENCE",
            "available": True,
            "admission_authority": "DETERMINISTIC_BOOTSTRAP",
            "bootstrap_available": True,
            "learned_model_available": False,
            **latest_training,
            "shadow_evidence_ready": evidence_ready,
            "shadow_min_observations": shadow_min_observations,
            "shadow_min_trading_days": shadow_min_days,
            "production_min_trading_days": 126,
            "production_influence": 0.0,
        }

    def paper_model_status(self, desk: str) -> Dict[str, Any]:
        return self._paper_model_status(str(desk or "").lower())

    def paper_admission_diagnostics(self, desk: str, candidate_ids: set[str]) -> Dict[str, Any]:
        """Summarise the latest automatic Model Paper admission result.

        The lifecycle previously collapsed every no-open condition into the
        sentence "Model Paper admission has not opened".  Operations could
        therefore not distinguish a legitimate entry-trigger wait from missing
        identity/quote/trade-map/sector/ADV/risk evidence.
        """
        if not candidate_ids or not self._exists("quant_paper_predictions"):
            return {
                "evaluated_candidates": 0, "states": {}, "blocker_counts": {},
                "actionable_blocker_counts": {}, "expected_wait_blocker_counts": {},
                "terminal_non_admission_counts": {}, "samples": [],
            }
        try:
            rows = self.store.conn.execute(
                """SELECT candidate_id,symbol,prediction_id,observed_at,payload_json
                     FROM quant_paper_predictions
                    WHERE mode=?
                    ORDER BY observed_at DESC,prediction_id DESC""",
                (desk,),
            ).fetchall()
        except Exception:
            rows = []
        latest: dict[str, Dict[str, Any]] = {}
        for raw in rows:
            row = dict(raw) if hasattr(raw, "keys") else {
                "candidate_id": raw[0], "symbol": raw[1], "prediction_id": raw[2],
                "observed_at": raw[3], "payload_json": raw[4],
            }
            candidate_id = str(row.get("candidate_id") or "").strip()
            if candidate_id not in candidate_ids or candidate_id in latest:
                continue
            try:
                payload = json.loads(row.get("payload_json") or "{}")
            except Exception:
                payload = {}
            admission = payload.get("automatic_paper_admission") if isinstance(payload, dict) else None
            if not isinstance(admission, dict):
                continue
            blockers = admission.get("blockers")
            if not isinstance(blockers, list):
                blockers = [admission.get("reason")] if admission.get("reason") else []
            latest[candidate_id] = {
                "candidate_id": candidate_id,
                "symbol": row.get("symbol"),
                "state": str(admission.get("state") or "UNKNOWN"),
                "blockers": [str(value) for value in blockers if value],
                "observed_at": row.get("observed_at"),
            }
        states: Dict[str, int] = {}
        blocker_counts: Dict[str, int] = {}
        expected_wait: Dict[str, int] = {}
        terminal: Dict[str, int] = {}
        actionable: Dict[str, int] = {}
        expected_markers = (
            "waiting for directional entry trigger",
            "market closed; fresh executable quote required at next session",
            "evaluation sleeve maximum open positions",
        )
        terminal_markers = (
            "intraday model-paper entry cutoff passed",
            "outside same-snapshot top quintile",
            "entry chase exceeds 0.75%",
            "delivery short unsupported",
            "ranker did not select",
            "candidate side is missing or unsupported",
        )
        for row in latest.values():
            state = str(row.get("state") or "UNKNOWN")
            states[state] = states.get(state, 0) + 1
            for blocker in row.get("blockers") or []:
                blocker_counts[blocker] = blocker_counts.get(blocker, 0) + 1
                lower = blocker.lower()
                if (
                    any(marker in lower for marker in expected_markers)
                    or ("fresh executable quote required" in lower and not is_india_market_open())
                ):
                    target = expected_wait
                elif any(marker in lower for marker in terminal_markers):
                    target = terminal
                else:
                    target = actionable
                target[blocker] = target.get(blocker, 0) + 1
        return {
            "evaluated_candidates": len(latest),
            "states": states,
            "blocker_counts": blocker_counts,
            "actionable_blocker_counts": actionable,
            "expected_wait_blocker_counts": expected_wait,
            "terminal_non_admission_counts": terminal,
            "samples": list(latest.values())[:20],
        }

    def _columns(self, table: str) -> set[str]:
        try:
            return {str(row[1]) for row in self.store.conn.execute(f"PRAGMA table_info({table})").fetchall()}
        except Exception:
            return set()

    def _paper_attribution_ids(self, desk: str) -> tuple[set[str], set[str]]:
        """Return settled IDs with outcome-ledger and performance evidence."""
        if not self._exists("quant_evaluation_positions"):
            return set(), set()
        columns = self._columns("quant_evaluation_positions")
        attribution_columns = {"outcome", "signal_outcome", "net_pnl", "closed_at", "exit_reason"} & columns
        select = ["prediction_id", "payload_json"]
        select += [name for name in ("outcome", "signal_outcome", "net_pnl", "closed_at", "exit_reason") if name in columns]
        try:
            rows = self.store.conn.execute(
                f"SELECT {','.join(select)} FROM quant_evaluation_positions WHERE mode=? AND status='CLOSED'",
                (desk,),
            ).fetchall()
        except Exception:
            return set(), set()
        ledger, performance = set(), set()
        for raw in rows:
            row = dict(raw) if hasattr(raw, "keys") else dict(zip(select, raw))
            candidate = self._payload_candidate(row.get("payload_json")) or str(row.get("prediction_id") or "")
            if not candidate:
                continue
            # Older continuity fixtures only expose CLOSED status and payload.
            # On the production schema, outcome and performance columns are
            # required; on that legacy schema CLOSED is the only available
            # immutable attribution proof.
            has_outcome = (not attribution_columns) or any(row.get(name) not in (None, "") for name in ("outcome", "signal_outcome", "exit_reason", "closed_at") if name in row)
            has_performance = (not attribution_columns) or (row.get("net_pnl") is not None if "net_pnl" in row else has_outcome)
            if has_outcome:
                ledger.add(candidate)
            if has_outcome and has_performance:
                performance.add(candidate)
        return ledger, performance
    def _desk(self, desk: str) -> Dict[str, Any]:
        platform = SelectionPlatformService(self.store)
        summary = platform.latest_summary(desk)
        fingerprint = str(summary.get("population_fingerprint") or "").strip()
        population_rows = CandidatePopulationService(self.store).rows(fingerprint) if fingerprint else []
        population = {
            str(row.get("candidate_id") or "") for row in population_rows
            if row.get("candidate_id") and str(row.get("mode") or "").lower() == desk
        }
        repo = getattr(self.store, "production_model_governance_repository", None)
        if repo is not None:
            features = {
                str(row.get("candidate_id") or "") for row in population_rows
                if row.get("candidate_id") and (
                    str(row.get("feature_snapshot_state") or "").upper() == "COMPLETE"
                    and str(row.get("feature_lineage_state") or "").upper() == "VERIFIED"
                )
            }
        else:
            features = self._ids(
                "SELECT candidate_id FROM quant_feature_snapshots WHERE population_fingerprint=? AND mode=?",
                (fingerprint, desk),
            ) if fingerprint and self._exists("quant_feature_snapshots") else set()
        arms: dict[str, set[str]] = {arm: set() for arm in ("heuristic", "quant", "hybrid")}
        for prediction in (platform.predictions(fingerprint) if fingerprint else []):
            arm = str(prediction.get("arm") or "").lower()
            candidate = str(prediction.get("candidate_id") or "")
            if arm in arms and candidate in population:
                arms[arm].add(candidate)
        all_three = set.intersection(*(arms.values())) if all(arms.values()) else set()
        # Paper/settlement ledgers are historical by design. Reconciliation
        # must scope them to the latest immutable population or old-architecture
        # backups can make a new population appear falsely advanced.
        paper_all = self._paper_ids(desk)
        open_paper_all = self._paper_ids(desk, closed=False)
        settled_all = self._paper_ids(desk, closed=True)
        paper = all_three & paper_all
        open_paper = all_three & open_paper_all
        settled = all_three & settled_all
        research_ledger_all, performance_attributed_all = self._paper_attribution_ids(desk)
        research_ledger = settled & research_ledger_all
        performance_attributed = settled & performance_attributed_all
        admission = self.paper_admission_diagnostics(desk, all_three - paper)
        paper_model = self._paper_model_status(desk)

        stages = {
            "captured": len(population),
            "feature_complete": len(population & features),
            "baseline_predicted": len(population & arms["heuristic"]),
            "ml_predicted": len(population & arms["quant"]),
            "hybrid_predicted": len(population & arms["hybrid"]),
            "same_population_three_arm": len(population & all_three),
            "paper_opened": len(paper),
            "monitoring": len(open_paper),
            "settled": len(settled),
            "research_ledger": len(settled & research_ledger),
            "performance_attributed": len(settled & performance_attributed),
        }
        blockers = []
        feature_diagnostics = []
        if repo is not None:
            missing_feature_ids = population - features
            for row in population_rows:
                candidate_id = str(row.get("candidate_id") or "")
                if candidate_id not in missing_feature_ids:
                    continue
                snapshot = dict(row.get("governance_feature_snapshot") or {})
                feature_diagnostics.append({
                    "candidate_id": candidate_id,
                    "symbol": row.get("symbol"),
                    "snapshot_state": row.get("feature_snapshot_state"),
                    "lineage_state": row.get("feature_lineage_state"),
                    "freshness_state": snapshot.get("freshness_state"),
                    "freshness_eligible_for_training": snapshot.get("freshness_eligible_for_training"),
                    "compact_feature_coverage": snapshot.get("compact_feature_coverage"),
                    "feature_as_of": snapshot.get("feature_as_of"),
                    "source_as_of": snapshot.get("source_as_of"),
                    "received_at": snapshot.get("received_at"),
                    "regime_tag": snapshot.get("regime_tag"),
                    "missing_features": list(snapshot.get("missing_features") or [])[:20],
                    "lineage_missing": list(snapshot.get("lineage_missing") or [])[:20],
                })
        if not population:
            blockers.append("No immutable analysed-candidate population is captured.")
        if population - features:
            reason_counts = {}
            for row in feature_diagnostics:
                for reason in row.get("lineage_missing") or []:
                    key = f"lineage:{reason}"
                    reason_counts[key] = reason_counts.get(key, 0) + 1
                if str(row.get("freshness_state") or "").upper() not in {"LIVE", "FRESH", "CLOSED_MARKET", "VERIFIED_CLOSE"}:
                    key = f"freshness:{row.get('freshness_state') or 'UNKNOWN'}"
                    reason_counts[key] = reason_counts.get(key, 0) + 1
                if str(row.get("regime_tag") or "UNKNOWN").upper() == "UNKNOWN":
                    reason_counts["regime:UNKNOWN"] = reason_counts.get("regime:UNKNOWN", 0) + 1
            suffix = ""
            if reason_counts:
                top = sorted(reason_counts.items(), key=lambda item: (-item[1], item[0]))[:3]
                suffix = " · " + "; ".join(f"{reason} ({count})" for reason, count in top)
            blockers.append(f"{len(population-features)} candidate(s) lack COMPLETE point-in-time feature snapshots{suffix}.")
        for arm, label in (("heuristic", "Baseline"), ("quant", "ML"), ("hybrid", "Hybrid")):
            missing = population - arms[arm]
            if missing:
                blockers.append(f"{label} prediction is missing for {len(missing)} candidate(s).")
        if all_three and not paper:
            actionable_admission = dict(admission.get("actionable_blocker_counts") or {})
            expected_wait_admission = dict(admission.get("expected_wait_blocker_counts") or {})
            if actionable_admission:
                top = sorted(actionable_admission.items(), key=lambda item: (-item[1], item[0]))[:3]
                blockers.append(
                    "Model Paper admission blocked: "
                    + "; ".join(f"{reason} ({count})" for reason, count in top)
                )
            elif expected_wait_admission:
                top = sorted(expected_wait_admission.items(), key=lambda item: (-item[1], item[0]))[:3]
                blockers.append(
                    "Model Paper admission waiting on governed market condition: "
                    + "; ".join(f"{reason} ({count})" for reason, count in top)
                )
            elif admission.get("terminal_non_admission_counts"):
                top = sorted(dict(admission.get("terminal_non_admission_counts") or {}).items(), key=lambda item: (-item[1], item[0]))[:3]
                blockers.append(
                    "No Model Paper position is expected from this immutable population: "
                    + "; ".join(f"{reason} ({count})" for reason, count in top)
                )
            elif not paper_model.get("available"):
                obs = int(paper_model.get("observations") or 0)
                days = int(paper_model.get("trading_days") or 0)
                blockers.append(
                    "No governed shadow Model Paper ranker is available: "
                    f"settled training evidence {obs}/{paper_model.get('shadow_min_observations', 100)} observations · "
                    f"{days}/{paper_model.get('shadow_min_trading_days', 20)} trading days. "
                    "Production qualification remains 126+ trading days with untouched holdout and regime gates."
                )
            else:
                blockers.append("Same-population predictions exist but Model Paper admission has not been evaluated or persisted.")
        if open_paper:
            blockers.append(f"{len(open_paper)} Model Paper observation(s) await target/SL/trailing/managed/horizon settlement.")
        if settled - research_ledger:
            blockers.append(f"{len(settled-research_ledger)} settled observation(s) lack Research ledger outcome attribution.")
        if settled - performance_attributed:
            blockers.append(f"{len(settled-performance_attributed)} settled observation(s) lack Research performance attribution.")

        if not population:
            state = "NOT_STARTED"
        elif population - features:
            # A PARTIAL immutable snapshot with verified lineage is an evidence
            # sufficiency condition, not a crashed worker. R25 labelled these
            # rows FEATURES_INCOMPLETE, which caused the controller to keep
            # restarting a healthy data conveyor even though immutability means
            # that exact population can never be rewritten. Keep true lineage/
            # timestamp corruption actionable; classify source/feature scarcity
            # as expected evidence pending and let the next scan create a new
            # immutable population with richer PIT inputs.
            diagnostic_by_id = {
                str(row.get("candidate_id") or ""): row for row in feature_diagnostics
            }
            pending_only = bool(population - features) and all(
                str((diagnostic_by_id.get(candidate_id) or {}).get("snapshot_state") or "").upper() == "PARTIAL"
                and str((diagnostic_by_id.get(candidate_id) or {}).get("lineage_state") or "").upper() == "VERIFIED"
                for candidate_id in (population - features)
            )
            state = "FEATURE_EVIDENCE_PENDING" if pending_only else "FEATURES_INCOMPLETE"
        elif len(all_three) != len(population):
            state = "THREE_ARM_INCOMPLETE"
        elif not paper:
            if admission.get("actionable_blocker_counts"):
                state = "PAPER_ADMISSION_BLOCKED"
            elif admission.get("expected_wait_blocker_counts"):
                state = "PAPER_ADMISSION_WAITING"
            elif admission.get("terminal_non_admission_counts") and int(admission.get("evaluated_candidates") or 0) >= len(all_three):
                state = "PAPER_ADMISSION_NOT_SELECTED"
            elif not paper_model.get("available"):
                state = (
                    "PAPER_MODEL_TRAINING_BLOCKED"
                    if paper_model.get("shadow_evidence_ready")
                    else "PAPER_MODEL_EVIDENCE_WAITING"
                )
            else:
                state = "PAPER_ADMISSION_PENDING"
        elif open_paper:
            state = "MONITORING"
        elif settled - research_ledger:
            state = "RESEARCH_LEDGER_RECONCILIATION_PENDING"
        elif settled - performance_attributed:
            state = "PERFORMANCE_ATTRIBUTION_PENDING"
        else:
            state = "SETTLEMENT_ACTIVE"
        return {
            "desk": desk,
            "state": state,
            "population_fingerprint": fingerprint or None,
            "stages": stages,
            "blockers": blockers,
            "paper_admission": admission,
            "paper_model": paper_model,
            "feature_snapshot_diagnostics": feature_diagnostics[:20],
            "missing_candidate_ids": {
                "features": sorted(population - features)[:50],
                "baseline": sorted(population - arms["heuristic"])[:50],
                "ml": sorted(population - arms["quant"])[:50],
                "hybrid": sorted(population - arms["hybrid"])[:50],
                "paper": sorted(all_three - paper)[:50],
                "settlement": sorted(open_paper)[:50],
                "research_ledger": sorted(settled - research_ledger)[:50],
                "performance": sorted(settled - performance_attributed)[:50],
            },
            "next_action": blockers[0] if blockers else "Research lifecycle is reconciled through settled performance.",
            "completion_state": "RECONCILED_THROUGH_PERFORMANCE" if not blockers else None,
            "production_influence": 0.0,
            "broker_authority": "NONE",
        }

    def status(self) -> Dict[str, Any]:
        desks = {desk: self._desk(desk) for desk in ("delivery", "intraday")}
        complete = all(row["state"] == "SETTLEMENT_ACTIVE" and row.get("completion_state") == "RECONCILED_THROUGH_PERFORMANCE" for row in desks.values())
        return {
            "ok": True,
            "version": SERVICE_VERSION,
            "state": "RECONCILED" if complete else "ACTIVE_WITH_EXPLICIT_BLOCKERS",
            "operational": True,
            "by_desk": desks,
            "research_lane": "SEPARATE_FROM_PRODUCTION",
            "signal_ledger_policy": "Research settlements use an immutable Research ledger and independent Research Accuracy/Performance attribution; production Signal Ledger authority is not contaminated.",
            "production_influence": 0.0,
            "broker_authority": "NONE",
            "evaluated_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        }
