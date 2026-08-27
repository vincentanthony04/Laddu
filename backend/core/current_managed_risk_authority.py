"""Canonical initial-versus-managed Model Paper risk attribution.

The initial risk accepted at admission is immutable and remains the only risk
measure used for portfolio-heat admission.  Managed stops may only reduce that
risk.  Any stop beyond entry secures profit, which is reported separately and
must never be used to lever the portfolio up automatically.
"""
from __future__ import annotations

import math
from typing import Any, Mapping, Sequence


class CurrentManagedRiskAuthority:
    authority = "CurrentManagedRiskAuthority"
    authority_version = "1.0.0"
    policy_version = "model-paper-managed-risk-1.0.0"
    admission_heat_measure = "INITIAL_OPEN_RISK"
    managed_risk_usage = "ANALYTICS_ONLY_NO_AUTOMATIC_CAPACITY_RELEASE"

    @staticmethod
    def _number(value: Any) -> float | None:
        try:
            out = float(value)
            return out if math.isfinite(out) else None
        except (TypeError, ValueError):
            return None

    @classmethod
    def measure(cls, record: Mapping[str, Any]) -> dict[str, Any]:
        row = dict(record or {})
        side = str(row.get("side") or "").upper()
        entry = cls._number(row.get("entry_price") if row.get("entry_price") is not None else row.get("original_entry"))
        original_stop = cls._number(row.get("original_stop"))
        managed_stop = cls._number(row.get("managed_stop"))
        quantity = cls._number(row.get("quantity"))
        persisted_initial = cls._number(row.get("open_risk"))

        complete = side in {"LONG", "SHORT"} and None not in {entry, original_stop, managed_stop, quantity}
        if not complete or float(quantity) < 0:
            return {
                "state": "MISSING",
                "initial_open_risk": round(persisted_initial, 2) if persisted_initial is not None else None,
                "current_managed_risk": None,
                "secured_profit": None,
                "released_risk": None,
                "risk_widened": None,
                "authority": cls.authority,
                "authority_version": cls.authority_version,
                "policy_version": cls.policy_version,
                "admission_heat_measure": cls.admission_heat_measure,
                "managed_risk_usage": cls.managed_risk_usage,
            }

        entry = float(entry)
        original_stop = float(original_stop)
        managed_stop = float(managed_stop)
        quantity = float(quantity)
        calculated_initial = (
            max(0.0, entry - original_stop) * quantity
            if side == "LONG"
            else max(0.0, original_stop - entry) * quantity
        )
        initial = calculated_initial if persisted_initial is None else max(0.0, float(persisted_initial))

        if side == "LONG":
            widened = managed_stop < original_stop - 1e-9
            current = max(0.0, entry - managed_stop) * quantity
            secured = max(0.0, managed_stop - entry) * quantity
        else:
            widened = managed_stop > original_stop + 1e-9
            current = max(0.0, managed_stop - entry) * quantity
            secured = max(0.0, entry - managed_stop) * quantity

        # A managed stop must never make current downside exceed admitted risk.
        widened = bool(widened or current > initial + 1e-6)
        if widened:
            state = "RISK_WIDENED_BLOCKED"
        elif secured > 1e-9:
            state = "PROFIT_SECURED"
        elif current <= 1e-9:
            state = "BREAKEVEN_PROTECTED"
        elif current < initial - 1e-6:
            state = "RISK_REDUCED"
        else:
            state = "ORIGINAL_RISK"

        current_capped = min(initial, current) if not widened else current
        return {
            "state": state,
            "initial_open_risk": round(initial, 2),
            "calculated_initial_risk": round(calculated_initial, 2),
            "current_managed_risk": round(current_capped, 2),
            "secured_profit": round(secured, 2),
            "released_risk": round(max(0.0, initial - current_capped), 2),
            "risk_widened": widened,
            "side": side,
            "entry_price": round(entry, 6),
            "original_stop": round(original_stop, 6),
            "managed_stop": round(managed_stop, 6),
            "quantity": int(quantity) if quantity.is_integer() else quantity,
            "authority": cls.authority,
            "authority_version": cls.authority_version,
            "policy_version": cls.policy_version,
            "admission_heat_measure": cls.admission_heat_measure,
            "managed_risk_usage": cls.managed_risk_usage,
        }

    @classmethod
    def require_non_widening(cls, record: Mapping[str, Any]) -> dict[str, Any]:
        measured = cls.measure(record)
        if measured.get("state") == "MISSING":
            raise ValueError("managed risk inputs are incomplete")
        if measured.get("risk_widened"):
            raise ValueError("managed stop would widen accepted Model Paper risk")
        return measured

    @classmethod
    def portfolio(cls, rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        initial = 0.0
        current = 0.0
        secured = 0.0
        released = 0.0
        missing = 0
        widened = 0
        for row in rows or ():
            measured = cls.measure(row)
            if measured.get("state") == "MISSING":
                missing += 1
                initial += float(measured.get("initial_open_risk") or 0.0)
                # Fail conservative: unknown managed risk receives no capacity credit.
                current += float(measured.get("initial_open_risk") or 0.0)
                continue
            if measured.get("risk_widened"):
                widened += 1
            initial += float(measured.get("initial_open_risk") or 0.0)
            current += float(measured.get("current_managed_risk") or 0.0)
            secured += float(measured.get("secured_profit") or 0.0)
            released += float(measured.get("released_risk") or 0.0)
        return {
            "initial_open_risk": round(initial, 2),
            "current_managed_risk": round(current, 2),
            "secured_profit": round(secured, 2),
            "released_risk": round(released, 2),
            "missing_count": missing,
            "widened_count": widened,
            "admission_heat_measure": cls.admission_heat_measure,
            "managed_risk_usage": cls.managed_risk_usage,
            "authority": cls.authority,
            "authority_version": cls.authority_version,
            "policy_version": cls.policy_version,
        }


DEFAULT_CURRENT_MANAGED_RISK_AUTHORITY = CurrentManagedRiskAuthority()
