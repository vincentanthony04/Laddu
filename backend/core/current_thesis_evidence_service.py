"""Current canonical evidence packet for open Final Model-Paper reassessment.

The risk-critical lifecycle path never performs provider I/O here.  It combines
an already-verified live quote with the latest immutable canonical evidence
snapshot and cached read-only derivatives context.  Missing domains remain
explicit; quote-only evidence can never be labelled full-thesis validation.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping


class CurrentThesisEvidenceService:
    authority = "CurrentThesisEvidenceService"
    authority_version = "1.0.0"
    required_domains = ("price", "mtf", "market_sector", "fundamentals", "participation")

    def __init__(self, app: Any):
        self.app = app

    @staticmethod
    def _payload(row: Mapping[str, Any]) -> dict[str, Any]:
        raw = row.get("payload") if isinstance(row.get("payload"), Mapping) else row.get("payload_json")
        if isinstance(raw, Mapping):
            return dict(raw)
        try:
            value = json.loads(str(raw or "{}"))
            return dict(value) if isinstance(value, Mapping) else {}
        except Exception:
            return {}

    @staticmethod
    def _text(value: Any) -> str:
        return str(value or "").strip()

    @classmethod
    def _first(cls, *values: Any) -> Any:
        for value in values:
            if value not in (None, "", [], {}):
                return value
        return None

    @staticmethod
    def _canonical_hash(value: Any) -> str:
        body = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str, ensure_ascii=False)
        return hashlib.sha256(body.encode("utf-8", errors="replace")).hexdigest()

    @classmethod
    def _direction(cls, value: Any) -> str:
        if isinstance(value, Mapping):
            for key in ("direction", "bias", "trend", "state", "regime", "market_regime"):
                direction = cls._direction(value.get(key))
                if direction != "UNKNOWN":
                    return direction
            return "UNKNOWN"
        text = cls._text(value).upper().replace("_", " ")
        if any(token in text for token in ("BEAR", "DOWNTREND", "DOWNSIDE", "SHORT BIAS")):
            return "BEARISH"
        if any(token in text for token in ("BULL", "UPTREND", "UPSIDE", "LONG BIAS")):
            return "BULLISH"
        if any(token in text for token in ("NEUTRAL", "MIXED", "RANGE", "SIDEWAYS")):
            return "NEUTRAL"
        return "UNKNOWN"

    @classmethod
    def _alignment(cls, side: str, direction: str) -> str:
        side = cls._text(side).upper()
        direction = cls._text(direction).upper()
        if direction not in {"BULLISH", "BEARISH"}:
            return "UNKNOWN"
        supportive = (side == "LONG" and direction == "BULLISH") or (side == "SHORT" and direction == "BEARISH")
        return "SUPPORTS" if supportive else "CONTRADICTS"

    @classmethod
    def _mtf_domain(cls, component: Mapping[str, Any], side: str) -> dict[str, Any]:
        rows = [dict(row) for row in (component.get("records") or []) if isinstance(row, Mapping)]
        def _name(item: Mapping[str, Any]) -> str:
            raw = str(item.get("tf") or item.get("timeframe") or item.get("interval") or "").upper().strip()
            return "1MO" if raw in {"1M", "1MO", "MONTH", "MONTHLY"} else raw
        by_name = {_name(row): row for row in rows if _name(row)}
        required = {"30M", "1H", "4H", "1D", "1W", "1MO"}
        directions = [cls._direction(by_name[name]) for name in required if name in by_name]
        bull = sum(1 for value in directions if value == "BULLISH")
        bear = sum(1 for value in directions if value == "BEARISH")
        direction = "BULLISH" if bull > bear and bull >= 2 else "BEARISH" if bear > bull and bear >= 2 else "NEUTRAL" if bull or bear else "UNKNOWN"
        ready = str(component.get("state") or "").upper() == "READY" and required.issubset(by_name)
        return {
            "state": "READY" if ready else str(component.get("state") or "MISSING").upper(),
            "ready": ready,
            "records": rows[:10],
            "record_count": len(rows),
            "direction": direction,
            "alignment": cls._alignment(side, direction),
        }

    @classmethod
    def _market_sector_domain(cls, feature: Mapping[str, Any], decision: Mapping[str, Any], frozen: Mapping[str, Any], side: str) -> dict[str, Any]:
        market = cls._first(
            feature.get("market_context"), feature.get("market_regime"), feature.get("regime"),
            feature.get("breadth"), feature.get("market_breadth"), decision.get("market_context"),
        )
        sector = cls._first(
            feature.get("sector_context"), feature.get("sector_direction"), feature.get("sector"),
            decision.get("sector_context"), frozen.get("sector_context"), frozen.get("sector"), frozen.get("sector_label"),
        )
        market_direction = cls._direction(market)
        sector_direction = cls._direction(sector)
        direction = sector_direction if sector_direction != "UNKNOWN" else market_direction
        ready = market is not None and sector is not None
        return {
            "state": "READY" if ready else "MISSING",
            "ready": ready,
            "market": market,
            "sector": sector,
            "market_direction": market_direction,
            "sector_direction": sector_direction,
            "alignment": cls._alignment(side, direction),
        }

    @classmethod
    def _fundamental_domain(cls, feature: Mapping[str, Any], mode: str, side: str) -> dict[str, Any]:
        required = str(mode).lower() == "delivery"
        value = cls._first(feature.get("fundamentals"), feature.get("fundamental"), feature.get("fundamental_state"), feature.get("fundamental_score"))
        state_text = cls._text(cls._first(feature.get("fundamental_state"), value)).upper()
        score = cls._first(feature.get("fundamental_score"), feature.get("fundamental"))
        ready = (value is not None) if required else True
        weak = any(token in state_text for token in ("WEAK", "FAIL", "REJECT", "POOR"))
        alignment = "CONTRADICTS" if required and side == "LONG" and weak else "UNKNOWN"
        return {
            "state": "READY" if ready else "MISSING",
            "ready": ready,
            "required": required,
            "value": value,
            "score": score,
            "alignment": alignment,
        }

    @classmethod
    def _participation_domain(cls, feature: Mapping[str, Any]) -> dict[str, Any]:
        keys = (
            "participation", "participation_score", "participation_state", "participation_decision_usable",
            "session_relative_volume", "recent_volume_vs_base", "delivery_pct", "delivery_pct_surprise",
            "delivered_quantity_surprise", "relative_volume", "volume_confirmation",
        )
        evidence = {key: feature.get(key) for key in keys if feature.get(key) is not None}
        ready = bool(evidence) and evidence.get("participation_decision_usable") is not False
        return {"state": "READY" if ready else "MISSING", "ready": ready, "evidence": evidence, "alignment": "UNKNOWN"}

    def _derivatives_domain(self, instrument: Mapping[str, Any], frozen: Mapping[str, Any]) -> dict[str, Any]:
        required = bool(frozen.get("oi_context_required") is True)
        service = getattr(self.app, "derivatives_context", None)
        if service is None or not callable(getattr(service, "peek", None)):
            return {"state": "UNAVAILABLE", "ready": not required, "required": required, "evidence": {}, "alignment": "UNKNOWN"}
        try:
            evidence = dict(service.peek(dict(instrument or {})) or {})
        except Exception as exc:
            evidence = {"state": "UNAVAILABLE", "reason": str(exc)[:180], "provider_io": False}
        state = str(evidence.get("state") or "UNAVAILABLE").upper()
        usable = state == "READY" and evidence.get("stale") is not True
        not_applicable = state in {"NOT_APPLICABLE", "NOT_FNO_ELIGIBLE_OR_NO_CHAIN"}
        ready = usable or not_applicable or not required
        text = str(evidence.get("interpretation") or "").upper()
        alignment = "UNKNOWN"
        if "OVERHEAD PRESSURE" in text:
            alignment = "CONTRADICTS" if str(frozen.get("side") or "LONG").upper() == "LONG" else "SUPPORTS"
        return {"state": state, "ready": ready, "required": required, "usable": usable, "evidence": evidence, "alignment": alignment}

    def build(self, row: Mapping[str, Any], quote: Mapping[str, Any], *, at: Any = None) -> dict[str, Any]:
        frozen = self._payload(row)
        symbol = self._text(row.get("symbol") or frozen.get("symbol")).upper()
        mode = self._text(row.get("mode") or frozen.get("mode")).lower()
        side = self._text(row.get("side") or frozen.get("side")).upper()
        frozen_identity = dict(frozen.get("identity") or {}) if isinstance(frozen.get("identity"), Mapping) else {}
        instrument_key = self._text(
            frozen.get("instrument_key") or row.get("instrument_key") or frozen_identity.get("instrument_key")
        )
        snapshot = {}
        snapshot_error = None
        snapshots = getattr(self.app, "evidence_snapshots", None)
        try:
            if snapshots is not None and callable(getattr(snapshots, "latest", None)):
                snapshot = dict(snapshots.latest(symbol=symbol, mode=mode) or {})
        except Exception as exc:
            snapshot_error = str(exc)[:200]
        snapshot_state = str(snapshot.get("state") or "NOT_CAPTURED").upper()
        snapshot_verified = False
        if snapshot and snapshots is not None and callable(getattr(snapshots, "verify", None)) and snapshot.get("payload_hash"):
            try:
                snapshot_verified = bool(snapshots.verify(snapshot).get("ok"))
            except Exception:
                snapshot_verified = False
        elif snapshot and not snapshot.get("payload_hash"):
            snapshot_verified = False

        components = dict(snapshot.get("components") or {}) if isinstance(snapshot.get("components"), Mapping) else {}
        features_component = dict(components.get("features") or {}) if isinstance(components.get("features"), Mapping) else {}
        feature = features_component.get("feature_snapshot")
        if not isinstance(feature, Mapping):
            feature = features_component.get("scorecard")
        if not isinstance(feature, Mapping):
            feature = {}
        decision_component = dict(components.get("decision") or {}) if isinstance(components.get("decision"), Mapping) else {}
        decision = decision_component.get("decision") if isinstance(decision_component.get("decision"), Mapping) else {}
        timeframes = dict(components.get("timeframes") or {}) if isinstance(components.get("timeframes"), Mapping) else {}
        thesis_context = dict(components.get("thesis_context") or {}) if isinstance(components.get("thesis_context"), Mapping) else {}
        if isinstance(thesis_context.get("fundamentals"), Mapping) and thesis_context.get("fundamentals"):
            feature = {**dict(feature), "fundamentals": dict(thesis_context.get("fundamentals") or {})}
        if isinstance(thesis_context.get("market_sector"), Mapping) and thesis_context.get("market_sector"):
            market_sector_payload = dict(thesis_context.get("market_sector") or {})
            feature = {**dict(feature), "market_context": market_sector_payload}
            if market_sector_payload.get("stock_sector") is not None:
                feature.setdefault("sector", market_sector_payload.get("stock_sector"))
            if market_sector_payload.get("sector") is not None:
                feature.setdefault("sector", market_sector_payload.get("sector"))
        if isinstance(thesis_context.get("participation"), Mapping) and thesis_context.get("participation"):
            for key, value in dict(thesis_context.get("participation") or {}).items():
                if value is not None and key not in feature:
                    feature[key] = value

        price = self._first(quote.get("ltp"), quote.get("price"))
        price_domain = {"state": "READY" if price is not None else "MISSING", "ready": price is not None, "price": price, "source_time": quote.get("source_time") or quote.get("timestamp")}
        mtf_domain = self._mtf_domain(timeframes, side)
        market_sector = self._market_sector_domain(feature, decision, frozen, side)
        fundamentals = self._fundamental_domain(feature, mode, side)
        participation = self._participation_domain(feature)
        instrument = {"instrument_key": snapshot.get("instrument_key") or instrument_key, "trading_symbol": symbol, "symbol": symbol}
        derivatives = self._derivatives_domain(instrument, {**frozen, "side": side})

        domains = {
            "price": price_domain,
            "mtf": mtf_domain,
            "market_sector": market_sector,
            "fundamentals": fundamentals,
            "participation": participation,
            "derivatives_context": derivatives,
        }
        blockers = []
        if not snapshot_verified:
            blockers.append("CANONICAL_EVIDENCE_SNAPSHOT_UNVERIFIED" if snapshot else "CANONICAL_EVIDENCE_SNAPSHOT_NOT_CAPTURED")
        for name in self.required_domains:
            domain = domains[name]
            if domain.get("required") is False:
                continue
            if domain.get("ready") is not True:
                blockers.append(f"{name.upper()}_CURRENT_EVIDENCE_MISSING")
        if derivatives.get("required") and derivatives.get("ready") is not True:
            blockers.append("DERIVATIVES_CONTEXT_REQUIRED_BUT_UNAVAILABLE")
        contradictions = [name for name, domain in domains.items() if domain.get("alignment") == "CONTRADICTS"]
        full_ready = not blockers
        material = {
            "symbol": symbol, "mode": mode, "side": side,
            "snapshot_id": snapshot.get("snapshot_id"), "snapshot_payload_hash": snapshot.get("payload_hash"),
            "snapshot_captured_at": snapshot.get("captured_at"), "snapshot_state": snapshot_state,
            "domains": domains, "blockers": blockers, "contradictions": contradictions,
        }
        return {
            "authority": self.authority,
            "authority_version": self.authority_version,
            "symbol": symbol,
            "mode": mode,
            "side": side,
            "as_of": at,
            "canonical_snapshot_id": snapshot.get("snapshot_id"),
            "canonical_snapshot_hash": snapshot.get("payload_hash"),
            "canonical_snapshot_captured_at": snapshot.get("captured_at"),
            "canonical_snapshot_state": snapshot_state,
            "canonical_snapshot_verified": snapshot_verified,
            "snapshot_error": snapshot_error,
            "domains": domains,
            "blockers": blockers,
            "contradictions": contradictions,
            "full_thesis_ready": full_ready,
            "validation_scope": "FULL_THESIS" if full_ready else "PARTIAL_CURRENT_EVIDENCE",
            "provider_io": False,
            "broker_authority": "NONE",
            "active_trading_universe": "CASH_ONLY",
            "packet_hash": self._canonical_hash(material),
            "policy": "verified quote + latest immutable canonical evidence snapshot + cached read-only OI context; missing domains remain explicit",
        }
