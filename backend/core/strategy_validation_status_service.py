"""Truthful strategy-validation status for Project Laddu.

This read model separates software regression success, live outcome collection,
legacy/partial research reports and full-universe production-policy replay.  It
never converts unit-test counts or a small live journal into a strategy claim.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Dict, Optional

from config import DATA_DIR
from core.evidence_engine_service import INTRADAY_MODEL_VERSION, DELIVERY_MODEL_VERSION
from core.walk_forward_validation_service import CAPITAL_PROFILE, WalkForwardValidationService
from core.expectancy_semantics_authority import lane as expectancy_lane


STATUS_VERSION = "strategy-validation-status-1.0.0"
REQUIRED_INPUTS = (
    "candles_5m.zip",
    "delivery_export.csv",
    "bhav_export.zip",
    "real_data_export.zip",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class StrategyValidationStatusService:
    DESKS = {
        "intraday": INTRADAY_MODEL_VERSION,
        "delivery": DELIVERY_MODEL_VERSION,
    }

    def __init__(self, store: Any = None, *, data_dir: Optional[Path] = None,
                 project_root: Optional[Path] = None):
        self.store = store
        self.data_dir = Path(data_dir or DATA_DIR)
        self.project_root = Path(project_root or Path(__file__).resolve().parents[2])

    def _latest_capital_report(self, model_id: str) -> Optional[Dict[str, Any]]:
        if self.store is None:
            return None
        try:
            approvals = WalkForwardValidationService(self.store).status(
                model_id=model_id, profile=CAPITAL_PROFILE,
            ).get("approvals") or []
            return dict(approvals[0]) if approvals else None
        except Exception:
            return None

    @staticmethod
    def _desk_state(report: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        if not report:
            return {
                "state": "NOT_RUN",
                "approved_for_shadow": False,
                "full_universe": False,
                "production_policy_replay": False,
                "reason": "No persisted capital-profile production replay exists for this desk.",
            }
        declared_full = report.get("full_universe_replay") is True
        production_policy = report.get("production_policy_replay") is True
        coverage = float(report.get("universe_coverage_pct") or 0.0)
        approved = report.get("approved") is True and str(report.get("status") or "").upper() == "APPROVED"
        if approved and declared_full and production_policy and coverage >= 95.0:
            state = "APPROVED_FOR_SHADOW"
            reason = "Capital-profile full-universe production-policy replay passed; live capital remains unauthorized."
        elif approved:
            state = "PARTIAL_APPROVAL"
            reason = "A capital-profile report passed, but it is not marked as a >=95% full-universe production-policy replay."
        else:
            state = "FAILED_OR_INCOMPLETE"
            reason = "A capital-profile validation exists but did not pass every required gate."
        return {
            "state": state,
            "approved_for_shadow": state == "APPROVED_FOR_SHADOW",
            "full_universe": bool(declared_full and coverage >= 95.0),
            "production_policy_replay": production_policy,
            "universe_coverage_pct": coverage,
            "validated_at": report.get("validated_at"),
            "n_test": int(report.get("n_test") or 0),
            "n_test_days": int(report.get("n_test_days") or 0),
            "universe_symbols": int(report.get("universe_symbols") or 0),
            "win_rate": report.get("win_rate"),
            "expectancy": report.get("expectancy"),
            "profit_factor": report.get("profit_factor"),
            "approval_id": report.get("approval_id"),
            "reason": reason,
        }

    def _live_outcomes(self) -> Dict[str, Any]:
        result = {"state": "COLLECTING", "samples": 0, "wins": 0, "losses": 0,
                  "win_rate": None, "strategy_evidence": False,
                  "metric_lane": expectancy_lane("SIGNAL_ACCURACY_POINTS"),
                  "economic_performance_eligible": False}
        if self.store is None:
            return result
        try:
            exists = self.store.conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='outcome_learning'"
            ).fetchone()
            if not exists:
                return result
            row = self.store.conn.execute(
                """SELECT COUNT(*) AS n,
                          SUM(CASE WHEN pnl_points>0 THEN 1 ELSE 0 END) AS wins,
                          SUM(CASE WHEN pnl_points<0 THEN 1 ELSE 0 END) AS losses
                   FROM outcome_learning"""
            ).fetchone()
            values = dict(row) if hasattr(row, "keys") else {"n": row[0], "wins": row[1], "losses": row[2]}
            n, wins, losses = int(values.get("n") or 0), int(values.get("wins") or 0), int(values.get("losses") or 0)
            decisive = wins + losses
            result.update({
                "samples": n, "wins": wins, "losses": losses,
                "win_rate": round(wins * 100.0 / decisive, 2) if decisive else None,
                "confidence": "high" if decisive >= 100 else "medium" if decisive >= 30 else "low",
                "strategy_evidence": decisive >= 30,
                "reason": "Live/shadow outcomes are observational and do not replace out-of-sample production replay.",
            })
        except Exception as exc:
            result.update({"state": "UNAVAILABLE", "reason": str(exc)})
        return result

    def legacy_report(self) -> Dict[str, Any]:
        path = self.project_root / "REAL_WALK_FORWARD_v42.json"
        if not path.exists():
            return {"state": "NOT_AVAILABLE", "full_universe": False, "path": path.name}
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            return {"state": "INVALID", "full_universe": False, "path": path.name, "error": str(exc)}
        return {
            "state": "LEGACY_PARTIAL",
            "full_universe": False,
            "production_policy_replay": False,
            "path": path.name,
            "scope": "Legacy 97-symbol report; not accepted as full-universe production-equivalent evidence.",
            "report": report,
        }

    def status(self) -> Dict[str, Any]:
        desks = {
            desk: self._desk_state(self._latest_capital_report(model_id))
            for desk, model_id in self.DESKS.items()
        }
        full_pass = all(row["state"] == "APPROVED_FOR_SHADOW" for row in desks.values())
        inputs = [
            {
                "name": name,
                "present": (self.data_dir / name).exists(),
                "path": str(self.data_dir / name),
                "cache_class": "durable_point_in_time_backtest_input",
                "size_bytes": (self.data_dir / name).stat().st_size if (self.data_dir / name).exists() else 0,
            }
            for name in REQUIRED_INPUTS
        ]
        legacy = self.legacy_report()
        overall = "APPROVED_FOR_SHADOW" if full_pass else (
            "PARTIAL" if any(row["state"] != "NOT_RUN" for row in desks.values()) or legacy["state"] == "LEGACY_PARTIAL"
            else "NOT_RUN"
        )
        return {
            "ok": True,
            "status_version": STATUS_VERSION,
            "as_of": _now(),
            "overall_state": overall,
            "full_universe_production_replay": full_pass,
            "capital_authority": "NONE",
            "software_regression": {
                "state": "NOT_STRATEGY_EVIDENCE",
                "reason": "Passing unit, integration and UI tests demonstrates software behaviour, not trading profitability.",
            },
            "desks": desks,
            "legacy_report": legacy,
            "live_outcomes": self._live_outcomes(),
            "required_inputs": inputs,
            "missing_inputs": [row["name"] for row in inputs if not row["present"]],
            "backtest_cache_ready": all(row["present"] and row["size_bytes"] > 0 for row in inputs),
            "data_cache_policy": {
                "daily_ohlcv": "durable SQLite history; completed daily bars are not pruned",
                "intraday_backtest": "authorized point-in-time imports use a non-prunable archive source; runtime intraday cache remains bounded",
                "fundamentals": "every distinct verified filing snapshot is versioned by effective/as-of date",
                "live_price": "latest quote has a market-hours TTL; durable snapshots are audit evidence, never proof that a price is current",
                "missing_period_policy": "block or mark unscorable; never silently shrink a backtest sample",
            },
            "claims": {
                "validated_win_rate_available": full_pass,
                "full_universe_backtest_completed": full_pass,
                "live_capital_authorized": False,
            },
            "policy": "Win-rate or performance claims require full-universe, point-in-time, cost-adjusted production-policy replay for Intraday and Delivery plus separate shadow evidence.",
        }
