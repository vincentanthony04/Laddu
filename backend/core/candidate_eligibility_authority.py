"""Single production/research candidate eligibility authority.

Scanners discover candidates and EvidenceEngineService scores them, but neither
is allowed to invent a second definition of an actionable production signal.
This authority owns the deterministic eligibility contract used by persistence,
ledger, Model Paper bridge and exact evidence verification.  Vectorized
screening remains a non-authoritative accelerator and is release-gated by
parity against the exact EvidenceEngineService path.
"""
from __future__ import annotations

from typing import Any, Dict, Optional
import math

from core.numeric_semantics import finite_number
from core.strategy_mathematics_contract_authority import DEFAULT_STRATEGY_MATHEMATICS_CONTRACT_AUTHORITY

from core.production_mode_policy import (
    FINAL_DECISION_PIPELINE_VERSION,
    FINAL_PROMOTION_AUTHORITY,
    POLICY_VERSION,
    is_production_mode,
    require_production_mode,
)


class CandidateEligibilityAuthority:
    authority = "CandidateEligibilityAuthority"
    authority_version = "1.2.0-exact-strategy-contract-required"
    good_fundamentals = {"strong", "acceptable"}

    @staticmethod
    def _number(value: Any) -> float | None:
        return finite_number(value)

    @classmethod
    def _positive(cls, value: Any) -> float | None:
        out = cls._number(value)
        return out if out is not None and out > 0 else None

    @classmethod
    def evaluate(
        cls,
        decision: Dict[str, Any],
        *,
        market_open: Optional[bool] = None,
        require_final_authority: bool = True,
        research_only: bool = False,
    ) -> Dict[str, Any]:
        row = dict(decision or {})
        blockers: list[str] = []
        if not is_production_mode(row.get("mode")):
            blockers.append("UNSUPPORTED_PRODUCTION_MODE")
            return cls._result(False, row, blockers, require_final_authority, research_only)

        mode = require_production_mode(row.get("mode"))
        side = str(row.get("side") or "").upper()
        status = str(row.get("status") or "").upper()
        action = str(row.get("decision") or "").upper()

        if side not in {"LONG", "SHORT"}:
            blockers.append("DIRECTION_UNSUPPORTED")
        if mode == "delivery" and side == "SHORT":
            blockers.append("DELIVERY_LONG_ONLY")
        entry = cls._positive(row.get("entry"))
        target = cls._positive(row.get("target", row.get("t1")))
        stop = cls._positive(row.get("sl", row.get("stop")))
        if entry is None:
            blockers.append("ENTRY_MISSING_OR_INVALID")
        if target is None:
            blockers.append("TARGET_MISSING_OR_INVALID")
        if stop is None:
            blockers.append("STOP_MISSING_OR_INVALID")
        if entry is not None and target is not None and stop is not None:
            if side == "LONG" and not (stop < entry < target):
                blockers.append("LONG_TRADE_GEOMETRY_INVALID")
            if side == "SHORT" and not (target < entry < stop):
                blockers.append("SHORT_TRADE_GEOMETRY_INVALID")
        if not (row.get("trade_map_valid") is True or str(row.get("level_status") or "").lower() == "valid"):
            blockers.append("TRADE_MAP_NOT_VALID")
        if str(row.get("rank_readiness") or "").upper() != "READY":
            blockers.append("EVIDENCE_NOT_READY")

        if research_only:
            # Research publication still requires the exact final decision
            # pipeline identity; only capital approval differs. Unqualified
            # hypotheses may be studied here but cannot own capital admission.
            if status not in {"PROMOTED", "SIGNAL_OPEN", "WATCH"}:
                blockers.append("SOURCE_STATE_NOT_GOVERNED")
        else:
            if status not in {"PROMOTED", "SIGNAL_OPEN"} or action != "TRADE":
                blockers.append("SOURCE_STATE_NOT_ACTIONABLE")
            qualification = row.get("strategy_qualification_state")
            if not isinstance(qualification, dict):
                qualification = row.get("strategy_qualification") if isinstance(row.get("strategy_qualification"), dict) else {}
            strategy_version = str(row.get("strategy_version") or "").strip()
            row_contract = str(row.get("strategy_contract_hash") or "").strip().lower()
            qualification_contract = str(qualification.get("strategy_contract_hash") or "").strip().lower()
            try:
                current_contract = DEFAULT_STRATEGY_MATHEMATICS_CONTRACT_AUTHORITY.current_hash(
                    mode=mode, strategy_version=strategy_version,
                ) if strategy_version else ""
            except Exception:
                current_contract = ""
            if not (
                qualification.get("authority") == "StrategyQualificationAuthority"
                and qualification.get("qualified") is True
                and int(qualification.get("production_influence") or 0) == 1
                and str(qualification.get("state") or "").upper() == "QUALIFIED"
                and len(str(qualification.get("qualification_hash") or "")) == 64
                and len(row_contract) == 64
                and row_contract == qualification_contract == current_contract
            ):
                blockers.append("STRATEGY_EMPIRICAL_QUALIFICATION_REQUIRED")

        if mode == "delivery":
            fundamental_score = cls._number(row.get("fundamental_score"))
            if fundamental_score is None:
                blockers.append("FUNDAMENTAL_SCORE_MISSING_OR_INVALID")
            elif fundamental_score < 58:
                blockers.append("FUNDAMENTAL_SCORE_BELOW_ACCEPTABLE")
            if str(row.get("fundamental_state") or "").lower() not in cls.good_fundamentals:
                blockers.append("FUNDAMENTAL_STATE_NOT_ACCEPTABLE")
        else:
            resolved_open = market_open if market_open is not None else row.get("market_open_at_decision")
            freshness = str(row.get("freshness_state") or row.get("price_freshness") or "").lower()
            candle_state = str(row.get("candle_state") or "").lower()
            if resolved_open is not True:
                blockers.append("INTRADAY_MARKET_NOT_OPEN")
            if freshness not in {"live", "live_current"}:
                blockers.append("INTRADAY_LIVE_PRICE_REQUIRED")
            if candle_state not in {"fresh", "live", "delayed_warning"}:
                blockers.append("INTRADAY_FRESH_COMPLETED_CANDLE_REQUIRED")

        if require_final_authority or research_only:
            if str(row.get("promotion_authority") or "") != FINAL_PROMOTION_AUTHORITY:
                blockers.append("FINAL_PROMOTION_AUTHORITY_MISSING")
            if str(row.get("decision_pipeline_version") or "") != FINAL_DECISION_PIPELINE_VERSION:
                blockers.append("DECISION_PIPELINE_VERSION_MISMATCH")
            if str(row.get("policy_version") or "") != POLICY_VERSION:
                blockers.append("POLICY_VERSION_MISMATCH")
            invariants = row.get("final_promotion_invariants")
            if not isinstance(invariants, dict):
                blockers.append("FINAL_PROMOTION_INVARIANTS_MISSING")
                invariants = {}

            if research_only:
                if str(row.get("risk_admission_state") or "") != "APPROVED_RESEARCH_ONLY":
                    blockers.append("RESEARCH_ONLY_RISK_STATE_REQUIRED")
                if str(row.get("final_decision_state") or "") != "RESEARCH_ONLY":
                    blockers.append("RESEARCH_ONLY_FINAL_STATE_REQUIRED")
                required = (
                    "mode_supported", "evidence_ready", "scoring_normal",
                    "promotion_score_met", "desk_direction_valid",
                    "market_time_valid", "governed_edge_gates_passed",
                )
                if not all(invariants.get(key) is True for key in required):
                    blockers.append("RESEARCH_PROMOTION_INVARIANTS_FAILED")
                if invariants.get("capital_approved") is not False:
                    blockers.append("RESEARCH_MUST_NOT_HAVE_CAPITAL_APPROVAL")
            else:
                if str(row.get("risk_admission_state") or "") != "APPROVED_CAPITAL":
                    blockers.append("CAPITAL_NOT_APPROVED")
                if str(row.get("final_decision_state") or "") != "PROMOTED":
                    blockers.append("FINAL_DECISION_NOT_PROMOTED")
                if invariants.get("passed") is not True:
                    blockers.append("FINAL_PROMOTION_INVARIANTS_FAILED")

        return cls._result(not blockers, row, blockers, require_final_authority, research_only)

    @classmethod
    def _result(
        cls,
        eligible: bool,
        row: Dict[str, Any],
        blockers: list[str],
        require_final_authority: bool,
        research_only: bool,
    ) -> Dict[str, Any]:
        return {
            "eligible": bool(eligible),
            "authority": cls.authority,
            "authority_version": cls.authority_version,
            "mode": str(row.get("mode") or "").lower(),
            "symbol": str(row.get("symbol") or "").upper(),
            "scope": "RESEARCH_PUBLICATION" if research_only else ("FINAL_PRODUCTION_SIGNAL" if require_final_authority else "EVIDENCE_ACTIONABILITY"),
            "blockers": list(dict.fromkeys(blockers)),
            "policy": "Eligibility can only remove candidates; it never increases evidence score, model probability, or risk capacity.",
        }


DEFAULT_CANDIDATE_ELIGIBILITY_AUTHORITY = CandidateEligibilityAuthority()
