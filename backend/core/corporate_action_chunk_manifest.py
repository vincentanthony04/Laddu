"""Durable, resumable manifest for NSE corporate-action range chunks.

The manifest is deliberately file-backed and content-addressed.  Corporate-action
range acquisition is a research/background concern and must be able to checkpoint
progress even while PostgreSQL is temporarily contended.  Each chunk is written
atomically under the existing governed manifests tree; final symbol coverage is
still derived and committed to PostgreSQL only by the corporate-action authority.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable

from core.storage_layout import StorageLayout, atomic_write_json

MANIFEST_VERSION = "corporate-action-chunk-manifest-1.0.0-pl45"
TERMINAL_SUCCESS = frozenset({"PUBLISHED", "EMPTY_VALID"})
RETRYABLE = frozenset({"MISSING", "FAILED_RETRYABLE", "FETCHED", "VALIDATED"})
ALL_STATUSES = frozenset({
    "MISSING", "FETCHED", "VALIDATED", "PUBLISHED", "EMPTY_VALID",
    "FAILED_RETRYABLE", "FAILED_PERMANENT",
})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


class CorporateActionChunkManifest:
    """Atomic file-backed checkpoint authority for one source/range request."""

    def __init__(self, layout: StorageLayout) -> None:
        self.layout = layout
        self.root = layout.manifests_dir / "corporate_actions" / "chunks"
        self.root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def identity(*, source_family: str, exchange: str, range_start: str,
                 range_end: str, request_version: str) -> dict[str, str]:
        return {
            "source_family": str(source_family),
            "exchange": str(exchange).upper(),
            "range_start": str(range_start)[:10],
            "range_end": str(range_end)[:10],
            "request_version": str(request_version),
        }

    def path_for(self, **identity: str) -> Path:
        basis = self.identity(**identity)
        digest = hashlib.sha256(_canonical(basis).encode("utf-8")).hexdigest()[:24]
        return self.root / f"{basis['exchange']}-{basis['range_start']}-{basis['range_end']}-{digest}.json"

    def load(self, **identity: str) -> dict[str, Any]:
        basis = self.identity(**identity)
        path = self.path_for(**basis)
        try:
            row = json.loads(path.read_text(encoding="utf-8-sig"))
            if not isinstance(row, dict):
                raise ValueError("manifest is not an object")
            if any(str(row.get(k) or "") != str(v) for k, v in basis.items()):
                raise ValueError("manifest identity mismatch")
            return row
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return {
                **basis,
                "manifest_version": MANIFEST_VERSION,
                "status": "MISSING",
                "attempt_count": 0,
                "created_at": _now(),
            }

    def write(self, row: dict[str, Any]) -> dict[str, Any]:
        status = str(row.get("status") or "MISSING").upper()
        if status not in ALL_STATUSES:
            raise ValueError(f"Unsupported corporate-action chunk status: {status}")
        basis = self.identity(
            source_family=str(row.get("source_family") or ""),
            exchange=str(row.get("exchange") or ""),
            range_start=str(row.get("range_start") or ""),
            range_end=str(row.get("range_end") or ""),
            request_version=str(row.get("request_version") or ""),
        )
        payload = {
            **dict(row),
            **basis,
            "manifest_version": MANIFEST_VERSION,
            "status": status,
            "updated_at": _now(),
        }
        atomic_write_json(self.path_for(**basis), payload)
        return payload

    def completed(self, row: dict[str, Any]) -> bool:
        status = str(row.get("status") or "").upper()
        if status not in TERMINAL_SUCCESS:
            return False
        if not str(row.get("payload_sha256") or ""):
            return False
        if not row.get("validated_at"):
            return False
        if status == "PUBLISHED" and not row.get("published_at"):
            return False
        raw_path = str(row.get("raw_evidence_path") or "").strip()
        return bool(raw_path and Path(raw_path).is_file())

    @staticmethod
    def retry_due(row: dict[str, Any], *, now: datetime | None = None) -> bool:
        """Return whether the current request-version is eligible for work now.

        Permanent failures stay closed until a new request/parser version creates a
        new manifest identity. Retryable failures honour their durable cooldown so
        the supervisor cannot hammer NSE every cycle.
        """
        status = str(row.get("status") or "MISSING").upper()
        if status in TERMINAL_SUCCESS or status == "FAILED_PERMANENT":
            return False
        retry_at = str(row.get("next_retry_at") or "").strip()
        if not retry_at:
            return True
        try:
            parsed = datetime.fromisoformat(retry_at.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            current = now or datetime.now(timezone.utc)
            return current >= parsed.astimezone(timezone.utc)
        except ValueError:
            # Corrupt retry metadata must never turn into a permanent silent skip.
            return True

    def list_for_range(self, *, source_family: str, exchange: str,
                       request_version: str, requested: Iterable[tuple[str, str]]) -> list[dict[str, Any]]:
        return [self.load(
            source_family=source_family, exchange=exchange,
            range_start=start, range_end=end, request_version=request_version,
        ) for start, end in requested]

    @staticmethod
    def summary(rows: Iterable[dict[str, Any]]) -> dict[str, int]:
        values = [dict(row) for row in rows]
        counts: dict[str, int] = {status.lower(): 0 for status in ALL_STATUSES}
        for row in values:
            key = str(row.get("status") or "MISSING").lower()
            counts[key] = counts.get(key, 0) + 1
        return {
            "requested_chunks": len(values),
            "published_chunks": counts.get("published", 0),
            "empty_valid_chunks": counts.get("empty_valid", 0),
            "failed_retryable_chunks": counts.get("failed_retryable", 0),
            "failed_permanent_chunks": counts.get("failed_permanent", 0),
            "missing_chunks": counts.get("missing", 0),
            "validated_chunks": counts.get("validated", 0),
            "fetched_chunks": counts.get("fetched", 0),
        }
