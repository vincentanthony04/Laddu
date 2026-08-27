from __future__ import annotations

"""One-time startup guard for retained production lake safety metadata.

The historical lake manifest may predate the four-plane production architecture
and therefore contain a blank ``operational_prune`` object.  In production there
is no operational SQLite authority eligible for pruning: PostgreSQL owns
business state, QuestDB owns recent market time-series and Parquet/DuckDB owns
retained history/research.  This service records only that no-prune fact.  It
never invents reconciliation completion and never removes retained data.
"""

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Dict

from core.storage_layout import StorageLayout, atomic_write_json, interprocess_lock


PRODUCTION_NO_PRUNE_STATE = "NOT_APPLICABLE_PRODUCTION_DATA_PLANE"
AUTHORITY = "PRODUCTION_DATA_PLANE"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class ProductionLakeSafetyManifestService:
    VERSION = "production-lake-safety-manifest-1.0.0"

    def __init__(self, data_dir: Path | str):
        self.layout = StorageLayout.from_data_dir(Path(data_dir))

    @staticmethod
    def _read(path: Path) -> Dict[str, Any]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return payload if isinstance(payload, dict) else {}
        except (OSError, ValueError, TypeError):
            return {}

    @staticmethod
    def _valid(prune: Any) -> bool:
        return bool(
            isinstance(prune, dict)
            and str(prune.get("state") or "").strip() == PRODUCTION_NO_PRUNE_STATE
            and str(prune.get("authority") or "").strip() == AUTHORITY
        )

    def ensure(self) -> Dict[str, Any]:
        self.layout.ensure()
        path = self.layout.manifests_dir / "market-lake.json"
        if not path.exists():
            return {
                "ok": True,
                "state": "MANIFEST_NOT_PRESENT",
                "changed": False,
                "manifest": str(path),
                "version": self.VERSION,
            }
        lock_path = self.layout.locks_dir / "analytical-pipeline.lock"
        try:
            with interprocess_lock(lock_path, timeout_seconds=5.0):
                payload = self._read(path)
                if not payload:
                    return {
                        "ok": False,
                        "state": "MANIFEST_INVALID",
                        "changed": False,
                        "manifest": str(path),
                        "version": self.VERSION,
                    }
                prior = payload.get("operational_prune")
                if self._valid(prior):
                    return {
                        "ok": True,
                        "state": PRODUCTION_NO_PRUNE_STATE,
                        "changed": False,
                        "manifest": str(path),
                        "evidence": dict(prior),
                        "version": self.VERSION,
                    }
                payload["operational_prune"] = {
                    "state": PRODUCTION_NO_PRUNE_STATE,
                    "reason": "PostgreSQL, QuestDB and Parquet/DuckDB are authoritative; no operational SQLite authority is eligible for prune",
                    "authority": AUTHORITY,
                    "verified_at": _now(),
                    "source": self.VERSION,
                }
                # Preserve reconciliation exactly as retained. A blank or old
                # reconciliation remains blank/old and cannot be upgraded by
                # this no-prune safety assertion.
                atomic_write_json(path, payload)
                return {
                    "ok": True,
                    "state": PRODUCTION_NO_PRUNE_STATE,
                    "changed": True,
                    "manifest": str(path),
                    "evidence": dict(payload["operational_prune"]),
                    "version": self.VERSION,
                }
        except TimeoutError as exc:
            return {
                "ok": False,
                "state": "MANIFEST_BUSY",
                "changed": False,
                "manifest": str(path),
                "error": str(exc)[:200],
                "version": self.VERSION,
            }
