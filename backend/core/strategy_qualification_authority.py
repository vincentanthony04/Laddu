"""Governed production qualification gate for heuristic trading mathematics.

Deterministic arithmetic can be certified exactly.  Strategy weights, thresholds,
ATR multipliers, S/R ranking coefficients and setup tolerances are empirical
hypotheses.  This authority prevents those hypotheses from becoming actionable
unless a frozen qualification record proves every required research/forward gate.

The authority does not decide whether a setup is good; it only validates the
qualification evidence that is allowed to give a strategy version non-zero
production influence.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

from core.numeric_semantics import finite_number


class StrategyQualificationAuthority:
    authority = "StrategyQualificationAuthority"
    authority_version = "1.1.0-exact-strategy-contract-bound"
    required_boolean_gates = (
        "point_in_time",
        "purged_walk_forward",
        "embargo",
        "transaction_costs",
        "multiple_testing_control",
        "regime_stability",
        "sensitivity_stability",
        "forward_model_paper",
    )

    @staticmethod
    def _hash(payload: Mapping[str, Any]) -> str:
        raw = json.dumps(dict(payload), sort_keys=True, separators=(",", ":"), allow_nan=False)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @staticmethod
    def _positive_integer(value: Any) -> int | None:
        out = finite_number(value)
        if out is None or not float(out).is_integer() or int(out) <= 0:
            return None
        return int(out)

    @classmethod
    def evaluate(
        cls,
        *,
        mode: str,
        setup: str | None,
        strategy_version: str,
        evidence: Mapping[str, Any] | None,
        current_strategy_contract_hash: str | None = None,
    ) -> dict[str, Any]:
        desk = str(mode or "").strip().lower()
        if desk not in {"intraday", "delivery"}:
            return cls._blocked("unsupported production desk", desk=desk, strategy_version=strategy_version)
        row = dict(evidence or {})
        if not row:
            return cls._blocked("qualification evidence absent", desk=desk, strategy_version=strategy_version)
        if str(row.get("strategy_version") or "") != str(strategy_version):
            return cls._blocked("qualification strategy version mismatch", desk=desk, strategy_version=strategy_version)
        if str(row.get("mode") or "").lower() != desk:
            return cls._blocked("qualification desk mismatch", desk=desk, strategy_version=strategy_version)
        evidence_setup = str(row.get("setup") or "").strip()
        if setup and evidence_setup != str(setup).strip():
            return cls._blocked("qualification setup mismatch", desk=desk, strategy_version=strategy_version)
        expected_contract = str(current_strategy_contract_hash or "").strip().lower()
        evidence_contract = str(row.get("strategy_contract_hash") or "").strip().lower()
        if len(expected_contract) != 64:
            return cls._blocked("current strategy mathematics contract hash missing", desk=desk, strategy_version=strategy_version)
        if len(evidence_contract) != 64 or evidence_contract != expected_contract:
            return cls._blocked("qualification strategy mathematics contract mismatch", desk=desk, strategy_version=strategy_version)
        missing = [gate for gate in cls.required_boolean_gates if row.get(gate) is not True]
        if missing:
            return cls._blocked(
                "qualification gates incomplete: " + ", ".join(missing),
                desk=desk,
                strategy_version=strategy_version,
                missing=missing,
            )
        observations = cls._positive_integer(row.get("forward_observations"))
        trading_days = cls._positive_integer(row.get("forward_trading_days"))
        if observations is None or trading_days is None:
            return cls._blocked("positive integer forward sample and trading-day evidence required", desk=desk, strategy_version=strategy_version)
        frozen_hash = str(row.get("qualification_hash") or "").strip().lower()
        material = {k: row.get(k) for k in sorted(row) if k != "qualification_hash"}
        if len(frozen_hash) != 64 or cls._hash(material) != frozen_hash:
            return cls._blocked("qualification hash missing or invalid", desk=desk, strategy_version=strategy_version)
        return {
            "authority": cls.authority,
            "authority_version": cls.authority_version,
            "state": "QUALIFIED",
            "qualified": True,
            "production_influence": 1,
            "mode": desk,
            "setup": setup,
            "strategy_version": strategy_version,
            "forward_observations": observations,
            "forward_trading_days": trading_days,
            "qualification_hash": frozen_hash,
            "strategy_contract_hash": expected_contract,
            "policy": "heuristic strategy mathematics has zero actionable influence until every empirical gate and the exact strategy mathematics contract are frozen and hash-bound",
        }

    @classmethod
    def _blocked(cls, reason: str, *, desk: str, strategy_version: str, missing: list[str] | None = None) -> dict[str, Any]:
        return {
            "authority": cls.authority,
            "authority_version": cls.authority_version,
            "state": "RESEARCH_ONLY",
            "qualified": False,
            "production_influence": 0,
            "mode": desk or None,
            "strategy_version": strategy_version,
            "reason": reason,
            "missing_gates": list(missing or []),
            "policy": "unqualified weights/thresholds/tolerances cannot admit an actionable trade",
        }


DEFAULT_STRATEGY_QUALIFICATION_AUTHORITY = StrategyQualificationAuthority()
