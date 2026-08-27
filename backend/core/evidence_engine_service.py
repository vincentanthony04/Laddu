"""Production Evidence Engine for Project Laddu v40.

This module is deliberately small and portable.  It does not import the
research factor zoo and it never learns weights from the same rows it ranks.
It converts already-computed, point-in-time Laddu decisions into one stable
decision contract for the user-facing Today desk.

The score is an evidence-completeness/ranking score, not a probability of
profit.  Historical probability is intentionally absent until a separate
walk-forward validator has produced an approved evidence record.
"""
from __future__ import annotations

import threading

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
from typing import Any, Callable, Dict, Iterable, List, Optional
from actionability import is_actionable_signal
from core.numeric_semantics import finite_number
from core.production_mode_policy import (
    POLICY_VERSION, UnsupportedProductionMode, policy_for, require_production_mode,
)


CONTRACT_VERSION = "evidence-v2"
MODEL_VERSION = "laddu-dualdesk-evidence-4.1.0-strict-finite-contract"
INTRADAY_MODEL_VERSION = "laddu-intraday-evidence-4.1.0-strict-finite-contract"
DELIVERY_MODEL_VERSION = "laddu-delivery-evidence-4.1.0-strict-finite-contract"


def model_version_for_mode(mode: Any) -> str:
    canonical = require_production_mode(mode)
    return INTRADAY_MODEL_VERSION if canonical == "intraday" else DELIVERY_MODEL_VERSION
READINESS_STATES = ("READY", "WATCH", "EXTENDED", "AVOID")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _num(value: Any) -> Optional[float]:
    return finite_number(value)


def _text(value: Any) -> str:
    return str(value or "").strip().lower()


def _contains(value: Any, *needles: str) -> bool:
    text = _text(value)
    return any(n.lower() in text for n in needles)


def _component(
    name: str,
    points: float,
    maximum: int,
    reason: str,
    available: bool = True,
    *,
    status: Optional[str] = None,
    data_quality: Optional[str] = None,
    missing_inputs: Optional[List[str]] = None,
) -> Dict[str, Any]:
    maximum_value = finite_number(maximum)
    points_value = finite_number(points)
    if maximum_value is None or maximum_value <= 0 or int(maximum_value) != maximum_value:
        raise ValueError(f"{name}: max_points must be a positive finite integer")
    maximum_int = int(maximum_value)
    if points_value is None:
        points_value = 0.0
        available = False
        status = status or "missing"
        data_quality = data_quality or "invalid_numeric_evidence"
    resolved_status = status or ("available" if available else "missing")
    resolved_quality = data_quality or ("verified" if resolved_status == "available" else "unavailable")
    return {
        "name": name,
        "points": round(max(0.0, min(float(maximum_int), points_value)), 1),
        "max_points": maximum_int,
        "available": resolved_status != "missing",
        "status": resolved_status,
        "data_quality": resolved_quality,
        "missing_inputs": list(missing_inputs or []),
        "reason": reason,
    }


