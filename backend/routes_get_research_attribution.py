"""Project Laddu GET handler for immutable Research-vs-Final attribution."""
from __future__ import annotations

from routes_get_dependencies import _qint


def r_research_final_attribution(app, qs, q, mode):
    """Immutable Research-vs-Final multidimensional attribution (AC-069)."""
    from core.research_final_attribution_service import ResearchFinalAttributionService

    def _qtext(name: str, default: str = "all") -> str:
        return str(qs.get(name, [default])[0] or default)

    selected_mode = _qtext("desk", mode if mode in {"delivery", "intraday"} else "all").lower()
    return ResearchFinalAttributionService(app.store).report(
        period=_qtext("period", "ALL"),
        desk=selected_mode,
        horizon=_qtext("horizon", "") or None,
        sector=_qtext("sector"),
        regime=_qtext("regime"),
        signal_age_bucket=_qtext("signal_age_bucket"),
        confidence_bucket=_qtext("confidence_bucket"),
        model_version=_qtext("model_version"),
        feature_manifest_hash=_qtext("feature_manifest_hash"),
        arm=_qtext("arm"),
        disposition=_qtext("disposition"),
        promotion_reason=_qtext("promotion_reason"),
        limit=_qint(qs, "limit", 5000, min_val=1, max_val=20000),
    )


def r_final_rolling_performance(app, qs, q, mode):
    """Rolling canonical Final Model Paper performance (AC-071)."""
    from core.final_rolling_performance_service import FinalRollingPerformanceService

    selected_mode = str(qs.get("desk", [mode if mode in {"delivery", "intraday"} else "all"])[0] or "all").lower()
    return FinalRollingPerformanceService(app.store).report(
        period=str(qs.get("period", ["30D"])[0] or "30D"),
        desk=selected_mode,
        limit=_qint(qs, "limit", 1000, min_val=1, max_val=5000),
    )
