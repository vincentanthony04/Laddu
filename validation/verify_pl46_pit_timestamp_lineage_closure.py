"""P0-01 regression: received_at must never survive stale from a prior
pipeline hop once a new quote is classified into research_capture_row().

Reproduces the exact failure class observed on MAHABANK/MCX/PAYTM/SYNGENE:
  source_as_of=2026-08-22T12:07:24+05:30 (new, correct quote)
  received_at =2026-08-22T11:49:20+05:30 (stale, inherited from earlier hop)
-> false INVALID_TIMESTAMP_ORDER on a valid observation.

Run: python validation/verify_pl46_pit_timestamp_lineage_closure.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from core.scan_orchestration_rows import research_capture_row  # noqa: E402


def test_stale_received_at_does_not_survive_new_quote():
    decision = {"received_at": "2026-08-22T11:49:20+05:30"}
    instrument = {"trading_symbol": "MAHABANK", "instrument_key": "NSE_EQ|INE457A01014"}
    quote = {
        "ltp": 50.1,
        "provider_timestamp": "2026-08-22T12:07:24+05:30",
        "identity_verified": True,
    }
    row = research_capture_row(decision, instrument, "delivery", quote)
    assert row["source_as_of"] == "2026-08-22T12:07:24+05:30"
    assert row["received_at"] >= row["source_as_of"], (
        f"stale received_at survived: source_as_of={row['source_as_of']!r} "
        f"received_at={row['received_at']!r}"
    )


def test_quote_supplied_received_at_still_honored():
    decision = {"received_at": "2026-08-22T09:00:00+05:30"}
    instrument = {"trading_symbol": "MCX", "instrument_key": "NSE_EQ|INE745G01035"}
    quote = {
        "ltp": 1234.5,
        "provider_timestamp": "2026-08-22T10:00:00+05:30",
        "received_at": "2026-08-22T10:00:02+05:30",
        "identity_verified": True,
    }
    row = research_capture_row(decision, instrument, "delivery", quote)
    assert row["received_at"] == "2026-08-22T10:00:02+05:30"


if __name__ == "__main__":
    test_stale_received_at_does_not_survive_new_quote()
    test_quote_supplied_received_at_still_honored()
    print("PASS: P0-01 PIT timestamp lineage regression")
