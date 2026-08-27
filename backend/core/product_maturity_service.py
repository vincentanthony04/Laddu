"""Evidence-based product maturity and Level-4 release-gate authority.

This service reports only evidence already produced by the installed product.
It never promotes a model, changes a ranking, advances scanner cursors or
converts an unavailable observation into a pass.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Mapping

from core.market_cycle_maturity_service import MarketCycleMaturityService
from core.production_ranking_service import RANKING_VERSION
from core.decision_surface_reconciliation_service import DecisionSurfaceReconciliationService
from core.model_learning_audit_service import ModelLearningAuditService
from core.operational_evidence_integrity_service import OperationalEvidenceIntegrityService
from config import APP_VERSION


SERVICE_VERSION = "product-maturity-1.1.0-integrity-reconciled"
CONTRACT_VERSION = "level4-release-gates-1.1.0"


class ProductMaturityService:
    """Aggregate architecture, runtime, scanner, decision and model evidence."""

    LEVELS = {
        0: "NOT_INSTALLABLE",
        1: "ENGINEERING_FOUNDATION",
        2: "OPERATIONAL_FOUNDATION",
        3: "CONTROLLED_MODEL_PAPER_PILOT",
        4: "PRODUCTION_READY_MODEL_PAPER",
        5: "MATURE_GOVERNED_PRODUCTION",
    }

    def __init__(self, app: Any):
        self.app = app
        self.store = getattr(app, "store", None)

    @staticmethod
    def _map(value: Any) -> Dict[str, Any]:
        return dict(value) if isinstance(value, Mapping) else {}

    @staticmethod
    def _rows(value: Any) -> list[Dict[str, Any]]:
        if not isinstance(value, Iterable) or isinstance(value, (str, bytes, Mapping)):
            return []
        return [dict(row) for row in value if isinstance(row, Mapping)]

    def _kv(self, key: str, default: Any = None) -> Any:
        getter = getattr(self.store, "get_kv", None)
        if not callable(getter):
            return default
        try:
            return getter(key, default)
        except TypeError:
            try:
                value = getter(key)
                return default if value is None else value
            except Exception:
                return default
        except Exception:
            return default

    def _readiness(self) -> Dict[str, Any]:
        try:
            return self._map(self.app.product_readiness())
        except Exception as exc:
            return {"ok": False, "product_state": "UNAVAILABLE", "error": str(exc)[:180]}

    def _scanner_root(self) -> Dict[str, Any]:
        try:
            payload = self._map(self.app.scanner_status())
        except Exception as exc:
            return {"error": str(exc)[:180], "mode_scanners": {}}
        scanner = self._map(payload.get("scanner"))
        return scanner or payload

    def _snapshot(self, desk: str) -> Dict[str, Any]:
        orchestration = getattr(self.app, "scan_orchestration", None)
        resolver = getattr(orchestration, "_snapshot_identity", None)
        if callable(resolver):
            try:
                return self._map(resolver(desk))
            except Exception:
                pass
        status = self._map(getattr(self.app, "status", {}))
        universe = self._map(status.get("universe_authority"))
        return self._map(self._map(universe.get("snapshots")).get(desk))

    @staticmethod
    def _accounting_intraday(row: Mapping[str, Any], population: int) -> Dict[str, Any]:
        attempted = int(row.get("attempted") or row.get("sweep_attempted") or 0)
        returned = int(row.get("returned") or row.get("sweep_returned") or 0)
        verified = int(row.get("verified") or row.get("sweep_verified") or 0)
        missing = int(row.get("missing") or row.get("sweep_missing") or 0)
        unverified = int(row.get("unverified") or row.get("sweep_unverified") or 0)
        terminal = bool(
            population > 0
            and attempted == population
            and returned + missing == attempted
            and verified + unverified == returned
        )
        return {
            "population_count": population,
            "attempted": attempted,
            "returned": returned,
            "verified": verified,
            "missing": missing,
            "unverified": unverified,
            "terminal_accounting_ok": terminal,
            "fully_verified": terminal and verified == population,
        }

    @staticmethod
    def _accounting_delivery(row: Mapping[str, Any], population: int) -> Dict[str, Any]:
        scanned = int(row.get("scanned") or row.get("sweep_scanned") or 0)
        return {
            "population_count": population,
            "scanned": scanned,
            "terminal_accounting_ok": bool(population > 0 and scanned == population),
            "fully_verified": bool(population > 0 and scanned == population),
        }

    def _scanner_gate(self, desk: str, scanner_root: Mapping[str, Any]) -> Dict[str, Any]:
        modes = self._map(scanner_root.get("mode_scanners") or scanner_root.get("scanners"))
        mode = self._map(modes.get(desk))
        lane = self._map(mode.get("coverage") if desk == "intraday" else mode.get("analysis"))
        snapshot = self._snapshot(desk)
        population = int(snapshot.get("population_count") or lane.get("universe_size") or mode.get("population_count") or 0)
        evidence = self._map(self._kv(f"scanner_cycle_evidence:{desk}", {}))
        last_full = self._map(
            evidence.get("full_sweep")
            or lane.get("last_completed_sweep")
            or lane.get("last_completed")
        )
        accounting = (
            self._accounting_intraday(last_full, population)
            if desk == "intraday"
            else self._accounting_delivery(last_full, population)
        )
        market_cycle = self._map(evidence.get("market_hours_analysis"))
        market_hours_proven = bool(
            market_cycle.get("completed_at")
            and int(market_cycle.get("scanned") or 0) > 0
            and market_cycle.get("market_open") is True
        )
        passed = bool(accounting["terminal_accounting_ok"] and market_hours_proven)
        missing = []
        if not accounting["terminal_accounting_ok"]:
            missing.append("one exact-snapshot full-universe terminal accounting cycle")
        if not market_hours_proven:
            missing.append("one completed market-hours analysis cycle")
        return {
            "desk": desk,
            "state": "PASS" if passed else "PENDING_EVIDENCE",
            "passed": passed,
            "snapshot": snapshot,
            "accounting": accounting,
            "market_hours_analysis": market_cycle or None,
            "ranking_version": evidence.get("ranking_version") or RANKING_VERSION,
            "missing_gates": missing,
            "evidence": evidence,
        }

    def _decision_rows(self) -> list[Dict[str, Any]]:
        getter = getattr(self.store, "latest_decisions", None)
        if not callable(getter):
            return []
        try:
            return self._rows(getter("all", limit=300) or [])
        except TypeError:
            try:
                return self._rows(getter("all", 300) or [])
            except Exception:
                return []
        except Exception:
            return []

    def _decision_reconciliation(self) -> Dict[str, Any]:
        rows = self._decision_rows()
        governed = [row for row in rows if row.get("ranking_version") == RANKING_VERSION]
        traced = [row for row in governed if row.get("ranking_trace_id") and row.get("ranking_input_hash")]
        consumer_aware = [
            row for row in governed
            if set(row.get("model_ranking_consumers") or [])
            >= {"TODAY_ENTRY_SCANNER", "REASSESSMENT_SCANNER", "MANUAL_ANALYSIS"}
        ]
        duplicate_conflicts: list[Dict[str, Any]] = []
        by_decision: Dict[str, set[str]] = {}
        for row in traced:
            key = str(row.get("decision_id") or row.get("signal_id") or "").strip()
            trace = str(row.get("ranking_trace_id") or "").strip()
            if key and trace:
                by_decision.setdefault(key, set()).add(trace)
        for key, traces in by_decision.items():
            if len(traces) > 1:
                duplicate_conflicts.append({"decision_id": key, "ranking_trace_ids": sorted(traces)})
        observations_ready = bool(rows)
        passed = bool(
            observations_ready
            and len(governed) == len(rows)
            and len(traced) == len(rows)
            and len(consumer_aware) == len(rows)
            and not duplicate_conflicts
        )
        missing = []
        if not observations_ready:
            missing.append("at least one canonical evaluated decision after the current installation")
        if rows and len(governed) != len(rows):
            missing.append("all current canonical decisions must use the current governed ranker")
        if rows and len(traced) != len(rows):
            missing.append("ranking input/result trace on every current decision")
        if duplicate_conflicts:
            missing.append("zero canonical decision/ranking-trace conflicts")
        return {
            "state": "PASS" if passed else "PENDING_EVIDENCE",
            "passed": passed,
            "observations": len(rows),
            "governed_ranker_rows": len(governed),
            "trace_complete_rows": len(traced),
            "consumer_contract_rows": len(consumer_aware),
            "conflicts": duplicate_conflicts,
            "ranking_version": RANKING_VERSION,
            "missing_gates": missing,
        }

    def _model_evidence(self) -> Dict[str, Any]:
        repository = getattr(self.store, "production_model_governance_repository", None)
        publication: Dict[str, Any] = {}
        governance: Dict[str, Any] = {}
        if repository is not None:
            try:
                publication = self._map(repository.training_publication_status())
            except Exception as exc:
                publication = {"ok": False, "error": str(exc)[:180]}
            try:
                governance = self._map(repository.status())
            except Exception as exc:
                governance = {"ok": False, "error": str(exc)[:180]}
        counts = self._map(governance.get("counts"))
        shadow_predictions = int(publication.get("shadow_predictions") or 0)
        settled = int(counts.get("settled_outcomes") or 0)
        active = self._rows(governance.get("active_champions"))
        shadow_capture_ready = shadow_predictions > 0
        # Counts are descriptive only. Level 5 authority comes exclusively
        # from Level5ForwardMaturityService; no raw count may self-award it.
        forward_ready = False
        return {
            "state": "SHADOW_LEARNING" if shadow_capture_ready else "AWAITING_SHADOW_OBSERVATIONS",
            "shadow_predictions": shadow_predictions,
            "unsettled_predictions": int(publication.get("unsettled_predictions") or 0),
            "settled_outcomes": settled,
            "models": int(counts.get("models") or 0),
            "experiments": int(counts.get("experiments") or 0),
            "active_champions": len(active),
            "shadow_capture_ready": shadow_capture_ready,
            "forward_promotion_ready": forward_ready,
            "production_weight_policy": "GOVERNED_ACTIVE_CAP_15_WITH_DETERMINISTIC_FALLBACK",
            "publication": publication,
            "governance": governance,
            "missing_gates": [
                item for item, passed in (
                    ("shadow predictions recorded from canonical scanner cycles", shadow_capture_ready),
                    ("at least 100 settled forward outcomes", settled >= 100),
                    ("an effective governed champion assignment", bool(active)),
                ) if not passed
            ],
        }

    def _browser_and_soak(self) -> Dict[str, Any]:
        browser = self._map(self._kv("level4_browser_proof:last", {}))
        soak = self._map(self._kv("level4_market_soak:last", {}))
        browser_pass = bool(browser.get("passed") is True and browser.get("build") == APP_VERSION)
        soak_pass = bool(soak.get("passed") is True and soak.get("build") == APP_VERSION)
        return {
            "state": "PASS" if browser_pass and soak_pass else "PENDING_EVIDENCE",
            "passed": browser_pass and soak_pass,
            "browser": browser or None,
            "market_hours_soak": soak or None,
            "missing_gates": [
                item for item, passed in (
                    ("exported current-build browser self-check proof", browser_pass),
                    ("market-hours soak/restart/forced-flatten proof", soak_pass),
                ) if not passed
            ],
        }

    def status(self) -> Dict[str, Any]:
        readiness = self._readiness()
        operational = str(readiness.get("product_state") or readiness.get("truth_level") or "").upper() == "OPERATIONAL"
        architecture_ready = bool(
            readiness.get("data_plane", {}).get("state") == "READY"
            if isinstance(readiness.get("data_plane"), Mapping)
            else operational
        )
        scanner_root = self._scanner_root()
        scanner = {
            desk: self._scanner_gate(desk, scanner_root)
            for desk in ("intraday", "delivery")
        }
        decisions = self._decision_reconciliation()
        surfaces = DecisionSurfaceReconciliationService(self.app).status()
        models = self._model_evidence()
        forward_service = getattr(self.app, "level5_forward_maturity", None)
        try:
            forward_maturity = forward_service.status() if forward_service is not None else {
                "ok": False, "state": "FORWARD_MATURITY_SERVICE_UNAVAILABLE", "level5_ready": False,
                "missing_gates": ["level5_forward_maturity_service"],
            }
        except Exception as exc:
            forward_maturity = {
                "ok": False, "state": "FORWARD_MATURITY_UNAVAILABLE", "level5_ready": False,
                "error": str(exc)[:240], "missing_gates": ["level5_forward_maturity_checkpoint"],
            }
        models["forward_promotion_ready"] = bool(forward_maturity.get("level5_ready"))
        models["level5_forward_maturity_state"] = forward_maturity.get("state")
        learning_audit = ModelLearningAuditService(self.app).status()
        evidence_integrity = OperationalEvidenceIntegrityService(self.app).status()
        browser_soak = self._browser_and_soak()
        market_cycle = MarketCycleMaturityService(self.app).status()
        context_level = int(market_cycle.get("maturity_level") or 0)

        level4_gates = {
            "operational_installation": operational,
            "intraday_full_cycle": scanner["intraday"]["passed"],
            "delivery_full_cycle": scanner["delivery"]["passed"],
            "canonical_ranking_reconciliation": decisions["passed"],
            "decision_surface_reconciliation": surfaces["passed"],
            "shadow_learning_capture": models["shadow_capture_ready"],
            "model_learning_integrity": learning_audit["passed"],
            "verified_market_context": context_level >= 1,
            "browser_and_market_soak": browser_soak["passed"],
            "operational_evidence_integrity": evidence_integrity["passed"],
        }
        level4_ready = all(level4_gates.values())
        level5_ready = bool(
            level4_ready
            and forward_maturity.get("level5_ready") is True
            and context_level >= 4
        )

        level = 0
        if architecture_ready:
            level = 1
        if operational:
            level = 2
        if operational and getattr(self.app, "production_ranker", None) is not None:
            level = 3
        if level4_ready:
            level = 4
        if level5_ready:
            level = 5

        missing_level4 = [name for name, passed in level4_gates.items() if not passed]
        return {
            "ok": operational,
            "version": SERVICE_VERSION,
            "contract_version": CONTRACT_VERSION,
            "build": APP_VERSION,
            "maturity_level": level,
            "maturity_max": 5,
            "maturity_state": self.LEVELS[level],
            "release_class": (
                "MATURE_GOVERNED_PRODUCTION" if level >= 5
                else "PRODUCTION_READY_MODEL_PAPER" if level >= 4
                else "CONTROLLED_MODEL_PAPER_PILOT" if level >= 3
                else "OPERATIONAL_FOUNDATION" if level >= 2
                else "ENGINEERING_FOUNDATION" if level >= 1
                else "NOT_INSTALLABLE"
            ),
            "level4_ready": level4_ready,
            "level5_ready": level5_ready,
            "level4_gates": level4_gates,
            "missing_level4_gates": missing_level4,
            "scanner": scanner,
            "canonical_ranking": decisions,
            "decision_surfaces": surfaces,
            "models": models,
            "level5_forward_maturity": forward_maturity,
            "model_learning_audit": learning_audit,
            "operational_evidence_integrity": evidence_integrity,
            "market_cycle_and_sector_rotation": market_cycle,
            "browser_and_market_soak": browser_soak,
            "readiness_summary": {
                "product_state": readiness.get("product_state"),
                "truth_level": readiness.get("truth_level"),
                "customer_usefulness": self._map(readiness.get("customer_usefulness")).get("state"),
            },
            "policy": (
                "Level 4 is earned only after exact-snapshot scanner cycles, canonical ranking trace reconciliation, "
                "decision-surface reconciliation, shadow-learning integrity, hash-chained target-machine browser/market-hours proof. Level 5 additionally requires "
                "immutable same-population three-arm forward evidence, capital-profile purged walk-forward approval, exact governed champion lineage for both desks, and mature market-cycle validation."
            ),
            "evaluated_at": datetime.now(timezone.utc).isoformat(),
        }
