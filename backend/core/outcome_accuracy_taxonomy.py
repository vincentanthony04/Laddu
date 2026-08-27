"""Canonical outcome and accuracy taxonomy for governed Model Paper settlement.

Signal quality and economic P&L answer different questions and must never be
silently converted into one another:

* signal_outcome: SUCCESS / FAILURE / NEUTRAL / UNSCORABLE
* economic_outcome: WIN / LOSS / BREAKEVEN / UNSCORABLE

Accuracy is defined only over decisive signal outcomes (SUCCESS + FAILURE).
NEUTRAL remains a settled, observable signal outcome but is excluded from the
accuracy denominator. UNSCORABLE is excluded from both accuracy and realized
performance. Economic win rate is defined over decisive economic outcomes
(WIN + LOSS); BREAKEVEN is reported separately and excluded from that rate.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping


@dataclass(frozen=True)
class OutcomeAccuracyTaxonomy:
    authority: str = "OutcomeAccuracyTaxonomyAuthority"
    authority_version: str = "1.1.0-intraday-cutoff-signal-failure"
    pnl_epsilon_inr: float = 0.005

    SIGNAL_SUCCESS: str = "SUCCESS"
    SIGNAL_FAILURE: str = "FAILURE"
    SIGNAL_NEUTRAL: str = "NEUTRAL"
    SIGNAL_UNSCORABLE: str = "UNSCORABLE"

    ECONOMIC_WIN: str = "WIN"
    ECONOMIC_LOSS: str = "LOSS"
    ECONOMIC_BREAKEVEN: str = "BREAKEVEN"
    ECONOMIC_UNSCORABLE: str = "UNSCORABLE"

    @property
    def signal_decisive(self) -> frozenset[str]:
        return frozenset({self.SIGNAL_SUCCESS, self.SIGNAL_FAILURE})

    @property
    def signal_all(self) -> frozenset[str]:
        return frozenset({
            self.SIGNAL_SUCCESS,
            self.SIGNAL_FAILURE,
            self.SIGNAL_NEUTRAL,
            self.SIGNAL_UNSCORABLE,
        })

    @property
    def economic_decisive(self) -> frozenset[str]:
        return frozenset({self.ECONOMIC_WIN, self.ECONOMIC_LOSS})

    @property
    def economic_all(self) -> frozenset[str]:
        return frozenset({
            self.ECONOMIC_WIN,
            self.ECONOMIC_LOSS,
            self.ECONOMIC_BREAKEVEN,
            self.ECONOMIC_UNSCORABLE,
        })

    @staticmethod
    def _upper(value: Any) -> str:
        return str(value or "").upper().strip()

    def signal_from_exit_reason(self, exit_reason: Any, *, unscorable: bool = False) -> str:
        if unscorable:
            return self.SIGNAL_UNSCORABLE
        reason = self._upper(exit_reason)
        if reason == "TARGET_HIT":
            return self.SIGNAL_SUCCESS
        if reason in {"STOP_HIT", "SL_HIT", "EXIT_INVALIDATED", "TIME_EXIT_1500_TARGET_NOT_HIT_BY_CUTOFF", "ENTRY_NOT_TRIGGERED_BY_DEADLINE"}:
            return self.SIGNAL_FAILURE
        # Weakening exits, managed/trailing exits and other
        # non-terminal-thesis closures are observable but not target/stop
        # accuracy events. Intraday cutoff is explicitly FAILURE above; other
        # exits remain NEUTRAL rather than being
        # backfilled from the sign of P&L.
        return self.SIGNAL_NEUTRAL

    def economic_from_pnl(self, net_pnl: Any, *, unscorable: bool = False) -> str:
        if unscorable:
            return self.ECONOMIC_UNSCORABLE
        try:
            value = float(net_pnl)
        except (TypeError, ValueError):
            return self.ECONOMIC_UNSCORABLE
        if value > self.pnl_epsilon_inr:
            return self.ECONOMIC_WIN
        if value < -self.pnl_epsilon_inr:
            return self.ECONOMIC_LOSS
        return self.ECONOMIC_BREAKEVEN

    def normalize_signal(self, value: Any) -> str | None:
        token = self._upper(value)
        if token in self.signal_all:
            return token
        # Compatibility-only aliases. This mapping is directional from legacy
        # signal semantics; economic P&L is never used to infer signal quality.
        if token in {"TARGET_HIT"}:
            return self.SIGNAL_SUCCESS
        if token in {"FAIL", "SL_HIT", "STOP_HIT"}:
            return self.SIGNAL_FAILURE
        return None

    def normalize_economic(self, value: Any) -> str | None:
        token = self._upper(value)
        return token if token in self.economic_all else None

    def compatibility_result(self, signal_outcome: Any) -> str:
        signal = self.normalize_signal(signal_outcome) or self.SIGNAL_UNSCORABLE
        return {
            self.SIGNAL_SUCCESS: "WIN",
            self.SIGNAL_FAILURE: "LOSS",
            self.SIGNAL_NEUTRAL: "NEUTRAL",
            self.SIGNAL_UNSCORABLE: "UNSCORABLE",
        }[signal]

    def accuracy_eligible(self, signal_outcome: Any) -> bool:
        return self.normalize_signal(signal_outcome) in self.signal_decisive

    def performance_eligible(self, signal_outcome: Any, economic_outcome: Any) -> bool:
        signal = self.normalize_signal(signal_outcome)
        economic = self.normalize_economic(economic_outcome)
        return signal in {
            self.SIGNAL_SUCCESS, self.SIGNAL_FAILURE, self.SIGNAL_NEUTRAL
        } and economic in {
            self.ECONOMIC_WIN,
            self.ECONOMIC_LOSS,
            self.ECONOMIC_BREAKEVEN,
        }

    def summarize(self, rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
        materialized = [dict(row or {}) for row in rows]
        signals = [self.normalize_signal(row.get("signal_outcome")) for row in materialized]
        economics = [self.normalize_economic(row.get("economic_outcome")) for row in materialized]
        success = sum(value == self.SIGNAL_SUCCESS for value in signals)
        failure = sum(value == self.SIGNAL_FAILURE for value in signals)
        neutral = sum(value == self.SIGNAL_NEUTRAL for value in signals)
        unscorable = sum(value == self.SIGNAL_UNSCORABLE for value in signals)
        accuracy_denominator = success + failure
        wins = sum(value == self.ECONOMIC_WIN for value in economics)
        losses = sum(value == self.ECONOMIC_LOSS for value in economics)
        breakeven = sum(value == self.ECONOMIC_BREAKEVEN for value in economics)
        economic_unscorable = sum(value == self.ECONOMIC_UNSCORABLE for value in economics)
        win_rate_denominator = wins + losses
        return {
            "taxonomy_authority": self.authority,
            "taxonomy_version": self.authority_version,
            "signal": {
                "success": success,
                "failure": failure,
                "neutral": neutral,
                "unscorable": unscorable,
                "accuracy_denominator": accuracy_denominator,
                "accuracy_pct": round(success * 100.0 / accuracy_denominator, 2) if accuracy_denominator else None,
                "neutral_excluded_from_accuracy": True,
                "unscorable_excluded_from_accuracy": True,
            },
            "economic": {
                "wins": wins,
                "losses": losses,
                "breakeven": breakeven,
                "unscorable": economic_unscorable,
                "win_rate_denominator": win_rate_denominator,
                "win_rate_pct": round(wins * 100.0 / win_rate_denominator, 2) if win_rate_denominator else None,
                "breakeven_excluded_from_win_rate": True,
                "unscorable_excluded_from_performance": True,
            },
        }


DEFAULT_OUTCOME_ACCURACY_TAXONOMY = OutcomeAccuracyTaxonomy()
