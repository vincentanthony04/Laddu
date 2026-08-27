"""Hash-bound contract for every production strategy mathematics dependency.

Empirical qualification is only valid for the exact mathematics that was
qualified.  A strategy version string alone is not sufficient: scoring logic,
production desk policy, indicator/structure/risk geometry and context math can
change without a human remembering to bump that string.

This authority therefore freezes a deterministic contract that includes the
current strategy version, the full canonical desk policy and SHA-256 hashes of
all source modules that can change actionable strategy mathematics.  Any source
or policy change produces a new contract hash and automatically invalidates old
qualification evidence until the new contract earns fresh WFA/forward proof.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from core.production_mode_policy import policy_for, require_production_mode


class StrategyMathematicsContractAuthority:
    authority = "StrategyMathematicsContractAuthority"
    authority_version = "1.3.0-expanded-final-ranking-risk-dependency-bound"

    # Source files that contain production-influencing strategy mathematics.
    # This is intentionally conservative: even a non-semantic source edit will
    # require requalification rather than risk a silent mathematical change.
    critical_source_files = (
        "engines.py",
        "market_layers.py",
        "core/production_mode_policy.py",
        "core/numeric_semantics.py",
        "actionability.py",
        "core/decision_engine_service.py",
        "core/production_ranking_service.py",
        "core/production_risk_authority_service.py",
        "core/production_decision_math_service.py",
        "core/promotion_math_service.py",
        "core/opportunity_scoring_service.py",
        "core/ai_governance_service.py",
        "core/evidence_engine_service.py",
        "core/canonical_candle_projection_service.py",
        "core/trading_session_authority.py",
        "core/candle_freshness_service.py",
        "core/completeness_freshness_authority.py",
        "core/indicator_snapshot_authority.py",
        "core/market_level_service.py",
        "core/session_vwap_authority.py",
        "core/intraday_session_structure_authority.py",
        "core/participation_evidence_authority.py",
        "core/compression_expansion_authority.py",
        "core/institutional_signal_service.py",
        "core/market_sector_context_analysis_authority.py",
        "core/fundamental_scoring_authority.py",
        "core/trade_geometry_authority.py",
        "core/structural_trade_map_service.py",
        "core/india_cost_model.py",
        "core/risk_admission_and_sizing_authority.py",
        "core/calibrated_edge_service.py",
        "core/execution_quality_service.py",
        "core/event_risk_policy_service.py",
        "core/performance_drift_guard_service.py",
        "core/evidence_score_validation_service.py",
        "core/strategy_qualification_authority.py",
        "core/candidate_eligibility_authority.py",
    )

    @classmethod
    def _backend_root(cls) -> Path:
        return Path(__file__).resolve().parents[1]

    @staticmethod
    def _hash_bytes(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    @staticmethod
    def _canon_hash(value: Mapping[str, Any]) -> str:
        raw = json.dumps(dict(value), sort_keys=True, separators=(",", ":"), allow_nan=False, ensure_ascii=True)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @classmethod
    def source_hashes(cls) -> dict[str, str]:
        root = cls._backend_root()
        hashes: dict[str, str] = {}
        for rel in cls.critical_source_files:
            path = root / rel
            if not path.is_file():
                raise RuntimeError(f"strategy mathematics source missing: {rel}")
            hashes[rel] = cls._hash_bytes(path.read_bytes())
        return hashes

    @classmethod
    def build(cls, *, mode: str, strategy_version: str) -> dict[str, Any]:
        desk = require_production_mode(mode)
        version = str(strategy_version or "").strip()
        if not version:
            raise ValueError("strategy_version is required")
        material = {
            "authority": cls.authority,
            "authority_version": cls.authority_version,
            "mode": desk,
            "strategy_version": version,
            "production_policy": policy_for(desk).to_dict(),
            "critical_source_hashes": cls.source_hashes(),
        }
        contract_hash = cls._canon_hash(material)
        return {
            **material,
            "strategy_contract_hash": contract_hash,
            "qualification_rule": "any source/policy change invalidates prior empirical qualification",
        }

    @classmethod
    def current_hash(cls, *, mode: str, strategy_version: str) -> str:
        return str(cls.build(mode=mode, strategy_version=strategy_version)["strategy_contract_hash"])

    @classmethod
    def matches_current(cls, *, mode: str, strategy_version: str, contract_hash: Any) -> bool:
        supplied = str(contract_hash or "").strip().lower()
        return len(supplied) == 64 and supplied == cls.current_hash(mode=mode, strategy_version=strategy_version)


DEFAULT_STRATEGY_MATHEMATICS_CONTRACT_AUTHORITY = StrategyMathematicsContractAuthority()
