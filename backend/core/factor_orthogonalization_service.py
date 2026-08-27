"""Factor redundancy, orthogonalization and PCA diagnostics.

This module is deliberately dependency-light and research-only.  It never
changes production factor weights.  Its job is to expose duplicate factors,
construct a stable orthogonal basis and estimate the effective dimensionality
of a factor panel before a challenger model is trained.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
import statistics
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


ORTHOGONALIZATION_VERSION = "factor-orthogonalization-1.0.0"


def _finite(value: Any) -> Optional[float]:
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def _mean(values: Sequence[float]) -> float:
    return statistics.fmean(values) if values else 0.0


def _std(values: Sequence[float]) -> float:
    return statistics.pstdev(values) if len(values) > 1 else 0.0


def _median(values: Sequence[float]) -> float:
    return statistics.median(values) if values else 0.0


def _corr(left: Sequence[Optional[float]], right: Sequence[Optional[float]]) -> Optional[float]:
    pairs = [(a, b) for a, b in zip(left, right) if a is not None and b is not None]
    if len(pairs) < 3:
        return None
    xs = [float(a) for a, _ in pairs]
    ys = [float(b) for _, b in pairs]
    mx, my = _mean(xs), _mean(ys)
    vx = sum((x - mx) ** 2 for x in xs)
    vy = sum((y - my) ** 2 for y in ys)
    if vx <= 0 or vy <= 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / math.sqrt(vx * vy)


def _normalize(vector: Sequence[float]) -> Tuple[List[float], float]:
    norm = math.sqrt(sum(value * value for value in vector))
    if norm <= 1e-12:
        return [0.0 for _ in vector], 0.0
    return [value / norm for value in vector], norm


def _mat_vec(matrix: Sequence[Sequence[float]], vector: Sequence[float]) -> List[float]:
    return [sum(cell * value for cell, value in zip(row, vector)) for row in matrix]


def _dot(left: Sequence[float], right: Sequence[float]) -> float:
    return sum(a * b for a, b in zip(left, right))


@dataclass(frozen=True)
class FactorPanel:
    names: Tuple[str, ...]
    columns: Dict[str, Tuple[Optional[float], ...]]
    rows: int


class FactorOrthogonalizationService:
    """Audit factor overlap and create deterministic research diagnostics."""

    def panel(self, rows: Iterable[Mapping[str, Any]], factor_names: Optional[Iterable[str]] = None) -> FactorPanel:
        materialized = [dict(row) for row in rows]
        if factor_names is None:
            names = sorted({key for row in materialized for key in row if key not in {"symbol", "date", "timestamp", "label"}})
        else:
            names = sorted({str(name) for name in factor_names if str(name)})
        columns = {
            name: tuple(_finite(row.get(name)) for row in materialized)
            for name in names
        }
        return FactorPanel(tuple(names), columns, len(materialized))

    def audit(
        self,
        rows: Iterable[Mapping[str, Any]],
        *,
        factor_names: Optional[Iterable[str]] = None,
        quality_scores: Optional[Mapping[str, float]] = None,
        max_abs_correlation: float = 0.90,
        max_missing_rate: float = 0.25,
        pca_components: int = 10,
        pca_feature_cap: int = 64,
    ) -> Dict[str, Any]:
        panel = self.panel(rows, factor_names)
        quality = {str(key): abs(float(value)) for key, value in (quality_scores or {}).items() if _finite(value) is not None}
        diagnostics: Dict[str, Dict[str, Any]] = {}
        usable: List[str] = []
        for name in panel.names:
            column = panel.columns[name]
            present = [value for value in column if value is not None]
            missing_rate = 1.0 - (len(present) / panel.rows if panel.rows else 0.0)
            variance = statistics.pvariance(present) if len(present) > 1 else 0.0
            state = "usable"
            if panel.rows < 3:
                state = "insufficient_rows"
            elif missing_rate > max_missing_rate:
                state = "missingness_blocked"
            elif variance <= 1e-16:
                state = "constant"
            else:
                usable.append(name)
            diagnostics[name] = {
                "state": state,
                "present": len(present),
                "missing_rate": missing_rate,
                "variance": variance,
                "quality_score": quality.get(name),
            }

        ordered = sorted(usable, key=lambda name: (-quality.get(name, 0.0), name))
        selected: List[str] = []
        rejected: Dict[str, Dict[str, Any]] = {}
        pairwise: List[Dict[str, Any]] = []
        for name in ordered:
            strongest_name = None
            strongest = 0.0
            for incumbent in selected:
                correlation = _corr(panel.columns[name], panel.columns[incumbent])
                if correlation is None:
                    continue
                pairwise.append({"left": name, "right": incumbent, "correlation": correlation})
                if abs(correlation) > abs(strongest):
                    strongest = correlation
                    strongest_name = incumbent
            if strongest_name is not None and abs(strongest) >= max_abs_correlation:
                rejected[name] = {
                    "state": "redundant",
                    "correlated_with": strongest_name,
                    "correlation": strongest,
                    "threshold": max_abs_correlation,
                }
            else:
                selected.append(name)

        orthogonal = self._orthogonal_basis(panel, selected)
        pca = self._pca(panel, selected[:max(1, int(pca_feature_cap))], components=pca_components)
        redundancy_rate = len(rejected) / len(usable) if usable else 0.0
        return {
            "ok": True,
            "version": ORTHOGONALIZATION_VERSION,
            "rows": panel.rows,
            "factors_total": len(panel.names),
            "factors_usable": len(usable),
            "selected_factors": selected,
            "redundant_factors": rejected,
            "redundancy_rate": redundancy_rate,
            "factor_diagnostics": diagnostics,
            "pairwise_checked": len(pairwise),
            "strongest_correlations": sorted(pairwise, key=lambda row: abs(row["correlation"]), reverse=True)[:25],
            "orthogonal_basis": orthogonal,
            "pca": pca,
            "governance": {
                "research_only": True,
                "production_weight_changes": False,
                "policy": "Correlation pruning and PCA diagnose redundancy; predictive approval still requires point-in-time walk-forward evidence.",
            },
        }

    @staticmethod
    def _imputed_standardized(panel: FactorPanel, name: str) -> List[float]:
        raw = panel.columns[name]
        present = [value for value in raw if value is not None]
        fill = _median(present)
        values = [float(value if value is not None else fill) for value in raw]
        mean, std = _mean(values), _std(values)
        return [(value - mean) / std for value in values] if std > 1e-12 else [0.0 for _ in values]

    def _orthogonal_basis(self, panel: FactorPanel, names: Sequence[str]) -> Dict[str, Any]:
        basis: List[List[float]] = []
        rows: List[Dict[str, Any]] = []
        for name in names:
            original = self._imputed_standardized(panel, name)
            residual = list(original)
            projections: Dict[str, float] = {}
            for prior, vector in zip((item["factor"] for item in rows if item["state"] == "retained"), basis):
                coefficient = _dot(residual, vector)
                projections[str(prior)] = coefficient
                residual = [value - coefficient * component for value, component in zip(residual, vector)]
            normalized, norm = _normalize(residual)
            if norm <= 1e-8:
                rows.append({"factor": name, "state": "linearly_redundant", "residual_norm": norm, "projections": projections})
                continue
            basis.append(normalized)
            rows.append({"factor": name, "state": "retained", "residual_norm": norm, "projections": projections})
        return {
            "state": "MEASURED" if panel.rows >= 3 else "INSUFFICIENT_SAMPLE",
            "rank": len(basis),
            "input_factors": len(names),
            "rows": rows,
        }

    def _pca(self, panel: FactorPanel, names: Sequence[str], *, components: int) -> Dict[str, Any]:
        if panel.rows < 5 or not names:
            return {"state": "INSUFFICIENT_SAMPLE", "components": []}
        matrix = [self._imputed_standardized(panel, name) for name in names]
        m = len(matrix)
        covariance = [[_dot(matrix[i], matrix[j]) / max(1, panel.rows - 1) for j in range(m)] for i in range(m)]
        trace = sum(max(0.0, covariance[i][i]) for i in range(m))
        working = [list(row) for row in covariance]
        eigenvalues: List[float] = []
        loadings: List[Dict[str, float]] = []
        for component in range(min(max(1, int(components)), m)):
            vector = [1.0 / math.sqrt(m) if i == component % m else (0.5 / math.sqrt(m)) for i in range(m)]
            vector, _ = _normalize(vector)
            for _ in range(100):
                candidate, norm = _normalize(_mat_vec(working, vector))
                if norm <= 1e-12:
                    break
                if sum(abs(a - b) for a, b in zip(candidate, vector)) < 1e-9:
                    vector = candidate
                    break
                vector = candidate
            eigenvalue = max(0.0, _dot(vector, _mat_vec(working, vector)))
            if eigenvalue <= 1e-10:
                break
            eigenvalues.append(eigenvalue)
            loadings.append({name: value for name, value in sorted(zip(names, vector), key=lambda item: abs(item[1]), reverse=True)[:12]})
            for i in range(m):
                for j in range(m):
                    working[i][j] -= eigenvalue * vector[i] * vector[j]
        ratios = [value / trace if trace > 0 else 0.0 for value in eigenvalues]
        cumulative: List[float] = []
        running = 0.0
        for ratio in ratios:
            running += ratio
            cumulative.append(running)
        # Participation-ratio effective dimension uses the complete covariance
        # matrix and therefore remains valid even when only the leading PCA
        # components are explicitly extracted. For eigenvalues lambda_i it is
        # (sum lambda_i)^2 / sum(lambda_i^2); the denominator equals the
        # squared Frobenius norm for a symmetric covariance matrix.
        frobenius_sq = sum(cell * cell for row in covariance for cell in row)
        effective_rank = (trace * trace / frobenius_sq) if frobenius_sq > 1e-16 else 0.0
        return {
            "state": "MEASURED",
            "feature_count": m,
            "rows": panel.rows,
            "eigenvalues": eigenvalues,
            "explained_variance_ratio": ratios,
            "cumulative_explained_variance": cumulative,
            "effective_dimension_participation_ratio": effective_rank,
            "top_loadings": loadings,
            "role": "diagnostic_or_compression_benchmark_only",
        }
