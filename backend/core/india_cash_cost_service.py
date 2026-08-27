"""Effective-dated Indian cash-equity transaction-cost authority.

The model is deliberately explicit: every levy is stored as a separate
component, simulated fills reserve slippage/impact, and income tax is excluded
because it is an investor-level liability rather than an executable-trade cost.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime
import math
from typing import Any, Dict, Mapping

from core.execution_slippage_calibration_authority import DEFAULT_EXECUTION_SLIPPAGE_CALIBRATION_AUTHORITY
from core.numeric_semantics import finite_number


@dataclass(frozen=True)
class IndiaCashCostSchedule:
    # Official Upstox/NSE cash tariff effective 1 March 2026.  The schedule
    # version identifies broker/statutory charges only; slippage/impact have a
    # separate execution-assumption version below.
    version: str = "upstox-nse-cash-2026-03-01-v1"
    effective_from: str = "2026-03-01"
    tariff_source: str = "https://upstox.com/brokerage-charges/"
    tariff_precision: str = "REFERENCE_PUBLIC_TARIFF"
    account_plan: str = "UPSTOX_BASIC_REFERENCE"
    brokerage_cap: float = 20.0
    intraday_brokerage_rate: float = 0.001
    # Public pricing is a reference only. Exact account/order charges must be
    # supplied through BrokerChargeSnapshotAuthority. Delivery is represented
    # as the documented Basic-plan cap/rate pair rather than an unconditional
    # flat charge, so low-turnover orders cannot be overstated by construction.
    delivery_brokerage_rate: float = 0.025
    delivery_brokerage_flat: float = 0.0
    delivery_stt_rate: float = 0.001
    intraday_sell_stt_rate: float = 0.00025
    nse_transaction_rate: float = 0.0000307
    nse_ipft_rate: float = 0.000000001
    # BSE cash transaction fees are scrip-group dependent.  These rates are
    # per rupee of turnover and are intentionally explicit instead of
    # pretending BSE shares the NSE flat rate.  Unknown groups fail closed.
    # A/B: Rs375/crore; X/XT/Z/ZP/XC/XD: Rs10,000/crore;
    # T/SS/ST: Rs1,00,000/crore.
    bse_standard_transaction_rate: float = 0.0000375
    bse_x_transaction_rate: float = 0.001
    bse_high_transaction_rate: float = 0.01
    bse_ipft_rate: float = 0.000000001
    sebi_rate: float = 0.000001
    delivery_buy_stamp_rate: float = 0.00015
    intraday_buy_stamp_rate: float = 0.00003
    gst_rate: float = 0.18
    delivery_dp_sell: float = 20.0
    intraday_slippage_bps: float = 3.0
    delivery_slippage_bps: float = 5.0
    execution_assumption_version: str = "cash-execution-reserve-v1.0.0"


def _money(value: float) -> float:
    out = finite_number(value)
    if out is None or out < 0:
        raise ValueError("money component must be finite and non-negative")
    return round(out, 2)


class IndiaCashCostService:
    authority = "IndiaCashCostAuthority"
    authority_version = "1.6.0-canonical-finite-date-and-spread-contract"
    def __init__(self, schedule: IndiaCashCostSchedule | None = None, impact_bps: float = 0.0):
        self.schedule = schedule or IndiaCashCostSchedule()
        value = finite_number(impact_bps)
        if value is None or value < 0:
            raise ValueError("impact_bps must be finite and non-negative")
        self.impact_bps = value

    @staticmethod
    def _quantity(value: Any) -> int:
        out = finite_number(value)
        if out is None or not float(out).is_integer() or int(out) < 1:
            raise ValueError("quantity must be a positive integer")
        return int(out)

    @staticmethod
    def _nonnegative(name: str, value: Any) -> float:
        out = finite_number(value)
        if out is None or out < 0:
            raise ValueError(f"{name} must be finite and non-negative")
        return out

    @staticmethod
    def _trading_date(value: date | str | None) -> str | None:
        if value is None:
            return None
        if isinstance(value, bool):
            raise ValueError("traded_on must be a valid ISO trading date")
        if isinstance(value, datetime):
            # A timestamp is accepted only through its explicit calendar date.
            value = value.date()
        if isinstance(value, date):
            return value.isoformat()
        text = str(value).strip()
        try:
            parsed = date.fromisoformat(text)
        except Exception as exc:
            raise ValueError("traded_on must be a valid ISO trading date") from exc
        return parsed.isoformat()

    def schedule_for(self, traded_on: date | str | None = None) -> IndiaCashCostSchedule:
        value = self._trading_date(traded_on)
        if value is not None and value < self.schedule.effective_from:
            raise ValueError(f"no governed cash-cost schedule for {value}")
        return self.schedule

    def _brokerage(self, mode: str, turnover: float) -> float:
        cfg = self.schedule
        if mode == "intraday":
            return min(cfg.brokerage_cap, turnover * cfg.intraday_brokerage_rate)
        return min(cfg.brokerage_cap, cfg.delivery_brokerage_flat + turnover * cfg.delivery_brokerage_rate)

    def _exchange_rates(self, exchange: str, bse_group: str | None) -> tuple[float, float, str]:
        cfg = self.schedule
        venue = str(exchange or "").strip().upper()
        if venue == "NSE":
            if str(bse_group or "").strip():
                raise ValueError("NSE cash cost requires bse_group=None")
            return cfg.nse_transaction_rate, cfg.nse_ipft_rate, "NSE"
        if venue != "BSE":
            raise ValueError("exchange must be NSE or BSE")
        group = str(bse_group or "").strip().upper()
        if not group:
            raise ValueError("BSE cash cost requires scrip group; cost authority fails closed without it")
        if group in {"A", "B"}:
            return cfg.bse_standard_transaction_rate, cfg.bse_ipft_rate, group
        if group in {"X", "XT", "Z", "ZP", "XC", "XD"}:
            return cfg.bse_x_transaction_rate, cfg.bse_ipft_rate, group
        if group in {"T", "SS", "ST"}:
            return cfg.bse_high_transaction_rate, cfg.bse_ipft_rate, group
        # Group E has an exclusive/non-exclusive rate distinction and other
        # groups may change independently.  Do not guess.
        raise ValueError(f"no governed BSE cash transaction-charge schedule for group {group}")

    def venue_identity(self, *, exchange: str, bse_group: str | None) -> Dict[str, str | None]:
        """Validate and normalize the immutable venue cost identity."""
        _transaction_rate, _ipft_rate, resolved_group = self._exchange_rates(exchange, bse_group)
        venue = str(exchange).strip().upper()
        return {
            "exchange": venue,
            "bse_group": resolved_group if venue == "BSE" else None,
        }

    def side_cost(
        self,
        mode: str,
        transaction_side: str,
        price: float,
        quantity: int,
        *,
        exchange: str,
        bse_group: str | None,
        traded_on: date | str | None = None,
        impact_bps: float | None = None,
        slippage_bps: float | None = None,
    ) -> Dict[str, float]:
        cfg = self.schedule_for(traded_on)
        mode = str(mode or "").lower()
        side = str(transaction_side or "").upper()
        if mode not in {"intraday", "delivery"}:
            raise ValueError("mode must be intraday or delivery")
        if side not in {"BUY", "SELL"}:
            raise ValueError("transaction_side must be BUY or SELL")
        qty = self._quantity(quantity)
        px = finite_number(price)
        if px is None or px <= 0:
            raise ValueError("positive finite price is required")
        turnover = px * qty
        brokerage = self._brokerage(mode, turnover)
        stt = turnover * (
            cfg.delivery_stt_rate
            if mode == "delivery"
            else cfg.intraday_sell_stt_rate if side == "SELL" else 0.0
        )
        transaction_rate, ipft_rate, exchange_group = self._exchange_rates(exchange, bse_group)
        exchange_fee = turnover * transaction_rate
        ipft = turnover * ipft_rate
        sebi = turnover * cfg.sebi_rate
        stamp = turnover * (
            cfg.delivery_buy_stamp_rate if mode == "delivery" else cfg.intraday_buy_stamp_rate
        ) if side == "BUY" else 0.0
        # DP is a per-scrip/per-trading-day delivery-sale charge, not a
        # per-execution levy. The per-execution kernel therefore owns no DP.
        # DeliveryDpDailyChargeAuthority / round_trip adds the single daily DP
        # amount and its GST exactly once at the aggregation boundary.
        dp = 0.0
        # GST on this execution covers execution-scoped components only.
        gst = (brokerage + exchange_fee + ipft) * cfg.gst_rate
        default_slip_bps = cfg.intraday_slippage_bps if mode == "intraday" else cfg.delivery_slippage_bps
        slip_bps = self._nonnegative("slippage_bps", default_slip_bps if slippage_bps is None else slippage_bps)
        impact_rate_bps = self._nonnegative("impact_bps", self.impact_bps if impact_bps is None else impact_bps)
        slippage = turnover * slip_bps / 10_000.0
        impact = turnover * impact_rate_bps / 10_000.0
        values = {
            "brokerage": brokerage,
            "stt": stt,
            "exchange_transaction": exchange_fee,
            "ipft": ipft,
            "sebi_fee": sebi,
            "stamp_duty": stamp,
            "gst": gst,
            "dp_charge": dp,
            "slippage": slippage,
            "impact": impact,
        }
        values = {key: _money(value) for key, value in values.items()}
        values["total"] = _money(sum(values.values()))
        return values

    def aggregate_delivery_sales(
        self,
        executions: list[Mapping[str, Any]],
        *,
        instrument_key: str,
        traded_on: date | str,
        exchange: str,
        bse_group: str | None,
        dp_plan_min_expense: float | None = None,
        account_plan: str | None = None,
    ) -> Dict[str, Any]:
        """Aggregate same-scrip/day Delivery SELL executions exactly once for DP.

        The caller must group by ``(traded_on, instrument_key, account)``. This
        method deliberately rejects mixed instruments/dates at the API boundary
        rather than relying on call order or mutable process state.
        """
        key = str(instrument_key or "").strip()
        day = self._trading_date(traded_on)
        if not key or day is None:
            raise ValueError("instrument_key and trading date are required for daily DP aggregation")
        rows = [dict(row or {}) for row in executions]
        if not rows:
            raise ValueError("at least one delivery SELL execution is required")
        summed = {name: 0.0 for name in (
            "brokerage", "stt", "exchange_transaction", "ipft", "sebi_fee",
            "stamp_duty", "gst", "dp_charge", "slippage", "impact",
        )}
        total_qty = 0
        total_turnover = 0.0
        for row in rows:
            row_key = str(row.get("instrument_key") or key).strip()
            row_day = self._trading_date(row.get("traded_on") if row.get("traded_on") is not None else day)
            if row_key != key or row_day != day:
                raise ValueError("daily DP aggregation cannot mix instrument/date identities")
            qty = self._quantity(row.get("quantity"))
            px = finite_number(row.get("price"))
            if px is None or px <= 0:
                raise ValueError("positive finite execution price is required")
            cost = self.side_cost(
                "delivery", "SELL", px, qty,
                exchange=exchange, bse_group=bse_group, traded_on=day,
                impact_bps=row.get("impact_bps"), slippage_bps=row.get("slippage_bps"),
            )
            for name in summed:
                summed[name] += float(cost.get(name) or 0.0)
            total_qty += qty
            total_turnover += px * qty
        cfg = self.schedule_for(day)
        dp = cfg.delivery_dp_sell if dp_plan_min_expense is None else self._nonnegative("dp_plan_min_expense", dp_plan_min_expense)
        summed = {name: _money(value) for name, value in summed.items()}
        summed["dp_charge"] = _money(dp)
        summed["gst"] = _money(summed["gst"] + dp * cfg.gst_rate)
        summed["total"] = _money(sum(summed.values()))
        return {
            "authority": "DeliveryDpDailyChargeAuthority",
            "authority_version": "1.0.0-grouped-scrip-day",
            "tariff_schedule_version": cfg.version,
            "traded_on": day,
            "instrument_key": key,
            "account_plan": str(account_plan or "REFERENCE_SCHEDULE"),
            "dp_plan_min_expense": _money(dp),
            "execution_count": len(rows),
            "quantity": total_qty,
            "turnover": round(total_turnover, 2),
            "costs": summed,
            "grouping_policy": "exactly one DP charge per account+scrip+trading-day sales group",
        }

    def round_trip(
        self,
        mode: str,
        position_side: str,
        entry_price: float,
        exit_price: float,
        quantity: int,
        *,
        exchange: str,
        bse_group: str | None,
        traded_on: date | str | None = None,
        impact_bps: float | None = None,
        slippage_bps: float | None = None,
        spread_bps: float = 0.0,
        execution_model: Mapping[str, Any] | None = None,
    ) -> Dict[str, Any]:
        side = str(position_side or "").upper()
        if side not in {"LONG", "SHORT"}:
            raise ValueError("position_side must be LONG or SHORT")
        model = dict(execution_model or {})
        if model:
            validated = DEFAULT_EXECUTION_SLIPPAGE_CALIBRATION_AUTHORITY.validate_contract(model, mode=mode)
            if validated is None:
                raise ValueError("invalid or mutated execution-model contract")
            model = validated
            impact_bps = float(model.get("impact_bps_each_side") or 0.0)
            slippage_bps = float(model.get("slippage_bps_each_side") or 0.0)
            spread_bps = float(model.get("spread_bps_round_trip") or 0.0)
        entry_txn, exit_txn = ("BUY", "SELL") if side == "LONG" else ("SELL", "BUY")
        entry = self.side_cost(mode, entry_txn, entry_price, quantity, traded_on=traded_on, impact_bps=impact_bps, slippage_bps=slippage_bps, exchange=exchange, bse_group=bse_group)
        exit_ = self.side_cost(mode, exit_txn, exit_price, quantity, traded_on=traded_on, impact_bps=impact_bps, slippage_bps=slippage_bps, exchange=exchange, bse_group=bse_group)
        components = {
            key: _money(entry[key] + exit_[key])
            for key in (
                "brokerage",
                "stt",
                "exchange_transaction",
                "ipft",
                "sebi_fee",
                "stamp_duty",
                "gst",
                "dp_charge",
                "slippage",
                "impact",
            )
        }
        if str(mode or "").lower() == "delivery":
            daily_dp = _money(self.schedule_for(traded_on).delivery_dp_sell)
            components["dp_charge"] = daily_dp
            components["gst"] = _money(components["gst"] + daily_dp * self.schedule_for(traded_on).gst_rate)
        # One round-trip spread reserve, matching prospective admission math.
        entry_px = finite_number(entry_price)
        exit_px = finite_number(exit_price)
        qty = self._quantity(quantity)
        spread = self._nonnegative("spread_bps", spread_bps)
        if entry_px is None or entry_px <= 0 or exit_px is None or exit_px <= 0:
            raise ValueError("positive finite entry/exit prices are required")
        components["spread"] = _money(entry_px * qty * spread / 10_000.0)
        statutory_keys = ("brokerage", "stt", "exchange_transaction", "ipft", "sebi_fee", "stamp_duty", "gst", "dp_charge")
        execution_keys = ("slippage", "impact", "spread")
        statutory_total = _money(sum(components[key] for key in statutory_keys))
        execution_total = _money(sum(components[key] for key in execution_keys))
        components["statutory_total"] = statutory_total
        components["execution_reserve_total"] = execution_total
        components["total"] = _money(statutory_total + execution_total)
        gross = (
            (exit_px - entry_px)
            if side == "LONG"
            else (entry_px - exit_px)
        ) * qty
        return {
            "cost_authority": self.authority,
            "cost_authority_version": self.authority_version,
            "cost_precision": "REFERENCE_ESTIMATE",
            "exact_broker_cost_evidence": False,
            "decision_usable_for_exact_pnl": False,
            "tariff_precision": self.schedule_for(traded_on).tariff_precision,
            "account_plan": self.schedule_for(traded_on).account_plan,
            "tariff_schedule_version": self.schedule_for(traded_on).version,
            "execution_assumption_version": self.schedule_for(traded_on).execution_assumption_version,
            "execution_model_version": model.get("execution_model_version") if model else None,
            "execution_model_contract_hash": model.get("contract_hash") if model else None,
            "execution_calibration_state": model.get("calibration_state") if model else None,
            "execution_calibration_snapshot_hash": model.get("calibration_snapshot_hash") if model else None,
            "schedule": asdict(self.schedule_for(traded_on)),
            "mode": str(mode).lower(),
            "exchange": str(exchange).strip().upper(),
            "bse_group": str(bse_group or "").upper() or None,
            "side": side,
            "quantity": qty,
            "entry_price": entry_px,
            "exit_price": exit_px,
            "gross_pnl": round(gross, 2),
            "costs": components,
            "net_pnl": round(gross - components["total"], 2),
            "excluded": ["income tax"],
            "exclusion_reason": "investor-level liability; not allocated per simulated trade",
        }

    def reserve(
        self, mode: str, position_side: str, reference_price: float, quantity: int,
        *, exchange: str, bse_group: str | None, execution_model: Mapping[str, Any] | None = None,
    ) -> float:
        """Conservative same-price round-trip reserve used before admission."""
        return self.round_trip(
            mode, position_side, reference_price, reference_price, quantity,
            exchange=exchange, bse_group=bse_group, execution_model=execution_model,
        )["costs"]["total"]
