"""Current-build operational proof gate for Level 5 maturity.

This service never manufactures target evidence. It only reconciles persisted
browser, market-soak, recovery, data-authority, ML-population and forward
checkpoints produced by the exact installed build.
"""
from __future__ import annotations

from typing import Any, Dict, Mapping

from config import APP_VERSION, BROKER_ORDER_EXECUTION_ENABLED, PRODUCT_MODE
from core.ml_population_qualification_service import MLPopulationQualificationService
from core.level5_qualification_repository import Level5QualificationRepository
from core.nse_cash_data_authority_service import NseCashDataAuthorityService
from models import now_iso


def _map(value: Any) -> Dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


class Level5OperationalProofService:
    VERSION = "level5-operational-proof-1.1.0-progressive-gates"
    KV_KEY = "level5_operational_proof:last"

    def __init__(self, app: Any):
        self.app = app

    def _kv(self, key: str) -> Dict[str, Any]:
        try:
            return _map(self.app.store.get_kv(key, {}))
        except Exception:
            return {}

    @staticmethod
    def _current_build_pass(row: Mapping[str, Any]) -> bool:
        return row.get("passed") is True and str(row.get("build") or "") == APP_VERSION

    def status(self) -> Dict[str, Any]:
        browser = self._kv("level4_browser_proof:last")
        soak = self._kv("level4_market_soak:last")
        resilience = self._kv("level5_resilience_drill:last")
        try:
            recovery = self.app.priority_pipeline.recovery_status()
        except Exception as exc:
            recovery = {"ok": False, "state": "UNAVAILABLE", "error": str(exc)[:240]}
        try:
            qualification = MLPopulationQualificationService(self.app).status()
        except Exception as exc:
            qualification = {"ok": False, "state": "UNAVAILABLE", "error": str(exc)[:240], "desks": {}}
        try:
            forward = self.app.level5_forward_maturity.status()
        except Exception as exc:
            forward = {"ok": False, "state": "UNAVAILABLE", "error": str(exc)[:240]}
        try:
            from config import DATA_DIR
            nse = NseCashDataAuthorityService(self.app.store, DATA_DIR).cached_status()
        except Exception as exc:
            nse = {"ok": False, "state": "UNAVAILABLE", "error": str(exc)[:240]}
        desks = _map(qualification.get("desks"))
        gates = {
            "exact_build_browser_workflows": self._current_build_pass(browser),
            "market_session_soak": self._current_build_pass(soak),
            "recovery_and_tamper_drill": self._current_build_pass(resilience),
            "priority_pipeline_recovered": bool(recovery.get("ok", True)) and int(recovery.get("stale") or 0) == 0 and int(recovery.get("blocked") or 0) == 0,
            "official_nse_data_complete": str(nse.get("state") or "").upper() == "CURRENT",
            "delivery_ml_population_qualified": bool(_map(desks.get("delivery")).get("can_walk_forward")),
            "intraday_ml_population_qualified": bool(_map(desks.get("intraday")).get("can_walk_forward")),
            "forward_post_cost_authority": bool(forward.get("level5_ready")),
            "model_paper_only": PRODUCT_MODE == "AUTOMATIC_MODEL_PAPER_ONLY" and BROKER_ORDER_EXECUTION_ENABLED is False,
        }
        evidence = {
            "browser": browser,
            "market_soak": soak,
            "resilience": resilience,
            "recovery": recovery,
            "nse_data": nse,
            "ml_qualification": qualification,
            "forward": forward,
        }
        captured_at = now_iso()
        projected_gates = {**gates, "append_only_proof_projection": True}
        projection_candidate = {
            "build": APP_VERSION,
            "state": "LEVEL5_TARGET_PROOF_COMPLETE" if all(projected_gates.values()) else "TARGET_PROOF_IN_PROGRESS",
            "passed": all(projected_gates.values()),
            "gates": projected_gates,
            "missing_gates": [name for name, value in projected_gates.items() if not value],
            "captured_at": captured_at,
        }
        try:
            projection = Level5QualificationRepository(self.app.store).persist_proof(projection_candidate)
        except Exception as exc:
            projection = {"state": "PROJECTION_FAILED", "persisted": False, "error": f"{type(exc).__name__}: {exc}"[:240]}
        final_gates = {**gates, "append_only_proof_projection": bool(projection.get("persisted"))}
        passed = all(final_gates.values())
        desk_rows = [_map(desks.get(name)) for name in ("delivery", "intraday")]
        progress = {
            "foundation_ready": bool(gates["model_paper_only"] and gates["priority_pipeline_recovered"]),
            "evidence_start_ready": all(bool(row.get("can_train")) for row in desk_rows),
            "forward_clock_running": all(bool(row.get("evidence_clock_eligible")) for row in desk_rows),
            "walk_forward_ready": all(bool(row.get("can_walk_forward")) for row in desk_rows),
            "level5_certified": passed,
        }
        payload = {
            "ok": True,
            "version": self.VERSION,
            "build": APP_VERSION,
            "state": "LEVEL5_TARGET_PROOF_COMPLETE" if passed else "TARGET_PROOF_IN_PROGRESS",
            "passed": passed,
            "progress": progress,
            "maturity_stage": ("LEVEL5_CERTIFIED" if passed else "WALK_FORWARD_QUALIFICATION" if progress["walk_forward_ready"] else "FORWARD_EVIDENCE_ACCUMULATION" if progress["forward_clock_running"] else "SHADOW_EVIDENCE_START" if progress["evidence_start_ready"] else "FOUNDATION_RECOVERY"),
            "gates": final_gates,
            "missing_gates": [name for name, value in final_gates.items() if not value],
            "evidence": evidence,
            "append_only_projection": projection,
            "production_change_allowed": False,
            "broker_authority": "NONE",
            "captured_at": captured_at,
        }
        try:
            self.app.store.set_kv(self.KV_KEY, payload)
        except Exception:
            pass
        return payload
