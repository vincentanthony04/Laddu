"""Net-rupee performance and accuracy projections for settled Model Paper only."""
from __future__ import annotations

from datetime import datetime
import math
from typing import Any, Dict, Iterable

from core.india_time import INDIA_TZ
from core.expectancy_semantics_authority import lane as expectancy_lane
from core.model_paper_performance_period_authority import DEFAULT_MODEL_PAPER_PERFORMANCE_PERIOD_AUTHORITY
from core.outcome_accuracy_taxonomy import DEFAULT_OUTCOME_ACCURACY_TAXONOMY
from core.signal_ledger_continuity_service import SignalLedgerContinuityService
from core.signal_age_authority import DEFAULT_SIGNAL_AGE_AUTHORITY


def _parse(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(INDIA_TZ)
    except Exception:
        return None


def _finite_number(value: Any) -> float | None:
    try:
        if value is None or isinstance(value, bool):
            return None
        out = float(value)
        return out if math.isfinite(out) else None
    except (TypeError, ValueError):
        return None


class ModelPortfolioPerformanceService:
    VERSION = "model-paper-performance-authority-1.4.0-finite-causal"

    def __init__(self, store: Any, *, repository: Any | None = None):
        self.store = store
        self.repository = repository
        self.continuity = SignalLedgerContinuityService(store)

    def _positions(self, status: str) -> list[Dict[str, Any]]:
        """Return only governed Model Paper positions.

        Production positions live in PostgreSQL through
        ``ProductionModelPortfolioRepository``. SQLite is retained strictly as
        a compatibility/test path. Research/quant counterfactual positions are
        deliberately *not* merged into this economic book.
        """
        if self.repository is not None:
            if str(status).upper() == "CLOSED" and callable(getattr(self.repository, "settled_learning_rows", None)):
                # Performance and governed Learning intentionally consume the
                # same canonical settled attribution rows (MFE/MAE/R/holding/path).
                return [dict(row or {}) for row in self.repository.settled_learning_rows(limit=100000)]
            return [dict(row or {}) for row in self.repository.list_positions(status)]
        return [
            dict(row)
            for row in self.store.conn.execute(
                """SELECT * FROM model_portfolio_positions
                   WHERE status=? ORDER BY COALESCE(closed_at,updated_at) DESC""",
                (status,),
            ).fetchall()
        ]

    def _research_counterfactual(self, status: str) -> list[Dict[str, Any]]:
        """Compatibility research lane, kept separate from Model Paper P&L."""
        try:
            exists = self.store.conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='quant_evaluation_positions'"
            ).fetchone()
        except Exception:
            return []
        if not exists:
            return []
        rows: list[Dict[str, Any]] = []
        for raw in self.store.conn.execute(
            """SELECT * FROM quant_evaluation_positions
               WHERE status=? ORDER BY COALESCE(closed_at,updated_at) DESC""",
            (status,),
        ).fetchall():
            row = dict(raw)
            outcome = str(row.get("outcome") or "").upper()
            row["economic_outcome"] = outcome if outcome in {"WIN", "LOSS", "BREAKEVEN"} else None
            row["signal_outcome"] = (
                "SUCCESS" if outcome == "WIN"
                else "FAILURE" if outcome == "LOSS"
                else "UNSCORABLE" if row.get("unscorable")
                else "NEUTRAL" if outcome == "BREAKEVEN" else None
            )
            rows.append(row)
        return rows

    @staticmethod
    def _aggregate(rows: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
        rows = [dict(row or {}) for row in rows]
        taxonomy = DEFAULT_OUTCOME_ACCURACY_TAXONOMY
        invalid_economic_rows = []
        for row in rows:
            row["signal_outcome"] = taxonomy.normalize_signal(row.get("signal_outcome"))
            row["economic_outcome"] = taxonomy.normalize_economic(row.get("economic_outcome"))
            net = _finite_number(row.get("net_pnl"))
            gross = _finite_number(row.get("gross_pnl"))
            costs = _finite_number(row.get("total_cost"))
            # Settled economic evidence is atomic: missing/non-finite INR values
            # cannot participate in P&L, accuracy, expectancy or learning.
            if net is None or gross is None or costs is None:
                row["signal_outcome"] = taxonomy.SIGNAL_UNSCORABLE
                row["economic_outcome"] = None
                row["performance_evidence_state"] = "INVALID_NONFINITE_ECONOMICS"
                invalid_economic_rows.append(row)
            else:
                row["net_pnl"] = net
                row["gross_pnl"] = gross
                row["total_cost"] = costs
                row["performance_evidence_state"] = "FINITE"

        # Realized performance and signal accuracy use the same settled rows but
        # have deliberately different denominators. NEUTRAL is observable and
        # economically valid, but it is not a target/stop accuracy observation.
        eligible = [
            row for row in rows
            if taxonomy.performance_eligible(row.get("signal_outcome"), row.get("economic_outcome"))
        ]
        decisive = [row for row in eligible if taxonomy.accuracy_eligible(row.get("signal_outcome"))]
        success = sum(row.get("signal_outcome") == taxonomy.SIGNAL_SUCCESS for row in decisive)
        failure = sum(row.get("signal_outcome") == taxonomy.SIGNAL_FAILURE for row in decisive)
        neutral = sum(row.get("signal_outcome") == taxonomy.SIGNAL_NEUTRAL for row in eligible)
        unscorable_rows = [row for row in rows if row.get("signal_outcome") == taxonomy.SIGNAL_UNSCORABLE]

        net_values = [float(row["net_pnl"]) for row in eligible]
        gross_profit = sum(value for value in net_values if value > 0)
        gross_loss = abs(sum(value for value in net_values if value < 0))
        chronological = sorted(eligible, key=lambda row: str(row.get("closed_at") or row.get("updated_at") or ""))
        curve = peak = 0.0
        max_drawdown = 0.0
        for row in chronological:
            curve += float(row["net_pnl"])
            peak = max(peak, curve)
            max_drawdown = min(max_drawdown, curve - peak)

        settled = len(eligible)
        wins = sum(row.get("economic_outcome") == taxonomy.ECONOMIC_WIN for row in eligible)
        losses = sum(row.get("economic_outcome") == taxonomy.ECONOMIC_LOSS for row in eligible)
        breakeven = sum(row.get("economic_outcome") == taxonomy.ECONOMIC_BREAKEVEN for row in eligible)
        accuracy_denominator = success + failure
        economic_win_rate_denominator = wins + losses
        attributed = [row for row in eligible if row.get("excursion_attribution_complete") is True]
        def _avg(key: str):
            values = []
            for row in attributed:
                try:
                    value = float(row.get(key))
                    if math.isfinite(value):
                        values.append(value)
                except (TypeError, ValueError):
                    continue
            return round(sum(values) / len(values), 6) if values else None
        exit_reason_counts: Dict[str, int] = {}
        for row in eligible:
            reason = str(row.get("exit_reason") or "UNKNOWN")
            exit_reason_counts[reason] = exit_reason_counts.get(reason, 0) + 1
        signal_age_attribution = DEFAULT_SIGNAL_AGE_AUTHORITY.aggregate(eligible)
        return {
            "closed_observations": len(rows),
            "settled_trades": settled,
            "performance_eligible_trades": settled,
            "excluded_unscorable": len(unscorable_rows),
            "invalid_economic_observations": len(invalid_economic_rows),
            "unscorable_observed_net_pnl": round(sum(value for row in unscorable_rows if (value := _finite_number(row.get("net_pnl"))) is not None), 2),
            "scored_trades": accuracy_denominator,  # compatibility alias: decisive accuracy observations only
            "accuracy_denominator": accuracy_denominator,
            "wins": wins,
            "losses": losses,
            "breakeven": breakeven,
            "economic_win_rate_denominator": economic_win_rate_denominator,
            "win_rate_pct": round(wins / economic_win_rate_denominator * 100, 2) if economic_win_rate_denominator else None,
            "success": success,
            "failure": failure,
            "neutral": neutral,
            "unscorable": len(unscorable_rows),
            "accuracy_pct": round(success / accuracy_denominator * 100, 2) if accuracy_denominator else None,
            "neutral_excluded_from_accuracy": True,
            "unscorable_excluded_from_accuracy": True,
            "breakeven_excluded_from_win_rate": True,
            "outcome_taxonomy_authority": taxonomy.authority,
            "outcome_taxonomy_version": taxonomy.authority_version,
            "gross_pnl": round(sum(float(row["gross_pnl"]) for row in eligible), 2),
            "costs": round(sum(float(row["total_cost"]) for row in eligible), 2),
            "net_pnl": round(sum(net_values), 2),
            "expectancy_semantics": expectancy_lane("MODEL_PAPER_REALIZED_EXPECTANCY"),
            "realized_expectancy_inr_per_trade": round(sum(net_values) / settled, 2) if settled else None,
            "expectancy_net": round(sum(net_values) / settled, 2) if settled else None,
            "average_win": round(gross_profit / wins, 2) if wins else None,
            "average_loss": round(gross_loss / losses, 2) if losses else None,
            "profit_factor": round(gross_profit / gross_loss, 4) if gross_loss > 0 else None,
            "max_drawdown": round(max_drawdown, 2),
            "signal_age_attribution": signal_age_attribution,
            "excursion_attribution": {
                "attributed_trades": len(attributed),
                "average_mfe_r": _avg("mfe_r"),
                "average_mae_r": _avg("mae_r"),
                "average_realized_r": _avg("realized_r"),
                "average_holding_minutes": _avg("holding_minutes"),
                "exit_reason_counts": exit_reason_counts,
                "authority": "FinalExcursionAttributionAuthority",
                "authority_version": "1.0.0",
                "source": "same canonical settled rows consumed by Performance and Level-5 Learning",
            },
        }

    def report(self, *, as_of: datetime | None = None) -> Dict[str, Any]:
        now = as_of or datetime.now(INDIA_TZ)
        if now.tzinfo is None:
            now = now.replace(tzinfo=INDIA_TZ)
        now = now.astimezone(INDIA_TZ)
        closed = self._positions("CLOSED")
        period_authority = DEFAULT_MODEL_PAPER_PERFORMANCE_PERIOD_AUTHORITY
        filters: Dict[str, Any] = {}
        for label in ("today", "week", "month", "quarter", "year", "all"):
            period_rows = [
                row for row in closed
                if period_authority.contains(_parse(row.get("closed_at")), label, now)
            ]
            filters[label] = {
                "all": self._aggregate(period_rows),
                "intraday": self._aggregate(row for row in period_rows if row.get("mode") == "intraday"),
                "delivery": self._aggregate(row for row in period_rows if row.get("mode") == "delivery"),
            }
        open_rows = self._positions("OPEN")
        open_delivery = [row for row in open_rows if row.get("mode") == "delivery"]
        open_intraday = [row for row in open_rows if row.get("mode") == "intraday"]
        taxonomy_summary = DEFAULT_OUTCOME_ACCURACY_TAXONOMY.summarize(closed)
        research_closed = self._research_counterfactual("CLOSED")
        research_open = self._research_counterfactual("OPEN")
        return {
            "ok": True,
            "version": self.VERSION,
            "period_authority": DEFAULT_MODEL_PAPER_PERFORMANCE_PERIOD_AUTHORITY.authority,
            "period_authority_version": DEFAULT_MODEL_PAPER_PERFORMANCE_PERIOD_AUTHORITY.authority_version,
            "authority": "POSTGRESQL_MODEL_PAPER_POSITIONS" if self.repository is not None else "SQLITE_COMPAT_MODEL_PAPER_POSITIONS",
            "as_of": now.isoformat(timespec="seconds"),
            "book": "MODEL_PAPER",
            "units": "INR_NET_OF_EXECUTION_COSTS",
            "filters": filters,
            "activity": {
                "total_settled": len(closed),
                "intraday_settled": sum(row.get("mode") == "intraday" for row in closed),
                "delivery_settled": sum(row.get("mode") == "delivery" for row in closed),
                "open_now": len(open_rows),
                "intraday_open": len(open_intraday),
                "delivery_open": len(open_delivery),
            },
            "accuracy": {
                "scored": taxonomy_summary["signal"]["accuracy_denominator"],
                "accuracy_denominator": taxonomy_summary["signal"]["accuracy_denominator"],
                "accuracy_pct": taxonomy_summary["signal"]["accuracy_pct"],
                "success": taxonomy_summary["signal"]["success"],
                "failure": taxonomy_summary["signal"]["failure"],
                "neutral": taxonomy_summary["signal"]["neutral"],
                "unscorable": taxonomy_summary["signal"]["unscorable"],
                "neutral_excluded_from_accuracy": True,
                "unscorable_excluded_from_accuracy": True,
                "outcome_taxonomy_authority": DEFAULT_OUTCOME_ACCURACY_TAXONOMY.authority,
                "outcome_taxonomy_version": DEFAULT_OUTCOME_ACCURACY_TAXONOMY.authority_version,
            },
            "open_mtm": {
                "positions": len(open_rows),
                "intraday_positions": len(open_intraday),
                "delivery_positions": len(open_delivery),
                "net_pnl": round(sum(float(row.get("net_pnl") or 0) for row in open_rows), 2),
                "excluded_from_settled_performance": True,
                "excluded_from_accuracy": True,
            },
            "open_delivery_mtm": {
                "positions": len(open_delivery),
                "net_pnl": round(sum(float(row.get("net_pnl") or 0) for row in open_delivery), 2),
                "excluded_from_accuracy": True,
            },
            # This is deliberately separate from ``filters``. Older persisted
            # signal outcomes prove signal history, but they do not freeze
            # quantity, statutory costs or governed capital admission and
            # therefore cannot be blended into Model Paper rupee performance.
            "research_counterfactual": {
                "included_in_model_paper": False,
                "included_in_model_paper_pnl": False,
                "units": "INR_RESEARCH_COUNTERFACTUAL",
                "settled": self._aggregate(research_closed),
                "open_positions": len(research_open),
                "policy": "Quant/Research evaluation positions remain a separate evidence lane and never blend into governed Model Paper economics.",
            },
            "continuity": self.continuity.report(as_of=now),
            "policy": "settled governed Model Paper only; Research Counterfactual, legacy signal points and Manual Holdings excluded",
        }
