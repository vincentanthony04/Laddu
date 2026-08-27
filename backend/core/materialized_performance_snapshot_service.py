from __future__ import annotations

"""Bounded materialized Performance/Accuracy read model.

Foreground HTTP never executes the canonical lifecycle/journal/settlement joins.
It returns the latest immutable snapshot immediately and coalesces a background
refresh.  Cold readers receive an explicit WARMING projection while the
canonical Product State remains available to the UI as the core truth.
"""

from datetime import datetime, timezone
import threading
from typing import Any, Dict

from core.background_repair_dispatcher import for_app as repair_dispatcher_for_app


class MaterializedPerformanceSnapshotService:
    VERSION = "materialized-performance-snapshot-1.2.0-foreground-memory-only"
    KEY_PREFIX = "performance_summary:v120:"
    FRESH_SECONDS = 120.0

    def __init__(self, app: Any):
        self.app = app
        self.store = app.store
        if not hasattr(app, "_materialized_performance_cache"):
            setattr(app, "_materialized_performance_cache", {})
        if not hasattr(app, "_materialized_performance_lock"):
            setattr(app, "_materialized_performance_lock", threading.RLock())
        self.cache = app._materialized_performance_cache
        self.lock = app._materialized_performance_lock

    @classmethod
    def _key(cls, mode: str, start: str, end: str) -> str:
        scope = str(mode or "all").lower()
        return f"{cls.KEY_PREFIX}{scope}:{str(start or '')}:{str(end or '')}"

    @staticmethod
    def _parse_time(value: Any) -> datetime | None:
        try:
            return datetime.fromisoformat(str(value or "").replace("Z", "+00:00")).astimezone(timezone.utc)
        except Exception:
            return None

    def _load(self, key: str) -> Dict[str, Any]:
        # Foreground Performance is an in-memory projection read only. Persistence
        # I/O belongs to the background operator lane and can never hold an HTTP
        # request behind the PostgreSQL pool.
        with self.lock:
            return dict(self.cache.get(key) or {})

    def prime_from_persistence(self, *, mode: str = "all", start: str = "", end: str = "") -> Dict[str, Any]:
        """Background-only warm start from persisted materialized evidence."""
        key = self._key(mode, start, end)
        try:
            persisted = dict(self.store.get_kv(key, {}) or {})
        except Exception as exc:
            error = str(exc)[:300] or "MATERIALIZED_PERFORMANCE_STORE_UNAVAILABLE"
            setattr(self.app, "_materialized_performance_persistence_error", error)
            return {"ok": False, "state": "PERSISTENCE_UNAVAILABLE", "error": error}
        if persisted:
            with self.lock:
                self.cache[key] = dict(persisted)
        setattr(self.app, "_materialized_performance_persistence_error", None)
        return {"ok": True, "state": "PRIMED" if persisted else "EMPTY", "materialized": bool(persisted)}

    def _compute(self, *, mode: str, start: str, end: str) -> Dict[str, Any]:
        from reference_catalog import final_journal_summary_payload
        from core.performance_evidence_authority import PerformanceEvidenceAuthority

        evidence = PerformanceEvidenceAuthority(self.app).report(
            mode=mode if mode in {"delivery", "intraday"} else "all"
        )
        if evidence.get("ok") is not True:
            raise RuntimeError(
                "PERFORMANCE_EVIDENCE_AUTHORITY_UNAVAILABLE:"
                + str(evidence.get("error") or evidence.get("state") or "UNKNOWN")
            )
        # Compatibility journal summary is an enrichment only after the
        # canonical performance authorities have proved available. It can never
        # be evaluated first and accidentally become the surviving truth.
        payload = final_journal_summary_payload(self.app, mode, start, end)
        lifecycle = dict(evidence.get("signal_accuracy") or {})
        payload = dict(payload or {})
        payload.update({
            "ok": True,
            "state": "READY",
            "service_version": self.VERSION,
            "authority": evidence.get("authority"),
            "authority_version": evidence.get("authority_version"),
            "canonical_lifecycle": lifecycle,
            "performance_evidence": evidence,
            "model_paper_performance": evidence.get("model_paper_performance"),
            "settlement_parity": evidence.get("settlement_parity"),
            "legacy_signal_summary_units": "PRICE_POINTS_ONLY",
            "legacy_signal_summary_currency_pnl_allowed": False,
            "accuracy_authority": "settled geometry-complete canonical decisions with Model Paper settlement lineage only",
            "performance_authority": (evidence.get("model_paper_performance") or {}).get("authority"),
            "accuracy_eligible": int((lifecycle.get("overall") or {}).get("accuracy_eligible") or 0),
            "materialized_at": datetime.now(timezone.utc).isoformat(),
            "source": "BACKGROUND_MATERIALIZED_PERFORMANCE",
        })
        return payload

    def refresh(self, *, mode: str = "all", start: str = "", end: str = "") -> Dict[str, Any]:
        key = self._key(mode, start, end)
        payload = self._compute(mode=mode, start=start, end=end)
        try:
            self.store.set_kv(key, payload)
        finally:
            with self.lock:
                self.cache[key] = dict(payload)
        return dict(payload)

    def request_refresh(self, *, mode: str = "all", start: str = "", end: str = "") -> bool:
        key = self._key(mode, start, end)
        token = f"performance-refresh:{key}"
        result = repair_dispatcher_for_app(self.app).submit(
            token, lambda: self.refresh(mode=mode, start=start, end=end)
        )
        return bool(result.accepted or result.state == "COALESCED")

    def read(self, *, mode: str = "all", start: str = "", end: str = "") -> Dict[str, Any]:
        key = self._key(mode, start, end)
        retained = self._load(key)
        if retained:
            stamp = self._parse_time(retained.get("materialized_at"))
            age = max(0.0, (datetime.now(timezone.utc) - stamp).total_seconds()) if stamp else None
            stale = age is None or age > self.FRESH_SECONDS
            queued = False
            refresh_error = None
            if stale:
                try:
                    queued = self.request_refresh(mode=mode, start=start, end=end)
                except Exception as exc:
                    refresh_error = str(exc)[:300]
            return {
                **retained,
                "read_model_version": self.VERSION,
                "read_state": "STALE_LAST_KNOWN" if stale else "CURRENT",
                "snapshot_age_sec": round(age, 3) if age is not None else None,
                "refreshing": queued,
                "refresh_error": refresh_error,
            }

        queued = False
        refresh_error = None
        try:
            queued = self.request_refresh(mode=mode, start=start, end=end)
        except Exception as exc:
            refresh_error = str(exc)[:300]
        try:
            envelope = dict(self.app.product_state_envelope.snapshot() or {})
            canonical_product_state = dict(envelope.get("performance") or {})
        except Exception as exc:
            canonical_product_state = {"state": "UNAVAILABLE", "error": str(exc)[:300]}
        persistence_error = getattr(self.app, "_materialized_performance_persistence_error", None)
        return {
            "ok": False,
            "state": "WARMING",
            "service_version": self.VERSION,
            "read_model_version": self.VERSION,
            "source": "MATERIALIZED_PERFORMANCE_PENDING",
            "data_available": False,
            "fallback_used": False,
            "refreshing": queued,
            "refresh_error": refresh_error,
            "persistence_error": persistence_error,
            "canonical_product_state": canonical_product_state,
            "canonical_lifecycle": None,
            "model_paper_performance": None,
            "policy": "Foreground Performance is memory-only; canonical Product State remains visible while background evidence materializes.",
        }
