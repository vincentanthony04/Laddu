"""Exact-production candidate replay for Intraday and Delivery validation.

The replay imports the same Evidence Engine used by production.  It cannot tune
weights or thresholds and fails closed when point-in-time lineage, future bars,
entry maps, benchmark evidence, or admission controls are missing.  Replayed
trades are admitted chronologically with duplicate-thesis, portfolio-capacity
and sector-concentration controls; Intraday bars cannot leak into another
session.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from core.evidence_engine_service import EvidenceEngineService, model_version_for_mode
from core.india_cost_model import IndiaCashCostModel
from core.intrabar_execution_policy import DEFAULT_INTRABAR_EXECUTION_POLICY
from core.india_time import INDIA_TZ
from core.trading_session_authority import DEFAULT_TRADING_SESSION_AUTHORITY
from core.historical_session_index_authority import HistoricalSessionIndexAuthority
from core.walk_forward_validation_service import WalkForwardValidationService


REPLAY_VERSION = "production-replay-1.2.0"
ADMISSION_POLICY_VERSION = "portfolio-admission-1.0.0"


def _num(value: Any) -> Optional[float]:
    try:
        if value in (None, "", "—"):
            return None
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def _parse(value: Any) -> Optional[datetime]:
    if value in (None, "", "—"):
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _bar_time(bar: Mapping[str, Any]) -> Optional[datetime]:
    return _parse(bar.get("timestamp") or bar.get("time") or bar.get("date"))


def _session_lineage(
    observed_at: datetime,
    *,
    mode: str,
    historical_sessions: HistoricalSessionIndexAuthority | None = None,
) -> Dict[str, Any]:
    local = observed_at.astimezone(INDIA_TZ)
    sessions = DEFAULT_TRADING_SESSION_AUTHORITY
    if sessions.calendar_covered(local.date()):
        window = sessions.session_window(local.date())
        ready = window is not None
        return {
            "session_authority": sessions.authority,
            "session_authority_version": sessions.authority_version,
            "session_index_fingerprint": sessions.calendar_fingerprint,
            "session_observed": ready,
            "session_window_proven": ready,
            "session_authority_ready": ready,
            "session_authority_state": "OFFICIAL_CALENDAR_SESSION" if ready else "OFFICIAL_CALENDAR_CLOSED_DAY",
        }
    if historical_sessions is None:
        return {
            "session_authority": "HistoricalSessionIndexAuthority",
            "session_authority_version": "1.0.0",
            "session_index_fingerprint": None,
            "session_observed": False,
            "session_window_proven": False,
            "session_authority_ready": False,
            "session_authority_state": "HISTORICAL_SESSION_INDEX_REQUIRED",
        }
    evidence = historical_sessions.evidence(local.date())
    observed = evidence.get("observed_session") is True
    window_proven = evidence.get("window_proven") is True
    ready = observed and (str(mode).lower() != "intraday" or window_proven)
    return {
        "session_authority": historical_sessions.authority,
        "session_authority_version": historical_sessions.authority_version,
        "session_index_fingerprint": historical_sessions.session_index_fingerprint,
        "session_observed": observed,
        "session_window_proven": window_proven,
        "session_authority_ready": ready,
        "session_authority_state": "HISTORICAL_SESSION_PROVEN" if ready else (
            "HISTORICAL_INTRADAY_WINDOW_UNPROVEN" if observed and str(mode).lower() == "intraday" else "HISTORICAL_SESSION_UNVERIFIED"
        ),
    }


def _same_intraday_session(
    observed_at: datetime,
    bar_at: datetime,
    historical_sessions: HistoricalSessionIndexAuthority | None = None,
) -> bool:
    local_observed = observed_at.astimezone(INDIA_TZ)
    local_bar = bar_at.astimezone(INDIA_TZ)
    if local_bar.date() != local_observed.date():
        return False
    sessions = DEFAULT_TRADING_SESSION_AUTHORITY
    if sessions.calendar_covered(local_observed.date()):
        window = sessions.session_window(local_observed.date())
        if window is None:
            return False
        return window.open_at() <= local_observed <= window.close_at() and window.open_at() <= local_bar <= window.close_at()
    # Historical Intraday replay never guesses a weekday or 15:30 boundary.
    # It requires an immutable observed-session index with a proved bar window.
    return bool(historical_sessions and historical_sessions.same_intraday_session(observed_at, bar_at))



class ProductionReplayService:
    def __init__(
        self,
        *,
        evidence: EvidenceEngineService | None = None,
        cost_model: IndiaCashCostModel | None = None,
        historical_sessions: HistoricalSessionIndexAuthority | None = None,
    ):
        self.evidence = evidence or EvidenceEngineService()
        self.cost_model_override = cost_model
        self.historical_sessions = historical_sessions

    def replay(self, cases: Iterable[Mapping[str, Any]], *, mode: str,
               conservative_same_bar: bool = True, max_concurrent_positions: int = 10,
               max_sector_positions: int = 2) -> Dict[str, Any]:
        desk = str(mode or "").lower()
        if desk not in ("intraday", "delivery"):
            raise ValueError("mode must be intraday or delivery")
        max_concurrent = max(1, int(max_concurrent_positions))
        max_sector = max(1, int(max_sector_positions))
        blocked: List[Dict[str, Any]] = []
        observations: List[Dict[str, Any]] = []
        active: List[Dict[str, Any]] = []

        ordered_cases = []
        for index, raw in enumerate(cases):
            case = dict(raw or {})
            candidate = dict(case.get("candidate") or {})
            observed_at = _parse(candidate.get("observed_at") or candidate.get("last_refresh") or case.get("decision_as_of"))
            ordered_cases.append((observed_at or datetime.max.replace(tzinfo=timezone.utc), index, case))
        ordered_cases.sort(key=lambda item: (item[0], item[1]))

        for observed_sort, original_index, case in ordered_cases:
            candidate = dict(case.get("candidate") or {})
            candidate["mode"] = desk
            symbol = str(candidate.get("symbol") or case.get("symbol") or "").upper().strip()
            candidate["symbol"] = symbol
            observed_at = None if observed_sort == datetime.max.replace(tzinfo=timezone.utc) else observed_sort
            lineage_missing = [
                key for key in ("dataset_fingerprint", "feature_manifest_hash", "universe_id")
                if not case.get(key)
            ]
            bars = [dict(bar) for bar in (case.get("future_bars") or []) if _bar_time(bar)]
            bars.sort(key=lambda bar: _bar_time(bar) or datetime.max.replace(tzinfo=timezone.utc))
            future = [bar for bar in bars if observed_at is not None and (_bar_time(bar) or observed_at) > observed_at]
            if desk == "intraday" and observed_at is not None:
                future = [bar for bar in future if _same_intraday_session(observed_at, _bar_time(bar) or observed_at, self.historical_sessions)]
            if not symbol or observed_at is None or lineage_missing or not future:
                blocked.append({
                    "case": original_index,
                    "symbol": symbol,
                    "state": "REPLAY_INPUT_BLOCKED",
                    "reasons": (["symbol missing"] if not symbol else [])
                    + (["decision_as_of missing/invalid"] if observed_at is None else [])
                    + (["lineage missing: " + ", ".join(lineage_missing)] if lineage_missing else [])
                    + (["no bars strictly after decision time in the permitted session"] if not future else []),
                })
                continue

            candidate["observed_at"] = observed_at.isoformat()
            decision = self.evidence.score_candidate(
                candidate,
                delivery=dict(case.get("delivery") or {}),
                regime=dict(case.get("regime") or {}),
            ).to_dict()
            try:
                case_cost_model = self.cost_model_override or IndiaCashCostModel.for_evidence(desk, candidate)
            except ValueError as exc:
                blocked.append({
                    "case": original_index, "symbol": symbol, "state": "COST_AUTHORITY_UNAVAILABLE",
                    "reasons": [str(exc)],
                })
                continue
            if decision["readiness"] != "READY" or decision.get("actionability_verified") is not True:
                blocked.append({
                    "case": original_index,
                    "symbol": symbol,
                    "state": "NOT_PROMOTED_BY_PRODUCTION_POLICY",
                    "readiness": decision["readiness"],
                    "score": decision["evidence_score"],
                    "reasons": decision.get("conflicts") or [decision.get("waiting_for")],
                })
                continue

            simulated = self._simulate(
                decision, future, cost_model=case_cost_model,
                conservative_same_bar=conservative_same_bar,
            )
            if not simulated.get("ok"):
                blocked.append({"case": original_index, "symbol": symbol, **simulated})
                continue
            benchmark_return = _num(case.get("benchmark_return"))
            if benchmark_return is None:
                blocked.append({
                    "case": original_index, "symbol": symbol, "state": "BENCHMARK_MISSING",
                    "reasons": ["benchmark_return is mandatory"],
                })
                continue

            # Retire positions whose replayed exits occurred before this decision.
            active = [position for position in active if position["exit_at"] > observed_at]
            sector = str(candidate.get("sector") or candidate.get("sector_label") or "Unknown")
            if any(position["symbol"] == symbol for position in active):
                blocked.append({
                    "case": original_index, "symbol": symbol, "state": "DUPLICATE_OPEN_THESIS",
                    "reasons": ["same symbol and desk already active at decision time"],
                })
                continue
            if len(active) >= max_concurrent:
                blocked.append({
                    "case": original_index, "symbol": symbol, "state": "PORTFOLIO_CAPACITY_BLOCKED",
                    "reasons": [f"maximum {max_concurrent} concurrent positions reached"],
                })
                continue
            sector_active = sum(position["sector"] == sector for position in active)
            if sector != "Unknown" and sector_active >= max_sector:
                blocked.append({
                    "case": original_index, "symbol": symbol, "state": "SECTOR_CAP_BLOCKED",
                    "reasons": [f"maximum {max_sector} concurrent positions in {sector} reached"],
                })
                continue

            baseline_returns = {
                str(name): float(value)
                for name, value in dict(case.get("baseline_returns") or {}).items()
                if _num(value) is not None
            }
            session_lineage = _session_lineage(
                observed_at, mode=desk, historical_sessions=self.historical_sessions
            )
            advanced_lineage = {
                "feature_as_of": case.get("feature_as_of"),
                "fundamental_as_of": case.get("fundamental_as_of") if desk == "delivery" else None,
                "universe_as_of": case.get("universe_as_of"),
                "corporate_action_adjusted": case.get("corporate_action_adjusted") is True,
                "survivorship_bias_controlled": case.get("survivorship_bias_controlled") is True,
            }
            capital_input_ready = bool(
                advanced_lineage["feature_as_of"]
                and advanced_lineage["universe_as_of"]
                and (desk != "delivery" or advanced_lineage["fundamental_as_of"])
                and advanced_lineage["corporate_action_adjusted"]
                and advanced_lineage["survivorship_bias_controlled"]
                and session_lineage["session_authority_ready"]
                and bool(session_lineage["session_index_fingerprint"])
                and len(baseline_returns) >= 3
            )
            net_return = simulated["gross_return"] - simulated["cost_return"]
            basis = {
                "symbol": symbol,
                "sector": sector,
                "mode": desk,
                "decision_as_of": observed_at.isoformat(),
                "outcome_as_of": simulated["exit_time"],
                "date": str(case.get("date") or observed_at.date().isoformat())[:10],
                "rank_score": decision["evidence_score"],
                "forward_return": simulated["gross_return"],
                "cost_return": simulated["cost_return"],
                "net_return": net_return,
                "benchmark_return": benchmark_return,
                "baseline_returns": baseline_returns,
                "outcome": simulated["outcome"],
                "entry": simulated["entry"],
                "exit": simulated["exit"],
                "stop": decision.get("stop"),
                "target": decision.get("target"),
                "side": simulated["side"],
                "dataset_fingerprint": case["dataset_fingerprint"],
                "feature_manifest_hash": case["feature_manifest_hash"],
                "universe_id": case["universe_id"],
                "cost_model_version": case_cost_model.config.version,
                "cost_model_profile": case_cost_model.config.profile,
                "cost_exchange": case_cost_model.config.exchange,
                "cost_bse_group": case_cost_model.config.bse_group,
                "execution_model_version": "next-completed-bar-conservative-1.0.0",
                "admission_policy_version": ADMISSION_POLICY_VERSION,
                "portfolio_slot_weight": 1.0 / max_concurrent,
                "capital_input_ready": capital_input_ready,
                **session_lineage,
                **advanced_lineage,
                "model_id": model_version_for_mode(desk),
                "replay_version": REPLAY_VERSION,
                "evidence_contract_version": decision.get("contract_version"),
                "evidence_model_version": decision.get("model_version"),
            }
            basis["observation_id"] = hashlib.sha256(
                json.dumps(basis, sort_keys=True, separators=(",", ":"), default=str).encode()
            ).hexdigest()[:24]
            observations.append(basis)
            exit_at = _parse(simulated["exit_time"]) or observed_at
            active.append({"symbol": symbol, "sector": sector, "exit_at": exit_at})

        return {
            "ok": True,
            "replay_version": REPLAY_VERSION,
            "admission_policy_version": ADMISSION_POLICY_VERSION,
            "mode": desk,
            "model_id": model_version_for_mode(desk),
            "case_count": len(observations) + len(blocked),
            "promoted_count": len(observations),
            "blocked_count": len(blocked),
            "max_concurrent_positions": max_concurrent,
            "max_sector_positions": max_sector,
            "capital_input_ready_count": sum(bool(row.get("capital_input_ready")) for row in observations),
            "session_authority_ready_count": sum(bool(row.get("session_authority_ready")) for row in observations),
            "observations": observations,
            "blocked": blocked,
            "policy": "Same production Evidence Engine; next completed bar execution; Intraday session boundary; conservative stop-first; duplicate, capacity and sector admission controls.",
        }

    def replay_and_validate(self, cases: Iterable[Mapping[str, Any]], *, mode: str,
                            trial_count: int, **validation_kwargs: Any) -> Dict[str, Any]:
        replay = self.replay(cases, mode=mode)
        validation = WalkForwardValidationService().validate_capital(
            replay["model_id"], replay["observations"],
            trial_count=max(1, int(trial_count)), persist=False,
            **validation_kwargs,
        )
        return {"ok": True, "replay": replay, "validation": validation}

    def _simulate(self, decision: Mapping[str, Any], future_bars: Sequence[Mapping[str, Any]], *,
                  cost_model: IndiaCashCostModel, conservative_same_bar: bool) -> Dict[str, Any]:
        first = future_bars[0]
        entry = _num(first.get("open"))
        stop = _num(decision.get("stop"))
        target = _num(decision.get("target"))
        side = str(decision.get("side") or "LONG").upper()
        if entry is None or entry <= 0 or stop is None or target is None:
            return {"ok": False, "state": "EXECUTION_MAP_INCOMPLETE", "reasons": ["next-bar open, stop and target are mandatory"]}
        short = side in ("SHORT", "SELL")
        if (not short and not (stop < entry < target)) or (short and not (target < entry < stop)):
            return {"ok": False, "state": "EXECUTION_MAP_INVALID", "reasons": ["stop/entry/target ordering invalid for side"]}

        exit_price = _num(future_bars[-1].get("close")) or entry
        exit_time = (_bar_time(future_bars[-1]) or datetime.now(timezone.utc)).isoformat()
        outcome = "EXPIRY"
        intrabar_resolution = {
            "outcome": None, "ambiguous": False, "state": "EXPIRY_NO_TOUCH",
            "authority": DEFAULT_INTRABAR_EXECUTION_POLICY.authority,
            "authority_version": DEFAULT_INTRABAR_EXECUTION_POLICY.authority_version,
            "production_eligible": True,
            "policy": DEFAULT_INTRABAR_EXECUTION_POLICY.production_policy,
        }
        for bar in future_bars:
            high = _num(bar.get("high"))
            low = _num(bar.get("low"))
            if high is None or low is None:
                continue
            stop_hit = high >= stop if short else low <= stop
            target_hit = low <= target if short else high >= target
            resolution = DEFAULT_INTRABAR_EXECUTION_POLICY.resolve(
                stop_hit=stop_hit, target_hit=target_hit, conservative=conservative_same_bar
            )
            resolved_outcome = resolution.get("outcome")
            if resolved_outcome is None:
                continue
            outcome = str(resolved_outcome)
            exit_price = stop if outcome == "STOP" else target
            intrabar_resolution = resolution
            exit_time = (_bar_time(bar) or datetime.now(timezone.utc)).isoformat()
            break

        gross_return = (entry - exit_price) / entry if short else (exit_price - entry) / entry
        costs = cost_model.round_trip(exit_price, entry, 1) if short else cost_model.round_trip(entry, exit_price, 1)
        cost_return = float(costs["costs"]["total"]) / entry if entry else 0.0
        return {
            "ok": True,
            "entry": entry,
            "exit": exit_price,
            "exit_time": exit_time,
            "side": "SHORT" if short else "LONG",
            "outcome": outcome,
            "intrabar_resolution": intrabar_resolution,
            "gross_return": gross_return,
            "cost_return": cost_return,
        }
