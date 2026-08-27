"""Reconcile official NSE corporate actions into row-scoped PIT adjustment authority.

PL42 derives factors only from published terms (and retained pre-ex close for
rights). Market-wide range evidence may certify zero-action symbols over that exact
window. Unresolved structural events block only the affected instrument; they never
poison unrelated stocks or get guessed factors.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from core.corporate_action_factor_derivation import VERSION as FACTOR_VERSION, derive_factors

AUTHORITY_VERSION = "corporate-action-official-reconciliation-2.0.0-pl42"


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _panel_scope(data_dir: str | Path | None) -> list[dict[str, Any]]:
    if not data_dir:
        return []
    from core.storage_layout import StorageLayout
    try:
        import duckdb
        layout = StorageLayout.from_data_dir(Path(data_dir))
        if not layout.analytics_db.is_file():
            return []
        db = duckdb.connect(str(layout.analytics_db), read_only=True)
        try:
            relations = {str(r[0]) for r in db.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema='main'"
            ).fetchall()}
            if "research_delivery_training_panel" not in relations:
                return []
            return [
                {"instrument_key": str(r[0]), "symbol": str(r[1]).upper(),
                 "start": str(r[2])[:10], "end": str(r[3])[:10], "days": int(r[4] or 0)}
                for r in db.execute(
                    """SELECT instrument_key,upper(symbol),min(date),max(date),count(DISTINCT date)
                         FROM research_delivery_training_panel
                        WHERE instrument_key IS NOT NULL AND symbol IS NOT NULL AND date IS NOT NULL
                        GROUP BY instrument_key,upper(symbol)"""
                ).fetchall()
            ]
        finally:
            db.close()
    except Exception:
        return []


def _pre_ex_close(data_dir: str | Path | None, instrument_key: str, ex_date: Any) -> float | None:
    if not data_dir or not instrument_key or not ex_date:
        return None
    from core.storage_layout import StorageLayout
    try:
        import duckdb
        layout = StorageLayout.from_data_dir(Path(data_dir))
        if not layout.analytics_db.is_file():
            return None
        db = duckdb.connect(str(layout.analytics_db), read_only=True)
        try:
            relations = {str(r[0]) for r in db.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema='main'"
            ).fetchall()}
            relation = "research_delivery_training_panel" if "research_delivery_training_panel" in relations else None
            if not relation:
                return None
            row = db.execute(
                f"""SELECT close FROM {relation}
                     WHERE instrument_key=? AND date < CAST(? AS DATE) AND close IS NOT NULL
                     ORDER BY date DESC LIMIT 1""", [str(instrument_key), str(ex_date)[:10]]
            ).fetchone()
            return float(row[0]) if row and row[0] is not None and float(row[0]) > 0 else None
        finally:
            db.close()
    except Exception:
        return None


def _identity(conn, symbol: str, ex_date: Any) -> dict[str, Any] | None:
    row = conn.execute(
        """SELECT provider_instrument_key,exchange,trading_symbol,isin
             FROM core.instruments
            WHERE exchange='NSE' AND upper(trading_symbol)=%s AND validation_status='ACCEPTED'
              AND active_from::date <= %s::date AND (active_to IS NULL OR active_to::date > %s::date)
            ORDER BY active_from DESC,universe_revision DESC LIMIT 1""",
        (symbol, ex_date, ex_date),
    ).fetchone()
    if row:
        return dict(row)
    # Historical effective-date damage must not erase an official action. Identity
    # fallback is labeling only; historical membership still comes from PL40's
    # exact-date candle/PIT authority.
    row = conn.execute(
        """SELECT provider_instrument_key,exchange,trading_symbol,isin
             FROM core.instruments
            WHERE exchange='NSE' AND upper(trading_symbol)=%s AND validation_status='ACCEPTED'
            ORDER BY active_from DESC NULLS LAST,universe_revision DESC NULLS LAST LIMIT 1""",
        (symbol,),
    ).fetchone()
    return dict(row) if row else None


def reconcile(
    dsn: str, *, data_dir: str | Path | None = None,
    coverage_start: str | None = None, coverage_end: str | None = None,
    range_source_hash: str | None = None,
) -> dict[str, Any]:
    import psycopg
    from psycopg.rows import dict_row

    if not str(dsn or "").strip():
        raise ValueError("PROJECT_LADDU_OPERATIONAL_DSN is required")
    inserted = updated = unresolved = identity_missing = 0
    neutral = derived = explicit = 0
    unresolved_symbols: set[str] = set()
    action_symbols: set[str] = set()
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        rows = conn.execute("""
            SELECT e.trade_date,e.source_record_id,e.symbol,e.isin,e.ex_date,e.action_type,
                   e.purpose,e.price_factor,e.volume_factor,e.published_at,e.content_hash,e.raw_payload,
                   r.state AS ingestion_state,r.rows_projected
              FROM reference.nse_market_events e
              LEFT JOIN LATERAL (
                    SELECT state,rows_projected FROM reference.nse_official_ingestion_runs r0
                     WHERE r0.source_key=e.source_key AND r0.trade_date=e.trade_date AND r0.content_hash=e.content_hash
                     ORDER BY projected_at DESC LIMIT 1
              ) r ON TRUE
             WHERE e.source_key='corporate_actions'
               AND (%s::date IS NULL OR COALESCE(e.ex_date,e.trade_date) >= %s::date)
               AND (%s::date IS NULL OR COALESCE(e.ex_date,e.trade_date) <= %s::date)
             ORDER BY COALESCE(e.ex_date,e.trade_date),e.symbol,e.source_record_id
        """, (coverage_start, coverage_start, coverage_end, coverage_end)).fetchall()
        for raw in rows:
            row = dict(raw)
            if str(row.get("ingestion_state") or "").upper() != "PROJECTED" or int(row.get("rows_projected") or 0) <= 0:
                continue
            ex_date = row.get("ex_date") or row.get("trade_date")
            symbol = str(row.get("symbol") or "").strip().upper()
            if not symbol or not ex_date:
                identity_missing += 1
                continue
            action_symbols.add(symbol)
            instrument = _identity(conn, symbol, ex_date)
            if not instrument:
                identity_missing += 1
                unresolved_symbols.add(symbol)
                continue
            raw_payload = dict(row.get("raw_payload") or {})
            derivation = derive_factors(
                purpose=row.get("purpose"), action_type=row.get("action_type"),
                face_value=raw_payload.get("face_value"),
                pre_ex_close=_pre_ex_close(data_dir, str(instrument["provider_instrument_key"]), ex_date),
                explicit_price_factor=row.get("price_factor"), explicit_volume_factor=row.get("volume_factor"),
            )
            if not derivation.get("ok"):
                unresolved += 1
                unresolved_symbols.add(symbol)
                continue
            pf, vf = float(derivation["price_factor"]), float(derivation["volume_factor"])
            if derivation["state"] == "EXPLICIT_SOURCE_FACTORS": explicit += 1
            elif derivation["state"] == "NO_SHARE_BASIS_ADJUSTMENT": neutral += 1
            else: derived += 1
            stored_action = str(derivation.get("action_type") or "OTHER").upper()
            if stored_action not in {"SPLIT", "BONUS", "CONSOLIDATION", "RIGHTS"}:
                stored_action = "OTHER"
            basis = {
                "source_key": "corporate_actions", "content_hash": str(row.get("content_hash") or ""),
                "source_record_id": str(row.get("source_record_id") or ""),
                "instrument_key": str(instrument["provider_instrument_key"]), "ex_date": str(ex_date),
                "action_type": stored_action, "price_factor": pf, "volume_factor": vf,
                "derivation_state": derivation.get("state"), "derivation_version": FACTOR_VERSION,
            }
            source_hash = hashlib.sha256(_canonical(basis).encode()).hexdigest()
            action_id = hashlib.sha256((basis["instrument_key"] + ":" + source_hash).encode()).hexdigest()[:32]
            existed = conn.execute("SELECT 1 FROM reference.corporate_actions WHERE action_id=%s", (action_id,)).fetchone() is not None
            note = "Official NSE content-addressed event; deterministic factor from published terms only. No price-jump inference. " + str(derivation.get("state"))
            conn.execute("""
                INSERT INTO reference.corporate_actions(
                    action_id,instrument_key,exchange,trading_symbol,isin,ex_date,action_type,
                    price_factor,volume_factor,source_name,source_record_id,source_hash,published_at,verified,verification_note
                ) VALUES (%s,%s,'NSE',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,true,%s)
                ON CONFLICT (action_id) DO UPDATE SET price_factor=EXCLUDED.price_factor,
                    volume_factor=EXCLUDED.volume_factor,source_hash=EXCLUDED.source_hash,
                    verified=true,verification_note=EXCLUDED.verification_note,ingested_at=now()
            """, (action_id,basis["instrument_key"],str(instrument["trading_symbol"]),
                  row.get("isin") or instrument.get("isin"),ex_date,stored_action,pf,vf,
                  "NSE_OFFICIAL_RANGE_AUTHORITY",basis["source_record_id"],source_hash,row.get("published_at"),note))
            updated += int(existed); inserted += int(not existed)

        panel = _panel_scope(data_dir)
        coverage_written = 0
        if panel and coverage_start and coverage_end and range_source_hash:
            # Market-wide range acquisition proves both present actions and the
            # absence of actions for zero-action names. Only unresolved structural
            # terms make that individual stock incomplete.
            for item in panel:
                symbol = item["symbol"]
                start = max(str(item["start"]), str(coverage_start))
                end = min(str(item["end"]), str(coverage_end))
                if start > end:
                    continue
                complete = symbol not in unresolved_symbols
                source_basis = {
                    "market_range_hash": str(range_source_hash), "instrument_key": item["instrument_key"],
                    "coverage_start": start, "coverage_end": end, "complete": complete,
                    "unresolved_structural": symbol in unresolved_symbols,
                }
                source_hash = hashlib.sha256(_canonical(source_basis).encode()).hexdigest()
                conn.execute("""
                    INSERT INTO reference.corporate_action_coverage(
                        instrument_key,exchange,trading_symbol,coverage_start,coverage_end,coverage_basis,
                        source_name,source_hash,complete,verified_at,updated_at
                    ) VALUES (%s,'NSE',%s,%s,%s,'MARKET_WIDE_NSE_CORPORATE_ACTION_RANGE',%s,%s,%s,now(),now())
                    ON CONFLICT (instrument_key) DO UPDATE SET
                        exchange=EXCLUDED.exchange,trading_symbol=EXCLUDED.trading_symbol,
                        coverage_start=LEAST(reference.corporate_action_coverage.coverage_start,EXCLUDED.coverage_start),
                        coverage_end=GREATEST(reference.corporate_action_coverage.coverage_end,EXCLUDED.coverage_end),
                        coverage_basis=EXCLUDED.coverage_basis,source_name=EXCLUDED.source_name,
                        source_hash=EXCLUDED.source_hash,complete=EXCLUDED.complete,verified_at=now(),updated_at=now()
                """, (item["instrument_key"],symbol,start,end,"NSE_OFFICIAL_CONTENT_ADDRESSED_RANGE",source_hash,complete))
                coverage_written += 1
        conn.commit()
        verified_actions = int(conn.execute("SELECT count(*) AS n FROM reference.corporate_actions WHERE verified IS TRUE").fetchone()["n"] or 0)
        complete_coverage = int(conn.execute("SELECT count(*) AS n FROM reference.corporate_action_coverage WHERE complete IS TRUE").fetchone()["n"] or 0)

    return {
        "ok": True,
        "state": "ROW_SCOPED_AUTHORITY_READY" if not identity_missing else "ROW_SCOPED_AUTHORITY_PARTIAL_IDENTITY",
        "authority": AUTHORITY_VERSION, "factor_authority": FACTOR_VERSION,
        "source_rows": len(rows), "inserted": inserted, "updated": updated,
        "derived_factors": derived, "explicit_factors": explicit, "neutral_actions": neutral,
        "unresolved_structural_actions": unresolved, "unresolved_symbols": sorted(unresolved_symbols),
        "identity_missing": identity_missing, "verified_action_rows": verified_actions,
        "panel_symbols": len(panel), "coverage_rows_written": coverage_written,
        "complete_coverage_rows": complete_coverage,
        "coverage_scope": {"start": coverage_start, "end": coverage_end, "range_source_hash": range_source_hash},
        "coverage_policy": "ZERO_ACTION_SYMBOLS_COVERED_BY_MARKET_WIDE_RANGE;UNRESOLVED_STRUCTURAL_BLOCKS_ONLY_AFFECTED_STOCK",
        "inference_from_price_jump_allowed": False, "broker_authority": "NONE",
        "captured_at": datetime.now(timezone.utc).isoformat(),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--operational-dsn", default=os.environ.get("PROJECT_LADDU_OPERATIONAL_DSN", ""))
    parser.add_argument("--data-dir", default=os.environ.get("PROJECT_LADDU_DATA_DIR", ""))
    parser.add_argument("--coverage-start", default="")
    parser.add_argument("--coverage-end", default="")
    parser.add_argument("--range-source-hash", default="")
    parser.add_argument("--output", default="")
    args = parser.parse_args()
    if not str(args.operational_dsn or "").strip():
        print(json.dumps({"ok": False, "state": "BLOCKED", "reason": "PROJECT_LADDU_OPERATIONAL_DSN is required"}, indent=2)); return 2
    try:
        result = reconcile(str(args.operational_dsn).strip(), data_dir=args.data_dir or None,
                           coverage_start=args.coverage_start or None, coverage_end=args.coverage_end or None,
                           range_source_hash=args.range_source_hash or None)
    except Exception as exc:
        print(json.dumps({"ok": False, "state": "BLOCKED", "reason": f"{type(exc).__name__}: {exc}"}, indent=2)); return 2
    text = json.dumps(result, indent=2, default=str); print(text)
    if args.output: Path(args.output).write_text(text + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
