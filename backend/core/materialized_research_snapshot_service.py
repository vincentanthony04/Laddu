from __future__ import annotations

"""Materialized retained Research read model for Clean Core.

The Stock Report never executes Research training, model inference, lifecycle
advancement or repository fanout. It reads the latest persisted Research
snapshot. Cold/stale composition runs through the bounded background producer
lane and stale snapshots remain visible while refresh occurs.
"""

from datetime import datetime, timezone
import hashlib
import json
import threading
from typing import Any, Dict

from core.local_projection_dispatcher import for_app as local_projection_dispatcher_for_app, PRIORITY_ENRICHMENT
from core.research_snapshot_read_service import ResearchSnapshotReadService


class MaterializedResearchSnapshotService:
    VERSION = "clean-core-materialized-research-snapshot-1.3.0-memory-reader"
    KEY_PREFIX = "clean_core:research_snapshot:v1:"
    FRESH_SECONDS = 300.0

    def __init__(self, app: Any):
        self.app = app
        self.store = app.store
        self.raw = ResearchSnapshotReadService(app)
        if not hasattr(app, "_clean_core_research_snapshot_cache"):
            setattr(app, "_clean_core_research_snapshot_cache", {})
        if not hasattr(app, "_clean_core_research_snapshot_lock"):
            setattr(app, "_clean_core_research_snapshot_lock", threading.RLock())
        self.cache = app._clean_core_research_snapshot_cache
        self.lock = app._clean_core_research_snapshot_lock

    @classmethod
    def _key(cls, instrument_key: str, mode: str) -> str:
        return cls.KEY_PREFIX + str(mode or "delivery").lower() + ":" + str(instrument_key or "")

    @staticmethod
    def _parse_time(value: Any) -> datetime | None:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)
        except Exception:
            return None

    def _refresh_needed(self, payload: Dict[str, Any]) -> bool:
        stamp = self._parse_time(payload.get("materialized_at") or payload.get("read_at") or payload.get("as_of"))
        if stamp is None:
            return True
        age = max(0.0, (datetime.now(timezone.utc) - stamp).total_seconds())
        return age > self.FRESH_SECONDS

    def _load(self, key: str) -> Dict[str, Any]:
        """Foreground memory-only lookup."""
        with self.lock:
            return dict(self.cache.get(key) or {})

    def _compose_local(self, *, symbol: str, instrument_key: str, mode: str) -> Dict[str, Any]:
        payload = dict(self.raw.read(symbol=symbol, instrument_key=instrument_key, mode=mode) or {})
        # Official NSE evidence remains part of selected-stock Research truth,
        # but is composed only on the background materializer. Foreground
        # StockSnapshot reads never open DuckDB or query a provider.
        try:
            from config import DATA_DIR
            from core.nse_official_evidence_service import NseOfficialEvidenceService
            official = NseOfficialEvidenceService(DATA_DIR).latest(symbol)
        except Exception as exc:
            official = {"ok": False, "state": "OFFICIAL_EVIDENCE_UNAVAILABLE", "risk_blocks": [], "error": str(exc)[:200]}
        payload["official_nse_evidence"] = dict(official or {})
        payload["ok"] = bool(payload.get("ok") or official.get("ok"))
        materialized_at = datetime.now(timezone.utc).isoformat()
        identity = {
            "symbol": symbol,
            "instrument_key": instrument_key,
            "mode": str(mode or "delivery").lower(),
            "active": (payload.get("active_prediction") or {}).get("prediction_id") or (payload.get("active_prediction") or {}).get("model_version"),
            "shadow": (payload.get("shadow_prediction") or {}).get("prediction_id") or (payload.get("shadow_prediction") or {}).get("model_version"),
            "decision": (payload.get("canonical_decision") or {}).get("decision_id") or (payload.get("canonical_decision") or {}).get("signal_id"),
            "retention_hash": (payload.get("retention_high_water") or {}).get("content_hash"),
            "official_nse_as_of": (payload.get("official_nse_evidence") or {}).get("as_of"),
            "official_nse_state": (payload.get("official_nse_evidence") or {}).get("state"),
            "as_of": payload.get("as_of"),
        }
        snapshot_id = hashlib.sha256(json.dumps(identity, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:24]
        return {
            **payload,
            "service_version": self.VERSION,
            "snapshot_id": snapshot_id,
            "materialized_at": materialized_at,
            "source": "LOCAL_RETAINED_COLD_FALLBACK",
            "policy": "Materialized retained Research; no training/inference/provider work on Stock Report requests.",
        }

    def _store(self, key: str, payload: Dict[str, Any]) -> None:
        self.store.set_kv(key, payload)
        with self.lock:
            self.cache[key] = dict(payload)

    def cache_token(self, *, instrument_key: str, mode: str) -> tuple[str, str]:
        """Cheap immutable token for selected-stock structural cache invalidation.

        This reads only the in-memory retained snapshot reference while holding the
        service lock; it does not copy the (potentially large) Research payload.
        A materialized replacement changes snapshot_id.  Freshness is included so
        a response cached while CURRENT cannot remain labelled CURRENT after the
        governed freshness window expires.
        """
        key = self._key(instrument_key, str(mode or "delivery").lower())
        with self.lock:
            retained = self.cache.get(key)
            if not isinstance(retained, dict) or not retained:
                return ("", "WARMING")
            snapshot_id = str(retained.get("snapshot_id") or "")
            freshness = "STALE" if self._refresh_needed(retained) else "CURRENT"
            return (snapshot_id, freshness)

    def peek(self, *, symbol: str, instrument_key: str, mode: str) -> Dict[str, Any]:
        """Read retained in-memory Research without scheduling enrichment."""
        mode = str(mode or "delivery").lower()
        key = self._key(instrument_key, mode)
        retained = self._load(key)
        if retained:
            return {
                **retained, "service_version": self.VERSION,
                "source": "MATERIALIZED_RETAINED_SNAPSHOT",
                "freshness": "STALE" if self._refresh_needed(retained) else "CURRENT",
                "refreshing": False,
            }
        return {
            "ok": False, "state": "WARMING", "service_version": self.VERSION,
            "snapshot_id": None, "symbol": symbol, "instrument_key": instrument_key,
            "mode": mode, "active_prediction": {}, "shadow_prediction": {},
            "canonical_decision": {}, "retention_high_water": {},
            "official_nse_evidence": {}, "errors": [], "as_of": None,
            "source": "MATERIALIZED_RESEARCH_NOT_IN_MEMORY", "freshness": "WARMING",
            "refreshing": False,
        }

    def read(self, *, symbol: str, instrument_key: str, mode: str) -> Dict[str, Any]:
        mode = str(mode or "delivery").lower()
        key = self._key(instrument_key, mode)
        retained = self._load(key)
        if retained:
            stale = self._refresh_needed(retained)
            queued = False
            if stale:
                def refresh_local() -> None:
                    fresh = self._compose_local(symbol=symbol, instrument_key=instrument_key, mode=mode)
                    fresh["source"] = "MATERIALIZED_RETAINED_REFRESH"
                    # Preserve a useful prior snapshot if retained authorities are
                    # temporarily unavailable rather than replacing it with blank.
                    if fresh.get("ok"):
                        self._store(key, fresh)

                result = local_projection_dispatcher_for_app(self.app).submit(
                    f"research-refresh:{mode}:{instrument_key}", refresh_local, priority=PRIORITY_ENRICHMENT
                )
                queued = bool(result.accepted)
            return {
                **retained,
                "service_version": self.VERSION,
                "source": "MATERIALIZED_RETAINED_SNAPSHOT",
                "freshness": "STALE" if stale else "CURRENT",
                "refreshing": queued,
            }

        def warm_local() -> None:
            try:
                retained = dict(self.store.get_kv(key, {}) or {})
            except Exception:
                retained = {}
            if retained:
                with self.lock:
                    self.cache[key] = retained
                return
            fresh = self._compose_local(symbol=symbol, instrument_key=instrument_key, mode=mode)
            if fresh.get("ok"):
                fresh["source"] = "MATERIALIZED_RETAINED_PROJECTION"
                self._store(key, fresh)

        result = local_projection_dispatcher_for_app(self.app).submit(
            f"research-project:{mode}:{instrument_key}", warm_local, priority=PRIORITY_ENRICHMENT
        )
        return {
            "ok": False,
            "state": "WARMING",
            "service_version": self.VERSION,
            "snapshot_id": None,
            "symbol": symbol,
            "instrument_key": instrument_key,
            "mode": mode,
            "active_prediction": {},
            "shadow_prediction": {},
            "canonical_decision": {},
            "retention_high_water": {},
            "official_nse_evidence": {},
            "errors": [],
            "as_of": None,
            "source": "MATERIALIZED_RESEARCH_PENDING",
            "freshness": "WARMING",
            "refreshing": bool(result.accepted),
            "policy": "Foreground Research read is projection-only; background materializer owns retained repository composition.",
        }
