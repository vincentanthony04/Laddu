"""Canonical rolling Final Model Paper performance for AC-071.

This service exists so the Performance UI can request 7D/30D/90D/all-time
realized Final economics without borrowing Research horizon semantics or
compatibility SQLite.  Every metric is calculated from the same operational
PostgreSQL Model Paper settlement rows and carries an explicit unit contract.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Mapping, Sequence

from core.research_final_attribution_service import ResearchFinalAttributionService


SERVICE_VERSION = "final-rolling-performance-1.0.0-ac071"
PERIODS = {"7D": 7, "30D": 30, "90D": 90, "ALL": None}


def _dt(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _record(raw: Mapping[str, Any]) -> Dict[str, Any]:
    row = dict(raw or {})
    return {
        "position_id": row.get("position_id"),
        "source_signal_id": row.get("source_signal_id"),
        "decision_id": row.get("decision_id"),
        "symbol": row.get("symbol"),
        "exchange": row.get("exchange"),
        "mode": row.get("mode"),
        "side": row.get("side"),
        "entry_price": _float(row.get("entry_price")),
        "exit_price": _float(row.get("exit_price")),
        "gross_pnl_inr": _float(row.get("gross_pnl")),
        "costs_inr": _float(row.get("total_cost")),
        "net_pnl_inr": _float(row.get("net_pnl")),
        "signal_outcome": row.get("signal_outcome"),
        "economic_outcome": row.get("economic_outcome"),
        "exit_reason": row.get("exit_reason"),
        "opened_at": row.get("opened_at"),
        "closed_at": row.get("closed_at"),
        "realized_r": _float(row.get("realized_r")),
        "mfe_r": _float(row.get("mfe_r")),
        "mae_r": _float(row.get("mae_r")),
        "holding_minutes": _float(row.get("holding_minutes")),
        "decision_delay_bucket": row.get("decision_delay_bucket"),
        "open_age_bucket": row.get("open_age_bucket"),
        "age_bucket_policy_version": row.get("age_bucket_policy_version"),
        "cause": str(row.get("exit_reason") or row.get("signal_outcome") or row.get("economic_outcome") or "CAUSE_NOT_RECORDED"),
        "units": {
            "prices": "INR_PER_SHARE",
            "gross_pnl_inr": "INR_REALIZED",
            "costs_inr": "INR_REALIZED_EXECUTION_COSTS",
            "net_pnl_inr": "INR_REALIZED_NET_OF_EXECUTION_COSTS",
            "realized_r": "R_REALIZED_NET_PNL_OVER_IMMUTABLE_INITIAL_RISK",
            "mfe_r": "R_FAVORABLE_EXCURSION_OVER_IMMUTABLE_INITIAL_RISK",
            "mae_r": "R_ADVERSE_EXCURSION_OVER_IMMUTABLE_INITIAL_RISK",
            "holding_minutes": "MINUTES",
        },
    }


class FinalRollingPerformanceService:
    """Rolling Final performance from operational PostgreSQL only."""

    def __init__(self, store: Any):
        self.repository = getattr(store, "production_model_portfolio_repository", None)

    def report(
        self, *, period: str = "30D", desk: str = "all", as_of: datetime | None = None,
        limit: int = 1000,
    ) -> Dict[str, Any]:
        if self.repository is None or not callable(getattr(self.repository, "settled_final_economics", None)):
            return {
                "ok": False,
                "state": "OPERATIONAL_FINAL_ECONOMICS_UNAVAILABLE",
                "version": SERVICE_VERSION,
                "authority": "OPERATIONAL_POSTGRESQL_MODEL_PAPER_ONLY",
                "book": "MODEL_PAPER",
                "broker_authority": "NONE",
            }
        period_key = str(period or "30D").upper()
        if period_key not in PERIODS:
            period_key = "30D"
        desk_key = str(desk or "all").lower()
        if desk_key not in {"all", "delivery", "intraday"}:
            desk_key = "all"
        now = as_of or datetime.now(timezone.utc)
        now = now.astimezone(timezone.utc) if now.tzinfo else now.replace(tzinfo=timezone.utc)
        days = PERIODS[period_key]
        cutoff = now - timedelta(days=days) if days is not None else None
        rows = self.repository.settled_final_economics(
            closed_since=cutoff,
            mode=None if desk_key == "all" else desk_key,
            position_ids=None,
            limit=max(1, min(int(limit), 100000)),
        )
        # Repository compatibility does not guarantee an upper as-of filter.
        # Re-apply the causal boundary here so a future settlement can never
        # leak into a historical rolling report.
        causal_rows = []
        for row in rows:
            try:
                closed = datetime.fromisoformat(str(row.get("closed_at") or "").replace("Z", "+00:00"))
                closed = closed.astimezone(timezone.utc) if closed.tzinfo else closed.replace(tzinfo=timezone.utc)
            except Exception:
                continue
            if closed > now:
                continue
            if cutoff is not None and closed < cutoff:
                continue
            causal_rows.append(row)
        # Retain repository ordering for drawdown/history fidelity.
        rows = causal_rows
        metrics = {
            "all": ResearchFinalAttributionService._final_metrics(rows),
            "delivery": ResearchFinalAttributionService._final_metrics([r for r in rows if str(r.get("mode") or "").lower() == "delivery"]),
            "intraday": ResearchFinalAttributionService._final_metrics([r for r in rows if str(r.get("mode") or "").lower() == "intraday"]),
        }
        return {
            "ok": True,
            "state": "SOURCE_FINAL_ROLLING_PERFORMANCE_AVAILABLE",
            "version": SERVICE_VERSION,
            "period": period_key,
            "desk": desk_key,
            "period_start": cutoff.isoformat().replace("+00:00", "Z") if cutoff else None,
            "period_end": now.isoformat().replace("+00:00", "Z"),
            "authority": "OPERATIONAL_POSTGRESQL_MODEL_PAPER_ONLY",
            "book": "MODEL_PAPER",
            "broker_authority": "NONE",
            "metrics": metrics,
            "records": [_record(row) for row in rows[-max(1, min(int(limit), 5000)):]],
            "record_count": len(rows),
            "unit_contract": {
                "realized_economics": "INR",
                "drawdown": "INR_EQUITY_CURVE_DRAWDOWN",
                "signal_accuracy": "PERCENT_DECISIVE_SIGNAL_OUTCOMES_ONLY",
                "economic_win_rate": "PERCENT_DECISIVE_ECONOMIC_OUTCOMES_ONLY",
                "normalized_return": "R_REALIZED_NET_PNL_OVER_IMMUTABLE_INITIAL_RISK",
                "capital_return": "NOT_REPORTED_WITHOUT_CANONICAL_CAPITAL_DENOMINATOR",
            },
            "policy": "Final lane is canonical PostgreSQL Model Paper only; Research counterfactual returns are never blended into realized INR.",
        }
