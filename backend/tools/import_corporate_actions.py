"""Import independently verified corporate-action factors into PostgreSQL.

Accepted input is CSV or JSON/JSONL with one row per action. Required fields:
exchange, trading_symbol, ex_date, action_type, price_factor, volume_factor.
An instrument_key may be supplied; otherwise the point-in-time accepted
instrument is resolved from core.instruments. Coverage is never marked complete
unless --mark-complete is explicit and every imported row is verified.
"""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from typing import Iterable

ALLOWED_ACTIONS = {"SPLIT", "BONUS", "CONSOLIDATION", "RIGHTS", "OTHER"}


def canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def load_rows(path: Path) -> list[dict]:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            return [dict(row) for row in csv.DictReader(handle)]
    text = path.read_text(encoding="utf-8")
    if suffix in {".jsonl", ".ndjson"}:
        return [json.loads(line) for line in text.splitlines() if line.strip()]
    payload = json.loads(text)
    if isinstance(payload, dict):
        payload = payload.get("actions") or payload.get("rows") or []
    if not isinstance(payload, list):
        raise ValueError("JSON input must be a list or contain an actions/rows list")
    return [dict(row) for row in payload]


def normalise(row: dict, source_name: str, source_file_sha256: str | None = None) -> dict:
    exchange = str(row.get("exchange") or "NSE").strip().upper()
    symbol = str(row.get("trading_symbol") or row.get("symbol") or "").strip().upper()
    ex_date = str(row.get("ex_date") or row.get("date") or "")[:10]
    action_type = str(row.get("action_type") or row.get("type") or "").strip().upper()
    if not symbol or len(ex_date) != 10 or action_type not in ALLOWED_ACTIONS:
        raise ValueError(f"Invalid corporate-action identity: {row}")
    price_factor = float(row.get("price_factor"))
    volume_factor = float(row.get("volume_factor"))
    if price_factor <= 0 or volume_factor <= 0:
        raise ValueError("price_factor and volume_factor must be positive")
    basis = {
        "exchange": exchange,
        "trading_symbol": symbol,
        "ex_date": ex_date,
        "action_type": action_type,
        "price_factor": price_factor,
        "volume_factor": volume_factor,
        "source_name": source_name,
        "source_record_id": str(row.get("source_record_id") or row.get("id") or ""),
        "source_file_sha256": str(source_file_sha256 or ""),
    }
    source_hash = hashlib.sha256(canonical(basis).encode()).hexdigest()
    return {
        **basis,
        "instrument_key": str(row.get("instrument_key") or "").strip(),
        "isin": str(row.get("isin") or "").strip() or None,
        "source_hash": source_hash,
        "published_at": row.get("published_at") or None,
        "verified": str(row.get("verified", "true")).strip().lower() in {"1", "true", "yes", "y"},
        "verification_note": str(row.get("verification_note") or "").strip() or None,
        "source_file_sha256": str(source_file_sha256 or "") or None,
    }


