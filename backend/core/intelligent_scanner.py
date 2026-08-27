"""Deterministic intelligent-priority scanner with terminal-state accounting."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Mapping, Sequence

from core.universe_authority import UniverseSnapshot


SCANNER_VERSION = "pl-scanner-69.8.0"


class TerminalState(str, Enum):
    ANALYSED = "ANALYSED"
    CANDIDATE = "CANDIDATE"
    REJECTED_PRICE = "REJECTED_PRICE"
    REJECTED_LIQUIDITY = "REJECTED_LIQUIDITY"
    REJECTED_DATA = "REJECTED_DATA"
    DEFERRED_HISTORY = "DEFERRED_HISTORY"
    DEFERRED_RATE_LIMIT = "DEFERRED_RATE_LIMIT"
    IDENTITY_ERROR = "IDENTITY_ERROR"


RESEARCH_STATES = {"RESEARCH", "WATCH", "PREPARING", "WAITING_FOR_CONFIRMATION", "REJECTED"}
TRADE_STATES = {"OPEN", "HOLD", "TARGET_HIT", "STOP_HIT", "CLOSED", "SETTLED"}


@dataclass(frozen=True)
class ScanEvaluation:
    run_id: str
    snapshot_id: str
    security_id: str
    listing_id: str
    terminal_state: str
    priority_tier: str
    priority_score: float
    evidence: Mapping[str, Any]
    rejection_reasons: tuple[str, ...]
    research_state: str
    canonical_decision_allowed: bool


@dataclass(frozen=True)
class ScanRun:
    run_id: str
    desk: str
    snapshot_id: str
    population_count: int
    evaluations: tuple[ScanEvaluation, ...]
    completed_at: str

    @property
    def terminal_count(self) -> int:
        return len(self.evaluations)

    @property
    def candidate_count(self) -> int:
        return sum(row.terminal_state == TerminalState.CANDIDATE.value for row in self.evaluations)

    def proof(self) -> dict[str, Any]:
        states: dict[str, int] = {}
        for row in self.evaluations:
            states[row.terminal_state] = states.get(row.terminal_state, 0) + 1
        return {
            "run_id": self.run_id, "snapshot_id": self.snapshot_id, "desk": self.desk,
            "population_count": self.population_count, "terminal_count": self.terminal_count,
            "complete": self.population_count == self.terminal_count,
            "candidate_count": self.candidate_count, "terminal_states": states,
            "completed_at": self.completed_at,
        }


def priority_score(row: Mapping[str, Any]) -> tuple[str, float]:
    if row.get("open_position") or row.get("active_decision") or row.get("held_delivery"):
        return "P0", 1000.0
    score = (
        min(abs(float(row.get("price_atr_displacement") or 0)), 5.0) * 14
        + min(max(float(row.get("relative_volume") or 0) - 1.0, 0), 5.0) * 12
        + min(float(row.get("liquidity_quality") or 0), 1.0) * 20
        + min(float(row.get("sector_alignment") or 0), 1.0) * 12
        + min(float(row.get("breakout_proximity") or 0), 1.0) * 12
        + min(float(row.get("data_repair_urgency") or 0), 1.0) * 6
    )
    if score >= 45:
        return "P1", round(score, 4)
    if float(row.get("liquidity_quality") or 0) >= 0.7:
        return "P2", round(score, 4)
    if row.get("desk", "").upper() == "DELIVERY":
        return "P3", round(score, 4)
    return "P4", round(score, 4)


class IntelligentScanner:
    """Scanner consumes one immutable snapshot; it never discovers eligibility."""
    def __init__(self, *, max_workers: int = 8, min_price: float = 20.0, min_turnover: float = 50_000_000.0):
        self.max_workers = max(1, max_workers)
        self.min_price = float(min_price)
        self.min_turnover = float(min_turnover)

    def _evaluate(
        self,
        run_id: str,
        snapshot: UniverseSnapshot,
        security_id: str,
        listing_id: str,
        loader: Callable[[str, str], Mapping[str, Any]],
        *,
        market_open: bool,
    ) -> ScanEvaluation:
        try:
            row = dict(loader(security_id, listing_id) or {})
        except Exception as exc:
            row = {
                "identity_verified": True,
                "data_state": "ERROR",
                "error": f"{type(exc).__name__}: {exc}"[:240],
            }
        row["desk"] = snapshot.desk
        tier, score = priority_score(row)
        reasons: list[str] = []
        terminal = TerminalState.ANALYSED
        if row.get("identity_verified") is not True:
            terminal, reasons = TerminalState.IDENTITY_ERROR, ["IDENTITY_UNVERIFIED"]
        elif row.get("rate_limited"):
            terminal, reasons = TerminalState.DEFERRED_RATE_LIMIT, ["PROVIDER_RATE_LIMIT"]
        elif row.get("history_ready") is False:
            terminal, reasons = TerminalState.DEFERRED_HISTORY, ["MANDATORY_HISTORY_INCOMPLETE"]
        elif row.get("data_state") not in {"ACCEPTED", "REPAIRED"}:
            terminal, reasons = TerminalState.REJECTED_DATA, [str(row.get("data_state") or "DATA_NOT_ACCEPTED")]
        elif float(row.get("price") or 0) < self.min_price:
            terminal, reasons = TerminalState.REJECTED_PRICE, ["PRICE_BELOW_FLOOR"]
        elif float(row.get("avg_turnover") or 0) < self.min_turnover:
            terminal, reasons = TerminalState.REJECTED_LIQUIDITY, ["TURNOVER_BELOW_FLOOR"]
        elif row.get("freshness_ok") is not True:
            terminal, reasons = TerminalState.REJECTED_DATA, ["FRESHNESS_GATE_FAILED"]
        else:
            deterministic = bool(row.get("deterministic_setup"))
            risk_passed = bool(row.get("risk_passed"))
            live_confirmation = bool(row.get("live_confirmation"))
            intraday_closed = snapshot.desk == "INTRADAY" and not market_open
            if deterministic and risk_passed and (snapshot.desk != "INTRADAY" or live_confirmation) and not intraday_closed:
                terminal = TerminalState.CANDIDATE
            elif deterministic:
                reasons.append("WAITING_FOR_LIVE_CONFIRMATION" if snapshot.desk == "INTRADAY" else "RISK_OR_CONFIRMATION_PENDING")

        admitted = terminal == TerminalState.CANDIDATE
        research_state = "WAITING_FOR_CONFIRMATION" if (
            terminal == TerminalState.ANALYSED and reasons
        ) else "RESEARCH" if terminal == TerminalState.ANALYSED else "REJECTED" if terminal.value.startswith("REJECTED") or terminal == TerminalState.IDENTITY_ERROR else "PREPARING"
        if research_state in TRADE_STATES:
            raise AssertionError("research evaluation cannot use trade lifecycle semantics")
        return ScanEvaluation(
            run_id=run_id, snapshot_id=snapshot.snapshot_id,
            security_id=security_id, listing_id=listing_id,
            terminal_state=terminal.value, priority_tier=tier, priority_score=score,
            evidence=row, rejection_reasons=tuple(reasons), research_state=research_state,
            canonical_decision_allowed=admitted,
        )

    def run(
        self,
        snapshot: UniverseSnapshot,
        loader: Callable[[str, str], Mapping[str, Any]],
        *,
        market_open: bool,
    ) -> ScanRun:
        run_id = f"{snapshot.snapshot_id}:{datetime.now(timezone.utc).strftime('%H%M%S%f')}"
        inputs = list(zip(snapshot.security_ids, snapshot.listing_ids))
        evaluations: list[ScanEvaluation] = []
        with ThreadPoolExecutor(max_workers=self.max_workers, thread_name_prefix=f"Laddu{snapshot.desk.title()}") as pool:
            jobs = {
                pool.submit(self._evaluate, run_id, snapshot, security_id, listing_id, loader, market_open=market_open): security_id
                for security_id, listing_id in inputs
            }
            for future in as_completed(jobs):
                try:
                    evaluations.append(future.result())
                except Exception as exc:  # a symbol must never stall or disappear
                    security_id = jobs[future]
                    listing_id = snapshot.listing_ids[snapshot.security_ids.index(security_id)]
                    evaluations.append(ScanEvaluation(
                        run_id, snapshot.snapshot_id, security_id, listing_id,
                        TerminalState.REJECTED_DATA.value, "P4", 0.0,
                        {"error": f"{type(exc).__name__}: {exc}"[:240]},
                        ("UNHANDLED_SYMBOL_FAILURE",), "REJECTED", False,
                    ))
        evaluations.sort(key=lambda row: ({"P0": 0, "P1": 1, "P2": 2, "P3": 3, "P4": 4}[row.priority_tier], -row.priority_score, row.security_id))
        run = ScanRun(
            run_id, snapshot.desk, snapshot.snapshot_id, snapshot.population_count,
            tuple(evaluations), datetime.now(timezone.utc).isoformat(),
        )
        if run.terminal_count != snapshot.population_count:
            raise AssertionError("scanner terminal-state accounting is incomplete")
        return run
