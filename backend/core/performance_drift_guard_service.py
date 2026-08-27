"""Recent settled-performance and matured model-efficacy circuit breaker.

The guard cannot tune weights or thresholds.  It has two independent duties:
1) poor realised trading performance may pause *new promotions*; and
2) poor or unverifiable matured cross-sectional model efficacy withdraws only
   ML ranking authority, leaving the deterministic mathematical engine intact.
"""
from __future__ import annotations

import json
import threading
import time
from typing import Any, Dict, Optional

from core.production_mode_policy import require_production_mode
from core.model_efficacy_drift_authority import DEFAULT_MODEL_EFFICACY_DRIFT_AUTHORITY

DRIFT_GUARD_VERSION = "performance-drift-guard-1.2.0-regime-calibration-efficacy"


def _num(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _features(row: Dict[str, Any]) -> Dict[str, Any]:
    raw = row.get("feature_json")
    if isinstance(raw, dict):
        return dict(raw)
    try:
        decoded = json.loads(str(raw or "{}"))
        return dict(decoded) if isinstance(decoded, dict) else {}
    except Exception:
        return {}


def _outcome_r(row: Dict[str, Any]) -> Optional[float]:
    return _num(_features(row).get("outcome_r_multiple"))


def _mode(value: Any) -> Optional[str]:
    raw = str(value or "").lower().strip()
    try:
        return require_production_mode(raw)
    except ValueError:
        return ""


class PerformanceDriftGuardService:
    MIN_RECENT = 30
    RECENT_LIMIT = 60
    CACHE_SECONDS = 300.0

    def __init__(self, store: Any = None):
        self.store = store
        self._lock = threading.Lock()
        self._cache: Dict[str, tuple[float, Dict[str, Any]]] = {}

    def _model_efficacy(self, *, mode: str, model_id: str) -> Dict[str, Any]:
        if not model_id:
            return {"state": "NOT_APPLICABLE", "reason": "no governed model is requesting ranking authority"}
        repository = getattr(self.store, "production_model_governance_repository", None) if self.store is not None else None
        if repository is None or not hasattr(repository, "recent_model_efficacy"):
            return {
                "state": "UNVERIFIED", "model_id": model_id,
                "reason": "governance repository cannot prove recent matured model efficacy",
            }
        try:
            return dict(repository.recent_model_efficacy(
                model_id=model_id, desk=mode.upper(), limit_populations=self.RECENT_LIMIT,
            ) or {})
        except Exception as exc:
            return {
                "state": "UNVERIFIED", "model_id": model_id,
                "reason": f"matured efficacy query failed: {type(exc).__name__}: {exc}"[:240],
            }

    def evaluate(self, candidate: Dict[str, Any]) -> Dict[str, Any]:
        mode = require_production_mode(candidate.get("mode"))
        model_id = str(candidate.get("governed_model_id") or candidate.get("model_id") or "").strip()
        cache_key = f"{mode}:{model_id or 'NO_MODEL'}"
        now = time.time()
        with self._lock:
            cached = self._cache.get(cache_key)
            if cached and now - cached[0] < self.CACHE_SECONDS:
                return dict(cached[1])

        rows = []
        if self.store is not None and hasattr(self.store, "outcome_learning_rows"):
            try:
                rows = [dict(row) for row in self.store.outcome_learning_rows(limit=5000) or [] if _mode(row.get("mode")) == mode]
            except Exception:
                rows = []
        rows = rows[:self.RECENT_LIMIT]
        pnls = [value for row in rows if (value := _outcome_r(row)) is not None]
        unscaled = len(rows) - len(pnls)
        wins = [v for v in pnls if v > 0]
        losses = [v for v in pnls if v < 0]
        expectancy = sum(pnls) / len(pnls) if pnls else None
        gross_loss = abs(sum(losses))
        profit_factor = sum(wins) / gross_loss if gross_loss else (999.0 if wins else 0.0)
        if len(pnls) < self.MIN_RECENT:
            state, gate = "INSUFFICIENT_SAMPLE", "PASS"
            reason = f"{len(pnls)} recent outcomes; {self.MIN_RECENT} required"
        elif expectancy is not None and expectancy <= 0 and profit_factor < 1.0:
            state, gate = "PAUSE_PROMOTIONS", "BLOCK"
            reason = "recent expectancy is non-positive and profit factor is below one"
        elif expectancy is not None and (expectancy <= 0 or profit_factor < 1.0):
            state, gate = "WATCH_DRIFT", "PASS"
            reason = "one recent performance control is deteriorating"
        else:
            state, gate = "NORMAL", "PASS"
            reason = "recent settled-performance controls are within bounds"

        efficacy = self._model_efficacy(mode=mode, model_id=model_id)
        if not model_id:
            model_health = {
                "gate": "NOT_APPLICABLE", "authority_allowed": True,
                "reason": "no governed ML authority requested",
                "authority_version": "NOT_APPLICABLE",
                "policy": {},
            }
        else:
            model_health = DEFAULT_MODEL_EFFICACY_DRIFT_AUTHORITY.evaluate(efficacy)
        ml_gate = str(model_health.get("gate") or "WITHDRAW").upper()
        ml_reason = str(model_health.get("reason") or "matured model-efficacy health unavailable")

        report = {
            "version": DRIFT_GUARD_VERSION,
            "state": state,
            "gate": gate,
            "reason": reason,
            "samples": len(pnls),
            "legacy_unscaled_samples": unscaled,
            "outcome_scale": "initial_r_multiple",
            "expectancy_r": round(expectancy, 6) if expectancy is not None else None,
            "profit_factor": round(profit_factor, 6),
            "governed_model_id": model_id or None,
            "model_efficacy": efficacy,
            "ml_authority_gate": ml_gate,
            "ml_authority_allowed": bool(model_health.get("authority_allowed", ml_gate in {"ALLOW", "WATCH", "NOT_APPLICABLE"})),
            "ml_authority_reason": ml_reason,
            "model_efficacy_health": model_health,
            "ml_efficacy_policy": dict(model_health.get("policy") or {}),
            "policy": "may pause new promotions; matured aggregate/regime efficacy failure withdraws only ML ranking authority; calibration/drift deterioration is watched; automatic tuning is prohibited",
        }
        with self._lock:
            self._cache[cache_key] = (now, report)
        return dict(report)
