"""Pure portfolio sector/correlation exposure authority.

ProductionRiskAuthorityService owns operational data acquisition and sizing.
This authority owns the deterministic exposure thresholds so sector and
correlation policy cannot drift between callers.
"""
from __future__ import annotations

import math
from typing import Any, Dict, Iterable, Mapping


class PortfolioExposureAuthority:
    authority = "PortfolioExposureAuthority"
    authority_version = "1.0.0"

    @staticmethod
    def _num(value: Any, default: float = 0.0) -> float:
        try:
            out = float(value)
            return out if math.isfinite(out) else default
        except (TypeError, ValueError):
            return default

    @classmethod
    def evaluate(
        cls,
        *,
        sector: str,
        capital: Any,
        current_sector_notional: Any,
        current_sector_positions: int,
        proposed_notional: Any,
        correlation_pairs: Iterable[Mapping[str, Any]] | None,
        correlation_measured: bool,
        max_sector_exposure_pct: float,
        max_sector_open_positions: int,
        max_correlation: float,
        max_correlated_positions: int,
    ) -> Dict[str, Any]:
        sector_name = str(sector or "").strip()
        capital_value = cls._num(capital)
        proposed = max(0.0, cls._num(proposed_notional))
        current_notional = max(0.0, cls._num(current_sector_notional))
        projected_notional = current_notional + proposed
        projected_pct = projected_notional / capital_value * 100.0 if capital_value > 0 else float("inf")
        projected_positions = int(current_sector_positions or 0) + (1 if proposed > 0 else 0)

        pairs = []
        for raw in correlation_pairs or []:
            value = cls._num((raw or {}).get("correlation"), default=float("nan"))
            if math.isfinite(value):
                pairs.append({
                    "symbol": str((raw or {}).get("symbol") or "").upper(),
                    "correlation": round(value, 6),
                    "samples": (raw or {}).get("samples"),
                })
        high = [row for row in pairs if abs(row["correlation"]) >= float(max_correlation)]
        hard_blocks: list[str] = []
        capital_blocks: list[str] = []
        if not sector_name:
            capital_blocks.append("sector metadata missing")
        if capital_value <= 0:
            capital_blocks.append("portfolio capital base is unavailable")
        if projected_pct > float(max_sector_exposure_pct) + 1e-9:
            hard_blocks.append(
                f"projected sector exposure {projected_pct:.2f}% exceeds {float(max_sector_exposure_pct):.2f}%"
            )
        if projected_positions > int(max_sector_open_positions):
            hard_blocks.append(
                f"sector position count {projected_positions} exceeds {int(max_sector_open_positions)}"
            )
        if len(high) >= int(max_correlated_positions):
            hard_blocks.append(
                f"{len(high)} open positions have |correlation| >= {float(max_correlation):.2f}"
            )
        if not correlation_measured and projected_positions > 1:
            capital_blocks.append("correlation concentration is not measured")

        return {
            "ok": not hard_blocks,
            "authority": cls.authority,
            "authority_version": cls.authority_version,
            "sector": sector_name or None,
            "projected_sector_notional": round(projected_notional, 2),
            "projected_sector_exposure_pct": round(projected_pct, 4) if math.isfinite(projected_pct) else None,
            "projected_sector_positions": projected_positions,
            "correlation_measured": bool(correlation_measured),
            "correlation_pairs": pairs,
            "highly_correlated": high,
            "highly_correlated_count": len(high),
            "hard_blocks": hard_blocks,
            "capital_blocks": capital_blocks,
            "limits": {
                "max_sector_exposure_pct": float(max_sector_exposure_pct),
                "max_sector_open_positions": int(max_sector_open_positions),
                "max_correlation": float(max_correlation),
                "max_correlated_positions": int(max_correlated_positions),
            },
            "policy": "Exposure authority can only block/reduce admission. Missing correlation proof with existing positions withholds capital approval.",
        }


DEFAULT_PORTFOLIO_EXPOSURE_AUTHORITY = PortfolioExposureAuthority()
