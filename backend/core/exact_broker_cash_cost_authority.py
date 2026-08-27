"""Exact broker/account statutory-cost authority for simulated cash orders.

This authority does not call the network.  It consumes hash-bound snapshots
created by :mod:`broker_charge_snapshot_authority` from Upstox's authenticated
Brokerage Details API and combines the entry/exit orders into one immutable
round-trip cost statement.

Execution slippage/impact/spread remain separate assumptions; broker/statutory
charges are never mixed with those estimates.  Delivery DP is a per-scrip/day
sale charge exposed by the broker snapshot as ``dp_plan.min_expense`` and is
added exactly once, with GST, outside the per-order total as documented by the
provider.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from typing import Any, Mapping

from core.broker_charge_snapshot_authority import BrokerChargeSnapshotAuthority
from core.numeric_semantics import finite_number


def _finite(name: str, value: Any, *, nonnegative: bool = True) -> float:
    out = finite_number(value)
    if out is None or (nonnegative and out < 0):
        raise ValueError(f"{name} must be finite{' and non-negative' if nonnegative else ''}")
    return out


def _quantity(value: Any) -> int:
    out = finite_number(value)
    if out is None or not float(out).is_integer() or int(out) <= 0:
        raise ValueError("quantity must be a positive integer")
    return int(out)


def _money(value: float) -> float:
    out = _finite("money", value)
    return round(out, 2)


def _hash(value: Mapping[str, Any]) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False, ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ExactBrokerCashCostAuthority:
    authority: str = "ExactBrokerCashCostAuthority"
    authority_version: str = "1.1.0-upstox-order-snapshot-roundtrip-canonical-finite"
    gst_rate: float = 0.18

    @classmethod
    def round_trip(
        cls,
        *,
        mode: str,
        position_side: str,
        instrument_token: str,
        quantity: int,
        entry_price: float,
        exit_price: float,
        entry_snapshot: Mapping[str, Any],
        exit_snapshot: Mapping[str, Any],
        spread_bps: float = 0.0,
        slippage_bps_each_side: float = 0.0,
        impact_bps_each_side: float = 0.0,
    ) -> dict[str, Any]:
        desk = str(mode or "").strip().lower()
        side = str(position_side or "").strip().upper()
        token = str(instrument_token or "").strip()
        qty = _quantity(quantity)
        ep = _finite("entry_price", entry_price)
        xp = _finite("exit_price", exit_price)
        if desk not in {"intraday", "delivery"}:
            raise ValueError("mode must be intraday or delivery")
        if side not in {"LONG", "SHORT"}:
            raise ValueError("position_side must be LONG or SHORT")
        if not token or "|" not in token or qty <= 0 or ep <= 0 or xp <= 0:
            raise ValueError("valid instrument token, quantity and positive prices are required")
        product = "I" if desk == "intraday" else "D"
        entry_txn, exit_txn = (("BUY", "SELL") if side == "LONG" else ("SELL", "BUY"))
        entry = BrokerChargeSnapshotAuthority.require_match(
            entry_snapshot,
            instrument_token=token,
            quantity=qty,
            product=product,
            transaction_type=entry_txn,
            price=ep,
        )
        exit_ = BrokerChargeSnapshotAuthority.require_match(
            exit_snapshot,
            instrument_token=token,
            quantity=qty,
            product=product,
            transaction_type=exit_txn,
            price=xp,
        )
        account_a = str(entry.get("account_id_hash") or "").strip() or None
        account_b = str(exit_.get("account_id_hash") or "").strip() or None
        if account_a != account_b:
            raise ValueError("entry and exit broker snapshots must belong to the same account identity")

        component_keys = (
            "brokerage", "gst", "stt", "stamp_duty", "exchange_transaction",
            "clearing", "ipft", "sebi_fee", "other_costs",
        )
        components = {
            key: _money(
                _finite(f"entry.{key}", (entry.get("components") or {}).get(key) or 0.0)
                + _finite(f"exit.{key}", (exit_.get("components") or {}).get(key) or 0.0)
            )
            for key in component_keys
        }

        # DP applies only to Delivery SELL, once per account+scrip+day. The
        # Brokerage Details order total explicitly excludes the DP-plan daily
        # minimum. Pick the SELL leg's plan and add its GST once.
        dp = 0.0
        dp_gst = 0.0
        dp_plan_name = None
        if desk == "delivery":
            sell_snap = entry if entry_txn == "SELL" else exit_
            plan = dict(sell_snap.get("dp_plan") or {})
            dp = _finite("dp_plan.min_expense", plan.get("min_expense") or 0.0)
            dp_plan_name = str(plan.get("name") or "").strip() or None
            dp_gst = dp * cls.gst_rate
            components["gst"] = _money(components["gst"] + dp_gst)
        components["dp_charge"] = _money(dp)

        statutory_total = _money(sum(components.values()))
        entry_turnover = ep * qty
        exit_turnover = xp * qty
        slip = _finite("slippage_bps_each_side", slippage_bps_each_side)
        impact = _finite("impact_bps_each_side", impact_bps_each_side)
        spread = _finite("spread_bps", spread_bps)
        execution = {
            "slippage": _money((entry_turnover + exit_turnover) * slip / 10_000.0),
            "impact": _money((entry_turnover + exit_turnover) * impact / 10_000.0),
            # Existing Laddu convention reserves one round-trip spread against
            # entry notional. Keep it explicit/versioned rather than burying it.
            "spread": _money(entry_turnover * spread / 10_000.0),
        }
        execution_total = _money(sum(execution.values()))
        total = _money(statutory_total + execution_total)
        gross = ((xp - ep) if side == "LONG" else (ep - xp)) * qty
        frozen = {
            "authority": cls.authority,
            "authority_version": cls.authority_version,
            "cost_precision": "EXACT_BROKER_STATUTORY_PLUS_VERSIONED_EXECUTION_RESERVE",
            "exact_broker_cost_evidence": True,
            "decision_usable_for_exact_pnl": True,
            "mode": desk,
            "side": side,
            "instrument_token": token,
            "quantity": qty,
            "entry_price": ep,
            "exit_price": xp,
            "entry_snapshot_hash": str(entry.get("snapshot_hash") or ""),
            "exit_snapshot_hash": str(exit_.get("snapshot_hash") or ""),
            "account_id_hash": account_a,
            "dp_plan": {"name": dp_plan_name, "min_expense": _money(dp)},
            "dp_gst": _money(dp_gst),
            "broker_statutory_costs": components,
            "broker_statutory_total": statutory_total,
            "execution_reserve": execution,
            "execution_reserve_total": execution_total,
            "total_cost": total,
            "gross_pnl": round(gross, 2),
            "net_pnl": round(gross - total, 2),
        }
        frozen["cost_statement_hash"] = _hash(frozen)
        return frozen


DEFAULT_EXACT_BROKER_CASH_COST_AUTHORITY = ExactBrokerCashCostAuthority()
