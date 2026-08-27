"""Formula-identity governance for the research factor zoo.

A factor that executes is not automatically the published factor it is named
after.  This authority makes that distinction explicit and fail-closed:

* EXACT requires a hash-bound formula-verification record.
* ADAPTED/PROXY are always research-only under the original published name.
* UNVERIFIED is the default, even when runtime/purity/no-lookahead tests pass.

Predictive qualification (IC/IR/WFA/forward evidence) is a separate gate.  A
factor therefore needs BOTH formula identity and empirical qualification before
it can receive non-zero production influence.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Mapping


class FactorFormulaClassificationAuthority:
    authority = "FactorFormulaClassificationAuthority"
    authority_version = "1.0.0-exact-adapted-proxy-unverified"
    allowed = {"EXACT", "ADAPTED", "PROXY", "UNVERIFIED"}

    _proxy_patterns = (
        r"\bproxy\b", r"degraded\s+to", r"not\s+true", r"price[- ]proxy",
        r"approximate(?:d|s|ly)?", r"approximation", r"substitut(?:e|ed|ion)",
    )
    _adapt_patterns = (
        r"\badapt(?:ed|ation|s)?\b", r"window\s+\d+\s*[→>-]\s*\d+",
        r"warmup\s+feasibility", r"benchmark\s+unavailable", r"india[- ]path",
        r"source\s+field.*unavailable",
    )

    @staticmethod
    def _hash(value: Mapping[str, Any]) -> str:
        payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @classmethod
    def infer_research_class(cls, metadata: Mapping[str, Any] | None, source_text: str = "") -> str:
        meta = dict(metadata or {})
        explicit = str(meta.get("formula_class") or meta.get("formula_identity") or "").strip().upper()
        if explicit in cls.allowed:
            # An in-source EXACT label is not sufficient to certify itself.
            return "UNVERIFIED" if explicit == "EXACT" else explicit
        corpus = " ".join(str(meta.get(k) or "") for k in ("notes", "nickname", "formula_latex")) + " " + str(source_text or "")
        lower = corpus.lower()
        if any(re.search(pattern, lower, flags=re.I) for pattern in cls._proxy_patterns):
            return "PROXY"
        if any(re.search(pattern, lower, flags=re.I) for pattern in cls._adapt_patterns):
            return "ADAPTED"
        return "UNVERIFIED"

    @classmethod
    def verification_material(
        cls,
        *,
        factor_name: str,
        family: str,
        published_formula: str,
        implementation_hash: str,
        primary_source_id: str,
        verifier_version: str,
        oracle_cases_hash: str,
    ) -> dict[str, Any]:
        values = {
            "factor_name": str(factor_name).strip(),
            "family": str(family).strip(),
            "published_formula": str(published_formula).strip(),
            "implementation_hash": str(implementation_hash).strip().lower(),
            "primary_source_id": str(primary_source_id).strip(),
            "verifier_version": str(verifier_version).strip(),
            "oracle_cases_hash": str(oracle_cases_hash).strip().lower(),
        }
        if not all(values.values()):
            raise ValueError("complete factor formula verification material is required")
        for key in ("implementation_hash", "oracle_cases_hash"):
            if not re.fullmatch(r"[0-9a-f]{64}", values[key]):
                raise ValueError(f"{key} must be a SHA-256 hex digest")
        return values

    @classmethod
    def certify_exact(cls, **kwargs: Any) -> dict[str, Any]:
        supplied = str(kwargs.pop("verification_hash", "") or "").strip().lower()
        material = cls.verification_material(**kwargs)
        expected = cls._hash(material)
        if supplied != expected:
            return {
                "authority": cls.authority,
                "authority_version": cls.authority_version,
                "formula_class": "UNVERIFIED",
                "formula_identity_verified": False,
                "production_influence": 0,
                "reason": "formula verification hash missing or mismatched",
                "expected_verification_hash": expected,
            }
        return {
            "authority": cls.authority,
            "authority_version": cls.authority_version,
            "formula_class": "EXACT",
            "formula_identity_verified": True,
            "production_influence": 0,  # empirical qualification is a separate gate
            "verification": material,
            "verification_hash": expected,
            "reason": "published formula identity verified; predictive influence remains separately governed",
        }

    @classmethod
    def production_gate(
        cls,
        *,
        formula_evidence: Mapping[str, Any] | None,
        empirical_qualified: bool,
        empirical_qualification_hash: str | None,
    ) -> dict[str, Any]:
        evidence = dict(formula_evidence or {})
        exact = (
            evidence.get("authority") == cls.authority
            and evidence.get("formula_class") == "EXACT"
            and evidence.get("formula_identity_verified") is True
            and bool(evidence.get("verification_hash"))
        )
        empirical = bool(empirical_qualified and str(empirical_qualification_hash or "").strip())
        allowed = exact and empirical
        return {
            "authority": cls.authority,
            "authority_version": cls.authority_version,
            "formula_class": evidence.get("formula_class") or "UNVERIFIED",
            "formula_identity_verified": exact,
            "empirical_qualified": empirical,
            "production_influence": 1 if allowed else 0,
            "state": "PRODUCTION_ELIGIBLE" if allowed else "RESEARCH_ONLY",
        }


DEFAULT_FACTOR_FORMULA_CLASSIFICATION_AUTHORITY = FactorFormulaClassificationAuthority()
