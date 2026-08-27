from __future__ import annotations

"""Operational reference-data authority for production (PostgreSQL-backed).

core.reference_data_repository.ReferenceDataRepository is built directly on
a raw sqlite3 connection with no production delegate for most of its surface.
save_delivery_rows/latest_delivery/save_delivery_data/get_delivery_data are
already cut over separately via DeliveryLakeRepository (see
storage.py Store.save_delivery_rows etc. and main.py's
production_delivery_repository wiring) -- this repository does NOT touch
those. It covers the remaining surface: bulk/block deals,
market breadth, reference-run status, fundamentals cache, option chain
snapshots, earnings calendar -- against the `reference` schema (see
infra/postgres/operational/006_reference_data_authority.sql).

The legacy one-time SQLite date-cleanup helper is not part of the running
production API. Retained reference rows are migrated read-only by
`migrate_compatibility_state_to_postgres.py` before service start.

Keeps the exact external contract (method names, return shapes) of the
SQLite ReferenceDataRepository methods it replaces so callers do not change.
"""

import json
from typing import Any, Dict, List, Optional

from .postgres import PostgresAuthority


class ProductionReferenceDataRepository:
    """Operational PostgreSQL persistence for market reference data."""

    production_authority = True

    def __init__(self, operational: PostgresAuthority, read_authority: PostgresAuthority | None = None):
        self.operational = operational
        # Selected-stock, market-sector and operator reads use reserved
        # foreground capacity.  All writes remain on the operational authority.
        self.read_authority = read_authority or operational

    # -- bulk / block deals -------------------------------------------
    def save_bulk_block_deals(self, trade_date: str, deal_type: str, rows: List[Dict[str, Any]]) -> int:
        n = 0
        for r in rows or []:
            sym = str(r.get("symbol") or "").upper().strip()
            if not sym:
                continue
            try:
                self.operational.execute(
                    """
                    INSERT INTO reference.bulk_block_deals
                        (trade_date, symbol, deal_type, client_name, buy_sell, qty, price)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    (trade_date, sym, deal_type, r.get("client_name"), r.get("buy_sell"),
                     r.get("qty"), r.get("price")),
                )
                n += 1
            except Exception:
                continue
        return n

    def get_bulk_block_deals(self, symbol: str = "", days: int = 5) -> List[Dict[str, Any]]:
        if symbol:
            rows = self.read_authority.execute(
                "SELECT * FROM reference.bulk_block_deals WHERE symbol=%s AND trade_date >= (CURRENT_DATE - %s * INTERVAL '1 day') ORDER BY trade_date DESC",
                (str(symbol).upper().strip(), days),
                fetch="all",
            )
        else:
            rows = self.read_authority.execute(
                "SELECT * FROM reference.bulk_block_deals WHERE trade_date >= (CURRENT_DATE - %s * INTERVAL '1 day') ORDER BY trade_date DESC",
                (days,),
                fetch="all",
            )
        return [dict(r) for r in (rows or [])]

    # -- market breadth ---------------------------------------------------
    def save_market_breadth(self, universe: str, advances: int, declines: int, unchanged: int) -> None:
        from models import now_iso
        self.operational.execute(
            """
            INSERT INTO reference.market_breadth_daily (ts, universe, advances, declines, unchanged)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (ts, universe) DO UPDATE SET
                advances = excluded.advances, declines = excluded.declines, unchanged = excluded.unchanged
            """,
            (now_iso(), universe, advances, declines, unchanged),
        )

    def get_latest_market_breadth(self, universe: str = "NIFTY250_CORE") -> Optional[Dict[str, Any]]:
        row = self.read_authority.execute(
            "SELECT * FROM reference.market_breadth_daily WHERE universe=%s ORDER BY ts DESC LIMIT 1",
            (universe,),
            fetch="one",
        )
        return dict(row) if row else None

    # -- reference-run status ---------------------------------------------
    def record_reference_run(self, job_name: str, run_date: str, status: str, rows_written: int, error: str = "") -> None:
        rows_written = max(0, int(rows_written or 0))
        status = str(status or "UNKNOWN").upper()
        # A request that raised no exception but produced no verified rows is
        # not successful evidence.  Consumers must distinguish a real empty
        # trading-day result from absent/unparsed data.
        if status in {"OK", "READY", "COMPLETE"} and rows_written == 0:
            status = "EMPTY_UNVERIFIED"
            error = error or "provider completed without verified rows"
        self.operational.execute(
            """
            INSERT INTO reference.reference_data_runs (job_name, run_date, status, rows_written, error, finished_at)
            VALUES (%s, %s, %s, %s, %s, now())
            ON CONFLICT (job_name, run_date) DO UPDATE SET
                status = excluded.status, rows_written = excluded.rows_written,
                error = excluded.error, finished_at = now()
            """,
            (job_name, run_date, status, rows_written, error[:300] if error else ""),
        )

    def reference_run_status(self) -> List[Dict[str, Any]]:
        rows = self.read_authority.execute(
            "SELECT * FROM reference.reference_data_runs ORDER BY finished_at DESC LIMIT 20", fetch="all",
        )
        return [dict(r) for r in (rows or [])]

    # -- fundamentals cache -------------------------------------------------
    def save_fundamentals_cache(self, isin: str, ok: bool, payload: Dict[str, Any]) -> None:
        from models import now_iso
        isin = str(isin or "").strip().upper()
        if not isin:
            return
        payload_json = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        self.operational.execute(
            """
            INSERT INTO reference.fundamentals_cache (isin, ok, payload_json, fetched_at)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (isin) DO UPDATE SET
                ok = excluded.ok, payload_json = excluded.payload_json, fetched_at = excluded.fetched_at
            """,
            (isin, bool(ok), payload_json, now_iso()),
        )
        # NOTE: fundamentals_history (point-in-time provider-version archive)
        # is intentionally not mirrored here -- get_fundamentals_history is
        # not currently exposed on Store, so there is no production caller
        # to serve. Flagged rather than silently duplicated without a reader.

    def get_fundamentals_cache(self, isin: str) -> Optional[Dict[str, Any]]:
        isin = str(isin or "").strip().upper()
        if not isin:
            return None
        row = self.read_authority.execute(
            "SELECT ok, payload_json, fetched_at FROM reference.fundamentals_cache WHERE isin=%s",
            (isin,),
            fetch="one",
        )
        if not row:
            return None
        payload = row["payload_json"]
        try:
            payload = json.loads(payload) if isinstance(payload, str) else payload
        except Exception:
            return None
        return {"ok": bool(row["ok"]), "payload": payload, "fetched_at": row["fetched_at"]}

    def get_all_fundamentals_cache(self) -> Dict[str, Dict[str, Any]]:
        out: Dict[str, Dict[str, Any]] = {}
        try:
            rows = self.read_authority.execute(
                "SELECT isin, ok, payload_json, fetched_at FROM reference.fundamentals_cache", fetch="all",
            )
        except Exception:
            return out
        for row in rows or []:
            try:
                payload = row["payload_json"]
                payload = json.loads(payload) if isinstance(payload, str) else payload
                out[row["isin"]] = {"ok": bool(row["ok"]), "payload": payload, "fetched_at": row["fetched_at"]}
            except Exception:
                continue
        return out

    # -- option chain -------------------------------------------------------


    # -- earnings calendar ----------------------------------------------
    def save_earnings_calendar(self, rows: List[Dict[str, Any]]) -> int:
        n = 0
        for r in rows or []:
            sym = str(r.get("symbol") or "").upper().strip()
            ev_date = str(r.get("event_date") or "").strip()
            if not sym or not ev_date:
                continue
            try:
                self.operational.execute(
                    """
                    INSERT INTO reference.earnings_calendar (symbol, event_date, event_type, purpose)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (symbol, event_date, event_type) DO UPDATE SET purpose = excluded.purpose
                    """,
                    (sym, ev_date, r.get("event_type") or "board_meeting", r.get("purpose") or ""),
                )
                n += 1
            except Exception:
                continue
        return n

    def get_upcoming_earnings(self, symbol: str, within_days: int = 3) -> List[Dict[str, Any]]:
        rows = self.read_authority.execute(
            "SELECT * FROM reference.earnings_calendar WHERE symbol=%s AND event_date BETWEEN CURRENT_DATE AND (CURRENT_DATE + %s * INTERVAL '1 day') ORDER BY event_date",
            (str(symbol or "").upper().strip(), within_days),
            fetch="all",
        )
        return [dict(r) for r in (rows or [])]

    def event_risk_symbols(self, within_days: int = 3) -> Dict[str, str]:
        rows = self.read_authority.execute(
            """
            SELECT symbol, MIN(event_date) as nearest FROM reference.earnings_calendar
            WHERE event_date BETWEEN CURRENT_DATE AND (CURRENT_DATE + %s * INTERVAL '1 day')
            GROUP BY symbol
            """,
            (within_days,),
            fetch="all",
        )
        return {r["symbol"]: str(r["nearest"]) for r in (rows or [])}
