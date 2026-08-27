"""Single canonical scoring policy for normalized fundamental dimensions.

Provider adapters may obtain evidence differently (authorized imports, exchange
filings, Upstox fundamentals), but they do not own the final Laddu fundamental
score.  The authority consumes normalized 0..100 dimensions and applies one
versioned policy.  Institutional ownership/flow remains Participation evidence
and is deliberately excluded here to avoid double counting.
"""
from __future__ import annotations

from typing import Any, Mapping

from core.numeric_semantics import finite_number

AUTHORITY_NAME = "FundamentalScoringAuthority"
AUTHORITY_VERSION = "1.1.0-finite-numeric-input"
CORE_WEIGHTS = {"quality": 0.35, "growth": 0.25, "safety": 0.25, "valuation": 0.15}
SECTOR_OVERLAY_WEIGHT = 0.25


class FundamentalScoringAuthority:
    authority = AUTHORITY_NAME
    authority_version = AUTHORITY_VERSION
    weights = CORE_WEIGHTS
    sector_overlay_weight = SECTOR_OVERLAY_WEIGHT

    @staticmethod
    def _number(value: Any) -> float | None:
        out = finite_number(value)
        return max(0.0, min(100.0, out)) if out is not None else None

    def score_dimensions(
        self,
        dimensions: Mapping[str, Any],
        *,
        sector_score: Any = None,
        sector: str | None = None,
    ) -> dict[str, Any]:
        normalized = {name: self._number(dimensions.get(name)) for name in self.weights}
        missing = [name for name, value in normalized.items() if value is None]
        if missing:
            return {
                "ok": False,
                "score": None,
                "state": "incomplete",
                "authority": self.authority,
                "authority_version": self.authority_version,
                "dimensions": normalized,
                "missing_dimensions": missing,
                "reason": "all canonical fundamental dimensions are required: " + ", ".join(missing),
                "score_method": {
                    "weights": dict(self.weights),
                    "sector_overlay_weight": self.sector_overlay_weight,
                    "institutional_evidence": "excluded; owned by Participation authority",
                    "missing_policy": "fail closed; no imputation or weight renormalization",
                },
            }
        core_score = round(sum(normalized[name] * weight for name, weight in self.weights.items()), 1)
        sector_value = self._number(sector_score)
        if sector_value is None:
            final_score = core_score
        else:
            final_score = round(core_score * (1.0 - self.sector_overlay_weight) + sector_value * self.sector_overlay_weight, 1)
        safety = normalized["safety"]
        if safety is not None and safety < 35:
            state = "debt_risk"
        elif final_score >= 72:
            state = "strong"
        elif final_score >= 58:
            state = "acceptable"
        elif final_score >= 45:
            state = "weak_watch"
        else:
            state = "avoid"
        return {
            "ok": True,
            "score": final_score,
            "state": state,
            "universal_score": core_score,
            "sector": sector,
            "sector_score": sector_value,
            "authority": self.authority,
            "authority_version": self.authority_version,
            "dimensions": normalized,
            "score_method": {
                "weights": dict(self.weights),
                "sector_overlay_weight": self.sector_overlay_weight,
                "institutional_evidence": "excluded; owned by Participation authority",
                "missing_policy": "all four core dimensions mandatory; no imputation; fixed weights",
            },
        }


DEFAULT_FUNDAMENTAL_SCORING_AUTHORITY = FundamentalScoringAuthority()