@dataclass(frozen=True)
class EvidenceDecision:
    symbol: str
    exchange: str
    mode: str
    readiness: str
    evidence_score: int
    confidence: str
    thesis: str
    waiting_for: str
    invalidation_reason: str
    entry: Optional[float]
    stop: Optional[float]
    target: Optional[float]
    rr: Optional[float]
    ltp: Optional[float]
    freshness_state: str
    components: List[Dict[str, Any]]
    conflicts: List[str]
    source_decision: str
    observed_at: str
    contract_version: str = CONTRACT_VERSION
    model_version: str = MODEL_VERSION
    historical_evidence: Optional[Dict[str, Any]] = None
    institutional_stage: Optional[str] = None
    institutional_signals: Optional[Dict[str, Any]] = None
    dwap: Optional[Dict[str, Any]] = None
    sector: Optional[str] = None
    sector_index: Optional[str] = None
    sector_change_pct: Optional[float] = None
    relative_volume: Optional[float] = None
    lineage: Optional[Dict[str, Any]] = None
    actionability_verified: bool = False
    fundamental_score: Optional[float] = None
    fundamental_state: Optional[str] = None
    raw_score: float = 0.0
    effective_max_score: float = 100.0
    normalized_score: int = 0
    scoring_state: str = "NORMAL"
    degraded_components: Optional[List[str]] = None
    missing_inputs: Optional[List[str]] = None
    gate_failures: Optional[List[str]] = None
    veto_reasons: Optional[List[str]] = None
    threshold_version: str = POLICY_VERSION
    policy_version: str = POLICY_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class EvidenceEngineService:
    """Pure ranker plus a thin SQLite snapshot adapter."""

    def __init__(self, store: Any = None):
        self.store = store
        # v60.14 P0 fix: write_lock may be absent on lightweight test doubles.
        if store is not None and not hasattr(store, "write_lock"):
            store.write_lock = threading.Lock()

    @staticmethod
    def _institutional(candidate: Dict[str, Any], delivery: Dict[str, Any]) -> Dict[str, Any]:
        score = _num(delivery.get("score"))
        if score is not None:
            # Make stage influence explicit and auditable.  The underlying NSE
            # delivery model supplies 24 points; historically stronger/later
            # lifecycle states receive at most six additional points.  This is
            # deterministic calibration, not an AI probability.
            stage = str(delivery.get("stage") or "Unclassified")
            stage_points = {
                "Climax": 6, "Institutional Trend": 5, "Markup": 4,
                "Reset": 3, "Confirmed Accumulation": 2,
                "Silent Accumulation": 0, "Dormant": 1,
                "Distribution": 0, "Unclassified": 0,
            }.get(stage, 0)
            stake_delta = _num(candidate.get("institutional_delta")) or 0.0
            stake_points = 2 if stake_delta > 0.5 else 1 if stake_delta > 0 else -2 if stake_delta < -0.5 else 0
            pts = (min(100, score) * 0.22) + stage_points + stake_points
            reason = delivery.get("summary") or "NSE delivery evidence"
            return _component(
                "Institutional participation", pts, 30,
                f"{reason} Stage {stage_points}/6 ({stage}); reported FII/MF/DII stake context {stake_points:+d}/2.",
                status="available", data_quality="verified",
            )
        evidence = " ".join(str(x) for x in (candidate.get("evidence") or []))
        bucket = " ".join(str(x) for x in (candidate.get("discovery_buckets") or []))
        combined = f"{evidence} {bucket} {candidate.get('setup') or ''}"
        pts = 0
        if _contains(combined, "institutional", "delivery", "accumulation"): pts += 15
        if _num(candidate.get("institutional_delta")) not in (None, 0): pts += 7
        available = pts > 0
        if available:
            return _component(
                "Institutional participation", pts, 30,
                "Fallback text/stake evidence present; official NSE delivery score is unavailable",
                True, status="degraded", data_quality="fallback",
                missing_inputs=["nse_delivery_score"],
            )
        return _component(
            "Institutional participation", 0, 30, "No current institutional delivery evidence",
            False, status="missing", data_quality="unavailable",
            missing_inputs=["nse_delivery_score", "delivery_history"],
        )

    @staticmethod
    def _technical(candidate: Dict[str, Any]) -> Dict[str, Any]:
        pts, reasons = 0.0, []
        structure = candidate.get("market_structure")
        structure_score = _num(candidate.get("market_structure_score"))
        if structure_score is not None:
            pts += min(10, structure_score / 10)
            reasons.append("measured structure")
        elif _contains(structure, "bull", "higher high", "break", "uptrend", "supportive"):
            pts += 8; reasons.append("constructive structure")
        elif _contains(structure, "bear", "weak", "lower low", "breakdown"):
            reasons.append("weak structure")
        rsi = _num(candidate.get("rsi"))
        if rsi is not None:
            if 48 <= rsi <= 68: pts += 5; reasons.append("healthy RSI")
            elif rsi > 75: reasons.append("RSI extended")
            else: pts += 2
        adx = _num(candidate.get("adx"))
        if adx is not None:
            pts += 4 if adx >= 20 else 2
            reasons.append("trend strength measured")
        weekly, monthly = _text(candidate.get("weekly_state")), _text(candidate.get("monthly_state"))
        if weekly == "bullish": pts += 2; reasons.append("weekly trend aligned")
        if monthly == "bullish": pts += 2; reasons.append("monthly trend aligned")
        ltp, vwap = _num(candidate.get("ltp")), _num(candidate.get("vwap"))
        side = _text(candidate.get("side"))
        if ltp is not None and vwap is not None:
            aligned = (side not in ("short", "sell") and ltp >= vwap) or (side in ("short", "sell") and ltp <= vwap)
            if aligned: pts += 6; reasons.append("VWAP aligned")
        return _component("Technical confirmation", pts, 25, ", ".join(reasons) or "Technical confirmation incomplete", bool(reasons))

    @staticmethod
    def _intraday_setup(candidate: Dict[str, Any]) -> Dict[str, Any]:
        pts, reasons = 0.0, []
        freshness = _text(candidate.get("freshness_state") or candidate.get("price_freshness"))
        if freshness == "live" or freshness.startswith("live"):
            pts += 7; reasons.append("live quote")
        candle_state = _text(candidate.get("candle_state"))
        if candle_state == "fresh":
            pts += 5; reasons.append("closed candle current")
        phase = _text(candidate.get("orb_phase") or candidate.get("phase"))
        orb_state = _text(candidate.get("orb_state"))
        if phase == "orb5_ready":
            pts += 4; reasons.append("ORB5 ready")
        if candidate.get("orb_confirmed") is True:
            pts += 6; reasons.append("ORB confirmed")
        ltp, vw = _num(candidate.get("ltp")), _num(candidate.get("vwap"))
        side = _text(candidate.get("side"))
        if ltp is not None and vw is not None and ((side in ("short", "sell") and ltp <= vw) or (side not in ("short", "sell") and ltp >= vw)):
            pts += 5; reasons.append("session VWAP aligned")
        participation_usable = candidate.get("participation_decision_usable") is True
        if participation_usable and (_num(candidate.get("session_relative_volume")) or 0) >= 1.2:
            pts += 3; reasons.append("volume confirms")
        return _component("Intraday setup", pts, 30, ", ".join(reasons) or "Same-day setup evidence incomplete", bool(reasons))

    @staticmethod
    def _intraday_technical(candidate: Dict[str, Any]) -> Dict[str, Any]:
        pts, reasons = 0.0, []
        side = _text(candidate.get("side"))
        rsi = _num(candidate.get("rsi"))
        if rsi is not None:
            aligned = 48 <= rsi <= 68 if side not in ("short", "sell") else 32 <= rsi <= 52
            pts += 7 if aligned else 2
            reasons.append("side-aware RSI" if aligned else "RSI not ideally aligned")
        adx = _num(candidate.get("adx"))
        if adx is not None:
            pts += 7 if adx >= 22 else 3; reasons.append("trend strength")
        structure = _text(candidate.get("market_structure"))
        aligned_structure = (_contains(structure, "bull", "higher high", "uptrend") if side not in ("short", "sell") else _contains(structure, "bear", "lower low", "downtrend", "breakdown"))
        if aligned_structure:
            pts += 7; reasons.append("structure aligned")
        return _component("Intraday technical", pts, 25, ", ".join(reasons) or "Intraday technical evidence incomplete", bool(reasons))

    @staticmethod
    def _participation(candidate: Dict[str, Any]) -> Dict[str, Any]:
        pts, reasons, measured = 0.0, [], False
        vp_score = _num(candidate.get("volume_profile_score"))
        if vp_score is not None:
            pts += min(10, vp_score / 10); reasons.append("volume profile measured"); measured = True
        elif _contains(candidate.get("volume_profile"), "accum", "expan", "support", "positive"):
            pts += 8; reasons.append("constructive volume profile"); measured = True
        if _contains(candidate.get("volume_state"), "expan", "high", "rising", "strong"):
            pts += 6; reasons.append("volume expanding"); measured = True
        participation_usable = candidate.get("participation_decision_usable") is True
        recent = _num(candidate.get("session_relative_volume") if candidate.get("session_relative_volume") is not None else candidate.get("recent_volume_vs_base")) if participation_usable else None
        if recent is not None:
            pts += 6 if recent >= 1.5 else 3 if recent >= 1.0 else 0
            reasons.append("relative volume measured"); measured = True
        elif candidate.get("participation_decision_usable") is False:
            reasons.append("participation metric diagnostic/stale; no decision points")
        return _component("Participation quality", pts, 20, ", ".join(reasons) or "Participation evidence incomplete", measured)

    @staticmethod
    def _tradeability(candidate: Dict[str, Any]) -> Dict[str, Any]:
        pts, reasons = 0.0, []
        rr = _num(candidate.get("est_net_rr") if candidate.get("est_net_rr") is not None else candidate.get("rr"))
        if rr is not None:
            pts += 6 if rr >= 2 else 4 if rr >= 1.5 else 1
            reasons.append(f"R:R {rr:.2f}")
        if candidate.get("trade_map_valid") is True or _text(candidate.get("level_status")) == "valid":
            pts += 5; reasons.append("level map valid")
        freshness = _text(candidate.get("freshness_state") or candidate.get("price_freshness"))
        if freshness in ("live", "delayed") or freshness.startswith("live"):
            pts += 4; reasons.append("price current")
        elif "historical" in freshness:
            reasons.append("historical price only")
        return _component("Tradeability", pts, 15, ", ".join(reasons) or "Trade map unavailable", bool(reasons))

    @staticmethod
    def _regime(candidate: Dict[str, Any], regime: Dict[str, Any]) -> Dict[str, Any]:
        status = _text(regime.get("state") or candidate.get("index_context"))
        side = _text(candidate.get("side"))
        if status in ("supportive", "risk_on", "bullish", "green"):
            return _component("Market regime", 10 if side not in ("short", "sell") else 2, 10, "Broad market supportive")
        if status in ("hostile", "risk_off", "bearish", "red"):
            return _component("Market regime", 2 if side not in ("short", "sell") else 10, 10, "Broad market conflicts with long exposure")
        return _component("Market regime", 0, 10, "Market regime unavailable; no points awarded", False)

    def _unsupported_decision(self, candidate: Dict[str, Any], reason: str) -> EvidenceDecision:
        mode = _text(candidate.get("mode")) or "unknown"
        return EvidenceDecision(
            symbol=str(candidate.get("symbol") or "").upper(),
            exchange=str(candidate.get("exchange") or "NSE").upper(),
            mode=mode,
            readiness="AVOID",
            evidence_score=0,
            confidence="LOW",
            thesis="Unsupported production desk",
            waiting_for="Use Intraday or Delivery",
            invalidation_reason=reason,
            entry=_num(candidate.get("entry")),
            stop=_num(candidate.get("sl") if candidate.get("sl") is not None else candidate.get("stop")),
            target=_num(candidate.get("t1")),
            rr=_num(candidate.get("est_net_rr") if candidate.get("est_net_rr") is not None else candidate.get("rr")),
            ltp=_num(candidate.get("ltp")),
            freshness_state=_text(candidate.get("freshness_state") or "unknown"),
            components=[],
            conflicts=[reason],
            source_decision=str(candidate.get("decision") or candidate.get("status") or "UNKNOWN"),
            observed_at=str(candidate.get("observed_at") or candidate.get("last_refresh") or candidate.get("last_update") or _now()),
            actionability_verified=False,
            raw_score=0.0,
            effective_max_score=0.0,
            normalized_score=0,
            scoring_state="BLOCKED",
            degraded_components=[],
            missing_inputs=["supported_production_mode"],
            gate_failures=[reason],
            veto_reasons=[reason],
            model_version="unsupported-production-mode",
        )

    def score_candidate(self, candidate: Dict[str, Any], delivery: Optional[Dict[str, Any]] = None, regime: Optional[Dict[str, Any]] = None) -> EvidenceDecision:
        delivery, regime = delivery or {}, regime or {}
        try:
            mode = require_production_mode(candidate.get("mode"))
        except UnsupportedProductionMode as exc:
            return self._unsupported_decision(candidate, str(exc))
        policy = policy_for(mode)
        intraday_policy = mode == "intraday"
        components = (
            [self._intraday_setup(candidate), self._intraday_technical(candidate)]
            if intraday_policy
            else [self._institutional(candidate, delivery), self._technical(candidate)]
        ) + [self._participation(candidate), self._tradeability(candidate), self._regime(candidate, regime)]

        expected_weights = list(policy.weights.values())
        actual_weights = [int(c["max_points"]) for c in components]
        if actual_weights != expected_weights:
            raise RuntimeError(f"component contract drift for {mode}: {actual_weights} != {expected_weights}")

        conflicts: List[str] = []
        gate_failures: List[str] = []
        veto_reasons: List[str] = []
        decision = _text(candidate.get("decision"))
        side = _text(candidate.get("side"))
        freshness = _text(candidate.get("freshness_state") or candidate.get("price_freshness") or "unknown")
        rsi = _num(candidate.get("rsi"))
        rr = _num(candidate.get("est_net_rr") if candidate.get("est_net_rr") is not None else candidate.get("rr"))

        def conflict(reason: str, *, veto: bool = False) -> None:
            if reason not in conflicts:
                conflicts.append(reason)
            if reason not in gate_failures:
                gate_failures.append(reason)
            if veto and reason not in veto_reasons:
                veto_reasons.append(reason)

        if side not in ("long", "short", "buy", "sell"):
            conflict("Direction is missing or unsupported", veto=True)
        if mode == "delivery" and side in ("short", "sell"):
            conflict("Delivery production desk is long-only", veto=True)
        if decision in ("avoid", "avoid_long", "blocked", "no_trade", "wait") or side in ("avoid_long",):
            conflict("Source engine blocks the trade", veto=True)
        if decision == "accumulate":
            conflict("Generic ACCUMULATE is not a canonical production action", veto=True)
        if _text(candidate.get("status")) == "blocked":
            conflict("Source engine gate status is BLOCKED", veto=True)
        if _contains(candidate.get("market_structure"), "bear", "weak", "breakdown", "lower low") and side not in ("short", "sell"):
            conflict("Price structure conflicts with a long thesis")
        if rsi is not None and rsi > 75:
            conflict("Momentum is extended")
        if rr is None:
            conflict("Net reward-to-risk is unavailable")
        elif rr < policy.minimum_net_rr:
            conflict(f"Net reward-to-risk {rr:.2f} is below {policy.minimum_net_rr:.2f}")
        if freshness in ("stale", "invalid", "pending") or "stale" in freshness:
            conflict("Price evidence is stale", veto=True)
        if intraday_policy and _contains(candidate.get("orb_state"), "failed_breakout", "failed_breakdown"):
            conflict("ORB rejection is reversal-watch evidence, not a confirmed continuation")

        actionable_source = decision in ("trade", "buy", "sell") or _text(candidate.get("status")) in ("promoted", "triggered", "selected", "open", "signal_open")
        if actionable_source and not (candidate.get("trade_map_valid") is True or _text(candidate.get("level_status")) == "valid"):
            conflict("Entry/stop/target map is not validated", veto=True)

        if mode == "intraday":
            candle_state = _text(candidate.get("candle_state"))
            if candidate.get("market_open_at_decision") is not True:
                conflict("Intraday requires market-open decision time", veto=True)
            if candidate.get("hard_late_session_block") is True:
                conflict("Intraday entry is blocked inside the hard close window", veto=True)
            if freshness not in ("live", "live_current") and not freshness.startswith("live"):
                conflict("Intraday requires a verified live quote", veto=True)
            if candle_state not in ("fresh", "live", "delayed_warning"):
                conflict("Intraday requires a fresh completed 5-minute candle", veto=True)
        else:
            institutional = components[0]
            if institutional.get("status") != "available":
                conflict("Institutional delivery evidence is degraded or unavailable", veto=True)
            if delivery.get("state") == "collecting_evidence":
                conflict("Institutional delivery history is still collecting evidence", veto=True)
            if _num(candidate.get("fundamental_score")) is None or _text(candidate.get("fundamental_state")) not in ("strong", "acceptable"):
                conflict("Verified fundamental contract is incomplete or weak", veto=True)

        raw_score = sum(float(c["points"]) for c in components)
        effective_max = sum(float(c["max_points"]) for c in components if c.get("status") != "missing")
        penalty = min(30, len(conflicts) * 6)
        score = max(0, min(100, int(round(raw_score - penalty))))
        normalized_score = max(0, min(100, int(round((max(0.0, raw_score - penalty) / effective_max) * 100)))) if effective_max > 0 else 0
        degraded_components = [str(c["name"]) for c in components if c.get("status") != "available"]
        missing_inputs = sorted({str(item) for c in components for item in (c.get("missing_inputs") or [])})
        scoring_state = "BLOCKED" if veto_reasons else "DEGRADED" if degraded_components else "NORMAL"

        ltp, entry = _num(candidate.get("ltp")), _num(candidate.get("entry"))
        extended = False
        extension_reference = (
            (_num(candidate.get("orb_low")) if side in ("short", "sell") else _num(candidate.get("orb_high")))
            if intraday_policy and candidate.get("orb_confirmed") is True
            else (_num(candidate.get("planned_entry")) if _num(candidate.get("planned_entry")) is not None else entry)
        )
        if ltp is not None and extension_reference not in (None, 0):
            extended = abs(ltp - extension_reference) / abs(extension_reference) > (policy.extension_limit_pct / 100.0)

        hard_avoid = bool(veto_reasons)
        ready_gate = (
            actionable_source
            and score >= policy.evidence_ready_threshold
            and not gate_failures
            and rr is not None
            and rr >= policy.minimum_net_rr
            and scoring_state == "NORMAL"
        )
        if hard_avoid:
            readiness = "AVOID"
        elif extended and score >= 60:
            readiness = "EXTENDED"
        elif ready_gate:
            readiness = "READY"
        else:
            readiness = "WATCH"

        confidence = "HIGH" if score >= 75 and not conflicts and scoring_state == "NORMAL" else "MEDIUM" if score >= 55 else "LOW"
        top = sorted(components, key=lambda c: c["points"] / c["max_points"], reverse=True)[:2]
        thesis = "; ".join(c["reason"] for c in top if c["points"] > 0) or "Evidence is incomplete; keep under observation"
        waiting = "Entry map and all production gates confirmed" if readiness == "READY" else (
            "Pullback toward validated entry" if readiness == "EXTENDED" else
            "Conflict resolution and fresh price confirmation" if readiness == "AVOID" else
            str(candidate.get("waiting_for") or "More institutional, technical and trade-map confirmation")
        )
        invalidation_reason = "; ".join(conflicts) or str(candidate.get("risk") or "Close beyond the validated stop or thesis failure")
        verified = is_actionable_signal(dict(candidate, mode=mode, rank_readiness=readiness), require_final_authority=False) and readiness == "READY"
        desk_model_version = model_version_for_mode(mode)
        return EvidenceDecision(
            symbol=str(candidate.get("symbol") or "").upper(), exchange=str(candidate.get("exchange") or "NSE").upper(),
            mode=mode, readiness=readiness, evidence_score=score,
            confidence=confidence, thesis=thesis, waiting_for=waiting, invalidation_reason=invalidation_reason,
            entry=entry, stop=_num(candidate.get("sl") if candidate.get("sl") is not None else candidate.get("invalidation")),
            target=_num(candidate.get("t1")), rr=rr, ltp=ltp, freshness_state=freshness or "unknown",
            components=components, conflicts=conflicts, source_decision=str(candidate.get("decision") or candidate.get("status") or "UNKNOWN"),
            observed_at=str(candidate.get("observed_at") or candidate.get("last_refresh") or candidate.get("last_update") or _now()),
            institutional_stage=delivery.get("stage"), institutional_signals=delivery.get("signals") or {}, dwap=delivery.get("dwap") or {},
            sector=str(candidate.get("sector") or candidate.get("sector_label") or "") or None,
            sector_index=str(candidate.get("sector_index") or "") or None,
            sector_change_pct=_num(candidate.get("sector_change_pct")),
            relative_volume=_num(candidate.get("recent_volume_vs_base")),
            actionability_verified=verified, fundamental_score=_num(candidate.get("fundamental_score")), fundamental_state=str(candidate.get("fundamental_state") or "") or None,
            model_version=desk_model_version,
            lineage={"institutional_model": delivery.get("model_version"), "institutional_formula": delivery.get("formula"), "delivery_coverage": delivery.get("coverage"), "evidence_model": desk_model_version, "policy_version": POLICY_VERSION},
            raw_score=round(raw_score, 1), effective_max_score=round(effective_max, 1), normalized_score=normalized_score,
            scoring_state=scoring_state, degraded_components=degraded_components, missing_inputs=missing_inputs,
            gate_failures=gate_failures, veto_reasons=veto_reasons,
        )

    @staticmethod
    def _dedupe(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
        best: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            symbol = str((row or {}).get("symbol") or "").upper().strip()
            if not symbol:
                continue
            current = best.get(symbol)
            row_score = _num((row or {}).get("score"))
            if row_score is None:
                row_score = _num((row or {}).get("priority_score"))
            current_score = _num((current or {}).get("score"))
            if current_score is None:
                current_score = _num((current or {}).get("priority_score"))
            value = int(row_score) if row_score is not None else 0
            current_value = int(current_score) if current_score is not None else -1
            if current is None or value > current_value:
                best[symbol] = dict(row)
        return list(best.values())

    def build_today(self, rows: Iterable[Dict[str, Any]], delivery_lookup: Optional[Callable[[str], Dict[str, Any]]] = None,
                    regime: Optional[Dict[str, Any]] = None, limit: int = 15, persist: bool = True) -> Dict[str, Any]:
        decisions = []
        for candidate in self._dedupe(rows):
            try:
                mode = require_production_mode(candidate.get("mode"))
            except UnsupportedProductionMode:
                continue
            candidate = dict(candidate, mode=mode)
            delivery = delivery_lookup(candidate.get("symbol")) if delivery_lookup and mode == "delivery" else {}
            stage = _text(delivery.get("stage") or candidate.get("institutional_stage"))
            prepared = _text(candidate.get("prepared_state")) in ("armed", "triggered")
            if mode == "delivery" and stage == "dormant" and not prepared:
                continue
            item = self.score_candidate(candidate, delivery=delivery, regime=regime).to_dict()
            approved_evidence = self._approved_historical_evidence(mode)
            item["historical_evidence"] = approved_evidence
            item["historical_evidence_state"] = "APPROVED_WALK_FORWARD" if approved_evidence else "UNAVAILABLE_UNTIL_WALK_FORWARD_APPROVAL"
            item["validation_model_id"] = model_version_for_mode(mode)
            decisions.append(item)
        state_order = {"READY": 0, "WATCH": 1, "EXTENDED": 2, "AVOID": 3}
        decisions.sort(key=lambda d: (state_order.get(d["readiness"], 9), -d["evidence_score"], d["symbol"]))
        limit_value = finite_number(limit)
        safe_limit = int(limit_value) if limit_value is not None and limit_value >= 1 and int(limit_value) == limit_value else 15
        decisions = decisions[:max(1, min(safe_limit, 100))]
        result = {
            "ok": True, "as_of": _now(), "contract_version": CONTRACT_VERSION, "model_version": MODEL_VERSION,
            "score_semantics": "Evidence completeness/ranking score; not probability of profit",
            "policy_version": POLICY_VERSION,
            "allowed_modes": ["intraday", "delivery"],
            "historical_evidence_state": "APPROVED_WALK_FORWARD" if decisions and all(d.get("historical_evidence") for d in decisions) else "UNAVAILABLE_UNTIL_EACH_DESK_WALK_FORWARD_APPROVAL",
            "counts": {state: sum(1 for d in decisions if d["readiness"] == state) for state in READINESS_STATES},
            "opportunities": decisions,
        }
        if persist and self.store is not None:
            self._persist(result)
        return result

    def _approved_historical_evidence(self, mode: Any) -> Optional[Dict[str, Any]]:
        if self.store is None:
            return None
        try:
            model_id = model_version_for_mode(mode)
            row = self.store.conn.execute("SELECT payload_json FROM validation_approvals WHERE model_id=? AND status='APPROVED' ORDER BY validated_at DESC LIMIT 1", (model_id,)).fetchone()
            if not row:
                return None
            value = json.loads(row[0])
            return {k: value.get(k) for k in ("approval_id", "authority_version", "status", "validated_at", "n_test", "horizon_days", "mean_net_return", "mean_excess_return", "win_rate", "fold_stability", "max_drawdown")}
        except Exception:
            return None

    def _persist(self, payload: Dict[str, Any]) -> None:
        conn = self.store.conn
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        snapshot_id = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]
        with self.store.write_lock:
            self._ensure_schema(conn)
            conn.execute("INSERT OR IGNORE INTO evidence_snapshots(snapshot_id,as_of,contract_version,model_version,payload_json) VALUES(?,?,?,?,?)",
                         (snapshot_id, payload["as_of"], CONTRACT_VERSION, MODEL_VERSION, canonical))
            conn.commit()
        payload["snapshot_id"] = snapshot_id

    @staticmethod
    def _ensure_schema(conn: Any) -> None:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS evidence_snapshots (
          snapshot_id TEXT PRIMARY KEY, as_of TEXT NOT NULL, contract_version TEXT NOT NULL,
          model_version TEXT NOT NULL, payload_json TEXT NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS ix_evidence_snapshots_asof ON evidence_snapshots(as_of);
        """)
        conn.commit()

    def history(self, limit: int = 20) -> Dict[str, Any]:
        if self.store is None:
            return {"ok": True, "snapshots": []}
        self._ensure_schema(self.store.conn)
        limit_value = finite_number(limit)
        safe_limit = int(limit_value) if limit_value is not None and limit_value >= 1 and int(limit_value) == limit_value else 20
        rows = self.store.conn.execute("SELECT snapshot_id,as_of,contract_version,model_version,payload_json FROM evidence_snapshots ORDER BY as_of DESC LIMIT ?", (max(1, min(safe_limit, 200)),)).fetchall()
        return {"ok": True, "snapshots": [{"snapshot_id": r[0], "as_of": r[1], "contract_version": r[2], "model_version": r[3], "payload": json.loads(r[4])} for r in rows]}
