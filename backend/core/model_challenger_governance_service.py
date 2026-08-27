"""Governed readiness rules for statistical and ML challenger models.

The service never trains or promotes a model.  It defines the evidence each
family must provide before it may progress from research to shadow review.
"""
from __future__ import annotations

import importlib.util
from typing import Any, Dict, Mapping, Optional


CHALLENGER_GOVERNANCE_VERSION = "model-challenger-governance-1.2.0-pl26"


FAMILIES: Dict[str, Dict[str, Any]] = {
    "logistic_ridge": {"role": "dependency_free_regularized_shadow_baseline", "package": None, "min_samples": 300, "min_dates": 126, "min_symbols": 20},
    "ridge": {"role": "linear_baseline", "package": "sklearn", "min_samples": 300, "min_dates": 60, "min_symbols": 20},
    "lasso": {"role": "sparse_linear_baseline", "package": "sklearn", "min_samples": 500, "min_dates": 80, "min_symbols": 25},
    "elastic_net": {"role": "regularized_linear_baseline", "package": "sklearn", "min_samples": 500, "min_dates": 80, "min_symbols": 25},
    "lightgbm": {"role": "nonlinear_ranker", "package": "lightgbm", "min_samples": 2500, "min_dates": 180, "min_symbols": 50},
    "catboost": {"role": "nonlinear_ranker", "package": "catboost", "min_samples": 2500, "min_dates": 180, "min_symbols": 50},
    "hist_gradient_boosting": {"role": "nonlinear_tabular_ranker", "package": "sklearn", "min_samples": 2500, "min_dates": 180, "min_symbols": 50},
    "lambdamart": {"role": "cross_sectional_learning_to_rank", "package": "lightgbm", "min_samples": 5000, "min_dates": 220, "min_symbols": 75},
    "meta_label": {"role": "signal_acceptance_and_sizing_only", "package": "sklearn", "min_samples": 1000, "min_dates": 120, "min_symbols": 30},
    "pca": {"role": "diagnostic_or_compression_only", "package": None, "min_samples": 250, "min_dates": 60, "min_symbols": 20},
    "lstm": {"role": "sequence_challenger", "package": "torch", "min_samples": 10000, "min_dates": 300, "min_symbols": 75},
}


def normalize_family(value: Any) -> str:
    text = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "light_gbm": "lightgbm", "lgbm": "lightgbm", "cat_boost": "catboost",
        "lambda_mart": "lambdamart", "learning_to_rank": "lambdamart",
        "meta_labeling": "meta_label", "metalabel": "meta_label",
        "elasticnet": "elastic_net", "principal_component_analysis": "pca",
        "histgradientboosting": "hist_gradient_boosting", "hist_gradient_boosting_regressor": "hist_gradient_boosting",
        "l2_logistic_plus_ridge_return": "logistic_ridge",
    }
    return aliases.get(text, text)


