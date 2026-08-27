from __future__ import annotations

"""Materialized selected-stock fundamental projection.

The foreground Stock Report reads only retained/materialized evidence.  Canonical
fundamental normalization/scoring remains unchanged and executes only in the
background producer lane.  Provider refresh is owned by ReferenceDataService,
not by this reader.
"""

from datetime import datetime, timezone
import hashlib
import json
import threading
from typing import Any, Dict

from core.local_projection_dispatcher import for_app as local_projection_dispatcher_for_app, PRIORITY_ENRICHMENT


class MaterializedFundamentalSnapshotService:
    VERSION = "materialized-fundamental-snapshot-1.1.0-memory-reader"
    KEY_PREFIX = "clean_core:fundamental_snapshot:v1:"

    def __init__(self, app: Any):
        self.app = app
        self.store = app.store
        if not hasattr(app, "_clean_core_fundamental_snapshot_cache"):
            setattr(app, "_clean_core_fundamental_snapshot_cache", {})
        if not hasattr(app, "_clean_core_fundamental_snapshot_lock"):
            setattr(app, "_clean_core_fundamental_snapshot_lock", threading.RLock())
        self.cache = app._clean_core_fundamental_snapshot_cache
        self.lock = app._clean_core_fundamental_snapshot_lock

    @classmethod
    def _key(cls, instrument_key: str) -> str:
        return cls.KEY_PREFIX + str(instrument_key or "")

    def _load(self, instrument_key: str) -> Dict[str, Any]:
        """Foreground memory-only lookup."""
        with self.lock:
            return dict(self.cache.get(instrument_key) or {})

    def project_local(self, instrument: Dict[str, Any]) -> Dict[str, Any]:
        instrument_key = str(instrument.get("instrument_key") or "")
        symbol = str(instrument.get("trading_symbol") or instrument.get("symbol") or "").upper()
        isin = str(instrument.get("isin") or "").upper().strip()
        candidates: list[Dict[str, Any]] = []

        # Persisted provider cache is already normalized/scored by the canonical
        # FundamentalDimension/Scoring authorities. Reading it cannot schedule
        # a provider revalidation.
        if isin and hasattr(self.store, "get_fundamentals_cache"):
            try:
                cached_row = self.store.get_fundamentals_cache(isin) or {}
                cached_payload = cached_row.get("payload") if isinstance(cached_row, dict) else None
                if isinstance(cached_payload, dict) and cached_payload:
                    candidates.append(dict(cached_payload))
            except Exception:
                pass

        # Authorized local filing/import scoring is local-only and runs in this
        # background producer, never on the browser request thread.
        fundamentals = getattr(self.app, "fundamentals", None)
        if fundamentals is not None:
            try:
                if not getattr(fundamentals, "loaded_at", None):
                    fundamentals.load(force=False)
                local = dict(fundamentals.score(instrument) or {})
                if local:
                    candidates.append(local)
            except Exception as exc:
                if not candidates:
                    candidates.append({"ok": False, "state": "UNAVAILABLE", "error": str(exc)[:160], "source": "local_fundamental_projection"})

        def _rank(row: Dict[str, Any]) -> tuple[int, str]:
            ok = 1 if row.get("ok") else 0
            stamp = str(row.get("effective_date") or row.get("as_of") or row.get("fetched_at") or "")
            return ok, stamp

        evidence = max(candidates, key=_rank) if candidates else {"ok": False, "state": "UNAVAILABLE", "source": "none"}
        source = str(evidence.get("source") or "GOVERNED_LOCAL_FUNDAMENTALS")
        now = datetime.now(timezone.utc).isoformat()
        material = {
            "instrument_key": instrument_key,
            "symbol": symbol,
            "evidence": evidence,
            "as_of": evidence.get("as_of") or evidence.get("effective_date") or evidence.get("fetched_at"),
            "source": source,
        }
        snapshot_id = hashlib.sha256(
            json.dumps(material, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()[:24]
        return {
            "ok": bool(evidence.get("ok")),
            "version": self.VERSION,
            "snapshot_id": snapshot_id,
            "instrument_key": instrument_key,
            "symbol": symbol,
            "as_of": evidence.get("as_of") or evidence.get("effective_date") or evidence.get("fetched_at") or now,
            "projected_at": now,
            "source": source,
            "fundamentals": evidence,
            "state": str(evidence.get("state") or ("READY" if evidence.get("ok") else "UNAVAILABLE")).upper(),
            "policy": "Background canonical fundamental projection from local/persisted evidence; no provider scheduling from reader.",
        }

    def request_projection(self, instrument: Dict[str, Any]) -> bool:
        instrument_key = str(instrument.get("instrument_key") or "")
        if not instrument_key:
            return False

        def _project() -> None:
            # Persisted hydration is background-owned. A cold page never waits
            # for PostgreSQL just to discover a retained snapshot.
            try:
                retained = dict(self.store.get_kv(self._key(instrument_key), {}) or {})
            except Exception:
                retained = {}
            if retained:
                with self.lock:
                    self.cache[instrument_key] = retained
                return
            payload = self.project_local(instrument)
            try:
                self.store.set_kv(self._key(instrument_key), payload)
            except Exception:
                pass
            with self.lock:
                self.cache[instrument_key] = dict(payload)

        result = local_projection_dispatcher_for_app(self.app).submit(
            f"fundamental-project:{instrument_key}", _project, priority=PRIORITY_ENRICHMENT
        )
        return bool(result.accepted or result.state == "COALESCED")

    def cache_token(self, instrument_key: str) -> str:
        """Cheap immutable token for selected-stock structural cache invalidation."""
        with self.lock:
            retained = self.cache.get(str(instrument_key or ""))
            if not isinstance(retained, dict) or not retained:
                return ""
            return str(retained.get("snapshot_id") or retained.get("version") or "")

    def peek(self, instrument: Dict[str, Any]) -> Dict[str, Any]:
        """Read retained in-memory evidence without scheduling cold work."""
        instrument_key = str(instrument.get("instrument_key") or "")
        if not instrument_key:
            return {"ok": False, "version": self.VERSION, "state": "IDENTITY_UNAVAILABLE", "fundamentals": {}}
        retained = self._load(instrument_key)
        if retained:
            return {**retained, "read_source": "MATERIALIZED_RETAINED_SNAPSHOT", "refreshing": False}
        return {
            "ok": False, "version": self.VERSION, "state": "WARMING",
            "instrument_key": instrument_key,
            "symbol": str(instrument.get("trading_symbol") or instrument.get("symbol") or "").upper(),
            "fundamentals": {}, "refreshing": False,
            "read_source": "MATERIALIZED_SNAPSHOT_NOT_IN_MEMORY",
        }

    def read(self, instrument: Dict[str, Any]) -> Dict[str, Any]:
        instrument_key = str(instrument.get("instrument_key") or "")
        if not instrument_key:
            return {"ok": False, "version": self.VERSION, "state": "IDENTITY_UNAVAILABLE", "fundamentals": {}}
        retained = self._load(instrument_key)
        if retained:
            return {**retained, "read_source": "MATERIALIZED_RETAINED_SNAPSHOT", "refreshing": False}
        queued = self.request_projection(instrument)
        return {
            "ok": False,
            "version": self.VERSION,
            "state": "WARMING",
            "instrument_key": instrument_key,
            "symbol": str(instrument.get("trading_symbol") or instrument.get("symbol") or "").upper(),
            "fundamentals": {},
            "refreshing": queued,
            "read_source": "MATERIALIZED_SNAPSHOT_PENDING",
            "policy": "No foreground fundamental scoring/provider I/O; background materializer requested.",
        }
