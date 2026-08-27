"""Deterministic, cache-only proof of the binding Delivery MTF contract."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from delivery_timeframes import delivery_timeframe_context
from core.master_candle_service import MASTER_CANDLE_VERSION, evaluate_master_candle

SERVICE_VERSION = "binding-mtf-contract-1.0.0"
REQUIRED_TIMEFRAMES = ("30m", "1H", "4H", "1D", "1W", "1M")


def _bar(timestamp: str, open_: float, high: float, low: float, close: float) -> dict[str, Any]:
    return {
        "timestamp": timestamp,
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "volume": 1000,
        "is_closed": True,
        "forming": False,
        "session_partial": False,
        "pattern_eligible": True,
        "source": "installed_contract_self_test",
    }


class BindingMtfContractService:
    """Prove the deployed semantic wiring without network or database access."""

    @staticmethod
    def status() -> dict[str, Any]:
        weekly = [
            _bar("2026-01-05T00:00:00+05:30", 100, 120, 90, 110),
            _bar("2026-01-12T00:00:00+05:30", 108, 118, 94, 112),
            _bar("2026-01-19T00:00:00+05:30", 112, 117, 96, 115),
            _bar("2026-01-26T00:00:00+05:30", 116, 126, 114, 124),
            _bar("2026-02-02T00:00:00+05:30", 123, 125, 119.99, 122),
        ]
        first = evaluate_master_candle(weekly, instrument_key="SELF_TEST|DELIVERY", timeframe="1W")
        second = evaluate_master_candle(weekly, instrument_key="SELF_TEST|DELIVERY", timeframe="1W")

        start = datetime(2024, 1, 1, 15, 30)
        daily = []
        cursor = start
        while len(daily) < 420:
            if cursor.weekday() < 5:
                base = 100 + len(daily) * 0.05
                daily.append(_bar(cursor.isoformat() + "+05:30", base, base + 3, base - 2, base + 1))
            cursor += timedelta(days=1)
        context = delivery_timeframe_context(daily, instrument_key="SELF_TEST|DELIVERY")

        checks = {
            "timeframe_roster_exact": list(context.get("timeframe_roster") or []) == list(REQUIRED_TIMEFRAMES),
            "completed_periods_only": context.get("completed_periods_only") is True,
            "weekly_identity_present": bool(first.get("identity")) and len(str(first.get("identity"))) == 64,
            "weekly_identity_stable": first.get("identity") == second.get("identity"),
            "breakout_completed_close": bool(first.get("breakout")) and first["breakout"].get("completed") is True,
            "retest_completed_close": bool(first.get("retest")) and first["retest"].get("completed") is True,
            "weekly_retest_state": first.get("state") == "RETEST_CONFIRMED_UP",
            "monthly_structure_wired": "monthly_master_candle" in context,
        }
        ok = all(checks.values())
        return {
            "ok": ok,
            "state": "READY" if ok else "BLOCKED",
            "service_version": SERVICE_VERSION,
            "required_timeframes": list(REQUIRED_TIMEFRAMES),
            "completed_periods_only": True,
            "weekly_master_candle": {
                "version": MASTER_CANDLE_VERSION,
                "identity": first.get("identity"),
                "state": first.get("state"),
                "breakout": first.get("breakout"),
                "retest": first.get("retest"),
                "confirmation_policy": first.get("confirmation_policy"),
            },
            "checks": checks,
            "policy": "30m/1H execution timing; 4H/1D setup and retest; completed 1W master identity/trend; completed 1M secular trend",
            "probe": "deterministic_in_process_no_network_no_storage",
        }
