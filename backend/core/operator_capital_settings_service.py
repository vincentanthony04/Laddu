"""Governed operator capital settings for future Model Paper admissions.

The settings are persisted in PostgreSQL through Store's production KV
repository. They affect only future sizing/admission. Existing positions and
historical outcomes are never rewritten or resized.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict


class OperatorCapitalSettingsService:
    KEY = "operator_capital_settings:v1"
    VERSION = "operator-capital-settings-1.0.0"

    def __init__(self, store: Any, *, default_wallet: float = 500_000.0, default_intraday_cap: float = 100_000.0):
        self.store = store
        self.default_wallet = float(default_wallet)
        self.default_intraday_cap = float(default_intraday_cap)

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

    def _defaults(self) -> Dict[str, Any]:
        return {
            "version": self.VERSION,
            "model_wallet": self.default_wallet,
            "intraday_exposure_ceiling": min(self.default_intraday_cap, self.default_wallet),
            "effective_at": None,
            "updated_at": None,
            "updated_by": "release_default",
            "applies_to": "future_model_paper_admissions_only",
            "open_positions_resized": False,
            "broker_authority": "NONE",
        }

    def read(self) -> Dict[str, Any]:
        value = self.store.get_kv(self.KEY, {}) or {}
        out = self._defaults()
        if isinstance(value, dict):
            out.update(value)
        try:
            wallet = float(out.get("model_wallet"))
            cap = float(out.get("intraday_exposure_ceiling"))
        except (TypeError, ValueError):
            return self._defaults()
        if wallet <= 0 or cap < 0 or cap > wallet:
            return self._defaults()
        out["model_wallet"] = round(wallet, 2)
        out["intraday_exposure_ceiling"] = round(cap, 2)
        out["ok"] = True
        out["editable"] = True
        out["validation"] = {
            "wallet_positive": wallet > 0,
            "intraday_non_negative": cap >= 0,
            "intraday_within_wallet": cap <= wallet,
        }
        return out

    def update(self, *, model_wallet: float, intraday_exposure_ceiling: float, actor: str = "operator") -> Dict[str, Any]:
        wallet = float(model_wallet)
        cap = float(intraday_exposure_ceiling)
        if wallet < 10_000:
            raise ValueError("model_wallet must be at least INR 10,000")
        if wallet > 1_000_000_000:
            raise ValueError("model_wallet exceeds the governed maximum")
        if cap < 0:
            raise ValueError("intraday_exposure_ceiling cannot be negative")
        if cap > wallet:
            raise ValueError("intraday_exposure_ceiling cannot exceed model_wallet")
        now = self._now()
        previous = self.read()
        payload = {
            "version": self.VERSION,
            "model_wallet": round(wallet, 2),
            "intraday_exposure_ceiling": round(cap, 2),
            "effective_at": now,
            "updated_at": now,
            "updated_by": str(actor or "operator")[:120],
            "applies_to": "future_model_paper_admissions_only",
            "open_positions_resized": False,
            "broker_authority": "NONE",
            "previous": {
                "model_wallet": previous.get("model_wallet"),
                "intraday_exposure_ceiling": previous.get("intraday_exposure_ceiling"),
                "effective_at": previous.get("effective_at"),
            },
        }
        self.store.set_kv(self.KEY, payload)
        return self.read()
