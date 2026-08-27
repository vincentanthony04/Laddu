"""Canonical deterministic risk admission and position sizing mathematics.

One authority owns quantity arithmetic for both the pre-admission risk ceiling
and the final transactional Model Paper allocation.  Callers may apply hard
portfolio/risk vetoes around this service, but they must not independently
recompute quantity.

Heuristic/model confidence is intentionally absent.  Unqualified alpha, regime
or model scores are not allowed to manufacture or reduce a production quantity.
A separately approved, versioned *de-risk only* multiplier may be supplied by a
governed safety policy.
"""
from __future__ import annotations

import math
from typing import Any, Dict

from core.india_cash_cost_service import IndiaCashCostService
from core.numeric_semantics import finite_number


class RiskAdmissionAndSizingAuthority:
    authority = "RiskAdmissionAndSizingAuthority"
    authority_version = "1.2.0-canonical-finite-policy-input-contract"

    @staticmethod
    def _finite(name: str, value: Any, *, allow_zero: bool = False) -> float:
        out = finite_number(value)
        if out is None or out < 0 or (not allow_zero and out == 0):
            relation = "non-negative" if allow_zero else "positive"
            raise ValueError(f"{name} must be finite and {relation}")
        return out

    @classmethod
    def _integer(cls, name: str, value: Any, *, minimum: int = 1) -> int:
        out = finite_number(value)
        if out is None or not float(out).is_integer() or int(out) < minimum:
            raise ValueError(f"{name} must be an integer >= {minimum}")
        return int(out)

    @classmethod
    def _percent(cls, name: str, value: Any) -> float:
        out = cls._finite(name, value, allow_zero=True)
        if out > 100.0:
            raise ValueError(f"{name} must be between 0 and 100")
        return out

    @classmethod
    def _optional_nonnegative(cls, name: str, value: Any) -> float | None:
        if value in (None, ""):
            return None
        return cls._finite(name, value, allow_zero=True)

    @staticmethod
    def _qty(cash: float, price: float, lot_size: int = 1) -> int:
        if cash <= 0 or price <= 0:
            return 0
        raw = int(cash // price)
        lot = max(1, int(lot_size))
        return (raw // lot) * lot

    @staticmethod
    def _floor_lot(raw: int, lot_size: int) -> int:
        lot = max(1, int(lot_size))
        return (max(0, int(raw)) // lot) * lot

    @classmethod
    def ceiling(
        cls,
        *,
        mode: str,
        entry: float,
        stop: float,
        equity: float,
        available_cash: float,
        risk_per_trade_pct: float,
        max_symbol_pct: float,
        intraday_cap: float,
        intraday_used: float = 0.0,
        lot_size: int = 1,
        approved_derisk_multiplier: float = 1.0,
        derisk_policy_approved: bool = False,
        derisk_policy_version: str | None = None,
    ) -> Dict[str, Any]:
        mode = str(mode or "").lower()
        if mode not in {"intraday", "delivery"}:
            raise ValueError("mode must be intraday or delivery")
        entry = cls._finite("entry", entry)
        stop = cls._finite("stop", stop)
        equity = cls._finite("equity", equity)
        available_cash = cls._finite("available_cash", available_cash, allow_zero=True)
        intraday_cap = cls._finite("intraday_cap", intraday_cap, allow_zero=True)
        intraday_used = cls._finite("intraday_used", intraday_used, allow_zero=True)
        risk_pct = cls._percent("risk_per_trade_pct", risk_per_trade_pct)
        symbol_pct = cls._percent("max_symbol_pct", max_symbol_pct)
        lot = cls._integer("lot_size", lot_size, minimum=1)

        risk_per_share = abs(entry - stop)
        if risk_per_share <= 0:
            return cls._zero("invalid entry/stop", entry=entry, stop=stop)

        multiplier = 1.0
        policy_state = "MEASURE_ONLY"
        if derisk_policy_approved:
            if not str(derisk_policy_version or "").strip():
                raise ValueError("approved de-risk policy requires a version")
            raw = cls._finite("approved_derisk_multiplier", approved_derisk_multiplier, allow_zero=True)
            if raw > 1.0:
                raise ValueError("approved de-risk multiplier may not increase risk")
            multiplier = raw
            policy_state = "APPROVED_DE_RISK" if multiplier < 1.0 else "APPROVED_NEUTRAL"

        risk_budget = equity * risk_pct / 100.0 * multiplier
        symbol_budget = equity * symbol_pct / 100.0
        mode_budget = available_cash
        if mode == "intraday":
            mode_budget = min(available_cash, max(0.0, intraday_cap - intraday_used))

        constraints = {
            "risk": cls._floor_lot(int(risk_budget // risk_per_share), lot),
            "cash": cls._qty(available_cash, entry, lot),
            "mode_cap": cls._qty(mode_budget, entry, lot),
            "symbol_cap": cls._qty(symbol_budget, entry, lot),
        }
        quantity = min(constraints.values()) if constraints else 0
        binding = min(constraints, key=constraints.get) if constraints else "none"
        return {
            "quantity": quantity,
            "entry": round(entry, 6),
            "stop": round(stop, 6),
            "risk_per_share": round(risk_per_share, 6),
            "risk_budget": round(risk_budget, 2),
            "risk_cash": round(quantity * risk_per_share, 2),
            "notional": round(quantity * entry, 2),
            "available_cash": round(available_cash, 2),
            "effective_equity": round(equity, 2),
            "dynamic_intraday_cap": round(intraday_cap, 2) if mode == "intraday" else None,
            "intraday_used": round(intraday_used, 2) if mode == "intraday" else None,
            "constraints": constraints,
            "binding_constraint": binding,
            "blockers": [] if quantity > 0 else ["INSUFFICIENT_HARD_RISK_OR_CASH_CAPACITY"],
            "approved_derisk_multiplier": multiplier,
            "derisk_policy_state": policy_state,
            "derisk_policy_version": str(derisk_policy_version or "") or None,
            "quantity_authority": cls.authority,
            "quantity_authority_version": cls.authority_version,
            "quantity_semantics": "MAXIMUM_HARD_RISK_CEILING_NOT_FINAL_ALLOCATION",
            "unqualified_model_or_alpha_reducers_used": False,
        }

    @classmethod
    def allocate(
        cls,
        *,
        mode: str,
        side: str,
        exchange: str,
        bse_group: str | None,
        entry: float,
        stop: float,
        equity: float,
        free_cash: float,
        intraday_cap: float,
        intraday_used: float,
        risk_per_trade_pct: float,
        max_symbol_pct: float,
        symbol_used: float,
        max_sector_pct: float,
        sector_used: float,
        max_portfolio_heat_pct: float,
        open_risk: float,
        liquidity_participation_pct: float,
        avg_daily_value: float | None = None,
        risk_ceiling_quantity: int | None = None,
        approved_derisk_multiplier: float = 1.0,
        derisk_policy_approved: bool = False,
        derisk_policy_version: str | None = None,
        execution_model: Dict[str, Any] | None = None,
        cost_service: IndiaCashCostService | None = None,
    ) -> Dict[str, Any]:
        mode = str(mode or "").lower()
        side = str(side or "").upper()
        if mode not in {"intraday", "delivery"} or side not in {"LONG", "SHORT"}:
            raise ValueError("supported mode and side are required")
        service = cost_service or IndiaCashCostService()
        venue = service.venue_identity(exchange=exchange, bse_group=bse_group)

        entry = cls._finite("entry", entry)
        stop = cls._finite("stop", stop)
        equity = cls._finite("equity", equity)
        free_cash = cls._finite("free_cash", free_cash, allow_zero=True)
        intraday_cap = cls._finite("intraday_cap", intraday_cap, allow_zero=True)
        intraday_used = cls._finite("intraday_used", intraday_used, allow_zero=True)
        symbol_used = cls._finite("symbol_used", symbol_used, allow_zero=True)
        sector_used = cls._finite("sector_used", sector_used, allow_zero=True)
        open_risk = cls._finite("open_risk", open_risk, allow_zero=True)
        risk_pct = cls._percent("risk_per_trade_pct", risk_per_trade_pct)
        symbol_pct = cls._percent("max_symbol_pct", max_symbol_pct)
        sector_pct = cls._percent("max_sector_pct", max_sector_pct)
        heat_pct = cls._percent("max_portfolio_heat_pct", max_portfolio_heat_pct)
        liquidity_pct = cls._percent("liquidity_participation_pct", liquidity_participation_pct)
        adv = cls._optional_nonnegative("avg_daily_value", avg_daily_value)
        risk_per_share = abs(entry - stop)
        if risk_per_share <= 0:
            return cls._zero("invalid entry/stop", entry=entry, stop=stop)

        multiplier = 1.0
        policy_state = "MEASURE_ONLY"
        if derisk_policy_approved:
            if not str(derisk_policy_version or "").strip():
                raise ValueError("approved de-risk policy requires a version")
            raw = cls._finite("approved_derisk_multiplier", approved_derisk_multiplier, allow_zero=True)
            if raw > 1.0:
                raise ValueError("approved de-risk multiplier may not increase risk")
            multiplier = raw
            policy_state = "APPROVED_DE_RISK" if multiplier < 1.0 else "APPROVED_NEUTRAL"

        risk_budget = equity * risk_pct / 100.0 * multiplier
        heat_budget = max(0.0, equity * heat_pct / 100.0 - open_risk)
        symbol_budget = max(0.0, equity * symbol_pct / 100.0 - symbol_used)
        sector_budget = max(0.0, equity * sector_pct / 100.0 - sector_used)
        mode_budget = free_cash if mode == "delivery" else min(free_cash, max(0.0, intraday_cap - intraday_used))
        liquidity_budget = (
            adv * liquidity_pct / 100.0
            if adv is not None
            else equity
        )

        constraints = {
            "risk": int(min(risk_budget, heat_budget) // risk_per_share),
            "cash": cls._qty(free_cash, entry),
            "mode_cap": cls._qty(mode_budget, entry),
            "concentration": cls._qty(symbol_budget, entry),
            "sector": cls._qty(sector_budget, entry),
            "liquidity": cls._qty(liquidity_budget, entry),
        }
        ceiling = None if risk_ceiling_quantity is None else cls._integer("risk_ceiling_quantity", risk_ceiling_quantity, minimum=0)
        if ceiling is not None:
            constraints["production_risk_ceiling"] = ceiling

        quantity = min(constraints.values()) if constraints else 0
        # Cost reserve can make the last nominally affordable share unaffordable.
        while quantity > 0:
            reserve = service.reserve(
                mode, side, entry, quantity,
                exchange=str(venue["exchange"]), bse_group=venue["bse_group"], execution_model=execution_model,
            )
            required = entry * quantity + reserve
            if required <= free_cash + 1e-9 and required <= mode_budget + 1e-9:
                break
            quantity -= 1
        constraints["cost_reserve"] = quantity
        quantity = min(constraints.values()) if constraints else 0
        reserve = (
            service.reserve(
                mode, side, entry, quantity,
                exchange=str(venue["exchange"]), bse_group=venue["bse_group"], execution_model=execution_model,
            ) if quantity else 0.0
        )
        return {
            "quantity": quantity,
            "exchange": venue["exchange"],
            "bse_group": venue["bse_group"],
            "entry": round(entry, 6),
            "stop": round(stop, 6),
            "risk_per_share": round(risk_per_share, 6),
            "risk_budget": round(risk_budget, 2),
            "risk_cash": round(risk_per_share * quantity, 2),
            "notional": round(entry * quantity, 2),
            "cost_reserve": round(reserve, 2),
            "required_cash": round(entry * quantity + reserve, 2),
            "constraints": constraints,
            "binding_constraint": min(constraints, key=constraints.get) if constraints else "none",
            "effective_equity": round(equity, 2),
            "dynamic_intraday_cap": round(intraday_cap, 2),
            "approved_derisk_multiplier": multiplier,
            "derisk_policy_state": policy_state,
            "derisk_policy_version": str(derisk_policy_version or "") or None,
            "risk_ceiling_quantity": ceiling,
            "risk_ceiling_enforced": ceiling is not None,
            "quantity_authority": cls.authority,
            "quantity_authority_version": cls.authority_version,
            "quantity_semantics": "FINAL_TRANSACTIONAL_MODEL_PAPER_ALLOCATION",
            "execution_model_version": (execution_model or {}).get("execution_model_version"),
            "execution_model_contract_hash": (execution_model or {}).get("contract_hash"),
            "unqualified_model_or_alpha_reducers_used": False,
            "policy": "minimum of immutable initial-risk budget, portfolio heat, shared cash, intraday cap, concentration, sector, liquidity, optional production ceiling and canonical cost reserve",
        }

    @classmethod
    def _zero(cls, reason: str, *, entry: float | None = None, stop: float | None = None) -> Dict[str, Any]:
        return {
            "quantity": 0,
            "entry": entry,
            "stop": stop,
            "risk_per_share": None,
            "risk_budget": 0.0,
            "risk_cash": 0.0,
            "notional": 0.0,
            "constraints": {},
            "binding_constraint": "invalid_input",
            "blockers": [reason],
            "quantity_authority": cls.authority,
            "quantity_authority_version": cls.authority_version,
        }


DEFAULT_RISK_ADMISSION_AND_SIZING_AUTHORITY = RiskAdmissionAndSizingAuthority()
