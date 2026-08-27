"""Model Paper facade over the single canonical quantity/risk authority."""
from __future__ import annotations

from typing import Any, Dict

from core.india_cash_cost_service import IndiaCashCostService
from core.current_managed_risk_authority import DEFAULT_CURRENT_MANAGED_RISK_AUTHORITY
from core.risk_admission_and_sizing_authority import DEFAULT_RISK_ADMISSION_AND_SIZING_AUTHORITY


class ModelPortfolioRiskService:
    authority = DEFAULT_RISK_ADMISSION_AND_SIZING_AUTHORITY.authority
    authority_version = DEFAULT_RISK_ADMISSION_AND_SIZING_AUTHORITY.authority_version

    def __init__(
        self,
        *,
        equity: float = 500_000.0,
        intraday_cap: float = 100_000.0,
        risk_per_trade_pct: float = 1.0,
        max_symbol_pct: float = 15.0,
        max_sector_pct: float = 30.0,
        max_portfolio_heat_pct: float = 4.0,
        liquidity_participation_pct: float = 2.0,
        cost_service: IndiaCashCostService | None = None,
    ):
        self.equity = float(equity)
        self.intraday_cap = float(intraday_cap)
        self.risk_per_trade_pct = float(risk_per_trade_pct)
        self.max_symbol_pct = float(max_symbol_pct)
        self.max_sector_pct = float(max_sector_pct)
        self.max_portfolio_heat_pct = float(max_portfolio_heat_pct)
        self.liquidity_participation_pct = float(liquidity_participation_pct)
        self.cost_service = cost_service or IndiaCashCostService()

    def size(
        self,
        *,
        mode: str,
        side: str,
        exchange: str,
        bse_group: str | None,
        entry: float,
        stop: float,
        free_cash: float,
        intraday_used: float,
        symbol_used: float,
        sector_used: float = 0.0,
        open_risk: float,
        avg_daily_value: float | None = None,
        equity: float | None = None,
        risk_scale: float = 1.0,
        risk_ceiling_quantity: int | None = None,
        execution_model: Dict[str, Any] | None = None,
        risk_policy_approved: bool = False,
        risk_policy_version: str | None = None,
    ) -> Dict[str, Any]:
        effective_equity = float(self.equity if equity is None else equity)
        intraday_ratio = (self.intraday_cap / self.equity) if self.equity > 0 else 0.20
        dynamic_intraday_cap = max(0.0, effective_equity * intraday_ratio)
        result = DEFAULT_RISK_ADMISSION_AND_SIZING_AUTHORITY.allocate(
            mode=mode,
            side=side,
            exchange=exchange,
            bse_group=bse_group,
            entry=entry,
            stop=stop,
            equity=effective_equity,
            free_cash=free_cash,
            intraday_cap=dynamic_intraday_cap,
            intraday_used=intraday_used,
            risk_per_trade_pct=self.risk_per_trade_pct,
            max_symbol_pct=self.max_symbol_pct,
            symbol_used=symbol_used,
            max_sector_pct=self.max_sector_pct,
            sector_used=sector_used,
            max_portfolio_heat_pct=self.max_portfolio_heat_pct,
            open_risk=open_risk,
            liquidity_participation_pct=self.liquidity_participation_pct,
            avg_daily_value=avg_daily_value,
            risk_ceiling_quantity=risk_ceiling_quantity,
            approved_derisk_multiplier=risk_scale,
            derisk_policy_approved=risk_policy_approved,
            derisk_policy_version=risk_policy_version,
            execution_model=execution_model,
            cost_service=self.cost_service,
        )
        result.update({
            "portfolio_heat_measure": DEFAULT_CURRENT_MANAGED_RISK_AUTHORITY.admission_heat_measure,
            "managed_risk_usage": DEFAULT_CURRENT_MANAGED_RISK_AUTHORITY.managed_risk_usage,
            "managed_risk_not_used_to_increase_quantity": True,
        })
        return result
