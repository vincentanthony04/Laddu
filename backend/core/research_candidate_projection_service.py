"""Fail-closed projection of live scanner evidence into Research-only trade maps.

This service does not create production decisions. It may convert an analysed
scanner/opportunity row into a shadow Research candidate only when exact
identity, side, current price, ATR/structure evidence and point-in-time lineage
are present. Existing valid trade maps are preserved. Derived maps are fully
labelled and receive zero production/broker influence.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, Mapping

SERVICE_VERSION = "research-candidate-projection-1.2.0-ranking-admission-separation"
MIN_RR = 1.50


def _number(*values: Any) -> float | None:
    for value in values:
        try:
            parsed = float(value)
            if parsed > 0:
                return parsed
        except (TypeError, ValueError):
            continue
    return None


def _text(*values: Any) -> str:
    for value in values:
        value = str(value or "").strip()
        if value:
            return value
    return ""


def _side(value: Any) -> str:
    raw = str(value or "").upper().strip()
    if raw in {"BUY", "BULLISH", "LONG"}:
        return "LONG"
    if raw in {"SELL", "BEARISH", "SHORT"}:
        return "SHORT"
    return ""


def _parse_time(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


class ResearchCandidateProjectionService:
    def __init__(self, *, now: datetime | None = None):
        self.now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)

    def _fresh(self, row: Mapping[str, Any], desk: str) -> tuple[bool, str | None, str]:
        observed = _text(
            row.get("decision_ts"), row.get("decision_as_of"), row.get("observed_at"),
            row.get("updated_at"), row.get("last_seen_at"), row.get("last_refresh"),
            row.get("provider_timestamp"), row.get("quote_time"),
        )
        stamp = _parse_time(observed)
        if stamp is None:
            return False, None, "missing_point_in_time_timestamp"
        age = self.now - stamp
        # Research capture tolerates a completed scan snapshot, but not an
        # unbounded legacy row. Live settlement still requires fresh quotes.
        limit = timedelta(hours=30) if desk == "intraday" else timedelta(days=8)
        if age < timedelta(minutes=-5) or age > limit:
            return False, observed, "stale_candidate_snapshot"
        return True, observed, ""

    def project_for_ranking(self, row: Mapping[str, Any], *, desk: str) -> Dict[str, Any]:
        """Project exact PIT scanner evidence into the immutable ranking population.

        Ranking/learning capture is deliberately earlier than Model-Paper admission.
        A candidate may be valuable cross-sectional evidence before an executable
        Entry/T1/SL map exists.  This path therefore requires exact identity and
        decision-time freshness, but never manufactures trade geometry or grants
        production/broker authority.  Model-Paper admission remains governed by
        ``project``/QuantPaperActivationService and keeps the strict trade-map gate.
        """
        item = dict(row or {})
        desk = str(desk or "").lower().strip()
        symbol = _text(item.get("symbol"), item.get("trading_symbol")).upper()
        instrument_key = _text(item.get("instrument_key"))
        row_mode = str(item.get("mode") or desk).lower().strip()
        if desk not in {"delivery", "intraday"} or row_mode not in {desk, "all", ""}:
            return {"ok": False, "reason": "mode_mismatch", "symbol": symbol}
        if not symbol or not instrument_key:
            return {"ok": False, "reason": "exact_identity_missing", "symbol": symbol}
        fresh, observed_at, freshness_reason = self._fresh(item, desk)
        if not fresh:
            return {"ok": False, "reason": freshness_reason, "symbol": symbol, "observed_at": observed_at}
        side = _side(item.get("side") or item.get("position") or item.get("direction") or (item.get("trade_map") or {}).get("side"))
        origin_rejections = item.get("rejection_reasons")
        if not isinstance(origin_rejections, list):
            origin_rejections = [origin_rejections] if origin_rejections else []
        origin_blockers = item.get("promotion_blocked_by")
        if not isinstance(origin_blockers, list):
            origin_blockers = [origin_blockers] if origin_blockers else []
        projected = dict(item)
        projected.update({
            "symbol": symbol, "trading_symbol": symbol, "instrument_key": instrument_key,
            "exchange": str(item.get("exchange") or "NSE").upper(), "mode": desk,
            "side": side or "UNKNOWN", "identity_verified": True,
            "decision_ts": observed_at, "decision_as_of": observed_at, "observed_at": observed_at,
            "research_only": True, "model_influence_applied": False,
            "production_influence": 0.0, "broker_authority": "NONE",
            "_evaluation_objective": "CROSS_SECTIONAL_RANKING",
            "research_population_capture": True,
            "trade_map_required_for_capture": False,
            "origin_decision_id": _text(item.get("decision_id")),
            "origin_signal_id": _text(item.get("signal_id"), item.get("source_signal_id")),
            "origin_production_status": _text(item.get("production_status"), item.get("status")).upper(),
            "origin_production_decision": _text(item.get("production_decision"), item.get("decision")).upper(),
            "origin_rejection_reasons": [str(value) for value in origin_rejections if value],
            "origin_promotion_blocked_by": [str(value) for value in origin_blockers if value],
            "origin_qualification_blocker": _text(item.get("qualification_blocker"), item.get("waiting_for")),
            "origin_lineage_version": "research-origin-lineage-1.0.0",
            "research_projection_version": SERVICE_VERSION,
        })
        return {"ok": True, "candidate": projected, "source": "exact_pit_ranking_capture"}

    def project_many_for_ranking(self, rows: Iterable[Mapping[str, Any]], *, desk: str, limit: int = 240) -> Dict[str, Any]:
        accepted: list[Dict[str, Any]] = []
        rejected: list[Dict[str, Any]] = []
        seen: set[str] = set()
        for row in rows or []:
            result = self.project_for_ranking(row, desk=desk)
            if result.get("ok"):
                candidate = dict(result["candidate"])
                key = str(candidate.get("instrument_key") or "")
                if key in seen:
                    continue
                seen.add(key)
                accepted.append(candidate)
                if len(accepted) >= max(1, int(limit)):
                    break
            else:
                rejected.append({"symbol": result.get("symbol"), "reason": result.get("reason")})
        return {
            "ok": bool(accepted), "version": SERVICE_VERSION,
            "objective": "CROSS_SECTIONAL_RANKING",
            "accepted": accepted, "accepted_count": len(accepted),
            "rejected": rejected[:80], "rejected_count": len(rejected),
            "trade_map_required": False, "production_influence": 0.0,
            "broker_authority": "NONE",
        }

    @staticmethod
    def _existing_map(row: Mapping[str, Any], side: str) -> tuple[float, float, float, str] | None:
        trade_map = dict(row.get("trade_map") or {})
        entry = _number(row.get("planned_entry"), row.get("entry"), trade_map.get("entry"))
        target = _number(row.get("planned_t1"), row.get("target"), row.get("t1"), trade_map.get("target"), trade_map.get("t1"))
        stop = _number(row.get("planned_sl"), row.get("stop"), row.get("sl"), trade_map.get("stop"), trade_map.get("sl"))
        geometry = bool(
            side == "LONG" and all(value is not None for value in (stop, entry, target)) and stop < entry < target
            or side == "SHORT" and all(value is not None for value in (target, entry, stop)) and target < entry < stop
        )
        if not geometry:
            return None
        risk = abs(entry - stop)
        reward = abs(target - entry)
        if risk <= 0 or reward / risk < MIN_RR:
            return None
        return entry, target, stop, "canonical_existing_trade_map"

    @staticmethod
    def _derived_map(row: Mapping[str, Any], side: str) -> tuple[float, float, float, str] | None:
        price = _number(row.get("ltp"), row.get("current_price"), row.get("close"), row.get("price"))
        atr = _number(row.get("atr14"), row.get("atr"), (row.get("technical") or {}).get("atr14") if isinstance(row.get("technical"), Mapping) else None)
        support = _number(row.get("support"), row.get("canonical_support"), (row.get("levels") or {}).get("support") if isinstance(row.get("levels"), Mapping) else None)
        resistance = _number(row.get("resistance"), row.get("canonical_resistance"), (row.get("levels") or {}).get("resistance") if isinstance(row.get("levels"), Mapping) else None)
        if price is None or atr is None or atr / price > 0.25:
            return None
        # At least one independently calculated structural level is required;
        # ATR alone is not enough to manufacture a research trade map.
        if support is None and resistance is None:
            return None
        entry = price
        if side == "LONG":
            structural_stop = support if support is not None and support < entry else None
            stop = structural_stop if structural_stop and 0.20 * atr <= entry - structural_stop <= 2.50 * atr else entry - atr
            risk = entry - stop
            structural_target = resistance if resistance is not None and resistance > entry else None
            target = structural_target if structural_target and structural_target - entry >= MIN_RR * risk else entry + 1.80 * risk
        else:
            structural_stop = resistance if resistance is not None and resistance > entry else None
            stop = structural_stop if structural_stop and 0.20 * atr <= structural_stop - entry <= 2.50 * atr else entry + atr
            risk = stop - entry
            structural_target = support if support is not None and support < entry else None
            target = structural_target if structural_target and entry - structural_target >= MIN_RR * risk else entry - 1.80 * risk
        if min(entry, target, stop) <= 0 or risk <= 0:
            return None
        reward = abs(target - entry)
        if reward / risk < MIN_RR:
            return None
        return round(entry, 4), round(target, 4), round(stop, 4), "research_atr_structure_projection"

    def project(self, row: Mapping[str, Any], *, desk: str) -> Dict[str, Any]:
        item = dict(row or {})
        desk = str(desk or "").lower().strip()
        symbol = _text(item.get("symbol"), item.get("trading_symbol")).upper()
        instrument_key = _text(item.get("instrument_key"))
        side = _side(item.get("side") or item.get("position") or item.get("direction") or (item.get("trade_map") or {}).get("side"))
        row_mode = str(item.get("mode") or desk).lower().strip()
        if desk not in {"delivery", "intraday"} or row_mode not in {desk, "all", ""}:
            return {"ok": False, "reason": "mode_mismatch", "symbol": symbol}
        if not symbol or not instrument_key:
            return {"ok": False, "reason": "exact_identity_missing", "symbol": symbol}
        if side not in {"LONG", "SHORT"}:
            return {"ok": False, "reason": "direction_missing", "symbol": symbol}
        fresh, observed_at, freshness_reason = self._fresh(item, desk)
        if not fresh:
            return {"ok": False, "reason": freshness_reason, "symbol": symbol, "observed_at": observed_at}
        trade_map = self._existing_map(item, side) or self._derived_map(item, side)
        if trade_map is None:
            return {"ok": False, "reason": "valid_trade_map_unavailable", "symbol": symbol, "observed_at": observed_at}
        entry, target, stop, source = trade_map
        risk = abs(entry - stop)
        reward = abs(target - entry)
        # Preserve the exact production-origin context *before* converting the
        # row into a Research-only candidate.  Previous projections overwrote
        # status/decision with RESEARCH, which made later Research-vs-Final
        # attribution unable to prove promotion/rejection/admission lineage.
        # These fields are frozen inside the immutable population member; they
        # never grant production authority by themselves.
        origin_rejections = item.get("rejection_reasons")
        if not isinstance(origin_rejections, list):
            origin_rejections = [origin_rejections] if origin_rejections else []
        origin_blockers = item.get("promotion_blocked_by")
        if not isinstance(origin_blockers, list):
            origin_blockers = [origin_blockers] if origin_blockers else []
        origin = {
            "origin_decision_id": _text(item.get("decision_id")),
            "origin_signal_id": _text(item.get("signal_id"), item.get("source_signal_id")),
            "origin_production_status": _text(item.get("production_status"), item.get("status")).upper(),
            "origin_production_decision": _text(item.get("production_decision"), item.get("decision")).upper(),
            "origin_rejection_reasons": [str(value) for value in origin_rejections if value],
            "origin_promotion_blocked_by": [str(value) for value in origin_blockers if value],
            "origin_qualification_blocker": _text(item.get("qualification_blocker"), item.get("waiting_for")),
            "origin_lineage_version": "research-origin-lineage-1.0.0",
        }
        projected = dict(item)
        projected.update({
            "symbol": symbol,
            "trading_symbol": symbol,
            "instrument_key": instrument_key,
            "exchange": str(item.get("exchange") or "NSE").upper(),
            "mode": desk,
            "side": side,
            "identity_verified": True,
            "decision_ts": observed_at,
            "decision_as_of": observed_at,
            "observed_at": observed_at,
            "planned_entry": entry,
            "planned_t1": target,
            "planned_sl": stop,
            "planned_rr": round(reward / risk, 4),
            "trade_map_valid": True,
            "level_status": "valid",
            "trade_map_source": source,
            "research_trade_map_source": source,
            "research_only": True,
            "status": "RESEARCH",
            "decision": "RESEARCH",
            "production_status": "RESEARCH",
            "production_decision": "RESEARCH",
            "model_influence_applied": False,
            "production_influence": 0.0,
            "broker_authority": "NONE",
            "_evaluation_objective": "TARGET_STOP_TRADE_MAP_OVERLAY",
            "research_projection_version": SERVICE_VERSION,
            **origin,
        })
        return {"ok": True, "candidate": projected, "source": source}

    def project_many(self, rows: Iterable[Mapping[str, Any]], *, desk: str, limit: int = 120) -> Dict[str, Any]:
        accepted: list[Dict[str, Any]] = []
        rejected: list[Dict[str, Any]] = []
        seen: set[str] = set()
        for row in rows or []:
            result = self.project(row, desk=desk)
            if result.get("ok"):
                candidate = dict(result["candidate"])
                key = str(candidate.get("instrument_key") or "")
                if key in seen:
                    continue
                seen.add(key)
                accepted.append(candidate)
                if len(accepted) >= max(1, int(limit)):
                    break
            else:
                rejected.append({"symbol": result.get("symbol"), "reason": result.get("reason")})
        return {
            "ok": bool(accepted),
            "version": SERVICE_VERSION,
            "accepted": accepted,
            "accepted_count": len(accepted),
            "rejected": rejected[:40],
            "rejected_count": len(rejected),
            "production_influence": 0.0,
            "broker_authority": "NONE",
        }
