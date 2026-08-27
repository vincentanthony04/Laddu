"""Canonical Final-decision bridge into the governed Model Paper book.

The production signal ledger and Model Paper portfolio intentionally remain
separate stores. This bridge is the single, idempotent transition between
them: only a fully actionable Final decision may attempt portfolio admission.
The portfolio service then independently applies timing, fill, capital,
liquidity, concentration, FOMO and cost constraints.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict

from actionability import is_actionable_signal
from core.india_time import INDIA_TZ
from core.model_portfolio_service import ModelPortfolioService


class ModelPortfolioBridgeService:
    def __init__(self, store: Any, *, equity: float = 500_000.0, intraday_cap: float = 100_000.0):
        self.store = store
        existing = getattr(store, "model_portfolio_service", None)
        if existing is not None:
            self.portfolio = existing
        else:
            # Isolated tests/tools may use the local compatibility book. The
            # installed runtime attaches its already-constructed PostgreSQL
            # ModelPortfolioService, preventing a parallel SQLite authority.
            self.portfolio = ModelPortfolioService(store, equity=equity, intraday_cap=intraday_cap)

    @staticmethod
    def _local(at: datetime | None = None) -> datetime:
        current = at or datetime.now(INDIA_TZ)
        if current.tzinfo is None:
            return current.replace(tzinfo=INDIA_TZ)
        return current.astimezone(INDIA_TZ)

    @classmethod
    def _signal_id(cls, decision: Dict[str, Any], at: datetime | None = None) -> str:
        supplied = str(decision.get("signal_id") or decision.get("source_signal_id") or "").strip()
        if supplied:
            return supplied
        symbol = str(decision.get("symbol") or "").upper().strip()
        mode = str(decision.get("mode") or "").lower().strip()
        side = str(decision.get("side") or "").upper().strip()
        if mode == "intraday":
            return f"{cls._local(at).date().isoformat()}:{symbol}:intraday:{side}"
        return f"CARRY:{symbol}:delivery:{side}"

    @staticmethod
    def _quote(decision: Dict[str, Any]) -> Dict[str, Any]:
        freshness = str(
            decision.get("freshness_state")
            or decision.get("price_freshness_state")
            or decision.get("price_freshness")
            or ""
        ).lower().strip()
        identity_verified = decision.get("identity_verified") is True
        fresh = freshness in {"fresh", "live", "live_current", "closed_market"}
        usable = decision.get("usable_for_promotion") is not False
        return {
            "ltp": decision.get("ltp") if decision.get("ltp") is not None else decision.get("current_price"),
            "verified": identity_verified,
            "fresh": bool(fresh and usable),
            "executable": bool(identity_verified and fresh and usable),
            "as_of": decision.get("quote_as_of") or decision.get("source_time") or decision.get("timestamp"),
        }

    def observe_final(self, decision: Dict[str, Any], *, at: datetime | None = None) -> Dict[str, Any]:
        row = dict(decision or {})
        if not is_actionable_signal(row):
            return {
                "state": "SKIPPED",
                "reason": "decision is not a canonical capital-approved Final",
                "symbol": row.get("symbol"),
                "mode": row.get("mode"),
            }
        row.update({
            "signal_id": self._signal_id(row, at),
            "authority": "PRODUCTION_FINAL",
            "target": row.get("target") if row.get("target") is not None else row.get("t1"),
            "sl": row.get("sl") if row.get("sl") is not None else row.get("stop"),
        })
        return self.portfolio.admit(row, self._quote(row), at=self._local(at))

    @classmethod
    def observe_store_decision(cls, store: Any, decision: Dict[str, Any]) -> Dict[str, Any] | None:
        """Best-effort projection hook; canonical signal persistence stays authoritative."""
        if not is_actionable_signal(decision):
            return None
        try:
            admission = cls(store).observe_final(decision)
            store.event(
                "INFO", "model_portfolio", "Canonical Final observed by Model Paper",
                {
                    "symbol": decision.get("symbol"), "mode": decision.get("mode"),
                    "state": admission.get("state"), "disposition": admission.get("disposition"),
                },
            )
            return admission
        except Exception as exc:
            store.event(
                "ERROR", "model_portfolio", "Model Paper admission failed",
                {"symbol": decision.get("symbol"), "mode": decision.get("mode"), "error": str(exc)[:220]},
            )
            return {"state": "ERROR", "error": str(exc)[:220]}