def import_rows(*, dsn: str, rows: Iterable[dict], source_name: str, coverage_start: str,
                coverage_end: str, mark_complete: bool, mark_universe_complete: bool = False,
                source_file_sha256: str | None = None, coverage_symbols: Iterable[str] = ()) -> dict:
    import psycopg
    from psycopg.rows import dict_row

    records = [normalise(dict(row), source_name, source_file_sha256=source_file_sha256) for row in rows]
    coverage_symbols = tuple(coverage_symbols)
    if not records and not (mark_complete and any(str(value or "").strip() for value in coverage_symbols)):
        raise ValueError("No corporate-action rows were supplied; an empty verified range requires --mark-complete plus at least one --coverage-symbol")
    if mark_complete and not all(row["verified"] for row in records):
        raise ValueError("Coverage cannot be marked complete while any action row is unverified")
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        for row in records:
            if not row["instrument_key"]:
                match = conn.execute("""
                    SELECT provider_instrument_key
                      FROM core.instruments
                     WHERE exchange=%s AND upper(trading_symbol)=%s
                       AND validation_status='ACCEPTED'
                       AND active_from::date <= %s::date
                       AND (active_to IS NULL OR active_to::date > %s::date)
                     ORDER BY active_from DESC, universe_revision DESC
                     LIMIT 1
                """, (row["exchange"], row["trading_symbol"], row["ex_date"], row["ex_date"])).fetchone()
                if not match:
                    raise ValueError(f"No point-in-time instrument for {row['exchange']}:{row['trading_symbol']} on {row['ex_date']}")
                row["instrument_key"] = str(match["provider_instrument_key"])
            action_id = hashlib.sha256((row["instrument_key"] + ":" + row["source_hash"]).encode()).hexdigest()[:32]
            conn.execute("""
                INSERT INTO reference.corporate_actions(
                    action_id,instrument_key,exchange,trading_symbol,isin,ex_date,action_type,
                    price_factor,volume_factor,source_name,source_record_id,source_hash,
                    published_at,verified,verification_note
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (action_id) DO UPDATE SET
                    price_factor=EXCLUDED.price_factor,volume_factor=EXCLUDED.volume_factor,
                    source_name=EXCLUDED.source_name,source_record_id=EXCLUDED.source_record_id,
                    source_hash=EXCLUDED.source_hash,published_at=EXCLUDED.published_at,
                    verified=EXCLUDED.verified,verification_note=EXCLUDED.verification_note,
                    ingested_at=now()
            """, (
                action_id,row["instrument_key"],row["exchange"],row["trading_symbol"],row["isin"],
                row["ex_date"],row["action_type"],row["price_factor"],row["volume_factor"],
                row["source_name"],row["source_record_id"],row["source_hash"],row["published_at"],
                row["verified"],row["verification_note"],
            ))
        by_instrument = {}
        for row in records:
            by_instrument[row["instrument_key"]] = row
        requested_symbols = sorted({str(value or "").strip().upper() for value in coverage_symbols if str(value or "").strip()})
        if requested_symbols:
            if not mark_complete:
                raise ValueError("--coverage-symbol requires --mark-complete")
            for symbol in requested_symbols:
                match = conn.execute("""
                    SELECT provider_instrument_key AS instrument_key,exchange,trading_symbol
                      FROM core.instruments
                     WHERE upper(trading_symbol)=%s AND validation_status='ACCEPTED'
                       AND asset_class='CASH_EQUITY'
                     ORDER BY CASE WHEN exchange='NSE' THEN 0 ELSE 1 END, active_from DESC NULLS LAST
                     LIMIT 1
                """, (symbol,)).fetchone()
                if not match:
                    raise ValueError(f"No accepted cash-equity instrument for coverage symbol {symbol}")
                by_instrument.setdefault(str(match["instrument_key"]), {
                    "instrument_key": str(match["instrument_key"]),
                    "exchange": str(match["exchange"]),
                    "trading_symbol": str(match["trading_symbol"]),
                })
        if mark_universe_complete:
            if not mark_complete:
                raise ValueError("--mark-universe-complete requires --mark-complete")
            active = conn.execute("""
                SELECT provider_instrument_key AS instrument_key,exchange,trading_symbol
                  FROM core.instruments
                 WHERE validation_status='ACCEPTED' AND asset_class='CASH_EQUITY' AND active_to IS NULL
            """).fetchall()
            for item in active:
                by_instrument.setdefault(str(item["instrument_key"]), {
                    "instrument_key": str(item["instrument_key"]),
                    "exchange": str(item["exchange"]),
                    "trading_symbol": str(item["trading_symbol"]),
                })
        for instrument_key, row in by_instrument.items():
            coverage_hash = hashlib.sha256(canonical({
                "instrument_key": instrument_key,
                "coverage_start": coverage_start,
                "coverage_end": coverage_end,
                "source_name": source_name,
                "complete": bool(mark_complete),
                "coverage_basis": ("MANUAL_SCOPED_VERIFIED_RANGE" if requested_symbols and not mark_universe_complete else "FULL_LISTING_HISTORY"),
                "source_file_sha256": str(source_file_sha256 or ""),
            }).encode()).hexdigest()
            coverage_basis = 'MANUAL_SCOPED_VERIFIED_RANGE' if requested_symbols and not mark_universe_complete else 'FULL_LISTING_HISTORY'
            conn.execute("""
                INSERT INTO reference.corporate_action_coverage(
                    instrument_key,exchange,trading_symbol,coverage_start,coverage_end,
                    coverage_basis,source_name,source_hash,complete,verified_at,updated_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now())
                ON CONFLICT (instrument_key) DO UPDATE SET
                    exchange=EXCLUDED.exchange,trading_symbol=EXCLUDED.trading_symbol,
                    coverage_start=EXCLUDED.coverage_start,coverage_end=EXCLUDED.coverage_end,
                    coverage_basis=EXCLUDED.coverage_basis,source_name=EXCLUDED.source_name,
                    source_hash=EXCLUDED.source_hash,complete=EXCLUDED.complete,
                    verified_at=EXCLUDED.verified_at,updated_at=now()
            """, (
                instrument_key,row["exchange"],row["trading_symbol"],coverage_start,coverage_end,
                coverage_basis,source_name,coverage_hash,bool(mark_complete),
                datetime.now(timezone.utc) if mark_complete else None,
            ))
        conn.commit()
    return {
        "ok": True,
        "state": "CORPORATE_ACTION_AUTHORITY_IMPORTED",
        "rows": len(records),
        "instruments": len(by_instrument),
        "coverage_complete": bool(mark_complete),
        "universe_coverage_attested": bool(mark_universe_complete),
        "source_name": source_name,
        "source_file_sha256": source_file_sha256,
        "scoped_coverage_symbols": sorted({str(value or "").strip().upper() for value in coverage_symbols if str(value or "").strip()}),
        "broker_authority": "NONE",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", type=Path, required=True)
    parser.add_argument("--operational-dsn", default=os.environ.get("PROJECT_LADDU_OPERATIONAL_DSN", ""))
    parser.add_argument("--source-name", required=True)
    parser.add_argument("--coverage-start", required=True)
    parser.add_argument("--coverage-end", required=True)
    parser.add_argument("--mark-complete", action="store_true")
    parser.add_argument("--mark-universe-complete", action="store_true", help="Explicitly attest complete action coverage for every active cash equity")
    parser.add_argument("--coverage-symbol", action="append", default=[], help="Explicit symbol whose supplied official CSV proves complete coverage for the requested range; repeatable")
    args = parser.parse_args()
    if not args.operational_dsn.strip():
        raise SystemExit("PROJECT_LADDU_OPERATIONAL_DSN/--operational-dsn is required")
    source_file_sha256 = hashlib.sha256(args.file.read_bytes()).hexdigest()
    result = import_rows(
        dsn=args.operational_dsn.strip(), rows=load_rows(args.file), source_name=args.source_name,
        coverage_start=args.coverage_start, coverage_end=args.coverage_end,
        mark_complete=args.mark_complete,
        mark_universe_complete=args.mark_universe_complete,
        source_file_sha256=source_file_sha256, coverage_symbols=args.coverage_symbol,
    )
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
