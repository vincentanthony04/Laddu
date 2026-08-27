"""Active high-value research-method responsibilities for both Laddu desks.

This registry is an architectural contract, not a package availability score.
A method appears only when it has a concrete runtime, validation or risk job.
Missing dependencies prevent that method from entering the tournament; they do
not create evidence rows or alter production decisions.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
import importlib.util
from typing import Any, Dict, Iterable

from core.dual_desk_architecture_service import DESKS


SERVICE_VERSION = "active-research-methods-1.1.0-research-runtime-authority"


@dataclass(frozen=True)
class ResearchMethod:
    key: str
    package: str
    import_name: str
    responsibility: str
    lifecycle: str
    modes: tuple[str, ...]
    outputs: tuple[str, ...]
    production_boundary: str


METHODS: tuple[ResearchMethod, ...] = (
    ResearchMethod(
        "point_in_time_factor_engine", "numpy+pandas", "numpy",
        "point-in-time local factor zoo, IC/IR and decay measurement",
        "ACTIVE_RUNTIME", ("intraday", "delivery"),
        ("factor_value", "ic", "ir", "decay", "lookahead_gate"),
        "validated factors only; package presence has zero score",
    ),
    ResearchMethod(
        "ta_feature_projection", "ta", "ta",
        "isolated RSI, MACD, ADX, ATR and Bollinger feature projection",
        "ACTIVE_RUNTIME", ("intraday", "delivery"),
        ("rsi14", "macd_hist", "adx14", "atr14", "bollinger_position"),
        "bounded research projection only",
    ),
    ResearchMethod(
        "sklearn_governed_challenger", "scikit-learn", "sklearn",
        "walk-forward HistGradientBoosting baseline with immutable shadow publication",
        "ACTIVE_VALIDATION", ("intraday", "delivery"),
        ("expected_net_return", "equilibrium_distance", "reversion_probability"),
        "human-governed forward promotion required",
    ),
    ResearchMethod(
        "lightgbm_ranker", "lightgbm", "lightgbm",
        "cross-sectional ranking and horizon-specific return candidates",
        "ACTIVE_VALIDATION", ("intraday", "delivery"),
        ("rank_score", "positive_probability", "expected_net_return"),
        "finite tournament and forward promotion required",
    ),
    ResearchMethod(
        "duckdb_parquet_research_plane", "duckdb", "duckdb",
        "read-only point-in-time Parquet catalogue and reproducible model datasets",
        "ACTIVE_RUNTIME", ("intraday", "delivery"),
        ("dataset_fingerprint", "feature_snapshot", "label_vector", "model_artifact"),
        "offline process only; no operational writer authority",
    ),
)


class ActiveResearchMethodRegistry:
    def status(self, capabilities: Dict[str, Any] | None = None) -> Dict[str, Any]:
        """Report dependency readiness from the isolated research authority.

        The web backend intentionally does not import heavyweight research
        packages.  When the research registry is supplied, its effective
        research_venv result is authoritative; app-Python imports are only a
        compatibility fallback for unit tests and developer environments.
        """
        capabilities = dict(capabilities or {})
        effective = {
            str(row.get("import_name") or ""): dict(row)
            for row in (capabilities.get("libraries") or [])
            if isinstance(row, dict)
        }
        research_python = capabilities.get("research_python")
        methods = []
        by_mode = {mode: [] for mode in DESKS}
        for method in METHODS:
            capability = effective.get(method.import_name)
            if capability is not None:
                installed = capability.get("status") == "installed"
                dependency_runtime = capability.get("runtime") or "research_venv"
                dependency_version = capability.get("version")
            else:
                installed = importlib.util.find_spec(method.import_name) is not None
                dependency_runtime = "app_python_fallback"
                dependency_version = None
            row = {
                **asdict(method),
                "dependency_ready": installed,
                "dependency_runtime": dependency_runtime,
                "dependency_version": dependency_version,
                "admission_state": "READY_FOR_TOURNAMENT" if installed else "DEPENDENCY_NOT_INSTALLED",
                "production_influence": False,
                "reason": (
                    "Concrete responsibility is enabled; outputs still require finite validation."
                    if installed else
                    "Method is absent from scoring until its isolated research dependency is installed."
                ),
            }
            methods.append(row)
            for mode in method.modes:
                by_mode[mode].append(method.key)
        return {
            "ok": True,
            "version": SERVICE_VERSION,
            "principle": "A library must own a measurable job; package presence never creates evidence.",
            "methods": methods,
            "desks": by_mode,
            "research_python": research_python,
            "dependency_authority": "research_venv_registry" if effective else "app_python_fallback",
            "removed": {
                "unowned_dependency_candidates": "TA-Lib, Qlib, statsmodels, arch, skfolio, backtesting.py, pandas-ta-classic and smartmoneyconcepts are removed until an executable reviewed owner exists.",
                "vibe_trading": "No unique measurable model, validation or risk responsibility.",
            },
        }
