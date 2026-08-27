"""Versioned, snapshot-bound scanner progress checkpoints.

Scanner progress is operational state, not historical evidence.  A checkpoint
may be restored only when it belongs to the exact current desk snapshot.  Any
legacy or mismatched checkpoint is overwritten with a clean checkpoint before
it can reach the customer-facing progress contract.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Mapping

CHECKPOINT_VERSION = "scan-checkpoint-v123.0.0-authoritative-desk-population"
PROGRESS_CONTRACT_VERSION = "scanner-progress-contract-3.0.0"


class ScanCheckpointService:
    def __init__(self, store, *, event=None):
        self.store = store
        self.event = event

    @staticmethod
    def key(mode: str, lane: str) -> str:
        return f"scan_checkpoint:{str(mode).lower()}:{str(lane).lower()}"

    @staticmethod
    def universe_fingerprint(universe: Iterable[Dict[str, Any]]) -> str:
        identities = []
        for row in universe or []:
            identity = str(
                row.get("instrument_key")
                or row.get("trading_symbol")
                or row.get("symbol")
                or ""
            ).strip().upper()
            if identity:
                identities.append(identity)
        raw = "\n".join(identities).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    @staticmethod
    def _identity(expected: Mapping[str, Any] | None) -> Dict[str, Any]:
        expected = dict(expected or {})
        return {
            "snapshot_id": str(expected.get("snapshot_id") or ""),
            "content_hash": str(expected.get("content_hash") or ""),
            "population_count": max(0, int(expected.get("population_count") or 0)),
            "universe_revision": str(expected.get("universe_revision") or ""),
        }

    @staticmethod
    def _bounded_int(value: Any, *, lower: int = 0, upper: int | None = None) -> int:
        try:
            number = int(value or 0)
        except (TypeError, ValueError):
            number = 0
        number = max(lower, number)
        if upper is not None:
            number = min(upper, number)
        return number

    @classmethod
    def _validate(cls, payload: Mapping[str, Any], expected: Mapping[str, Any]) -> tuple[bool, str]:
        identity = cls._identity(expected)
        if not isinstance(payload, Mapping):
            return False, "MISSING_CHECKPOINT"
        if str(payload.get("version") or "") != CHECKPOINT_VERSION:
            return False, "LEGACY_CHECKPOINT_VERSION"
        for field in ("snapshot_id", "content_hash", "universe_revision"):
            if str(payload.get(field) or "") != identity[field]:
                return False, f"SNAPSHOT_{field.upper()}_MISMATCH"
        population = identity["population_count"]
        if population <= 0 or int(payload.get("population_count") or 0) != population:
            return False, "SNAPSHOT_POPULATION_MISMATCH"
        for field in (
            "cursor", "sweep_scanned", "sweep_attempted", "sweep_returned",
            "sweep_verified", "sweep_missing", "sweep_unverified",
        ):
            value = cls._bounded_int(payload.get(field), upper=population)
            try:
                raw = int(payload.get(field) or 0)
            except (TypeError, ValueError):
                raw = 0
            if raw != value:
                return False, f"OUT_OF_RANGE_{field.upper()}"
        last = dict(payload.get("last_completed_sweep") or {})
        if last:
            last_population = int(last.get("universe_size") or population)
            last_scanned = cls._bounded_int(last.get("scanned"), upper=population)
            if last_population != population or int(last.get("scanned") or 0) != last_scanned:
                return False, "INCOMPATIBLE_LAST_COMPLETED_SWEEP"
        last_cycle = dict(payload.get("last_completed") or {})
        for field in ("attempted", "scanned", "returned", "verified", "missing", "unverified"):
            if field not in last_cycle:
                continue
            raw = cls._bounded_int(last_cycle.get(field), upper=None)
            if raw > population:
                return False, f"OUT_OF_RANGE_LAST_COMPLETED_{field.upper()}"
        if last_cycle.get("universe_size") is not None and int(last_cycle.get("universe_size") or 0) != population:
            return False, "INCOMPATIBLE_LAST_COMPLETED_POPULATION"
        return True, "VERIFIED"

    def _clean_payload(
        self,
        mode: str,
        lane: str,
        expected: Mapping[str, Any],
        *,
        reason: str,
    ) -> Dict[str, Any]:
        identity = self._identity(expected)
        return {
            "version": CHECKPOINT_VERSION,
            "progress_contract_version": PROGRESS_CONTRACT_VERSION,
            "mode": str(mode).lower(),
            "lane": str(lane).lower(),
            **identity,
            "universe_size": identity["population_count"],
            "cursor": 0,
            "sweep_number": 1,
            "sweep_complete": False,
            "coverage_pct": 0.0,
            "verified_pct": 0.0,
            "sweep_scanned": 0,
            "sweep_attempted": 0,
            "sweep_returned": 0,
            "sweep_verified": 0,
            "sweep_missing": 0,
            "sweep_unverified": 0,
            "last_completed": None,
            "last_completed_sweep": None,
            "reset_reason": reason,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

    def _remove_obsolete_keys(self, active_key: str) -> None:
        deleter = getattr(self.store, "delete_kv", None)
        if not callable(deleter):
            return
        obsolete = [
            "scan_cursor_intraday",
            "scan_cursor_delivery",
            "scan_checkpoint:intraday:analysis",
            "scan_checkpoint:delivery:coverage",
        ]
        for obsolete_key in obsolete:
            if obsolete_key == active_key:
                continue
            try:
                deleter(obsolete_key)
            except Exception:
                pass

    def reconcile(
        self,
        mode: str,
        lane: str,
        *,
        expected: Mapping[str, Any],
    ) -> Dict[str, Any]:
        """Restore only an exact snapshot match; otherwise overwrite old state."""
        key = self.key(mode, lane)
        self._remove_obsolete_keys(key)
        payload = self.store.get_kv(key, {}) or {}
        valid, reason = self._validate(payload, expected)
        if valid:
            return {
                "state": "RESTORED_VERIFIED",
                "reason": reason,
                "checkpoint": dict(payload),
            }
        clean = self._clean_payload(mode, lane, expected, reason=reason)
        deleter = getattr(self.store, "delete_kv", None)
        if callable(deleter):
            try:
                deleter(key)
            except Exception:
                pass
        self.store.set_kv(key, clean)
        # Compatibility stores without delete support are neutralised.
        if not callable(deleter) and str(mode).lower() == "delivery":
            self.store.set_kv("scan_cursor_delivery", 0)
        if callable(self.event):
            try:
                self.event(
                    "INFO",
                    "scan_checkpoint",
                    "Discarded incompatible scanner progress checkpoint",
                    {
                        "mode": str(mode).lower(),
                        "lane": str(lane).lower(),
                        "reason": reason,
                        "snapshot_id": clean["snapshot_id"],
                        "population_count": clean["population_count"],
                    },
                )
            except Exception:
                pass
        return {
            "state": "RESET_INCOMPATIBLE",
            "reason": reason,
            "checkpoint": clean,
        }

    def persist(
        self,
        mode: str,
        lane: str,
        state: Dict[str, Any],
        *,
        universe: Iterable[Dict[str, Any]] = (),
        identity: Mapping[str, Any] | None = None,
    ) -> Dict[str, Any]:
        identity_row = self._identity(identity)
        population = identity_row["population_count"] or max(0, int(state.get("universe_size") or 0))
        identity_row["population_count"] = population
        allowed = {
            "cursor", "universe_size", "sweep_number", "sweep_complete", "coverage_pct", "verified_pct",
            "sweep_scanned", "sweep_attempted", "sweep_returned", "sweep_verified", "sweep_missing",
            "sweep_unverified", "estimated_cycles_remaining", "last_completed", "last_completed_sweep",
            "last_run", "next_batch_at", "next_run", "selection_scheduler", "selection_policy",
        }
        checkpoint = {key: state.get(key) for key in allowed if key in state}
        checkpoint["universe_size"] = population
        for field in (
            "cursor", "sweep_scanned", "sweep_attempted", "sweep_returned",
            "sweep_verified", "sweep_missing", "sweep_unverified",
        ):
            if field in checkpoint:
                checkpoint[field] = self._bounded_int(checkpoint[field], upper=population)
        if checkpoint.get("coverage_pct") is not None:
            checkpoint["coverage_pct"] = max(0.0, min(100.0, float(checkpoint["coverage_pct"] or 0.0)))
        if checkpoint.get("verified_pct") is not None:
            checkpoint["verified_pct"] = max(0.0, min(100.0, float(checkpoint["verified_pct"] or 0.0)))
        last_sweep = dict(checkpoint.get("last_completed_sweep") or {})
        if last_sweep:
            last_sweep["universe_size"] = population
            last_sweep["scanned"] = self._bounded_int(last_sweep.get("scanned"), upper=population)
            checkpoint["last_completed_sweep"] = last_sweep
        last_cycle = dict(checkpoint.get("last_completed") or {})
        if last_cycle:
            for field in ("attempted", "scanned", "returned", "verified", "missing", "unverified"):
                if field in last_cycle:
                    last_cycle[field] = self._bounded_int(last_cycle.get(field), upper=population)
            if "universe_size" in last_cycle:
                last_cycle["universe_size"] = population
            checkpoint["last_completed"] = last_cycle
        checkpoint.update({
            "version": CHECKPOINT_VERSION,
            "progress_contract_version": PROGRESS_CONTRACT_VERSION,
            "mode": str(mode).lower(),
            "lane": str(lane).lower(),
            **identity_row,
            "universe_fingerprint": self.universe_fingerprint(universe),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })
        self.store.set_kv(self.key(mode, lane), checkpoint)
        return checkpoint

    @staticmethod
    def apply(target: Dict[str, Any], checkpoint: Dict[str, Any]) -> None:
        for key, value in (checkpoint or {}).items():
            if key in {
                "version", "progress_contract_version", "mode", "lane", "updated_at",
                "universe_fingerprint", "snapshot_id", "content_hash", "population_count",
                "universe_revision", "reset_reason",
            }:
                continue
            if value is not None:
                target[key] = value
        target["checkpoint_version"] = checkpoint.get("version") or CHECKPOINT_VERSION
        target["checkpoint_snapshot_id"] = checkpoint.get("snapshot_id")
        target["checkpoint_population_count"] = checkpoint.get("population_count")
        target["checkpoint_restored"] = True
        target["checkpoint_updated_at"] = checkpoint.get("updated_at")
