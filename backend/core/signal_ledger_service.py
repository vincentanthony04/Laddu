"""
SignalLedgerService — v37.4, Cluster D decoupling.

Extracts audit_open_signal_ledger() (and its interval-selection helper) out
of LadduRuntime. This is Vincent's "signal_ledger.decision is the single
source of truth" logic: prove open signal outcomes from candles after
entry, settle them when resolved, otherwise persist evidence (MFE/MAE) on
the still-open row. Selected Candidates, Daily Performance, and Trade
Journal all read the rows this produces -- correctness here matters more
than almost anything else in the system, which is exactly why it deserves
its own module instead of living inside a 3,000-line class alongside quote
polling and HTTP routing.

Depends only on: Store (for ledger rows/settlement), MarketDataService (for
historical candles), InstrumentResolver (for symbol -> instrument_key), and
a ServiceLogger. Does not depend on engines.py or dashboard/discovery code.
"""
from __future__ import annotations

import json
from typing import Any, Dict, Optional

from core.production_mode_policy import require_production_mode


class SignalLedgerService:
    def __init__(self, store, market_data, instrument_resolver, logger=None):
        self.store = store
        self.market_data = market_data
        self.resolver = instrument_resolver
        self.logger = logger

    def _log(self, level: str, message: str, detail: Optional[Dict[str, Any]] = None) -> None:
        if self.logger is not None:
            self.logger.event(level, message, detail)

    @staticmethod
    def audit_interval(mode: str) -> tuple[str, int]:
        try:
            canonical = require_production_mode(mode)
        except ValueError:
            canonical = "delivery"
        return ("1minute", 5) if canonical == "intraday" else ("day", 60)

    def audit_open_signals(self, limit: int = 40) -> Dict[str, Any]:
        """Stage 2 validation: prove selected trade outcomes from candles
        after triggered_at/opened_at. Ledger-first: deliberately does not
        trust in-memory decision state, only what candles since entry show."""
        try:
            rows = self.store.open_signal_rows(limit=limit)
        except Exception as exc:
            self._log("WARN", "Failed to load open signal rows", {"error": str(exc)[:200]})
            return {"ok": False, "error": str(exc), "audited": 0, "settled": 0}

        audited = 0
        settled = 0
        ambiguous = 0
        open_kept = 0
        errors: list = []
        # Prioritize the fast desk: most likely to hit target/SL between live ticks.
        def _desk_rank(row: Dict[str, Any]) -> tuple[int, str]:
            try:
                desk = require_production_mode(row.get("mode"))
            except ValueError:
                desk = "delivery"
            return (0 if desk == "intraday" else 1, str(row.get("opened_at") or ""))

        rows.sort(key=_desk_rank)

        for row in rows[:limit]:
            try:
                sym = str(row.get("symbol") or "").upper().strip()
                side = str(row.get("side") or "").upper().strip()
                if not sym or side not in ("LONG", "SHORT"):
                    continue
                mode = require_production_mode(row.get("mode"))
                interval, days = self.audit_interval(mode)
                inst = self.resolver.resolve(sym)
                if not inst or not inst.get("instrument_key"):
                    continue
                candles = self.market_data.get_historical(inst["instrument_key"], interval, days)
                proof = self.store.evaluate_signal_from_candles(row, candles, interval=interval)
                audited += 1
                status = proof.get("status")
                result = proof.get("result")
                if status in ("SUCCESS", "FAIL", "AMBIGUOUS"):
                    self.store.settle_signal_by_id(row["signal_id"], status, result, proof.get("exit"), proof.get("pnl"), proof)
                    settled += 1
                    if status == "AMBIGUOUS":
                        ambiguous += 1
                else:
                    # Keep the open row, but persist MFE/MAE/proof so evidence is visible.
                    payload = json.loads(row.get("payload_json") or "{}")
                    payload.update({k: v for k, v in proof.items() if v is not None})
                    payload.update({
                        "validation_source": "post_trigger_candle_audit",
                        "validation_policy": "not closed until target/SL sequence is proven",
                    })
                    with self.store.write_lock:
                        self.store.conn.execute(
                            "UPDATE signal_ledger SET ltp=COALESCE(?, ltp), payload_json=?, last_update=CURRENT_TIMESTAMP WHERE signal_id=? AND status='OPEN'",
                            (proof.get("last_price"), json.dumps(payload), row["signal_id"]),
                        )
                        self.store.conn.commit()
                    open_kept += 1
            except Exception as exc:
                errors.append({"symbol": row.get("symbol"), "error": str(exc)[:180]})
                self._log("WARN", "Signal audit failed for row", {"symbol": row.get("symbol"), "error": str(exc)[:180]})

        result_summary = {
            "audited": audited, "settled": settled, "ambiguous": ambiguous,
            "open": open_kept, "errors": errors,
        }
        self._log("INFO" if audited or settled or errors else "DEBUG", f"Audited {audited} open signal(s); settled {settled}", result_summary)
        return {"ok": True, **result_summary}
