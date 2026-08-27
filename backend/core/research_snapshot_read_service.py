from __future__ import annotations

"""Read-only Research snapshot projection.

Existing Research is cumulative capital.  This service never trains, mutates,
or refreshes research; it exposes the latest retained evidence that already
exists in the governance/decision authorities.
"""

from datetime import datetime, timezone
from typing import Any, Dict


class ResearchSnapshotReadService:
    VERSION = "clean-core-research-read-1.0.0"

    def __init__(self, app: Any):
        self.app = app

    def read(self, *, symbol: str, instrument_key: str, mode: str) -> Dict[str, Any]:
        repo = getattr(self.app.store, "production_model_governance_repository", None)
        active = shadow = None
        errors = []
        if repo is not None:
            try:
                active = repo.latest_active_prediction(instrument_key=instrument_key, desk=mode.upper())
            except Exception as exc:
                errors.append(f"active:{str(exc)[:160]}")
            try:
                shadow = repo.latest_shadow_prediction(instrument_key=instrument_key, desk=mode.upper(), symbol=symbol)
            except Exception as exc:
                errors.append(f"shadow:{str(exc)[:160]}")
        decision = None
        try:
            rows = self.app.store.latest_decisions(mode, limit=100) or []
            for row in rows:
                item = dict(row or {})
                if str(item.get("instrument_key") or "") == instrument_key or str(item.get("symbol") or "").upper() == symbol.upper():
                    decision = item
                    break
        except Exception as exc:
            errors.append(f"decision:{str(exc)[:160]}")
        retention = {}
        try:
            retention = dict(self.app.store.get_kv("research_retention:last", {}) or {})
        except Exception:
            pass
        available = bool(active or shadow or decision or retention)
        as_of_candidates = [
            (active or {}).get("as_of"), (shadow or {}).get("as_of"),
            (decision or {}).get("created_at"), (decision or {}).get("updated_at"),
            retention.get("evaluated_at"),
        ]
        as_of = next((value for value in as_of_candidates if value), None)
        return {
            "ok": available,
            "service_version": self.VERSION,
            "state": "READY" if available else "UNAVAILABLE",
            "symbol": symbol,
            "instrument_key": instrument_key,
            "mode": mode,
            "as_of": as_of,
            "active_prediction": active,
            "shadow_prediction": shadow,
            "canonical_decision": decision,
            "retention_high_water": {
                "state": retention.get("state"),
                "content_hash": retention.get("content_hash"),
                "counts": retention.get("counts") or {},
                "evaluated_at": retention.get("evaluated_at"),
            },
            "errors": errors,
            "policy": "Read retained Research immediately; refresh/training is asynchronous and cannot gate the Stock Report.",
            "read_at": datetime.now(timezone.utc).isoformat(),
        }
