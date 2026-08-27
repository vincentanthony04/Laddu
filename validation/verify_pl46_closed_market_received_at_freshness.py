"""P0-01c regression: a closed-market completed_session_quote() record must
never leak its own stale received_at/received_time into the research capture
row lineage. Reproduces the exact residual failure observed on SYNGENE after
market close:
  completed_quote() returns a cached row with received_at from hours earlier
  feature capture later stamps a fresh source_as_of/feature_as_of
  -> false INVALID_TIMESTAMP_ORDER on a valid closed-market observation.

Calls the real ScanModeExecutionMixin._scanner_quotes_for_instruments with a
minimal fake host, so this exercises the actual fixed code path rather than
a re-implementation of its logic.

Run: python validation/verify_pl46_closed_market_received_at_freshness.py
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from core.scan_orchestration_modes import ScanModeExecutionMixin  # noqa: E402


class _FakeRuntimeMarketState:
    def latest_quotes(self, symbols):
        return []


class _FakeMarketData:
    def completed_session_quote(self, inst):
        # Simulate a cached historical closing quote whose own receipt
        # fields are from hours earlier -- exactly what leaked through on
        # SYNGENE.
        return {
            "ltp": 101.25,
            "received_at": "2026-08-22T11:49:20+05:30",
            "received_time": "2026-08-22T11:49:20+05:30",
        }


class _FakeHost:
    def __init__(self):
        self.runtime_market_state = _FakeRuntimeMarketState()
        self.market_data = _FakeMarketData()

    def record_error(self, *a, **kw):
        pass


class _Scanner(ScanModeExecutionMixin):
    def __init__(self):
        self.host = _FakeHost()


def test_closed_market_row_never_carries_stale_received_at():
    scanner = _Scanner()
    instruments = [{
        "trading_symbol": "SYNGENE",
        "symbol": "SYNGENE",
        "instrument_key": "NSE_EQ|INE175A01038",
    }]
    with patch("core.scan_orchestration_modes.is_india_market_open", return_value=False):
        quote_by_key = scanner._scanner_quotes_for_instruments(instruments, allow_rest=False)
    row = quote_by_key.get("NSE_EQ|INE175A01038")
    assert row is not None, "closed-market quote was not captured at all"
    assert row["received_at"] != "2026-08-22T11:49:20+05:30", (
        f"stale completed_session_quote received_at survived: {row['received_at']!r}"
    )
    assert "received_time" not in row, (
        f"stale completed_session_quote received_time survived: {row.get('received_time')!r}"
    )


if __name__ == "__main__":
    test_closed_market_row_never_carries_stale_received_at()
    print("PASS: P0-01c closed-market received_at freshness regression")
