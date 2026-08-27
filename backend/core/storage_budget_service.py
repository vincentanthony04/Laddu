from __future__ import annotations

"""Non-destructive Project Laddu storage budget authority.

The Windows Docker Desktop WSL data disk may grow much larger than the live
Docker objects because a dynamic VHDX retains previously allocated blocks.  A
trading workstation must make that pressure visible before it affects the SSD
or UI latency.  This service *observes only*; it never prunes volumes, deletes
PostgreSQL/QuestDB data, or runs Docker maintenance from an HTTP request.
"""

import os
from pathlib import Path
from typing import Any, Dict

VERSION = "storage-budget-authority-1.0.0"
TARGET_GB = 25.0
WARNING_GB = 20.0
CRITICAL_GB = 23.0


def _docker_vhdx_path() -> Path | None:
    override = str(os.getenv("PROJECT_LADDU_DOCKER_VHDX", "") or "").strip()
    if override:
        return Path(override)
    local = str(os.getenv("LOCALAPPDATA", "") or "").strip()
    if not local:
        return None
    return Path(local) / "Docker" / "wsl" / "disk" / "docker_data.vhdx"


class StorageBudgetService:
    VERSION = VERSION

    @staticmethod
    def snapshot() -> Dict[str, Any]:
        path = _docker_vhdx_path()
        size_gb = None
        exists = False
        error = None
        if path is not None:
            try:
                exists = path.exists()
                if exists:
                    size_gb = round(path.stat().st_size / (1024 ** 3), 2)
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"[:240]
        if size_gb is None:
            state = "UNAVAILABLE"
        elif size_gb >= CRITICAL_GB:
            state = "CRITICAL"
        elif size_gb >= WARNING_GB:
            state = "WARNING"
        else:
            state = "HEALTHY"
        return {
            "version": VERSION,
            "state": state,
            "docker_vhdx": {
                "available": bool(exists and size_gb is not None),
                "size_gb": size_gb,
                "path": str(path) if path else None,
                "error": error,
            },
            "budget": {
                "target_gb": TARGET_GB,
                "warning_gb": WARNING_GB,
                "critical_gb": CRITICAL_GB,
                "policy": "WARN_AND_MAINTAIN_NOT_HARD_QUOTA",
            },
            "safe_policy": {
                "automatic_volume_prune": False,
                "automatic_system_prune_with_volumes": False,
                "authoritative_databases_protected": True,
                "maintenance_trigger": "VHDX_WARNING_OR_DISPROPORTIONATE_TO_LIVE_DOCKER_USAGE",
            },
        }
