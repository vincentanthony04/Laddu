"""Immutable canonical evidence snapshots for one stock/desk decision.

A snapshot binds identity, exact-gap coverage, completed-bar mathematics,
feature/model state, risk admission and the published decision to one payload
hash.  UI surfaces may display a snapshot, but cannot silently merge values
from different refresh generations.
"""
from __future__ import annotations

from datetime import date, datetime
import hashlib
import json
from typing import Any, Dict, Mapping
from uuid import uuid4

from config import APP_VERSION
from models import now_iso


_REQUIRED = ("identity", "coverage", "timeframes", "mathematics", "features", "risk", "decision")
_ALLOWED_STATES = {"READY", "PARTIAL", "WAITING", "BLOCKED", "FAILED", "NOT_REQUIRED"}


def _jsonable(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _canonical(value: Any) -> str:
    return json.dumps(_jsonable(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8", errors="replace")).hexdigest()


def _state(value: Any, default: str = "WAITING") -> str:
    text = str(value or default).upper().strip().replace(" ", "_")
    return text if text in _ALLOWED_STATES else default


class CanonicalEvidenceSnapshotService:
    VERSION = "canonical-evidence-snapshot-1.0.0"

    def __init__(self, app: Any):
        self.app = app
        self.store = app.store
        plane = getattr(app, "production_data_plane", None)
        self.db = getattr(plane, "operational", None)

    @staticmethod
    def _mode(value: str) -> str:
        mode = str(value or "delivery").lower().strip()
        if mode not in {"intraday", "delivery"}:
            raise ValueError("evidence snapshots support Intraday and Delivery only")
        return mode

    @staticmethod
    def _kv_key(symbol: str, mode: str) -> str:
        return f"canonical_evidence_snapshot:{mode}:{symbol}"

    @staticmethod
    def _component_states(components: Mapping[str, Any]) -> Dict[str, str]:
        states: Dict[str, str] = {}
        for key in (*_REQUIRED, "inference"):
            row = components.get(key)
            if isinstance(row, Mapping):
                states[key] = _state(row.get("state"), "WAITING")
            elif row is None:
                states[key] = "WAITING" if key in _REQUIRED else "NOT_REQUIRED"
            else:
                states[key] = "READY"
        return states

    def capture(
        self,
        *,
        symbol: str,
        instrument_key: str,
        mode: str,
        components: Mapping[str, Any],
        blockers: list[Any] | None = None,
        source_revisions: Mapping[str, Any] | None = None,
    ) -> Dict[str, Any]:
        symbol = str(symbol or "").upper().strip()
        instrument_key = str(instrument_key or "").strip()
        mode = self._mode(mode)
        if not symbol or not instrument_key:
            raise ValueError("verified symbol and instrument key are required")
        clean_components = _jsonable(dict(components or {}))
        clean_blockers = [str(item) for item in (blockers or []) if str(item).strip()]
        clean_revisions = _jsonable(dict(source_revisions or {}))
        states = self._component_states(clean_components)
        ready = sum(1 for key in _REQUIRED if states.get(key) in {"READY", "NOT_REQUIRED"})
        completeness = round(ready * 100.0 / len(_REQUIRED), 1)
        if any(states.get(key) == "FAILED" for key in _REQUIRED):
            overall = "FAILED"
        elif clean_blockers or any(states.get(key) == "BLOCKED" for key in _REQUIRED):
            overall = "BLOCKED"
        elif ready == len(_REQUIRED):
            overall = "READY"
        elif ready:
            overall = "PARTIAL"
        else:
            overall = "WAITING"
        hash_material = {
            "symbol": symbol,
            "instrument_key": instrument_key,
            "mode": mode,
            "components": clean_components,
            "component_states": states,
            "blockers": clean_blockers,
            "source_revisions": clean_revisions,
            "build_version": APP_VERSION,
        }
        payload_hash = _hash(hash_material)
        snapshot_id = str(uuid4())
        captured_at = now_iso()
        payload = {
            "ok": True,
            "version": self.VERSION,
            "snapshot_id": snapshot_id,
            "symbol": symbol,
            "instrument_key": instrument_key,
            "mode": mode,
            "state": overall,
            "completeness_pct": completeness,
            "component_states": states,
            "components": clean_components,
            "blockers": clean_blockers,
            "source_revisions": clean_revisions,
            "payload_hash": payload_hash,
            "build_version": APP_VERSION,
            "captured_at": captured_at,
            "immutable": True,
        }
        if self.db is not None:
            row = self.db.execute(
                """INSERT INTO runtime_control.canonical_evidence_snapshots(
                       snapshot_id,symbol,instrument_key,mode,state,completeness_pct,
                       component_states,components,blockers,source_revisions,payload_hash,
                       build_version,captured_at)
                     VALUES(%s::uuid,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb,%s::jsonb,%s,%s,now())
                     ON CONFLICT(symbol,mode,payload_hash) DO UPDATE SET payload_hash=EXCLUDED.payload_hash
                     RETURNING snapshot_id::text,captured_at""",
                (
                    snapshot_id, symbol, instrument_key, mode, overall, completeness,
                    json.dumps(states), json.dumps(clean_components), json.dumps(clean_blockers),
                    json.dumps(clean_revisions), payload_hash, APP_VERSION,
                ),
                fetch="one", statement_timeout_ms=2500,
            ) or {}
            payload["snapshot_id"] = str(row.get("snapshot_id") or snapshot_id)
            if row.get("captured_at") is not None:
                payload["captured_at"] = _jsonable(row.get("captured_at"))
        else:
            self.store.set_kv(self._kv_key(symbol, mode), payload)
        return payload

    def capture_from_intelligence(
        self,
        *,
        symbol: str,
        instrument_key: str,
        mode: str,
        intelligence: Mapping[str, Any],
        pipeline: Mapping[str, Any] | None = None,
    ) -> Dict[str, Any]:
        payload = dict(intelligence or {})
        analysis = dict(payload.get("analysis") or {})
        decision = dict(analysis.get("decision") or {})
        scorecard = dict(payload.get("scorecard") or {})
        trade_map = dict(payload.get("trade_map") or {})
        mtf = list(payload.get("mtf_trend") or [])
        invalidation = [str(item) for item in (payload.get("invalidation") or []) if str(item).strip()]
        pipeline_map = dict(pipeline or payload.get("pipeline") or {})
        coverage_state = "READY" if pipeline_map.get("coverage_ready") is True else "PARTIAL" if pipeline_map else "WAITING"
        required_canonical_tfs = {"30M", "1H", "4H", "1D", "1W", "1MO"}
        def _tf_name(row: Mapping[str, Any]) -> str:
            raw = str(row.get("tf") or row.get("timeframe") or row.get("interval") or "").upper().strip()
            return "1MO" if raw in {"1M", "1MO", "MONTH", "MONTHLY"} else raw
        def _tf_usable(row: Mapping[str, Any]) -> bool:
            state = str(row.get("state") or row.get("status") or "READY").upper().strip()
            return state not in {"PENDING", "WAITING", "MISSING", "UNAVAILABLE", "FAILED", "BLOCKED", "LOADING"}
        mtf_by_name = {_tf_name(row): dict(row) for row in mtf if isinstance(row, Mapping) and _tf_name(row)}
        timeframes_ready = required_canonical_tfs.issubset(mtf_by_name) and all(_tf_usable(mtf_by_name[tf]) for tf in required_canonical_tfs)
        math_ready = scorecard.get("math_composite") is not None or decision.get("technical_score") is not None
        features_ready = any(scorecard.get(key) is not None for key in ("technical", "fundamental", "mtf", "math_composite"))
        inference_ready = scorecard.get("model_score") is not None or bool(decision.get("model_state") or decision.get("model_ranking_stage"))
        decision_ready = bool(decision.get("decision"))
        fundamental_context = payload.get("fundamentals") if isinstance(payload.get("fundamentals"), Mapping) else {}
        market_sector_context = (
            payload.get("market_sector_context") if isinstance(payload.get("market_sector_context"), Mapping) else
            payload.get("market_context") if isinstance(payload.get("market_context"), Mapping) else
            payload.get("heat_context") if isinstance(payload.get("heat_context"), Mapping) else {}
        )
        participation_context = payload.get("participation") if isinstance(payload.get("participation"), Mapping) else {
            key: scorecard.get(key) for key in (
                "participation", "participation_score", "participation_state", "participation_decision_usable",
                "session_relative_volume", "recent_volume_vs_base", "delivery_pct", "delivery_pct_surprise",
                "delivered_quantity_surprise", "relative_volume", "volume_confirmation",
            ) if scorecard.get(key) is not None
        }
        derivatives_context = payload.get("derivatives_context") if isinstance(payload.get("derivatives_context"), Mapping) else {}
        components = {
            "identity": {"state": "READY", "symbol": symbol, "instrument_key": instrument_key},
            "coverage": {"state": coverage_state, "evidence": pipeline_map},
            "timeframes": {"state": "READY" if timeframes_ready else "PARTIAL" if mtf else "WAITING", "count": len(mtf), "records": mtf[:10]},
            "mathematics": {"state": "READY" if math_ready else "WAITING", "scorecard": scorecard, "trade_map": trade_map},
            "features": {"state": "READY" if features_ready else "WAITING", "feature_snapshot": payload.get("feature_snapshot") or scorecard},
            "inference": {"state": "READY" if inference_ready else "NOT_REQUIRED" if features_ready else "WAITING", "model": {"score": scorecard.get("model_score"), "state": decision.get("model_state") or decision.get("model_ranking_stage")}},
            "risk": {"state": "BLOCKED" if invalidation else "READY" if math_ready else "WAITING", "blockers": invalidation},
            "decision": {"state": "READY" if decision_ready else "WAITING", "decision": decision, "trade_map_state": trade_map.get("state")},
            "thesis_context": {
                "state": "READY" if (market_sector_context and participation_context and (fundamental_context or mode == "intraday")) else "PARTIAL",
                "fundamentals": fundamental_context,
                "market_sector": market_sector_context,
                "participation": participation_context,
                "derivatives_context": derivatives_context,
                "required_canonical_timeframes": ["30m", "1H", "4H", "1D", "1W", "1M"],
                "canonical_timeframes_ready": bool(timeframes_ready),
            },
        }
        revisions = {
            "identity_revision": payload.get("identity_revision") or pipeline_map.get("identity_revision"),
            "candle_revision": payload.get("candle_revision") or pipeline_map.get("candle_revision"),
            "corporate_action_revision": payload.get("corporate_action_revision") or pipeline_map.get("corporate_action_revision"),
            "feature_revision": payload.get("feature_revision") or pipeline_map.get("feature_revision"),
            "model_version": decision.get("model_version") or decision.get("model_id"),
            "calculation_version": analysis.get("calculation_version") or payload.get("calculation_version"),
        }
        return self.capture(
            symbol=symbol,
            instrument_key=instrument_key,
            mode=mode,
            components=components,
            blockers=invalidation,
            source_revisions=revisions,
        )

    def latest(self, *, symbol: str, mode: str) -> Dict[str, Any]:
        symbol = str(symbol or "").upper().strip()
        mode = self._mode(mode)
        if self.db is None:
            return dict(self.store.get_kv(self._kv_key(symbol, mode), {}) or {
                "ok": True, "version": self.VERSION, "symbol": symbol, "mode": mode,
                "state": "NOT_CAPTURED", "immutable": True,
            })
        row = self.db.execute(
            """SELECT snapshot_id::text,symbol,instrument_key,mode,state,completeness_pct,
                      component_states,components,blockers,source_revisions,payload_hash,
                      build_version,captured_at
                 FROM runtime_control.canonical_evidence_snapshots
                WHERE symbol=%s AND mode=%s ORDER BY captured_at DESC LIMIT 1""",
            (symbol, mode), fetch="one", statement_timeout_ms=1800,
        )
        if not row:
            return {"ok": True, "version": self.VERSION, "symbol": symbol, "mode": mode, "state": "NOT_CAPTURED", "immutable": True}
        payload = _jsonable(dict(row))
        payload.update({"ok": True, "version": self.VERSION, "immutable": True})
        return payload

    def verify(self, snapshot: Mapping[str, Any]) -> Dict[str, Any]:
        row = dict(snapshot or {})
        material = {
            "symbol": row.get("symbol"),
            "instrument_key": row.get("instrument_key"),
            "mode": row.get("mode"),
            "components": row.get("components") or {},
            "component_states": row.get("component_states") or {},
            "blockers": row.get("blockers") or [],
            "source_revisions": row.get("source_revisions") or {},
            "build_version": row.get("build_version") or APP_VERSION,
        }
        actual = _hash(material)
        expected = str(row.get("payload_hash") or "")
        return {"ok": bool(expected and actual == expected), "expected_hash": expected, "actual_hash": actual, "tampered": bool(expected and actual != expected)}
