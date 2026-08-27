"""Vectorised evidence-screening accelerator with exact-engine parity.

This module is intentionally *not* a production decision authority.  It mirrors
only the deterministic, point-in-time EvidenceEngineService arithmetic so broad
historical populations can be screened in NumPy arrays before the exact engine
and chronological ProductionReplayService revalidate every shortlisted row.

Safety contract
---------------
* EvidenceEngineService remains the canonical score/readiness authority.
* Vector output may accelerate research/backtest screening only.
* A release must prove parity for score/readiness/scoring-state.
* The accelerator never grants actionability; exact CandidateEligibilityAuthority
  is evaluated only after shortlist revalidation.
* A conservative ``candidate_superset`` mask is exposed separately.  It only
  rejects rows that are guaranteed to be blocked by production policy, so a
  bulk accelerator never needs to use vector score as a hard production gate.
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

import numpy as np

from core.evidence_engine_service import EvidenceEngineService
from core.numeric_semantics import finite_number
from core.production_mode_policy import policy_for, require_production_mode

AUTHORITY_NAME = "VectorizedEvidenceScreeningService"
AUTHORITY_VERSION = "1.1.0-strict-finite-parity"
PARITY_CONTRACT_VERSION = "vectorized-exact-evidence-parity-1.1.0-no-actionability-authority"
ROLE = "NON_AUTHORITATIVE_RESEARCH_ACCELERATOR"


def _text(value: Any) -> str:
    return str(value or "").strip().lower()


def _float(value: Any) -> float:
    out = finite_number(value)
    return out if out is not None else float("nan")


def _contains(values: np.ndarray, *needles: str) -> np.ndarray:
    text = values.astype(str)
    out = np.zeros(text.shape, dtype=bool)
    for needle in needles:
        out |= np.char.find(text, needle.lower()) >= 0
    return out


def _strings(rows: Sequence[Mapping[str, Any]], key: str, fallback: str | None = None) -> np.ndarray:
    if fallback is None:
        return np.asarray([_text(row.get(key)) for row in rows], dtype=str)
    return np.asarray([_text(row.get(key) if row.get(key) is not None else row.get(fallback)) for row in rows], dtype=str)


def _nums(rows: Sequence[Mapping[str, Any]], key: str, fallback: str | None = None) -> np.ndarray:
    if fallback is None:
        return np.asarray([_float(row.get(key)) for row in rows], dtype=float)
    return np.asarray([_float(row.get(key) if row.get(key) is not None else row.get(fallback)) for row in rows], dtype=float)


class VectorizedEvidenceScreeningService:
    authority = AUTHORITY_NAME
    authority_version = AUTHORITY_VERSION
    parity_contract_version = PARITY_CONTRACT_VERSION
    role = ROLE

    def screen(
        self,
        candidates: Iterable[Mapping[str, Any]],
        *,
        mode: str,
        deliveries: Sequence[Mapping[str, Any]] | None = None,
        regimes: Sequence[Mapping[str, Any]] | None = None,
    ) -> dict[str, Any]:
        desk = require_production_mode(mode)
        policy = policy_for(desk)
        rows = [dict(row or {}) for row in candidates]
        n = len(rows)
        ds = [dict(row or {}) for row in (deliveries or [{} for _ in range(n)])]
        rs = [dict(row or {}) for row in (regimes or [{} for _ in range(n)])]
        if len(ds) != n or len(rs) != n:
            raise ValueError("deliveries/regimes must align one-to-one with candidates")
        if not rows:
            return {
                "ok": True, "authority": self.authority, "authority_version": self.authority_version,
                "role": self.role, "mode": desk, "count": 0, "results": [],
            }

        # Shared columns.
        side = _strings(rows, "side")
        decision = _strings(rows, "decision")
        status = _strings(rows, "status")
        freshness = _strings(rows, "freshness_state", "price_freshness")
        structure = _strings(rows, "market_structure")
        rsi = _nums(rows, "rsi")
        adx = _nums(rows, "adx")
        rr = _nums(rows, "est_net_rr", "rr")
        ltp = _nums(rows, "ltp")
        vwap = _nums(rows, "vwap")
        entry = _nums(rows, "entry")
        planned_entry = _nums(rows, "planned_entry")
        trade_map_valid = np.asarray([
            row.get("trade_map_valid") is True or _text(row.get("level_status")) == "valid" for row in rows
        ], dtype=bool)
        is_short = np.isin(side, ["short", "sell"])
        side_valid = np.isin(side, ["long", "short", "buy", "sell"])
        actionable_source = np.isin(decision, ["trade", "buy", "sell"]) | np.isin(
            status, ["promoted", "triggered", "selected", "open", "signal_open"]
        )

        # Component 1 + 2 differ by desk.
        if desk == "intraday":
            setup = np.zeros(n, dtype=float)
            setup_reason = np.zeros(n, dtype=bool)
            live = (freshness == "live") | np.char.startswith(freshness, "live")
            setup += live * 7; setup_reason |= live
            candle = _strings(rows, "candle_state")
            fresh_candle = candle == "fresh"
            setup += fresh_candle * 5; setup_reason |= fresh_candle
            phase = np.asarray([_text(row.get("orb_phase") or row.get("phase")) for row in rows], dtype=str)
            mature = phase == "orb5_ready"
            setup += mature * 4; setup_reason |= mature
            orb_confirmed = np.asarray([row.get("orb_confirmed") is True for row in rows], dtype=bool)
            setup += orb_confirmed * 6; setup_reason |= orb_confirmed
            vw_aligned = np.isfinite(ltp) & np.isfinite(vwap) & np.where(is_short, ltp <= vwap, ltp >= vwap)
            setup += vw_aligned * 5; setup_reason |= vw_aligned
            srv = _nums(rows, "session_relative_volume")
            participation_usable = np.asarray([row.get("participation_decision_usable") is not False for row in rows], dtype=bool)
            vol_confirm = participation_usable & (np.nan_to_num(srv, nan=0.0) >= 1.2)
            setup += vol_confirm * 3; setup_reason |= vol_confirm
            setup = np.minimum(setup, 30.0)

            technical = np.zeros(n, dtype=float)
            technical_reason = np.zeros(n, dtype=bool)
            rsi_present = np.isfinite(rsi)
            rsi_aligned = rsi_present & np.where(is_short, (rsi >= 32) & (rsi <= 52), (rsi >= 48) & (rsi <= 68))
            technical += np.where(rsi_present, np.where(rsi_aligned, 7.0, 2.0), 0.0)
            technical_reason |= rsi_present
            adx_present = np.isfinite(adx)
            technical += np.where(adx_present, np.where(adx >= 22, 7.0, 3.0), 0.0)
            technical_reason |= adx_present
            aligned_structure = np.where(
                is_short,
                _contains(structure, "bear", "lower low", "downtrend", "breakdown"),
                _contains(structure, "bull", "higher high", "uptrend"),
            )
            technical += aligned_structure * 7; technical_reason |= aligned_structure
            technical = np.minimum(technical, 25.0)
            component1, component2 = setup, technical
            available1, available2 = setup_reason, technical_reason
            degraded1 = ~available1
        else:
            delivery_score = np.asarray([_float(row.get("score")) for row in ds], dtype=float)
            delivery_score_present = np.isfinite(delivery_score)
            stages = np.asarray([str(row.get("stage") or "Unclassified") for row in ds], dtype=str)
            stage_map = {
                "Climax": 6, "Institutional Trend": 5, "Markup": 4, "Reset": 3,
                "Confirmed Accumulation": 2, "Silent Accumulation": 0,
                "Dormant": 1, "Distribution": 0, "Unclassified": 0,
            }
            stage_points = np.asarray([stage_map.get(stage, 0) for stage in stages], dtype=float)
            institutional_delta = _nums(rows, "institutional_delta")
            stake_points = np.where(institutional_delta > 0.5, 2.0,
                          np.where(institutional_delta > 0, 1.0,
                          np.where(institutional_delta < -0.5, -2.0, 0.0)))
            official_pts = np.minimum(100.0, np.nan_to_num(delivery_score, nan=0.0)) * 0.22 + stage_points + stake_points
            official_pts = np.clip(official_pts, 0.0, 30.0)

            evidence_text = np.asarray([
                _text(" ".join(str(x) for x in (row.get("evidence") or [])) + " " +
                      " ".join(str(x) for x in (row.get("discovery_buckets") or [])) + " " +
                      str(row.get("setup") or ""))
                for row in rows
            ], dtype=str)
            fallback_pts = _contains(evidence_text, "institutional", "delivery", "accumulation").astype(float) * 15.0
            fallback_pts += (np.isfinite(institutional_delta) & (institutional_delta != 0)).astype(float) * 7.0
            fallback_available = fallback_pts > 0
            institutional = np.where(delivery_score_present, official_pts, fallback_pts)
            institutional_status_available = delivery_score_present
            institutional_present = delivery_score_present | fallback_available

            technical = np.zeros(n, dtype=float)
            technical_reason = np.zeros(n, dtype=bool)
            structure_score = _nums(rows, "market_structure_score")
            ss_present = np.isfinite(structure_score)
            technical += np.where(ss_present, np.minimum(10.0, structure_score / 10.0), 0.0)
            technical_reason |= ss_present
            constructive = ~ss_present & _contains(structure, "bull", "higher high", "break", "uptrend", "supportive")
            weak_structure = ~ss_present & ~constructive & _contains(structure, "bear", "weak", "lower low", "breakdown")
            technical += constructive * 8; technical_reason |= constructive | weak_structure
            rsi_present = np.isfinite(rsi)
            technical += np.where(rsi_present, np.where((rsi >= 48) & (rsi <= 68), 5.0, np.where(rsi > 75, 0.0, 2.0)), 0.0)
            technical_reason |= rsi_present
            adx_present = np.isfinite(adx)
            technical += np.where(adx_present, np.where(adx >= 20, 4.0, 2.0), 0.0); technical_reason |= adx_present
            weekly = _strings(rows, "weekly_state"); monthly = _strings(rows, "monthly_state")
            technical += (weekly == "bullish") * 2; technical_reason |= weekly == "bullish"
            technical += (monthly == "bullish") * 2; technical_reason |= monthly == "bullish"
            vw_aligned = np.isfinite(ltp) & np.isfinite(vwap) & np.where(is_short, ltp <= vwap, ltp >= vwap)
            technical += vw_aligned * 6; technical_reason |= np.isfinite(ltp) & np.isfinite(vwap)
            technical = np.minimum(technical, 25.0)
            component1, component2 = institutional, technical
            available1, available2 = institutional_present, technical_reason
            degraded1 = ~institutional_status_available

        # Shared participation.
        participation = np.zeros(n, dtype=float); participation_reason = np.zeros(n, dtype=bool)
        vp_score = _nums(rows, "volume_profile_score")
        vp_present = np.isfinite(vp_score)
        participation += np.where(vp_present, np.minimum(10.0, vp_score / 10.0), 0.0); participation_reason |= vp_present
        vp_text = _strings(rows, "volume_profile")
        vp_fallback = ~vp_present & _contains(vp_text, "accum", "expan", "support", "positive")
        participation += vp_fallback * 8; participation_reason |= vp_fallback
        volume_state = _strings(rows, "volume_state")
        vol_state_hit = _contains(volume_state, "expan", "high", "rising", "strong")
        participation += vol_state_hit * 6; participation_reason |= vol_state_hit
        recent = np.asarray([
            _float(row.get("session_relative_volume") if row.get("session_relative_volume") is not None else row.get("recent_volume_vs_base"))
            if row.get("participation_decision_usable") is True else float("nan")
            for row in rows
        ], dtype=float)
        recent_present = np.isfinite(recent)
        participation += np.where(recent_present, np.where(recent >= 1.5, 6.0, np.where(recent >= 1.0, 3.0, 0.0)), 0.0)
        participation_reason |= recent_present
        participation = np.minimum(participation, 20.0)

        # Shared tradeability.
        tradeability = np.zeros(n, dtype=float); tradeability_reason = np.zeros(n, dtype=bool)
        rr_present = np.isfinite(rr)
        tradeability += np.where(rr_present, np.where(rr >= 2, 6.0, np.where(rr >= 1.5, 4.0, 1.0)), 0.0)
        tradeability_reason |= rr_present
        tradeability += trade_map_valid * 5; tradeability_reason |= trade_map_valid
        current_price = (freshness == "live") | (freshness == "delayed") | np.char.startswith(freshness, "live")
        historical_price = _contains(freshness, "historical")
        tradeability += current_price * 4; tradeability_reason |= current_price | historical_price
        tradeability = np.minimum(tradeability, 15.0)

        # Shared regime.
        regime_status = np.asarray([_text(rs[i].get("state") or rows[i].get("index_context")) for i in range(n)], dtype=str)
        supportive = np.isin(regime_status, ["supportive", "risk_on", "bullish", "green"])
        hostile = np.isin(regime_status, ["hostile", "risk_off", "bearish", "red"])
        regime_points = np.where(supportive, np.where(is_short, 2.0, 10.0),
                        np.where(hostile, np.where(is_short, 10.0, 2.0), 0.0))
        regime_available = supportive | hostile

        raw_score = component1 + component2 + participation + tradeability + regime_points

        # Conflict/gate arrays.  One boolean per unique exact-engine reason.
        conflict_masks: list[np.ndarray] = []
        veto_masks: list[np.ndarray] = []
        def add(mask: np.ndarray, *, veto: bool = False) -> None:
            conflict_masks.append(mask.astype(bool))
            if veto:
                veto_masks.append(mask.astype(bool))

        add(~side_valid, veto=True)
        if desk == "delivery": add(is_short, veto=True)
        add(np.isin(decision, ["avoid", "avoid_long", "blocked", "no_trade", "wait"]) | (side == "avoid_long"), veto=True)
        add(decision == "accumulate", veto=True)
        add(status == "blocked", veto=True)
        add(_contains(structure, "bear", "weak", "breakdown", "lower low") & ~is_short)
        add(np.isfinite(rsi) & (rsi > 75))
        add(~rr_present)
        add(rr_present & (rr < policy.minimum_net_rr))
        stale = np.isin(freshness, ["stale", "invalid", "pending"]) | _contains(freshness, "stale")
        add(stale, veto=True)
        if desk == "intraday":
            add(_contains(_strings(rows, "orb_state"), "failed_breakout", "failed_breakdown"))
        add(actionable_source & ~trade_map_valid, veto=True)

        if desk == "intraday":
            market_open = np.asarray([row.get("market_open_at_decision") is True for row in rows], dtype=bool)
            hard_late = np.asarray([row.get("hard_late_session_block") is True for row in rows], dtype=bool)
            candle = _strings(rows, "candle_state")
            verified_live = np.isin(freshness, ["live", "live_current"]) | np.char.startswith(freshness, "live")
            good_candle = np.isin(candle, ["fresh", "live", "delayed_warning"])
            add(~market_open, veto=True); add(hard_late, veto=True); add(~verified_live, veto=True); add(~good_candle, veto=True)
            all_component_available = available1 & available2 & participation_reason & tradeability_reason & regime_available
        else:
            collecting = np.asarray([_text(row.get("state")) == "collecting_evidence" for row in ds], dtype=bool)
            fundamental_score = _nums(rows, "fundamental_score")
            fundamental_state = _strings(rows, "fundamental_state")
            fundamental_bad = ~np.isfinite(fundamental_score) | ~np.isin(fundamental_state, ["strong", "acceptable"])
            add(~institutional_status_available, veto=True); add(collecting, veto=True); add(fundamental_bad, veto=True)
            all_component_available = institutional_status_available & available2 & participation_reason & tradeability_reason & regime_available

        conflict_count = np.sum(np.vstack(conflict_masks), axis=0) if conflict_masks else np.zeros(n, dtype=int)
        veto_any = np.any(np.vstack(veto_masks), axis=0) if veto_masks else np.zeros(n, dtype=bool)
        penalty = np.minimum(30.0, conflict_count.astype(float) * 6.0)
        score = np.clip(np.rint(raw_score - penalty), 0, 100).astype(int)
        scoring_state = np.where(veto_any, "BLOCKED", np.where(all_component_available, "NORMAL", "DEGRADED"))

        extension_reference = np.where(np.isfinite(planned_entry), planned_entry, entry)
        if desk == "intraday":
            orb_confirmed = np.asarray([row.get("orb_confirmed") is True for row in rows], dtype=bool)
            orb_reference = np.where(is_short, _nums(rows, "orb_low"), _nums(rows, "orb_high"))
            extension_reference = np.where(orb_confirmed & np.isfinite(orb_reference), orb_reference, extension_reference)
        extended = np.isfinite(ltp) & np.isfinite(extension_reference) & (extension_reference != 0) & (
            np.abs(ltp - extension_reference) / np.abs(extension_reference) > (policy.extension_limit_pct / 100.0)
        )
        no_gate_failures = conflict_count == 0
        ready_gate = actionable_source & (score >= policy.evidence_ready_threshold) & no_gate_failures & rr_present & (rr >= policy.minimum_net_rr) & (scoring_state == "NORMAL")
        readiness = np.where(veto_any, "AVOID", np.where(extended & (score >= 60), "EXTENDED", np.where(ready_gate, "READY", "WATCH")))

        # This NumPy accelerator is not an actionability authority.  READY is
        # only an evidence state; exact CandidateEligibilityAuthority (including
        # empirical strategy qualification and current contract hash) runs after
        # shortlist revalidation.  Therefore this layer can never assert True.
        actionability = np.zeros(n, dtype=bool)

        # Superset hard-blocker mask: only exact vetoes.  A row outside this mask
        # can never become production READY under the exact engine.
        candidate_superset = ~veto_any

        results = []
        for i, row in enumerate(rows):
            results.append({
                "case": i,
                "symbol": str(row.get("symbol") or "").upper(),
                "mode": desk,
                "evidence_score": int(score[i]),
                "readiness": str(readiness[i]),
                "scoring_state": str(scoring_state[i]),
                "actionability_verified": bool(actionability[i]),
                "candidate_superset": bool(candidate_superset[i]),
                "raw_score": round(float(raw_score[i]), 1),
                "conflict_count": int(conflict_count[i]),
                "hard_veto": bool(veto_any[i]),
            })
        return {
            "ok": True,
            "authority": self.authority,
            "authority_version": self.authority_version,
            "role": self.role,
            "mode": desk,
            "count": n,
            "results": results,
            "policy": "non-authoritative NumPy accelerator; exact EvidenceEngineService revalidates shortlisted rows before chronological admission",
        }

    def parity_with_exact(
        self,
        candidates: Sequence[Mapping[str, Any]],
        *,
        mode: str,
        deliveries: Sequence[Mapping[str, Any]] | None = None,
        regimes: Sequence[Mapping[str, Any]] | None = None,
    ) -> dict[str, Any]:
        desk = require_production_mode(mode)
        rows = [dict(row or {}) for row in candidates]
        ds = [dict(row or {}) for row in (deliveries or [{} for _ in rows])]
        rs = [dict(row or {}) for row in (regimes or [{} for _ in rows])]
        vector = self.screen(rows, mode=desk, deliveries=ds, regimes=rs)
        exact_engine = EvidenceEngineService()
        mismatches: list[dict[str, Any]] = []
        superset_violations: list[dict[str, Any]] = []
        for i, (candidate, delivery, regime, vec) in enumerate(zip(rows, ds, rs, vector["results"])):
            exact_candidate = dict(candidate, mode=desk)
            exact = exact_engine.score_candidate(exact_candidate, delivery=delivery, regime=regime).to_dict()
            diff = {}
            for field in ("evidence_score", "readiness", "scoring_state"):
                if vec.get(field) != exact.get(field):
                    diff[field] = {"vectorized": vec.get(field), "exact": exact.get(field)}
            if diff:
                mismatches.append({"case": i, "symbol": vec.get("symbol"), "differences": diff})
            if exact.get("readiness") == "READY" and exact.get("actionability_verified") is True and not vec.get("candidate_superset"):
                superset_violations.append({"case": i, "symbol": vec.get("symbol")})
        return {
            "ok": not mismatches and not superset_violations,
            "contract_version": self.parity_contract_version,
            "accelerator_version": self.authority_version,
            "exact_contract": "EvidenceEngineService/evidence-v2",
            "mode": desk,
            "case_count": len(rows),
            "mismatch_count": len(mismatches),
            "superset_violation_count": len(superset_violations),
            "mismatches": mismatches,
            "superset_violations": superset_violations,
            "policy": "release-blocking score/readiness parity; vectorized evidence never grants actionability; exact eligibility is revalidated after shortlist",
        }


DEFAULT_VECTORIZED_EVIDENCE_SCREENING_SERVICE = VectorizedEvidenceScreeningService()
