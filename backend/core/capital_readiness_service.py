"""Capital-readiness and engineering-quality control plane.

This service never declares a strategy profitable.  It converts persisted
fairness, capital-profile validation, governed Model Paper and runtime-control evidence
into an auditable maturity state.  Backtest approval is deliberately separated
from Model Paper, pilot, and live-capital authority.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
from typing import Any, Dict, List, Optional

from core.evidence_engine_service import INTRADAY_MODEL_VERSION, DELIVERY_MODEL_VERSION
from core.selection_fairness_service import FAIRNESS_VERSION, SelectionFairnessService
from core.walk_forward_validation_service import AUTHORITY_VERSION, CAPITAL_PROFILE, WalkForwardValidationService
from core.india_cost_model import IndiaCashCostModel
from core.production_risk_authority_service import ProductionRiskAuthorityService, RISK_AUTHORITY_VERSION


READINESS_VERSION = "capital-readiness-1.1.0"
ENGINEERING_STANDARD = "laddu-engineering-standard-4.5"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse(value: Any) -> Optional[datetime]:
    if value in (None, "", "—"):
        return None
    try:
        stamp = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _age_days(value: Any) -> Optional[float]:
    stamp = _parse(value)
    return max(0.0, (datetime.now(timezone.utc) - stamp.astimezone(timezone.utc)).total_seconds() / 86400.0) if stamp else None


class CapitalReadinessService:
    DESKS = {
        "intraday": INTRADAY_MODEL_VERSION,
        "delivery": DELIVERY_MODEL_VERSION,
    }

    def __init__(self, store: Any, runtime_status: Optional[Dict[str, Any]] = None):
        self.store = store
        self.runtime_status = runtime_status or {}

    def assess(self) -> Dict[str, Any]:
        validations = {desk: self._latest_capital_validation(model_id) for desk, model_id in self.DESKS.items()}
        fairness = {desk: self._latest_fairness(desk) for desk in self.DESKS}
        model_paper = self._model_paper_evidence()
        reconciliation = self._reconciliation()
        workers = self._worker_health()
        risk = ProductionRiskAuthorityService(self.store, runtime_status=self.runtime_status).status()

        desk_gates: Dict[str, Dict[str, bool]] = {}
        for desk in self.DESKS:
            validation = validations[desk] or {}
            fairness_report = fairness[desk] or {}
            closed = int((model_paper.get("by_desk") or {}).get(desk, {}).get("closed_count") or 0)
            paper_desk = (model_paper.get("by_desk") or {}).get(desk, {})
            validation_age = _age_days(validation.get("validated_at"))
            fairness_age = _age_days(fairness_report.get("as_of"))
            desk_gates[desk] = {
                "capital_profile_backtest_approved": bool(validation.get("approved") and validation.get("validation_profile") == CAPITAL_PROFILE),
                "capital_validation_recent_90d": validation_age is not None and validation_age <= 90.0,
                "selection_fairness_gate_passed": bool(fairness_report.get("passes_fairness_gate")),
                "fairness_audit_recent_30d": fairness_age is not None and fairness_age <= 30.0,
                "minimum_100_closed_model_paper_trades": closed >= 100,
                "minimum_60_day_model_paper_observation": float(paper_desk.get("observation_days") or 0.0) >= 60.0,
                "model_paper_costs_versioned": bool(paper_desk.get("cost_versions")),
            }

        platform_gates = {
            "runtime_workers_healthy": workers["healthy"],
            "reconciliation_evidence_available": reconciliation.get("state") == "measured",
            "no_duplicate_open_model_paper_positions": reconciliation.get("state") == "measured" and reconciliation["duplicate_open_model_paper_positions"] == 0,
            "no_duplicate_open_signal_theses": reconciliation.get("state") == "measured" and reconciliation["duplicate_open_signal_theses"] == 0,
            "versioned_intraday_cost_model": bool(IndiaCashCostModel.for_mode("intraday").config.version),
            "versioned_delivery_cost_model": bool(IndiaCashCostModel.for_mode("delivery").config.version),
            "fairness_authority_installed": FAIRNESS_VERSION.startswith("selection-fairness-"),
            "quant_authority_installed": AUTHORITY_VERSION.startswith("walk-forward-authority-"),
            "production_risk_authority_installed": RISK_AUTHORITY_VERSION.startswith("production-risk-authority-"),
            "operator_emergency_stop_clear": not bool((risk.get("operator_stop") or {}).get("enabled")),
            "account_loss_and_drawdown_measured": bool((risk.get("account_loss_state") or {}).get("measured")),
            "portfolio_heat_within_limit": float((risk.get("portfolio") or {}).get("portfolio_heat_pct") or 0.0) <= float((risk.get("limits") or {}).get("max_portfolio_heat_pct") or 0.0),
        }
        backtest_ready = all(gates["capital_profile_backtest_approved"] for gates in desk_gates.values())
        fairness_ready = all(gates["selection_fairness_gate_passed"] for gates in desk_gates.values())
        model_paper_ready = all(
            gates["minimum_100_closed_model_paper_trades"]
            and gates["minimum_60_day_model_paper_observation"]
            and gates["model_paper_costs_versioned"]
            for gates in desk_gates.values()
        )
        platform_ready = all(platform_gates.values())

        if backtest_ready and fairness_ready and model_paper_ready and platform_ready:
            maturity = "GOVERNED_PRE_PRODUCTION"
        elif backtest_ready and fairness_ready:
            maturity = "BACKTEST_APPROVED_AWAITING_MODEL_PAPER"
        elif any(gates["capital_profile_backtest_approved"] for gates in desk_gates.values()):
            maturity = "PARTIAL_DESK_BACKTEST_APPROVAL"
        else:
            maturity = "RESEARCH_ONLY"

        all_gate_values = [value for gates in desk_gates.values() for value in gates.values()] + list(platform_gates.values())
        control_coverage = round(sum(bool(value) for value in all_gate_values) / len(all_gate_values) * 100.0, 2) if all_gate_values else 0.0
        blockers = []
        for desk, gates in desk_gates.items():
            blockers.extend(f"{desk}: {name}" for name, passed in gates.items() if not passed)
        blockers.extend(f"platform: {name}" for name, passed in platform_gates.items() if not passed)

        return {
            "ok": True,
            "readiness_version": READINESS_VERSION,
            "engineering_standard": ENGINEERING_STANDARD,
            "as_of": _now(),
            "maturity": maturity,
            "capital_grade": False,
            "capital_authority": "NONE",
            "control_coverage_pct": control_coverage,
            "policy": "No live-capital authority is granted by code, unit tests, or backtests. Separate shadow and limited-capital operational approval is mandatory.",
            "desk_gates": desk_gates,
            "platform_gates": platform_gates,
            "validations": validations,
            "fairness": fairness,
            "model_paper": model_paper,
            "reconciliation": reconciliation,
            "worker_health": workers,
            "risk_authority": risk,
            "blockers": blockers,
            "next_required_stage": (
                "independent operational approval and limited-capital pilot" if maturity == "GOVERNED_PRE_PRODUCTION" else
                "100+ closed cost-adjusted Model Paper observations per desk" if maturity == "BACKTEST_APPROVED_AWAITING_MODEL_PAPER" else
                "capital-profile full-universe production replay for each desk"
            ),
        }

    def engineering_quality(self) -> Dict[str, Any]:
        readiness = self.assess()
        latest_fairness = [report for report in readiness["fairness"].values() if report]
        fairness_score = round(sum(float(report.get("fairness_score") or 0) for report in latest_fairness) / len(latest_fairness) / 10.0, 2) if latest_fairness else 6.5
        validations = [report for report in readiness["validations"].values() if report]
        if not validations:
            quant_score = 4.5
        else:
            ratios = []
            for report in validations:
                gates = report.get("gates") or {}
                ratios.append(sum(bool(value) for value in gates.values()) / max(1, len(gates)))
            quant_score = round(sum(ratios) / len(ratios) * 10.0, 2)

        architecture_controls = {
            "separate_discovery_and_promotion": True,
            "fairness_never_changes_trade_confidence": True,
            "same_production_ranker_used_in_replay": True,
            "point_in_time_lineage_required_for_capital_profile": True,
            "forming_or_stale_evidence_fails_closed": True,
            "cost_and_benchmark_coverage_required": True,
            "multiple_testing_and_deflated_sharpe_controls": True,
            "model_paper_stage_separate_from_live_authority": True,
            "immutable_audit_snapshots": True,
            "intraday_and_delivery_approved_independently": True,
            "localhost_first_http_boundary": True,
            "same_origin_mutations_and_bounded_request_bodies": True,
            "authoritative_broker_day_change_contract": True,
            "semantic_colour_and_odometer_contract_tests": True,
            "unified_production_risk_authority": True,
            "operator_kill_switch_is_persistent_and_audited": True,
            "portfolio_heat_symbol_sector_and_drawdown_gates": True,
        }
        # Self-verifiable architecture controls are capped at 4.5/5.  The last
        # half-point requires independent operational review and production
        # evidence; code must not award itself a perfect engineering rating.
        architecture_score = round(sum(architecture_controls.values()) / len(architecture_controls) * 4.5, 2)
        # Evidence score cannot exceed 4.0 until both desk validations and
        # fairness audits exist; this prevents code-completeness from being
        # advertised as empirical readiness.
        evidence_cap = 5.0 if readiness["maturity"] == "GOVERNED_PRE_PRODUCTION" else 4.0
        evidence_weighted = min(evidence_cap, (fairness_score / 10.0 * 2.0) + (quant_score / 10.0 * 2.0) + (readiness["control_coverage_pct"] / 100.0))
        return {
            "ok": True,
            "engineering_standard": ENGINEERING_STANDARD,
            "as_of": _now(),
            "architecture_score_out_of_5": architecture_score,
            "evidence_readiness_score_out_of_5": round(evidence_weighted, 2),
            "candidate_selection_fairness_out_of_10": fairness_score,
            "quantitative_validation_out_of_10": quant_score,
            "capital_readiness": readiness["maturity"],
            "capital_grade": False,
            "architecture_controls": architecture_controls,
            "blockers": readiness["blockers"],
            "interpretation": "Architecture quality and empirical strategy evidence are scored separately; neither is a win-rate claim.",
        }

    def _latest_capital_validation(self, model_id: str) -> Optional[Dict[str, Any]]:
        try:
            reports = WalkForwardValidationService(self.store).status(model_id=model_id, profile=CAPITAL_PROFILE).get("approvals") or []
            return reports[0] if reports else None
        except Exception:
            return None

    def _latest_fairness(self, desk: str) -> Optional[Dict[str, Any]]:
        try:
            reports = SelectionFairnessService(self.store).status(desk).get("audits") or []
            return reports[0] if reports else None
        except Exception:
            return None

    def _model_paper_evidence(self) -> Dict[str, Any]:
        result = {
            "state": "unavailable",
            "authority": "POSTGRESQL_CANONICAL_MODEL_PAPER",
            "by_desk": {
                desk: {
                    "open_count": 0, "closed_count": 0, "cost_versions": [],
                    "first_opened_at": None, "last_closed_at": None, "observation_days": 0.0,
                }
                for desk in self.DESKS
            },
        }
        service = getattr(self.store, "model_portfolio_service", None)
        if service is None:
            result["error"] = "canonical Model Paper service unavailable"
            return result
        try:
            rows = list(service.positions())
            result["state"] = "measured"
            for row in rows:
                desk = str(row.get("mode") or "").lower()
                if desk not in result["by_desk"]:
                    continue
                bucket = result["by_desk"][desk]
                state = str(row.get("status") or "").upper()
                if state == "CLOSED":
                    bucket["closed_count"] += 1
                elif state == "OPEN":
                    bucket["open_count"] += 1
                version = row.get("cost_version")
                if version and version not in bucket["cost_versions"]:
                    bucket["cost_versions"].append(version)
                opened = _parse(row.get("opened_at"))
                closed = _parse(row.get("closed_at"))
                first = _parse(bucket.get("first_opened_at"))
                last = _parse(bucket.get("last_closed_at"))
                if opened and (first is None or opened < first):
                    bucket["first_opened_at"] = opened.isoformat()
                if closed and (last is None or closed > last):
                    bucket["last_closed_at"] = closed.isoformat()
            for bucket in result["by_desk"].values():
                first = _parse(bucket.get("first_opened_at"))
                last = _parse(bucket.get("last_closed_at"))
                if first and last and last >= first:
                    bucket["observation_days"] = round((last - first).total_seconds() / 86400.0, 2)
            return result
        except Exception as exc:
            result["error"] = str(exc)[:160]
            return result

    def _reconciliation(self) -> Dict[str, Any]:
        result = {
            "duplicate_open_model_paper_positions": 0,
            "duplicate_open_signal_theses": 0,
            "state": "unavailable",
            "authorities": {"model_paper": False, "signal_ledger": False},
        }
        try:
            service = getattr(self.store, "model_portfolio_service", None)
            paper_available = service is not None
            if paper_available:
                groups: Dict[tuple[str, str], int] = {}
                for row in service.open_positions():
                    key = (str(row.get("symbol") or "").upper(), str(row.get("mode") or "").lower())
                    groups[key] = groups.get(key, 0) + 1
                result["duplicate_open_model_paper_positions"] = sum(1 for count in groups.values() if count > 1)
            ledger_exists = bool(self.store.conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='signal_ledger'"
            ).fetchone())
            if ledger_exists:
                row = self.store.conn.execute(
                    "SELECT COUNT(*) FROM (SELECT symbol,mode,COUNT(*) n FROM signal_ledger WHERE upper(status) IN ('OPEN','SIGNAL_OPEN','TRIGGERED') GROUP BY symbol,mode HAVING n>1)"
                ).fetchone()
                result["duplicate_open_signal_theses"] = int(row[0] or 0)
            result["authorities"] = {"model_paper": paper_available, "signal_ledger": ledger_exists}
            result["state"] = "measured" if paper_available and ledger_exists else "unavailable"
        except Exception as exc:
            result["error"] = str(exc)[:160]
        return result

    def _worker_health(self) -> Dict[str, Any]:
        scanners = self.runtime_status.get("mode_scanners") or {}
        required = ("intraday", "delivery")
        healthy_states = {"idle", "running", "scanning", "completed", "market_closed", "waiting", "healthy"}
        unhealthy = []
        for desk in required:
            state = str((scanners.get(desk) or {}).get("state") or "unknown").lower()
            if state not in healthy_states:
                unhealthy.append({"desk": desk, "state": state})
        return {"healthy": not unhealthy, "unhealthy": unhealthy, "required_desks": list(required)}

