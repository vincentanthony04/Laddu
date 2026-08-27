"""Cache-only contract for the isolated Quant/AI research plane.

The live service never imports heavy research dependencies.  Installation runs
those probes in the research venv and writes a signed-by-content manifest.  The
operational process reads only that bounded manifest and exposes truthful
readiness without allowing research failures to block quotes, stops or risk.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

CONTRACT_VERSION = "authoritative-quant-research-plane-1.3.0-brand-asset-cycle"
REQUIRED_POLICY = {
    "data_plane_mode": "production",
    "training_source_policy": "PARQUET_DUCKDB_ONLY",
    "operational_authority": "POSTGRESQL",
    "governance_authority": "POSTGRESQL",
    "market_time_series_authority": "QUESTDB",
    "publication_authority": "GOVERNANCE_POSTGRESQL_VIA_LIVE_SERVICE",
    "production_weight_policy": "GOVERNED_ACTIVE_CAP_15_WITH_DETERMINISTIC_FALLBACK",
    "broker_authority": "NONE",
}
REQUIRED_TASKS = (
    "ProjectLaddu-First-Useful-Mode",
    "ProjectLaddu-Premarket-Learning",
    "ProjectLaddu-PostClose-Settlement",
    "ProjectLaddu-NSE-Official-Data",
    "ProjectLaddu-AI-Training",
    "ProjectLaddu-Model-Governance",
    "ProjectLaddu-Brand-Assets",
    "ProjectLaddu-Weekend-Research",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def research_runtime_manifest_path(install_dir: Path) -> Path:
    return Path(install_dir) / "runtime" / "research_runtime.json"


def build_research_plane_status(install_dir: Path) -> dict[str, Any]:
    install_dir = Path(install_dir)
    path = research_runtime_manifest_path(install_dir)
    blockers: list[str] = []
    manifest: dict[str, Any] = {}
    if not path.is_file():
        blockers.append("RESEARCH_RUNTIME_MANIFEST_MISSING")
    else:
        try:
            manifest = json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception as exc:
            blockers.append(f"RESEARCH_RUNTIME_MANIFEST_INVALID:{type(exc).__name__}")
    if manifest:
        if manifest.get("contract_version") != CONTRACT_VERSION:
            blockers.append("RESEARCH_RUNTIME_CONTRACT_VERSION_MISMATCH")
        if str(manifest.get("state") or "").upper() != "READY":
            blockers.append("RESEARCH_RUNTIME_NOT_READY")
        python_path = Path(str(manifest.get("research_python") or ""))
        if not str(python_path) or not python_path.is_file():
            blockers.append("RESEARCH_PYTHON_MISSING")
        policies = dict(manifest.get("policies") or {})
        for key, expected in REQUIRED_POLICY.items():
            if str(policies.get(key) or "") != expected:
                blockers.append(f"RESEARCH_POLICY_MISMATCH:{key}")
        modules = dict(manifest.get("modules") or {})
        missing_modules = sorted(name for name, row in modules.items() if not bool(dict(row or {}).get("ok")))
        if missing_modules:
            blockers.append("RESEARCH_MODULES_UNAVAILABLE:" + ",".join(missing_modules))
        required_tasks = tuple(manifest.get("required_tasks") or ())
        if required_tasks != REQUIRED_TASKS:
            blockers.append("RESEARCH_TASK_CONTRACT_MISMATCH")
        if manifest.get("task_proof_required") is not True:
            blockers.append("RESEARCH_TASK_PROOF_MISSING")
        task_status = dict(manifest.get("task_status") or {})
        for task_name in REQUIRED_TASKS:
            if dict(task_status.get(task_name) or {}).get("ok") is not True:
                blockers.append(f"RESEARCH_TASK_UNAVAILABLE:{task_name}")
    analytics_db = install_dir / "data" / "analytics" / "project_laddu_quant.duckdb"
    lake_manifest = install_dir / "data" / "manifests" / "market-lake.json"
    dataset_state = "READY" if analytics_db.is_file() and lake_manifest.is_file() else "WARMING"
    return {
        "ok": not blockers,
        "contract_version": CONTRACT_VERSION,
        "state": "READY" if not blockers else "BLOCKED",
        "blockers": blockers,
        "manifest_path": str(path),
        "research_python": manifest.get("research_python"),
        "verified_at": manifest.get("verified_at"),
        "modules": manifest.get("modules") or {},
        "policies": manifest.get("policies") or {},
        "required_tasks": list(REQUIRED_TASKS),
        "dataset_state": dataset_state,
        "analytics_db_present": analytics_db.is_file(),
        "market_lake_manifest_present": lake_manifest.is_file(),
        "dataset_note": (
            "Curated Parquet/DuckDB dataset is available."
            if dataset_state == "READY"
            else "Research runtime is ready; curated history remains a truthful warming dependency and cannot be replaced by operational SQLite."
        ),
        "live_safety_boundary": "research failures never block quotes, stops, risk monitoring or governed Intraday mandatory-flat execution",
        "checked_at": _now(),
    }
