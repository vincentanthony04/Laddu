"""Vectorised historical execution projection with exact-replay parity.

This service deliberately does *not* replace ProductionReplayService.  It
vectorises the path-independent bulk work that is safe to express as arrays:
first target/stop touch and expiry selection for already-qualified canonical
trade maps.  Path-dependent portfolio admission, duplicate suppression,
capital release, sector limits and final validation remain chronological in the
exact replay.

Costs are never reimplemented here.  Once the vectorised engine determines an
exit, it delegates economics to the canonical IndiaCashCostModel.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

import numpy as np

from core.india_cost_model import IndiaCashCostModel
from core.intrabar_execution_policy import DEFAULT_INTRABAR_EXECUTION_POLICY
from core.production_replay_service import ProductionReplayService, _bar_time, _num, _parse, _same_intraday_session

AUTHORITY_NAME = "VectorizedStrategyBacktestService"
AUTHORITY_VERSION = "1.1.0"
PARITY_CONTRACT_VERSION = "vectorized-exact-execution-parity-1.0.0"
PIPELINE_PARITY_CONTRACT_VERSION = "vectorized-exact-candidate-execution-parity-1.0.0"


@dataclass(frozen=True)
class _PreparedCase:
    case_index: int
    side: str
    entry: float
    stop: float
    target: float
    bars: tuple[Mapping[str, Any], ...]
    cost_evidence: Mapping[str, Any]


class VectorizedStrategyBacktestService:
    authority = AUTHORITY_NAME
    authority_version = AUTHORITY_VERSION
    parity_contract_version = PARITY_CONTRACT_VERSION
    pipeline_parity_contract_version = PIPELINE_PARITY_CONTRACT_VERSION

    @staticmethod
    def _prepare(index: int, raw: Mapping[str, Any]) -> _PreparedCase:
        decision = dict(raw.get("decision") or {})
        bars = tuple(dict(item) for item in (raw.get("future_bars") or []) if _bar_time(item) is not None)
        if not bars:
            raise ValueError(f"case {index}: future_bars required")
        entry = _num(bars[0].get("open"))
        stop = _num(decision.get("stop"))
        target = _num(decision.get("target"))
        side = str(decision.get("side") or "LONG").upper()
        if side in {"BUY"}:
            side = "LONG"
        if side in {"SELL"}:
            side = "SHORT"
        if side not in {"LONG", "SHORT"} or entry is None or stop is None or target is None or entry <= 0:
            raise ValueError(f"case {index}: canonical entry/stop/target/side required")
        if side == "LONG" and not (stop < entry < target):
            raise ValueError(f"case {index}: invalid LONG geometry")
        if side == "SHORT" and not (target < entry < stop):
            raise ValueError(f"case {index}: invalid SHORT geometry")
        candidate = dict(raw.get("candidate") or {})
        cost_evidence = {**dict(decision), **candidate}
        return _PreparedCase(index, side, float(entry), float(stop), float(target), bars, cost_evidence)

    def backtest(
        self,
        cases: Iterable[Mapping[str, Any]],
        *,
        mode: str,
        conservative_same_bar: bool = True,
        cost_model: IndiaCashCostModel | None = None,
    ) -> dict[str, Any]:
        desk = str(mode or "").lower()
        if desk not in {"delivery", "intraday"}:
            raise ValueError("mode must be delivery or intraday")
        prepared = [self._prepare(index, row) for index, row in enumerate(cases)]
        if not prepared:
            return {
                "ok": True, "authority": self.authority, "authority_version": self.authority_version,
                "mode": desk, "case_count": 0, "results": [],
            }
        width = max(len(case.bars) for case in prepared)
        n = len(prepared)
        highs = np.full((n, width), np.nan, dtype=float)
        lows = np.full((n, width), np.nan, dtype=float)
        closes = np.full((n, width), np.nan, dtype=float)
        valid = np.zeros((n, width), dtype=bool)
        entries = np.asarray([case.entry for case in prepared], dtype=float)
        stops = np.asarray([case.stop for case in prepared], dtype=float)
        targets = np.asarray([case.target for case in prepared], dtype=float)
        shorts = np.asarray([case.side == "SHORT" for case in prepared], dtype=bool)

        for i, case in enumerate(prepared):
            for j, bar in enumerate(case.bars):
                high, low, close = _num(bar.get("high")), _num(bar.get("low")), _num(bar.get("close"))
                if high is None or low is None:
                    continue
                highs[i, j], lows[i, j] = high, low
                closes[i, j] = close if close is not None else case.entry
                valid[i, j] = True

        stop_hits = valid & np.where(shorts[:, None], highs >= stops[:, None], lows <= stops[:, None])
        target_hits = valid & np.where(shorts[:, None], lows <= targets[:, None], highs >= targets[:, None])
        any_hits = stop_hits | target_hits
        hit_exists = any_hits.any(axis=1)
        first_idx = np.where(hit_exists, any_hits.argmax(axis=1), -1)

        results: list[dict[str, Any]] = []
        for i, case in enumerate(prepared):
            model = cost_model or IndiaCashCostModel.for_evidence(desk, dict(case.cost_evidence))
            idx = int(first_idx[i])
            outcome = "EXPIRY"
            intrabar_resolution = {
                "outcome": None, "ambiguous": False, "state": "EXPIRY_NO_TOUCH",
                "authority": DEFAULT_INTRABAR_EXECUTION_POLICY.authority,
                "authority_version": DEFAULT_INTRABAR_EXECUTION_POLICY.authority_version,
                "production_eligible": True,
                "policy": DEFAULT_INTRABAR_EXECUTION_POLICY.production_policy,
            }
            if idx >= 0:
                stop_hit = bool(stop_hits[i, idx])
                target_hit = bool(target_hits[i, idx])
                intrabar_resolution = DEFAULT_INTRABAR_EXECUTION_POLICY.resolve(
                    stop_hit=stop_hit, target_hit=target_hit, conservative=conservative_same_bar
                )
                outcome = str(intrabar_resolution["outcome"])
                exit_price = case.stop if outcome == "STOP" else case.target
                exit_bar = case.bars[idx]
            else:
                valid_indices = np.flatnonzero(valid[i])
                last_idx = int(valid_indices[-1]) if len(valid_indices) else len(case.bars) - 1
                exit_bar = case.bars[last_idx]
                exit_price = _num(exit_bar.get("close")) or case.entry
            exit_at = (_bar_time(exit_bar) or datetime.now(timezone.utc)).isoformat()
            if case.side == "SHORT":
                gross_return = (case.entry - float(exit_price)) / case.entry
                cost = model.round_trip(float(exit_price), case.entry, 1)
            else:
                gross_return = (float(exit_price) - case.entry) / case.entry
                cost = model.round_trip(case.entry, float(exit_price), 1)
            cost_return = float(cost["costs"]["total"]) / case.entry
            results.append({
                "case": case.case_index,
                "entry": case.entry,
                "exit": float(exit_price),
                "exit_time": exit_at,
                "side": case.side,
                "outcome": outcome,
                "intrabar_resolution": intrabar_resolution,
                "gross_return": gross_return,
                "cost_return": cost_return,
                "net_return": gross_return - cost_return,
                "cost_authority": cost.get("cost_authority"),
                "cost_authority_version": cost.get("cost_authority_version"),
                "tariff_schedule_version": cost.get("tariff_schedule_version"),
                "cost_exchange": model.config.exchange,
                "cost_bse_group": model.config.bse_group,
            })
        return {
            "ok": True,
            "authority": self.authority,
            "authority_version": self.authority_version,
            "mode": desk,
            "case_count": len(results),
            "conservative_same_bar": bool(conservative_same_bar),
            "results": results,
            "scope": "vectorised target/stop/expiry execution for pre-qualified canonical trade maps; portfolio admission remains exact chronological replay",
        }

    def parity_with_exact(
        self,
        cases: Sequence[Mapping[str, Any]],
        *,
        mode: str,
        conservative_same_bar: bool = True,
        tolerance: float = 1e-12,
    ) -> dict[str, Any]:
        desk = str(mode or "").lower()
        vector = self.backtest(cases, mode=desk, conservative_same_bar=conservative_same_bar)
        exact_service = ProductionReplayService()
        mismatches: list[dict[str, Any]] = []
        for raw, vec in zip(cases, vector["results"]):
            decision = dict(raw.get("decision") or {})
            bars = [dict(item) for item in raw.get("future_bars") or []]
            evidence = {**decision, **dict(raw.get("candidate") or {})}
            model = IndiaCashCostModel.for_evidence(desk, evidence)
            exact = exact_service._simulate(
                decision, bars, cost_model=model, conservative_same_bar=conservative_same_bar
            )
            fields = ("entry", "exit", "side", "outcome")
            diff = {field: {"vectorized": vec.get(field), "exact": exact.get(field)} for field in fields if vec.get(field) != exact.get(field)}
            for field in ("gross_return", "cost_return"):
                if abs(float(vec.get(field) or 0.0) - float(exact.get(field) or 0.0)) > tolerance:
                    diff[field] = {"vectorized": vec.get(field), "exact": exact.get(field)}
            if diff:
                mismatches.append({"case": vec["case"], "differences": diff})
        return {
            "ok": not mismatches,
            "contract_version": self.parity_contract_version,
            "mode": desk,
            "case_count": len(cases),
            "mismatch_count": len(mismatches),
            "mismatches": mismatches,
            "policy": "vectorised execution may be used for bulk screening only when this parity contract passes; chronological replay remains final authority",
        }

    def pipeline_parity(
        self,
        cases: Sequence[Mapping[str, Any]],
        *,
        mode: str,
        conservative_same_bar: bool = True,
        max_concurrent_positions: int = 10,
        max_sector_positions: int = 2,
    ) -> dict[str, Any]:
        """Prove vector candidate + execution parity while retaining exact admission.

        Candidate scoring and target/stop/expiry execution are safe bulk-array
        acceleration domains.  Duplicate suppression, sector concentration,
        capital-slot release and chronological admission remain intentionally
        sequential in :class:`ProductionReplayService`.
        """
        from core.evidence_engine_service import EvidenceEngineService
        from core.vectorized_evidence_screening_service import VectorizedEvidenceScreeningService

        desk = str(mode or "").lower()
        if desk not in {"delivery", "intraday"}:
            raise ValueError("mode must be delivery or intraday")
        candidates = []
        deliveries = []
        regimes = []
        for raw in cases:
            candidate = dict(raw.get("candidate") or {})
            candidate["mode"] = desk
            candidates.append(candidate)
            deliveries.append(dict(raw.get("delivery") or {}))
            regimes.append(dict(raw.get("regime") or {}))

        evidence_accelerator = VectorizedEvidenceScreeningService()
        evidence_parity = evidence_accelerator.parity_with_exact(
            candidates, mode=desk, deliveries=deliveries, regimes=regimes
        )

        exact_engine = EvidenceEngineService()
        execution_cases = []
        execution_source_indices = []
        for index, raw in enumerate(cases):
            candidate = candidates[index]
            observed_at = _parse(
                candidate.get("observed_at") or candidate.get("last_refresh") or raw.get("decision_as_of")
            )
            bars = [dict(bar) for bar in (raw.get("future_bars") or []) if _bar_time(bar)]
            bars.sort(key=lambda bar: _bar_time(bar) or datetime.max.replace(tzinfo=timezone.utc))
            future = [bar for bar in bars if observed_at is not None and (_bar_time(bar) or observed_at) > observed_at]
            if desk == "intraday" and observed_at is not None:
                future = [bar for bar in future if _same_intraday_session(observed_at, _bar_time(bar) or observed_at)]
            if not future:
                continue
            decision = exact_engine.score_candidate(
                candidate, delivery=deliveries[index], regime=regimes[index]
            ).to_dict()
            if decision.get("readiness") != "READY" or decision.get("actionability_verified") is not True:
                continue
            execution_cases.append({
                "candidate": candidate,
                "decision": decision,
                "future_bars": future,
            })
            execution_source_indices.append(index)

        execution_parity = self.parity_with_exact(
            execution_cases, mode=desk, conservative_same_bar=conservative_same_bar
        ) if execution_cases else {
            "ok": True,
            "contract_version": self.parity_contract_version,
            "mode": desk,
            "case_count": 0,
            "mismatch_count": 0,
            "mismatches": [],
            "policy": "no exact-actionable execution cases in batch",
        }
        exact_replay = ProductionReplayService().replay(
            cases, mode=desk, conservative_same_bar=conservative_same_bar,
            max_concurrent_positions=max_concurrent_positions,
            max_sector_positions=max_sector_positions,
        )
        return {
            "ok": bool(evidence_parity.get("ok")) and bool(execution_parity.get("ok")),
            "contract_version": self.pipeline_parity_contract_version,
            "mode": desk,
            "case_count": len(cases),
            "vectorized_domains": ["candidate_evidence_score_and_readiness", "target_stop_expiry_execution"],
            "exact_only_domains": ["duplicate_open_thesis", "portfolio_capacity", "sector_concentration", "chronological_capital_release"],
            "evidence_parity": evidence_parity,
            "execution_parity": execution_parity,
            "execution_source_indices": execution_source_indices,
            "exact_replay": {
                "replay_version": exact_replay.get("replay_version"),
                "admission_policy_version": exact_replay.get("admission_policy_version"),
                "promoted_count": exact_replay.get("promoted_count"),
                "blocked_count": exact_replay.get("blocked_count"),
            },
            "policy": "vectorized candidate/execution acceleration is release-gated by parity; exact chronological replay remains final historical truth",
        }


DEFAULT_VECTORIZED_STRATEGY_BACKTEST_SERVICE = VectorizedStrategyBacktestService()
