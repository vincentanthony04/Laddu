"""Factor redundancy governance for the production research registry.

The service never deletes factor implementations. It classifies redundant
outputs, preserves provenance, and prevents aliases from being treated as
independent production evidence.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    import pandas as pd

from core.factors.factor_store import ensure_factor_tables

DEDUP_VERSION = "factor-dedup-1.0.0"
DEFAULT_THRESHOLD = 0.98
DEFAULT_MIN_OVERLAP = 60
MANIFEST_PATH = Path(__file__).resolve().parent / "factors" / "factor_redundancy_manifest.json"


def _require_pandas():
    """Load pandas only for offline correlation audits.

    Production startup, static-manifest enforcement, and factor authority must
    remain available on the lightweight runtime used by the Windows service.
    """
    try:
        import pandas as pd
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "Factor correlation audit requires the isolated research-runtime dependency pandas. "
            "Re-run the single INSTALL_UPDATE.cmd complete-build transaction before requesting a live dedup audit."
        ) from exc
    return pd


def load_static_manifest() -> Dict[str, Any]:
    try:
        data = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, json.JSONDecodeError):
        return {}


def static_redundancy_for(factor_name: str) -> Optional[Dict[str, Any]]:
    return (load_static_manifest().get("redundant_factors") or {}).get(str(factor_name or "").strip())


class FactorDedupService:
    def __init__(self, store: Any = None):
        self.store = store

    @staticmethod
    def _quality(row: Mapping[str, Any]) -> tuple[float, float, str]:
        status = str(row.get("status") or "").lower()
        alive_bonus = 1.0 if status == "alive" else 0.0
        try:
            ir = abs(float(row.get("ir_score") or 0.0))
        except (TypeError, ValueError):
            ir = 0.0
        try:
            ic = abs(float(row.get("ic_score") or 0.0))
        except (TypeError, ValueError):
            ic = 0.0
        return alive_bonus, ir + ic / 10.0, str(row.get("factor_name") or "")

    def audit_frame(
        self,
        frame: pd.DataFrame,
        *,
        registry_rows: Iterable[Mapping[str, Any]] = (),
        threshold: float = DEFAULT_THRESHOLD,
        min_overlap: int = DEFAULT_MIN_OVERLAP,
    ) -> Dict[str, Any]:
        """Greedy correlation pruning using IC/IR quality to choose canonical factors."""
        pd = _require_pandas()
        if frame is None or frame.empty:
            return {
                "ok": False,
                "state": "INSUFFICIENT_DATA",
                "version": DEDUP_VERSION,
                "threshold": threshold,
                "reason": "no factor-value panel",
                "canonical_factors": [],
                "redundant_factors": {},
            }
        numeric = frame.apply(pd.to_numeric, errors="coerce")
        usable = [name for name in numeric.columns if numeric[name].count() >= int(min_overlap) and numeric[name].std(skipna=True) not in (0, None)]
        if len(usable) < 2:
            return {
                "ok": False,
                "state": "INSUFFICIENT_DATA",
                "version": DEDUP_VERSION,
                "threshold": threshold,
                "reason": "fewer than two usable factors",
                "canonical_factors": usable,
                "redundant_factors": {},
            }
        metadata = {str(row.get("factor_name")): dict(row) for row in registry_rows}
        ordered = sorted(
            usable,
            key=lambda name: self._quality(metadata.get(name, {"factor_name": name})),
            reverse=True,
        )
        corr = numeric[usable].corr(min_periods=int(min_overlap))
        selected: list[str] = []
        redundant: Dict[str, Dict[str, Any]] = {}
        pairwise_checked = 0
        for name in ordered:
            strongest_name = None
            strongest = 0.0
            for canonical in selected:
                value = corr.at[name, canonical]
                if pd.isna(value):
                    continue
                pairwise_checked += 1
                if abs(float(value)) > abs(strongest):
                    strongest = float(value)
                    strongest_name = canonical
            if strongest_name is not None and abs(strongest) >= float(threshold):
                redundant[name] = {
                    "status": "REDUNDANT",
                    "canonical_factor_name": strongest_name,
                    "correlation": strongest,
                    "relationship": "inverse" if strongest < 0 else "same",
                }
            else:
                selected.append(name)
        return {
            "ok": True,
            "state": "MEASURED",
            "version": DEDUP_VERSION,
            "threshold": float(threshold),
            "min_overlap": int(min_overlap),
            "sample_rows": int(len(numeric)),
            "factors_total": int(len(frame.columns)),
            "factors_usable": int(len(usable)),
            "canonical_factors": selected,
            "redundant_factors": redundant,
            "redundant_count": len(redundant),
            "redundancy_rate": round(len(redundant) / len(usable), 6) if usable else 0.0,
            "pairwise_checked": pairwise_checked,
            "governance": "Redundant factors remain available for provenance but are excluded from production factor authority.",
        }

    def audit_store(
        self,
        *,
        threshold: float = DEFAULT_THRESHOLD,
        min_overlap: int = DEFAULT_MIN_OVERLAP,
        persist: bool = True,
    ) -> Dict[str, Any]:
        if self.store is None:
            raise ValueError("store is required")
        conn = self.store.conn
        ensure_factor_tables(conn)
        pd = _require_pandas()
        rows = conn.execute(
            "SELECT symbol, date, factor_name, value FROM factor_values WHERE value IS NOT NULL"
        ).fetchall()
        if not rows:
            return self.audit_frame(pd.DataFrame(), threshold=threshold, min_overlap=min_overlap)
        frame = pd.DataFrame(rows, columns=("symbol", "date", "factor_name", "value")).pivot_table(
            index=["date", "symbol"], columns="factor_name", values="value", aggfunc="last"
        )
        registry = [dict(row) for row in conn.execute(
            "SELECT factor_name,family,ic_score,ir_score,status,last_validated FROM factor_registry"
        ).fetchall()]
        report = self.audit_frame(frame, registry_rows=registry, threshold=threshold, min_overlap=min_overlap)
        if persist and report.get("ok"):
            self.persist_report(conn, report)
        return report

    @staticmethod
    def persist_report(conn, report: Mapping[str, Any]) -> None:
        ensure_factor_tables(conn)
        measured_at = datetime.now(timezone.utc).isoformat()
        version = str(report.get("version") or DEDUP_VERSION)
        redundant = report.get("redundant_factors") or {}
        canonical = set(report.get("canonical_factors") or [])
        with conn:
            for name in canonical:
                conn.execute(
                    """UPDATE factor_registry SET redundancy_status='CANONICAL',
                       canonical_factor_name=?, redundancy_correlation=1.0,
                       dedup_version=?, dedup_measured_at=? WHERE factor_name=?""",
                    (name, version, measured_at, name),
                )
            for name, detail in redundant.items():
                conn.execute(
                    """UPDATE factor_registry SET redundancy_status='REDUNDANT',
                       canonical_factor_name=?, redundancy_correlation=?,
                       dedup_version=?, dedup_measured_at=? WHERE factor_name=?""",
                    (
                        detail.get("canonical_factor_name"),
                        detail.get("correlation"),
                        version,
                        measured_at,
                        name,
                    ),
                )

    def apply_static_manifest(self) -> Dict[str, Any]:
        """Apply the two-panel stable synthetic audit to rows already registered."""
        if self.store is None:
            raise ValueError("store is required")
        manifest = load_static_manifest()
        redundant = manifest.get("redundant_factors") or {}
        conn = self.store.conn
        ensure_factor_tables(conn)
        measured_at = datetime.now(timezone.utc).isoformat()
        changed = 0
        with conn:
            for name, detail in redundant.items():
                cur = conn.execute(
                    """UPDATE factor_registry SET redundancy_status='REDUNDANT',
                       canonical_factor_name=?, redundancy_correlation=?,
                       dedup_version=?, dedup_measured_at=? WHERE factor_name=?""",
                    (
                        detail.get("canonical"), detail.get("correlation"),
                        manifest.get("version"), measured_at, name,
                    ),
                )
                changed += int(cur.rowcount or 0)
        return {
            "ok": True,
            "version": manifest.get("version"),
            "manifest_redundant": len(redundant),
            "registry_rows_changed": changed,
            "threshold": manifest.get("threshold"),
            "panels": manifest.get("panels"),
        }

    def status(self) -> Dict[str, Any]:
        manifest = load_static_manifest()
        result = {
            "ok": True,
            "service_version": DEDUP_VERSION,
            "static_manifest": {
                "version": manifest.get("version"),
                "threshold": manifest.get("threshold"),
                "panels": manifest.get("panels"),
                "pair_count": manifest.get("pair_count"),
                "redundant_count": manifest.get("redundant_count"),
            },
        }
        if self.store is not None:
            ensure_factor_tables(self.store.conn)
            rows = self.store.conn.execute(
                """SELECT redundancy_status, COUNT(*) FROM factor_registry
                   GROUP BY redundancy_status ORDER BY redundancy_status"""
            ).fetchall()
            result["registry_counts"] = {str(state): int(count) for state, count in rows}
        return result
