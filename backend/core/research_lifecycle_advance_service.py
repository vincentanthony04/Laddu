"""Operator-triggered, fail-closed Research lifecycle advancement.

The service replays the latest immutable Delivery and Intraday populations
through the three shadow arms, Model Paper admission, and the existing forward
settlement lifecycle. It never changes production model weight or broker
authority. Missing populations/data remain explicit blockers rather than being
reported as a successful run.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict

from core.candidate_population_service import CandidatePopulationService
from core.research_lifecycle_reconciliation_service import ResearchLifecycleReconciliationService
from core.selection_platform_service import SelectionPlatformService
from core.quant_scan_capture_service import QuantScanCaptureService
from core.research_candidate_projection_service import ResearchCandidateProjectionService
from core.quant_paper_activation_service import QuantPaperActivationService

SERVICE_VERSION = "research-lifecycle-advance-3.1.0-governance-ranking-capture"
RESEARCH_TRADE_MAP_FIELDS = ("planned_entry", "planned_t1", "planned_sl", "identity_verified")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class ResearchLifecycleAdvanceService:
    def __init__(self, app: Any):
        self.app = app
        self.store = app.store
        self.populations = CandidatePopulationService(self.store)
        self.platform = SelectionPlatformService(self.store)

    def _latest_population(self, desk: str) -> Dict[str, Any] | None:
        """Read the canonical selector population from its configured authority.

        Production populations live in Governance PostgreSQL.  Querying the
        retired/local candidate_populations table here made a healthy governed
        population look like zero and prevented Research/WFA from advancing.
        """
        repo = getattr(self.store, "production_model_governance_repository", None)
        if repo is not None and callable(getattr(repo, "latest_selector_population", None)):
            try:
                raw = repo.latest_selector_population(desk)
                if raw:
                    return dict(raw)
            except Exception:
                return None
        try:
            raw = self.store.conn.execute(
                """SELECT * FROM candidate_populations
                   WHERE mode=? ORDER BY observed_at DESC,created_at DESC LIMIT 1""",
                (desk,),
            ).fetchone()
            return dict(raw) if raw else None
        except Exception:
            return None

    def _recover_population_from_decisions(self, desk: str) -> Dict[str, Any] | None:
        """Repair scanner/opportunity → immutable Research population.

        Both canonical decisions and analysed opportunity-memory rows are
        considered. The projection service remains fail-closed: exact identity,
        point-in-time freshness, direction and a valid existing or evidence-
        derived Research trade map are mandatory. No production action or model
        influence is created here.
        """
        raw_rows: list[Dict[str, Any]] = []
        reader = getattr(self.store, "latest_decisions", None)
        if callable(reader):
            try:
                raw_rows.extend(dict(row) for row in (reader(desk, limit=160) or []))
            except Exception:
                pass
        opportunity_reader = getattr(self.store, "opportunity_candidates", None)
        if callable(opportunity_reader):
            try:
                raw_rows.extend(dict(row) for row in (opportunity_reader(desk, limit=240) or []))
            except Exception:
                pass
        # Immutable ranking/learning capture is intentionally earlier than
        # Model-Paper trade admission.  Pending Entry/T1/SL geometry is valid
        # research evidence but can never create a paper/production position.
        projection = ResearchCandidateProjectionService().project_many_for_ranking(raw_rows, desk=desk, limit=240)
        eligible = list(projection.get("accepted") or [])
        if not eligible:
            return {
                "state": "NO_ELIGIBLE_RESEARCH_CANDIDATES",
                "projection": projection,
                "production_influence": 0.0,
                "broker_authority": "NONE",
            }
        observed_at = max(str(row.get("decision_ts") or row.get("observed_at") or "") for row in eligible) or _now()
        result = QuantScanCaptureService(self.store).record(
            eligible,
            mode=desk,
            observed_at=observed_at,
            universe_size=len(eligible),
        )
        result = dict(result or {})
        result["projection"] = projection
        return result if result.get("population_fingerprint") else result

    def _advance_desk(self, desk: str) -> Dict[str, Any]:
        population = self._latest_population(desk)
        recovery = None
        rows: list[Dict[str, Any]] = []
        if population:
            fingerprint = str(population.get("population_fingerprint") or "")
            rows = self.populations.rows(fingerprint) if fingerprint else []
        if not population or not rows:
            recovery = self._recover_population_from_decisions(desk)
            population = self._latest_population(desk)
            if population:
                fingerprint = str(population.get("population_fingerprint") or "")
                rows = self.populations.rows(fingerprint) if fingerprint else []
        if not population or not rows:
            projection = dict((recovery or {}).get("projection") or {})
            rejected = list(projection.get("rejected") or [])
            reason_counts: Dict[str, int] = {}
            for row in rejected:
                key = str(row.get("reason") or "unknown")
                reason_counts[key] = reason_counts.get(key, 0) + 1
            return {
                "ok": False,
                "desk": desk,
                "state": "WAITING_FOR_IMMUTABLE_POPULATION",
                "reason": "No fresh exact-identity scanner candidate has reached immutable ranking capture.",
                "candidate_count": 0,
                "automatic_recovery_attempted": True,
                "projection_rejections": reason_counts,
                "next_action": "Complete exact identity/PIT scanner analysis. Entry/Target/SL is required later for Model Paper admission, not for ranking capture.",
                "production_influence": 0.0,
                "broker_authority": "NONE",
            }
        fingerprint = str(population.get("population_fingerprint") or "")
        try:
            # Scan capture already owns immutable population + three-arm
            # prediction publication. Re-evaluating through evaluate_population
            # would create a second population identity from already-stored rows.
            # Reuse the canonical predictions and only re-run the independently
            # governed Model-Paper admission/monitoring step.
            predictions = self.platform.predictions(fingerprint)
            by_arm: Dict[str, list[Dict[str, Any]]] = {arm: [] for arm in ("heuristic", "quant", "hybrid")}
            for prediction in predictions:
                arm = str(prediction.get("arm") or "").lower()
                if arm in by_arm:
                    by_arm[arm].append(dict(prediction))
            candidate_ids = {str(row.get("candidate_id") or "") for row in rows if row.get("candidate_id")}
            arm_ids = {
                arm: {str(row.get("candidate_id") or "") for row in values if row.get("candidate_id")}
                for arm, values in by_arm.items()
            }
            complete = bool(candidate_ids) and all(ids == candidate_ids for ids in arm_ids.values())
            missing_arm_repair = None
            if not complete:
                # P0-02: an existing immutable population with incomplete
                # three-arm predictions must self-repair the missing arms
                # from the SAME frozen population/features, not just report
                # the blocker forever. record_population() is fail-closed and
                # idempotent for identical immutable content (same fingerprint
                # in, same fingerprint out -- never a second population
                # identity), and selector-prediction persistence is INSERT OR
                # IGNORE, so re-running evaluate_population against the
                # canonical stored rows only fills in whatever arm/candidate
                # combinations are actually absent.
                try:
                    missing_arm_repair = self.platform.evaluate_population(
                        rows,
                        mode=desk,
                        observed_at=str(population.get("observed_at") or ""),
                        universe_id=str(population.get("universe_id") or ""),
                        dataset_fingerprint=str(population.get("dataset_fingerprint") or ""),
                        feature_manifest_hash=str(population.get("feature_manifest_hash") or ""),
                    )
                    repaired_fingerprint = str(missing_arm_repair.get("population_fingerprint") or "")
                    if repaired_fingerprint and repaired_fingerprint != fingerprint:
                        raise ValueError(
                            "repair attempt produced a different population identity; refusing to use it"
                        )
                except Exception as exc:
                    missing_arm_repair = {"ok": False, "error": str(exc)[:500]}
                predictions = self.platform.predictions(fingerprint)
                by_arm = {arm: [] for arm in ("heuristic", "quant", "hybrid")}
                for prediction in predictions:
                    arm = str(prediction.get("arm") or "").lower()
                    if arm in by_arm:
                        by_arm[arm].append(dict(prediction))
                arm_ids = {
                    arm: {str(row.get("candidate_id") or "") for row in values if row.get("candidate_id")}
                    for arm, values in by_arm.items()
                }
                complete = bool(candidate_ids) and all(ids == candidate_ids for ids in arm_ids.values())
            if not complete:
                return {
                    "ok": False, "desk": desk, "state": "THREE_ARM_CAPTURE_INCOMPLETE",
                    "population_fingerprint": fingerprint, "candidate_count": len(candidate_ids),
                    "arm_counts": {arm: len(ids) for arm, ids in arm_ids.items()},
                    "reason": "Canonical scan population exists but its three-arm selector predictions are incomplete.",
                    "automatic_recovery": recovery, "missing_arm_repair_attempted": missing_arm_repair is not None,
                    "missing_arm_repair": missing_arm_repair, "production_influence": 0.0,
                    "broker_authority": "NONE",
                }
            paper = QuantPaperActivationService(self.store).process_selection_population(
                mode=desk, population_fingerprint=fingerprint, candidates=rows,
                quant_predictions=by_arm["quant"], range_predictions=(),
            )
            return {
                "ok": bool(paper.get("ok", True)), "desk": desk,
                "state": str(paper.get("state") or "THREE_ARM_EVALUATED"),
                "population_fingerprint": fingerprint, "candidate_count": len(rows),
                "prediction_count": sum(len(values) for values in by_arm.values()),
                "paper_processed": int(paper.get("processed") or 0),
                "paper_reason": paper.get("reason"), "automatic_recovery": recovery,
                "population_reused_without_reidentification": True,
            }
        except Exception as exc:
            return {
                "ok": False, "desk": desk, "state": "THREE_ARM_ADVANCE_FAILED",
                "reason": str(exc)[:500], "population_fingerprint": fingerprint,
                "candidate_count": len(rows),
            }


    def run(self, *, settlement_limit: int = 80, advance_settlement: bool = True) -> Dict[str, Any]:
        desks = {desk: self._advance_desk(desk) for desk in ("delivery", "intraday")}
        lifecycle = getattr(self.app, "forward_evidence_lifecycle", None)
        if advance_settlement:
            if lifecycle is None:
                settlement = {"ok": False, "state": "SETTLEMENT_SERVICE_UNAVAILABLE"}
            else:
                try:
                    settlement = lifecycle.run_once(limit=max(1, min(250, int(settlement_limit))))
                except Exception as exc:
                    settlement = {"ok": False, "state": "SETTLEMENT_RUN_FAILED", "error": str(exc)[:500]}
        else:
            settlement = {
                "ok": True,
                "state": "DEFERRED_TO_FORWARD_EVIDENCE_WORKER",
                "note": "Dedicated forward-evidence worker owns settlement cadence; research activation does not duplicate it.",
            }
        reconciliation = ResearchLifecycleReconciliationService(self.store).status()
        # A stage execution failure must not collapse into the generic
        # PAPER_ADMISSION_PENDING state. Overlay the exact bounded failure into
        # the same canonical reconciliation returned to Operations.
        by_desk = dict(reconciliation.get("by_desk") or {})
        for desk, advance in desks.items():
            if advance.get("ok") is not False:
                continue
            row = dict(by_desk.get(desk) or {})
            if str(row.get("state") or "") == "PAPER_ADMISSION_PENDING":
                reason = str(
                    advance.get("paper_reason")
                    or advance.get("reason")
                    or advance.get("state")
                    or "Model Paper evaluation failed"
                )[:500]
                row["state"] = "PAPER_ADMISSION_BLOCKED"
                row["next_action"] = reason
                row["blockers"] = list(row.get("blockers") or []) + [reason]
                row["advance_failure"] = {
                    "state": advance.get("state"), "reason": reason,
                }
                by_desk[desk] = row
        reconciliation["by_desk"] = by_desk
        ok = all(row.get("ok") for row in desks.values()) and bool(settlement.get("ok"))
        return {
            "ok": ok,
            "version": SERVICE_VERSION,
            "state": "ADVANCED" if ok else "ADVANCED_WITH_EXPLICIT_BLOCKERS",
            "desks": desks,
            "settlement": settlement,
            "reconciliation": reconciliation,
            "production_influence": 0.0,
            "broker_authority": "NONE",
            "completed_at": _now(),
        }
