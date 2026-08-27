"""Canonical broker/account charge snapshot authority for Upstox cash orders.

The public tariff is only an estimate.  Exact hypothetical order charges are
accepted only from an authenticated Upstox Brokerage Details API response whose
request identity exactly matches the order being costed.  The broker response's
DP-plan minimum is preserved separately because Upstox documents it as a daily
minimum per scrip on sales and excludes it from the order charge total.

This module performs no HTTP.  It validates and freezes evidence supplied by an
adapter so deterministic cost mathematics can be tested without network access.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
from typing import Any, Mapping

from core.numeric_semantics import finite_number


class BrokerChargeSnapshotAuthority:
    authority = "BrokerChargeSnapshotAuthority"
    authority_version = "1.1.0-upstox-brokerage-details-canonical-finite"
    source = "UPSTOX_BROKERAGE_DETAILS_API"
    endpoint = "/v2/charges/brokerage"
    parity_tolerance_rupees = 0.02

    @staticmethod
    def _finite(name: str, value: Any, *, allow_zero: bool = True) -> float:
        out = finite_number(value)
        if out is None or out < 0 or (not allow_zero and out == 0):
            raise ValueError(f"{name} must be finite and {'non-negative' if allow_zero else 'positive'}")
        return out

    @classmethod
    def _quantity(cls, value: Any) -> int:
        out = finite_number(value)
        if out is None or not float(out).is_integer() or int(out) <= 0:
            raise ValueError("quantity must be a positive integer")
        return int(out)

    @staticmethod
    def _observed_at(value: Any) -> str:
        if isinstance(value, datetime):
            dt = value
        else:
            try:
                dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            except Exception as exc:
                raise ValueError("observed_at must be an offset-aware timestamp") from exc
        if dt.tzinfo is None:
            raise ValueError("observed_at must be timezone-aware")
        return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    @staticmethod
    def _canon_hash(value: Mapping[str, Any]) -> str:
        raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @classmethod
    def normalize_request(cls, request: Mapping[str, Any]) -> dict[str, Any]:
        req = dict(request or {})
        token = str(req.get("instrument_token") or req.get("instrument_key") or "").strip()
        if not token or "|" not in token:
            raise ValueError("instrument_token is required")
        qty = cls._quantity(req.get("quantity"))
        product = str(req.get("product") or "").upper().strip()
        if product not in {"D", "I"}:
            raise ValueError("cash product must be D or I")
        side = str(req.get("transaction_type") or req.get("side") or "").upper().strip()
        if side not in {"BUY", "SELL"}:
            raise ValueError("transaction_type must be BUY or SELL")
        price = cls._finite("price", req.get("price"), allow_zero=False)
        return {
            "instrument_token": token,
            "quantity": qty,
            "product": product,
            "transaction_type": side,
            "price": round(price, 8),
        }

    @classmethod
    def _extract_dp_plan(cls, charges: Mapping[str, Any]) -> dict[str, Any]:
        snake = charges.get("dp_plan")
        camel = charges.get("dpPlan")
        if snake is not None and not isinstance(snake, Mapping):
            raise ValueError("dp_plan must be an object")
        if camel is not None and not isinstance(camel, Mapping):
            raise ValueError("dpPlan must be an object")
        if isinstance(snake, Mapping) and isinstance(camel, Mapping):
            s_name, c_name = str(snake.get("name") or ""), str(camel.get("name") or "")
            s_min = cls._finite("dp_plan.min_expense", snake.get("min_expense"))
            c_min = cls._finite("dpPlan.min_expense", camel.get("min_expense"))
            if s_name != c_name or abs(s_min - c_min) > 1e-9:
                raise ValueError("duplicated dp_plan/dpPlan fields disagree")
            plan = snake
        else:
            plan = snake if isinstance(snake, Mapping) else camel if isinstance(camel, Mapping) else {}
        name = str(plan.get("name") or "").strip() or None
        min_expense = cls._finite("dp_plan.min_expense", plan.get("min_expense") or 0.0)
        return {"name": name, "min_expense": round(min_expense, 2)}

    @classmethod
    def normalize(
        cls,
        *,
        request: Mapping[str, Any],
        response: Mapping[str, Any],
        observed_at: Any,
        account_id_hash: str | None = None,
    ) -> dict[str, Any]:
        req = cls.normalize_request(request)
        raw = dict(response or {})
        if str(raw.get("status") or "").lower() != "success":
            raise ValueError("brokerage-details response is not successful")
        data = raw.get("data")
        if not isinstance(data, Mapping):
            raise ValueError("brokerage-details data object missing")
        charges = data.get("charges")
        if not isinstance(charges, Mapping):
            raise ValueError("brokerage-details charges object missing")
        taxes = charges.get("taxes")
        other = charges.get("other_charges")
        other_alias = charges.get("otherTaxes")
        if not isinstance(taxes, Mapping) or not isinstance(other, Mapping):
            raise ValueError("brokerage-details taxes/other_charges objects missing")
        if isinstance(other_alias, Mapping):
            for key in ("transaction", "clearing", "ipft", "sebi_turnover"):
                a = cls._finite(f"other_charges.{key}", other.get(key) or 0.0)
                b = cls._finite(f"otherTaxes.{key}", other_alias.get(key) or 0.0)
                if abs(a - b) > 1e-9:
                    raise ValueError("duplicated other_charges/otherTaxes fields disagree")

        components = {
            "brokerage": cls._finite("brokerage", charges.get("brokerage") or 0.0),
            "gst": cls._finite("gst", taxes.get("gst") or 0.0),
            "stt": cls._finite("stt", taxes.get("stt") or 0.0),
            "stamp_duty": cls._finite("stamp_duty", taxes.get("stamp_duty") or 0.0),
            "exchange_transaction": cls._finite("transaction", other.get("transaction") or 0.0),
            "clearing": cls._finite("clearing", other.get("clearing") or 0.0),
            "ipft": cls._finite("ipft", other.get("ipft") or 0.0),
            "sebi_fee": cls._finite("sebi_turnover", other.get("sebi_turnover") or 0.0),
            "other_costs": cls._finite("others", other.get("others") or 0.0),
        }
        total = cls._finite("total", charges.get("total"))
        component_total = sum(components.values())
        # Broker responses are rupee values and may be rounded.  More than two
        # paise of unexplained drift is not exact evidence.
        if abs(total - component_total) > cls.parity_tolerance_rupees + 1e-12:
            raise ValueError(
                f"broker charge component parity failed: total={total:.4f} components={component_total:.4f}"
            )
        dp_plan = cls._extract_dp_plan(charges)
        observed = cls._observed_at(observed_at)
        frozen = {
            "source": cls.source,
            "endpoint": cls.endpoint,
            "request": req,
            "components": {k: round(v, 2) for k, v in components.items()},
            "order_total": round(total, 2),
            "component_total": round(component_total, 2),
            "dp_plan": dp_plan,
            "observed_at": observed,
            "account_id_hash": str(account_id_hash or "").strip() or None,
        }
        snapshot_hash = cls._canon_hash(frozen)
        return {
            "authority": cls.authority,
            "authority_version": cls.authority_version,
            "state": "EXACT_BROKER_SNAPSHOT",
            "decision_usable": True,
            **frozen,
            "snapshot_hash": snapshot_hash,
            "dp_plan_semantics": "daily minimum per scrip on sales; excluded from order_total",
        }

    @classmethod
    def require_match(
        cls,
        snapshot: Mapping[str, Any],
        *,
        instrument_token: str,
        quantity: int,
        product: str,
        transaction_type: str,
        price: float,
    ) -> dict[str, Any]:
        snap = dict(snapshot or {})
        if snap.get("authority") != cls.authority or snap.get("state") != "EXACT_BROKER_SNAPSHOT":
            raise ValueError("exact broker charge snapshot required")
        expected = cls.normalize_request({
            "instrument_token": instrument_token,
            "quantity": quantity,
            "product": product,
            "transaction_type": transaction_type,
            "price": price,
        })
        actual = cls.normalize_request(snap.get("request") or {})
        if actual != expected:
            raise ValueError("broker charge snapshot request identity does not match order")
        frozen = {k: snap.get(k) for k in (
            "source", "endpoint", "request", "components", "order_total", "component_total",
            "dp_plan", "observed_at", "account_id_hash",
        )}
        if cls._canon_hash(frozen) != str(snap.get("snapshot_hash") or ""):
            raise ValueError("broker charge snapshot hash mismatch")
        return snap


DEFAULT_BROKER_CHARGE_SNAPSHOT_AUTHORITY = BrokerChargeSnapshotAuthority()