class ModelChallengerGovernanceService:
    """Assess research/shadow readiness using explicit family contracts."""

    @staticmethod
    def installed_packages() -> Dict[str, bool]:
        packages = {item.get("package") for item in FAMILIES.values() if item.get("package")}
        return {package: importlib.util.find_spec(package) is not None for package in sorted(packages)}

    def assess(self, model_spec: Mapping[str, Any], evidence: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
        evidence = dict(evidence or model_spec.get("evidence") or {})
        family = normalize_family(model_spec.get("model_family") or model_spec.get("family") or model_spec.get("framework"))
        if family not in FAMILIES:
            # Infer common framework strings without making unknown frameworks eligible.
            framework = str(model_spec.get("framework") or "").lower()
            compact = framework.replace(" ", "").replace("_", "").replace("-", "")
            family = next((name for name in FAMILIES if name.replace("_", "") in compact), family)
        contract = FAMILIES.get(family)
        if contract is None:
            return self._blocked(family or "unknown", "UNKNOWN_MODEL_FAMILY", ["Declare one governed model_family."], evidence)

        blockers = []
        warnings = []
        samples = int(evidence.get("samples") or 0)
        dates = int(evidence.get("dates") or evidence.get("unique_dates") or 0)
        symbols = int(evidence.get("symbols") or evidence.get("unique_symbols") or 0)
        for key, actual in (("samples", samples), ("dates", dates), ("symbols", symbols)):
            required = int(contract[f"min_{key}"])
            if actual < required:
                blockers.append(f"{key} {actual} < required {required}")

        mandatory_flags = {
            "point_in_time": "Point-in-time feature construction is required.",
            "purged_walk_forward": "Purged walk-forward validation is required.",
            "embargo": "An embargo between train and validation folds is required.",
            "costs_included": "Desk-specific costs and slippage are required.",
            "holdout_untouched": "A final untouched holdout is required.",
            "baseline_comparison": "Comparison with deterministic and regularized-linear baselines is required.",
            "trial_count_recorded": "The experiment/trial count is required for multiple-testing control.",
            "multiple_testing_control": "Multiple testing must be controlled; an uncorrected multi-trial result is blocked.",
            "feature_redundancy_audited": "Factor orthogonalization/redundancy evidence is required.",
        }
        if family == "pca":
            mandatory_flags = {
                "point_in_time": mandatory_flags["point_in_time"],
                "feature_redundancy_audited": mandatory_flags["feature_redundancy_audited"],
            }
        for flag, message in mandatory_flags.items():
            if not bool(evidence.get(flag)):
                blockers.append(message)

        if family in {"lightgbm", "catboost", "lambdamart"}:
            if not evidence.get("cross_sectional_groups"):
                blockers.append("Cross-sectional date groups are required for ranking evaluation.")
            if not evidence.get("rank_metrics"):
                blockers.append("Out-of-sample rank IC/NDCG metrics are required.")
            if not evidence.get("calibrated"):
                warnings.append("Probability/score calibration has not been demonstrated.")
        if family == "lambdamart":
            if not evidence.get("relevance_labels_point_in_time"):
                blockers.append("Point-in-time relevance labels are required for LambdaMART.")
            if not evidence.get("group_sizes_recorded"):
                blockers.append("Per-date query group sizes are required for LambdaMART.")
        if family == "meta_label":
            if not evidence.get("primary_model_id"):
                blockers.append("Meta-labeling requires a frozen primary signal model.")
            if not evidence.get("triple_barrier_labels"):
                blockers.append("Point-in-time triple-barrier or equivalent outcome labels are required.")
            if not evidence.get("direction_generation_disabled"):
                blockers.append("Meta-labeling must not generate trade direction independently.")
            if not evidence.get("probability_calibration"):
                blockers.append("Meta-label acceptance probabilities must be calibrated.")
        if family == "lstm":
            if not evidence.get("sequence_leakage_tested"):
                blockers.append("Sequence-window leakage and boundary tests are required.")
            if not evidence.get("beats_tree_and_linear_baselines"):
                blockers.append("LSTM must beat linear and tree rankers out of sample after costs.")
            if not evidence.get("multiple_seeds_stable"):
                blockers.append("LSTM stability across multiple random seeds is required.")
        if family == "pca":
            warnings.append("PCA components are difficult to explain and may drift; direct signal authority is prohibited.")

        package = contract.get("package")
        installed = True if not package else importlib.util.find_spec(package) is not None
        if package and not installed:
            warnings.append(f"Required isolated research package '{package}' is unavailable; candidate training is blocked without affecting live risk loops.")

        stage = "BLOCKED" if blockers else "RESEARCH_READY"
        if not blockers and bool(evidence.get("shadow_days", 0) >= 20) and bool(evidence.get("shadow_outcomes", 0) >= 30):
            stage = "SHADOW_EVIDENCE_READY"
        return {
            "ok": True,
            "version": CHALLENGER_GOVERNANCE_VERSION,
            "family": family,
            "role": contract["role"],
            "stage": stage,
            "eligible_for_research": not blockers,
            "eligible_for_production": False,
            "package": package,
            "package_installed": installed,
            "minimums": {key: contract[key] for key in ("min_samples", "min_dates", "min_symbols")},
            "observed": {"samples": samples, "dates": dates, "symbols": symbols},
            "blockers": blockers,
            "warnings": warnings,
            "governance": {
                "champion_unchanged": True,
                "automatic_promotion": False,
                "capital_authority": "NONE",
                "approval_required": True,
            },
        }

    def status(self) -> Dict[str, Any]:
        return {
            "ok": True,
            "version": CHALLENGER_GOVERNANCE_VERSION,
            "families": FAMILIES,
            "installed_packages": self.installed_packages(),
            "policy": [
                "Regularized linear models are mandatory baselines.",
                "Tree/ranking models must be validated cross-sectionally by date.",
                "Meta-labeling may accept/reject/size a primary signal but may not invent direction.",
                "LSTM remains a challenger until it beats simpler models after costs and across seeds.",
                "No challenger can modify production automatically.",
            ],
        }

    @staticmethod
    def _blocked(family: str, state: str, blockers, evidence) -> Dict[str, Any]:
        return {
            "ok": False,
            "version": CHALLENGER_GOVERNANCE_VERSION,
            "family": family,
            "stage": state,
            "eligible_for_research": False,
            "eligible_for_production": False,
            "blockers": list(blockers),
            "warnings": [],
            "observed": dict(evidence or {}),
            "governance": {"automatic_promotion": False, "capital_authority": "NONE"},
        }
