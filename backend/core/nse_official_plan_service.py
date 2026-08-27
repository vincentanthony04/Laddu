"""Install/upgrade the governed NSE source transport plan without losing operator choices."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from core.storage_layout import atomic_write_json

SERVICE_VERSION = "nse-official-plan-3.1.0-canonical-requiredness"
RETIRED_MANAGED_ARTIFACT_KEYS = {"index_daily_snapshot"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or not isinstance(value.get("artifacts"), list):
        raise ValueError(f"invalid NSE source plan: {path}")
    return value


def _urls(row: Mapping[str, Any]) -> list[str]:
    result = [str(value).strip() for value in (row.get("url_templates") or []) if str(value).strip()]
    legacy = str(row.get("url_template") or "").strip()
    if legacy and legacy not in result:
        result.insert(0, legacy)
    return result


def merge_nse_source_plan(default_path: Path, target_path: Path) -> dict[str, Any]:
    """Merge shipped transports into a retained plan without stale policy capture.

    Operator transport choices may be retained, but shipped artifact ``required`` policy is
    release-governed and always comes from the current package.  A historical target file
    therefore cannot silently promote an optional source into a permanent release blocker.
    Operator-only artifacts remain operator-owned.
    """
    default = _load(Path(default_path))
    target_path = Path(target_path)
    existing = _load(target_path) if target_path.is_file() else {"artifacts": []}
    existing_rows = {
        str(row.get("artifact_key") or row.get("source_key") or "").strip(): deepcopy(row)
        for row in existing.get("artifacts") or []
        if str(row.get("artifact_key") or row.get("source_key") or "").strip()
    }
    merged_rows: list[dict[str, Any]] = []
    inserted = 0
    transports_added = 0
    for shipped in default.get("artifacts") or []:
        key = str(shipped.get("artifact_key") or shipped.get("source_key") or "").strip()
        retained = existing_rows.pop(key, None)
        if retained is None:
            row = deepcopy(shipped)
            row["required_authority"] = SERVICE_VERSION
            merged_rows.append(row)
            inserted += 1
            continue
        merged = deepcopy(shipped)
        for field in ("enabled", "inbox_glob", "filename_template", "lookback_days"):
            if field in retained:
                merged[field] = retained[field]
        merged["required"] = bool(shipped.get("required"))
        merged["required_authority"] = SERVICE_VERSION
        retained_urls = _urls(retained)
        shipped_urls = _urls(shipped)
        if retained_urls:
            merged["url_templates"] = retained_urls + [url for url in shipped_urls if url not in retained_urls]
        elif shipped_urls:
            merged["url_templates"] = shipped_urls
            transports_added += 1
        merged.pop("url_template", None)
        # Preserve unrecognised operator metadata without allowing it to erase defaults.
        for field, value in retained.items():
            if field not in merged and field not in {"url_template", "url_templates"}:
                merged[field] = value
        merged_rows.append(merged)
    # Preserve operator-only artifacts at the end, but fail closed for shipped
    # artifact identities that have been explicitly retired because their prior
    # semantics were unsafe.  The retained row remains visible as forensic
    # configuration evidence; it simply cannot run.
    for key, retained in existing_rows.items():
        row = deepcopy(retained)
        if key in RETIRED_MANAGED_ARTIFACT_KEYS:
            row["enabled"] = False
            row["required"] = False
            row["retired_by"] = SERVICE_VERSION
            row["retired_reason"] = (
                "retired managed artifact: ind_close_all/index-level close evidence "
                "must not populate constituent membership"
            )
        merged_rows.append(row)
    merged = {
        "version": "3",
        "policy": default.get("policy"),
        "managed_by": SERVICE_VERSION,
        "merged_at": _now(),
        "artifacts": merged_rows,
    }
    serialised = json.dumps(merged, indent=2, sort_keys=False, ensure_ascii=False) + "\n"
    previous = target_path.read_text(encoding="utf-8") if target_path.is_file() else None
    changed = previous != serialised
    backup = None
    if changed and previous is not None:
        backup = target_path.with_suffix(target_path.suffix + ".pre-v107.bak")
        if not backup.exists():
            backup.parent.mkdir(parents=True, exist_ok=True)
            backup.write_text(previous, encoding="utf-8")
    target_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(target_path, merged)
    digest = hashlib.sha256(target_path.read_bytes()).hexdigest()
    return {
        "ok": True,
        "version": SERVICE_VERSION,
        "state": "PLAN_UPDATED" if changed else "PLAN_CURRENT",
        "path": str(target_path),
        "backup": str(backup) if backup else None,
        "artifact_count": len(merged_rows),
        "inserted_artifacts": inserted,
        "transports_added": transports_added,
        "active_url_artifacts": sum(1 for row in merged_rows if _urls(row) and row.get("enabled") is not False),
        "sha256": digest,
    }
