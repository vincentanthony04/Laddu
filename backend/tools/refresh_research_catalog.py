"""Build the read-only DuckDB research catalogue from production authorities.

from core.corporate_action_adjustment_authority import DEFAULT_CORPORATE_ACTION_ADJUSTMENT_AUTHORITY
The catalogue is metadata-incremental: unchanged Parquet file catalogues do not
trigger a full rescan or count(*).  Instrument history is projected from
PostgreSQL as both the active universe and a point-in-time security master so
walk-forward validation can reject survivorship-biased datasets explicitly.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
from typing import Iterable, Mapping

HERE = Path(__file__).resolve()
BACKEND = HERE.parents[1]
import sys
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from core.storage_layout import StorageLayout, atomic_write_json, interprocess_lock
from core.nse_official_report_ingestion_service import CANONICAL_COLUMNS, NUMERIC_COLUMNS

CATALOG_VERSION = "research-catalog-4.3.0-pl42-row-scoped-corporate-actions-adaptive-history"
PRODUCTION_NO_PRUNE_STATE = "NOT_APPLICABLE_PRODUCTION_DATA_PLANE"
PRODUCTION_NO_PRUNE_REASON = (
    "PostgreSQL, QuestDB and Parquet/DuckDB are authoritative; "
    "no operational SQLite authority is eligible for prune"
)


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _production_manifest_safety(prior: Mapping | None = None) -> dict:
    """Return safety fields that a catalogue refresh must never erase.

    Research-catalogue regeneration is not an operational prune.  In the
    production data-plane architecture PostgreSQL, QuestDB and
    Parquet/DuckDB remain authoritative, so an operational SQLite prune is
    explicitly not applicable.  Existing reconciliation evidence is
    preserved verbatim; missing reconciliation is not fabricated.
    """
    prior = dict(prior or {})
    reconciliation = prior.get("reconciliation")
    if not isinstance(reconciliation, dict):
        reconciliation = {}

    existing_prune = prior.get("operational_prune")
    prune = dict(existing_prune) if isinstance(existing_prune, dict) else {}
    prune.update({
        "state": PRODUCTION_NO_PRUNE_STATE,
        "reason": PRODUCTION_NO_PRUNE_REASON,
        "authority": "PRODUCTION_DATA_PLANE",
    })
    prune.setdefault("verified_at", now())
    return {"reconciliation": reconciliation, "operational_prune": prune}


def sql_list(paths: Iterable[Path]) -> str:
    values = ["'" + str(Path(path).resolve()).replace("\\", "/").replace("'", "''") + "'" for path in paths]
    if not values:
        raise RuntimeError("No authoritative Parquet files are available")
    return "[" + ",".join(values) + "]"


def _sql_identifier(value: str) -> str:
    return '"' + str(value).replace('"', '""') + '"'


def normalized_nse_official_union_sql(db, paths: Iterable[Path]) -> str:
    """Return a schema-stable union across legacy official-report Parquet.

    Older files were written through auto-inferred JSON.  An all-null text
    column could therefore become DuckDB JSON in one file and VARCHAR in the
    next.  ``union_by_name`` then selected JSON as the common type and failed
    while scanning valid text such as BUY/SELL.  Group files by physical schema,
    cast every canonical field inside each group, and only then union the stable
    projections.  This preserves all rows and lineage; no source family is
    dropped merely to make the research catalogue load.
    """
    groups: dict[tuple[tuple[str, str], ...], list[Path]] = {}
    for raw_path in sorted(Path(item) for item in paths):
        resolved = str(raw_path.resolve()).replace("\\", "/")
        description = db.execute(
            "DESCRIBE SELECT * FROM read_parquet(?)", [resolved]
        ).fetchall()
        schema = tuple((str(row[0]), str(row[1])) for row in description)
        groups.setdefault(schema, []).append(raw_path)
    if not groups:
        raise RuntimeError("No authoritative NSE official Parquet files are available")

    projections: list[str] = []
    for schema, group_paths in groups.items():
        available = {name for name, _ in schema}
        columns: list[str] = []
        for column in CANONICAL_COLUMNS:
            target_type = "DOUBLE" if column in NUMERIC_COLUMNS else "VARCHAR"
            alias = _sql_identifier(column)
            if column in available:
                columns.append(f"TRY_CAST({_sql_identifier(column)} AS {target_type}) AS {alias}")
            else:
                columns.append(f"CAST(NULL AS {target_type}) AS {alias}")
        columns.append("CAST(filename AS VARCHAR) AS filename")
        projections.append(
            "SELECT " + ",".join(columns)
            + f" FROM read_parquet({sql_list(group_paths)}, union_by_name=true, filename=true)"
        )
    return "\nUNION ALL BY NAME\n".join(projections)


def file_catalogue(paths: Iterable[Path], root: Path) -> list[dict]:
    output = []
    for path in sorted(Path(item) for item in paths):
        stat = path.stat()
        try:
            rel = path.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            rel = path.resolve().as_posix()
        output.append({"path": rel, "size": int(stat.st_size), "mtime_ns": int(stat.st_mtime_ns)})
    return output


def _write_jsonl_parquet(rows: list[Mapping], target: Path) -> None:
    import duckdb

    target.parent.mkdir(parents=True, exist_ok=True)
    work_root = target.parent / ".catalog-work"
    work_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="laddu-reference-catalog-", dir=work_root) as temp_dir:
        jsonl = Path(temp_dir) / "rows.jsonl"
        stage = Path(temp_dir) / target.name
        with jsonl.open("w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(dict(row), default=str, sort_keys=True) + "\n")
        source = str(jsonl.resolve()).replace("\\", "/").replace("'", "''")
        destination = str(stage.resolve()).replace("\\", "/").replace("'", "''")
        db = duckdb.connect()
        try:
            db.execute(
                f"COPY (SELECT * FROM read_json_auto('{source}', format='newline_delimited')) "
                f"TO '{destination}' (FORMAT PARQUET, COMPRESSION ZSTD)"
            )
        finally:
            db.close()
        os.replace(stage, target)


def project_instruments(layout: StorageLayout, operational_dsn: str) -> dict:
    import psycopg
    from psycopg.rows import dict_row

    if not operational_dsn:
        raise RuntimeError("PROJECT_LADDU_OPERATIONAL_DSN is required")
    query = """
        SELECT provider_instrument_key AS instrument_key,
               exchange,
               trading_symbol,
               display_name,
               isin,
               CASE WHEN asset_class='CASH_EQUITY' THEN 'EQ' ELSE 'INDEX' END AS instrument_type,
               exchange_series AS series,
               lot_size,
               tick_size,
               universe_revision,
               active_from,
               active_to,
               validation_status
          FROM core.instruments
         WHERE validation_status='ACCEPTED'
         ORDER BY exchange,trading_symbol,active_from,universe_revision
    """
    with psycopg.connect(operational_dsn, row_factory=dict_row) as conn:
        history = [dict(row) for row in conn.execute(query).fetchall()]
    if not history:
        raise RuntimeError("Operational PostgreSQL returned no accepted instruments")
    current = [row for row in history if row.get("active_to") is None]
    if not current:
        raise RuntimeError("Operational PostgreSQL returned no active instruments")
    history_target = layout.curated_lake_dir / "instruments" / "point_in_time.parquet"
    current_target = layout.curated_lake_dir / "instruments" / "current.parquet"
    _write_jsonl_parquet(history, history_target)
    _write_jsonl_parquet(current, current_target)
    content_hash = hashlib.sha256(canonical(history).encode("utf-8")).hexdigest()
    return {
        "current_file": current_target,
        "history_file": history_target,
        "current_count": len(current),
        "history_count": len(history),
        "current_equity_keys": sorted({
            str(row.get("instrument_key")) for row in current
            if str(row.get("instrument_type") or "").upper() in {"EQ", "EQUITY"}
        }),
        "content_hash": content_hash,
    }


def project_corporate_actions(layout: StorageLayout, operational_dsn: str, current_equity_keys: list[str]) -> dict:
    """Project verified action factors and explicit coverage attestations.

    Merely having some actions is not enough. ``complete`` becomes true only
    when every active equity has an explicit full-history coverage row and all
    imported action rows are verified.
    """
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(operational_dsn, row_factory=dict_row) as conn:
        actions = [dict(row) for row in conn.execute("""
            SELECT instrument_key,exchange,trading_symbol,isin,ex_date,action_type,
                   price_factor,volume_factor,source_name,source_record_id,
                   source_hash,published_at,verified
              FROM reference.corporate_actions
             ORDER BY instrument_key,ex_date,action_id
        """).fetchall()]
        coverage = [dict(row) for row in conn.execute("""
            SELECT instrument_key,exchange,trading_symbol,coverage_start,coverage_end,
                   coverage_basis,source_name,source_hash,complete,verified_at
              FROM reference.corporate_action_coverage
             ORDER BY instrument_key
        """).fetchall()]
    actions_target = layout.curated_lake_dir / "corporate_actions" / "actions.parquet"
    coverage_target = layout.curated_lake_dir / "corporate_actions" / "coverage.parquet"
    if actions:
        _write_jsonl_parquet(actions, actions_target)
    else:
        actions_target.unlink(missing_ok=True)
    if coverage:
        _write_jsonl_parquet(coverage, coverage_target)
    else:
        coverage_target.unlink(missing_ok=True)
    complete_keys = {str(row.get("instrument_key")) for row in coverage if row.get("complete") is True}
    expected_keys = {str(value) for value in current_equity_keys if value}
    all_actions_verified = all(row.get("verified") is True for row in actions)
    coverage_complete = bool(expected_keys) and expected_keys.issubset(complete_keys) and all_actions_verified
    content_hash = hashlib.sha256(canonical({"actions": actions, "coverage": coverage}).encode("utf-8")).hexdigest()
    return {
        "actions_file": actions_target if actions_target.is_file() else None,
        "coverage_file": coverage_target if coverage_target.is_file() else None,
        "action_count": len(actions),
        "coverage_count": len(coverage),
        "expected_equities": len(expected_keys),
        "complete_equities": len(expected_keys.intersection(complete_keys)),
        "all_actions_verified": all_actions_verified,
        "coverage_complete": coverage_complete,
        "content_hash": content_hash,
        "adjustment_authority": "CorporateActionAdjustmentAuthority",
        "adjustment_authority_version": "1.0.0",
    }


def _metadata_rows(db, paths: list[Path]) -> int | None:
    if not paths:
        return 0
    try:
        row = db.execute(
            f"SELECT SUM(num_rows) FROM parquet_file_metadata({sql_list(paths)})"
        ).fetchone()
        return int(row[0] or 0) if row else 0
    except Exception:
        return None



RESEARCH_PANEL_VERSION = "research-delivery-training-panel-1.2.0-pl42-row-scoped-corporate-actions"


def _materialize_delivery_training_panel(db, *, views: set[str]) -> dict:
    """Materialize one bounded, reusable Delivery research panel.

    The source lake remains complete.  This table is a *research-compute* projection
    that keeps every eligible historical daily row across the complete cross-section.
    Scanner, selector and production universe authorities are not changed by this
    projection; the materialization only removes repeated multi-million-row joins.

    Identity is point-in-time where an effective-dated row overlaps the candle and
    falls back row-by-row to the current catalogue only when historical identity is
    unavailable.  The fallback is explicitly recorded so downstream qualification
    stays fail-closed on survivorship control.
    """
    required = {"curated_candles", "current_instruments", "point_in_time_security_master"}
    if not required.issubset(views):
        raise RuntimeError("RESEARCH_PANEL_SOURCE_MISSING:" + ",".join(sorted(required - views)))
    candles = "curated_adjusted_candles" if "curated_adjusted_candles" in views else "curated_candles"
    corporate_cols = (
        "TRY_CAST(c.corporate_action_adjusted AS BOOLEAN) AS corporate_action_adjusted, "
        "CAST(c.corporate_action_coverage_state AS VARCHAR) AS corporate_action_coverage_state, "
        "CAST(c.corporate_action_coverage_basis AS VARCHAR) AS corporate_action_coverage_basis, "
        "CAST(c.corporate_action_coverage_hash AS VARCHAR) AS corporate_action_coverage_hash,"
        if candles == "curated_adjusted_candles" else
        "FALSE AS corporate_action_adjusted, 'COVERAGE_UNVERIFIED' AS corporate_action_coverage_state, "
        "NULL::VARCHAR AS corporate_action_coverage_basis, NULL::VARCHAR AS corporate_action_coverage_hash,"
    )
    pit_join = """
      LEFT JOIN point_in_time_security_master pit
        ON pit.instrument_key=c.instrument_key
       AND TRY_CAST(c.ts AS TIMESTAMP) >= COALESCE(TRY_CAST(pit.active_from AS TIMESTAMP), TIMESTAMP '1900-01-01')
       AND (pit.active_to IS NULL OR TRY_CAST(c.ts AS TIMESTAMP) < TRY_CAST(pit.active_to AS TIMESTAMP))
    """ if "point_in_time_security_master" in views else ""
    pit_symbol = "UPPER(pit.trading_symbol)" if "point_in_time_security_master" in views else "NULL"
    pit_type = "UPPER(pit.instrument_type)" if "point_in_time_security_master" in views else "NULL"
    pit_active = "TRY_CAST(pit.active_from AS TIMESTAMP)" if "point_in_time_security_master" in views else "NULL"
    pit_present = "pit.instrument_key IS NOT NULL" if "point_in_time_security_master" in views else "FALSE"
    # Historical identity labels are allowed to come from any retained accepted
    # instrument row, but they never decide membership. Membership is proven by
    # the exact-date PIT row or by the canonical daily candle observation itself.
    historical_identity_cte = """
      historical_identity AS (
        SELECT * EXCLUDE(rn) FROM (
          SELECT instrument_key,trading_symbol,instrument_type,
                 row_number() OVER (
                   PARTITION BY instrument_key
                   ORDER BY active_to DESC NULLS FIRST, active_from DESC NULLS LAST, universe_revision DESC NULLS LAST
                 ) AS rn
            FROM point_in_time_security_master
        ) WHERE rn=1
      ),
    """

    delivery_join = """
      LEFT JOIN curated_delivery d
        ON UPPER(CAST(d.symbol AS VARCHAR))=x.symbol
       AND TRY_CAST(d.trade_date AS DATE)=x.date
    """ if "curated_delivery" in views else ""
    delivery_cols = """
      TRY_CAST(d.traded_qty AS DOUBLE) AS traded_qty,
      TRY_CAST(d.deliverable_qty AS DOUBLE) AS deliverable_qty,
      TRY_CAST(d.delivery_pct AS DOUBLE) AS delivery_pct,
    """ if "curated_delivery" in views else """
      NULL::DOUBLE AS traded_qty,NULL::DOUBLE AS deliverable_qty,NULL::DOUBLE AS delivery_pct,
    """

    official_join = """
      LEFT JOIN curated_nse_daily_features n
        ON UPPER(CAST(n.symbol AS VARCHAR))=x.symbol
       AND TRY_CAST(n.trade_date AS DATE)=x.date
    """ if "curated_nse_daily_features" in views else ""
    official_cols = "n.* EXCLUDE(trade_date,symbol)," if "curated_nse_daily_features" in views else ""
    liquidity_expr = (
        "COALESCE(TRY_CAST(n.nse_turnover AS DOUBLE), x.close * x.volume, 0.0)"
        if "curated_nse_daily_features" in views else "COALESCE(x.close * x.volume,0.0)"
    )

    _drop_relation(db, "research_delivery_training_panel")
    db.execute(f"""
        CREATE TABLE research_delivery_training_panel AS
        WITH {historical_identity_cte}
        identity_candidates AS (
          SELECT TRY_CAST(c.ts AS DATE) AS date,
                 COALESCE({pit_symbol}, UPPER(hist.trading_symbol), UPPER(cur.trading_symbol), UPPER(CAST(c.instrument_key AS VARCHAR))) AS symbol,
                 c.instrument_key,
                 TRY_CAST(c.open AS DOUBLE) AS open,
                 TRY_CAST(c.high AS DOUBLE) AS high,
                 TRY_CAST(c.low AS DOUBLE) AS low,
                 TRY_CAST(c.close AS DOUBLE) AS close,
                 TRY_CAST(c.volume AS DOUBLE) AS volume,
                 TRY_CAST(c.oi AS DOUBLE) AS oi,
                 CASE WHEN {pit_present} THEN 'POINT_IN_TIME_SECURITY_MASTER'
                      ELSE 'CANONICAL_DAILY_CANDLE_OBSERVED_MEMBERSHIP' END AS universe_join_authority,
                 row_number() OVER (
                   PARTITION BY c.instrument_key,TRY_CAST(c.ts AS DATE)
                   ORDER BY {pit_active} DESC NULLS LAST,
                            TRY_CAST(c.received_at AS TIMESTAMPTZ) DESC NULLS LAST,
                            TRY_CAST(c.ts AS TIMESTAMP) DESC NULLS LAST
                 ) AS rn
            FROM {candles} c
            LEFT JOIN current_instruments cur ON cur.instrument_key=c.instrument_key
            LEFT JOIN historical_identity hist ON hist.instrument_key=c.instrument_key
            {pit_join}
           WHERE LOWER(CAST(c.interval AS VARCHAR)) IN ('1d','day','1day')
             AND c.close IS NOT NULL
             AND TRY_CAST(c.ts AS DATE) IS NOT NULL
             AND UPPER(CAST(c.instrument_key AS VARCHAR)) LIKE 'NSE_EQ|%'
             AND COALESCE({pit_type},UPPER(hist.instrument_type),UPPER(cur.instrument_type),'EQ') IN ('EQ','EQUITY')
        ), x AS (
          SELECT * EXCLUDE(rn) FROM identity_candidates WHERE rn=1
        )
        SELECT x.date,x.symbol,x.instrument_key,x.open,x.high,x.low,x.close,x.volume,x.oi,
               x.corporate_action_adjusted,x.corporate_action_coverage_state,
               x.corporate_action_coverage_basis,x.corporate_action_coverage_hash,
               x.universe_join_authority,
               {delivery_cols}
               {official_cols}
               {liquidity_expr} AS research_liquidity_value
          FROM x
          {delivery_join}
          {official_join}
    """)
    row = db.execute("""
        SELECT count(*) AS rows,
               count(DISTINCT symbol) AS symbols,
               count(DISTINCT date) AS dates,
               CAST(min(date) AS VARCHAR) AS start_date,
               CAST(max(date) AS VARCHAR) AS end_date,
               sum(CASE WHEN universe_join_authority='POINT_IN_TIME_SECURITY_MASTER' THEN 1 ELSE 0 END) AS pit_rows,
               sum(CASE WHEN universe_join_authority='CURRENT_INSTRUMENTS_SHADOW_FALLBACK' THEN 1 ELSE 0 END) AS fallback_rows,
               sum(CASE WHEN universe_join_authority='CANONICAL_DAILY_CANDLE_OBSERVED_MEMBERSHIP' THEN 1 ELSE 0 END) AS canonical_observed_membership_rows,
               sum(CASE WHEN corporate_action_adjusted IS TRUE THEN 1 ELSE 0 END) AS corporate_action_qualified_rows,
               max(count_per_date) AS max_names_per_date
          FROM (
            SELECT *,count(*) OVER(PARTITION BY date) AS count_per_date
              FROM research_delivery_training_panel
          ) t
    """).fetchone()
    result = {
        "version": RESEARCH_PANEL_VERSION,
        "rows": int(row[0] or 0), "symbols": int(row[1] or 0), "dates": int(row[2] or 0),
        "start": str(row[3])[:10] if row[3] else None, "end": str(row[4])[:10] if row[4] else None,
        "point_in_time_rows": int(row[5] or 0), "current_fallback_rows": int(row[6] or 0),
        "canonical_observed_membership_rows": int(row[7] or 0),
        "corporate_action_qualified_rows": int(row[8] or 0),
        "max_names_per_date": int(row[9] or 0),
        "survivorship_policy": "PIT_OR_POSITIVE_CANONICAL_DAILY_MEMBERSHIP_NO_CURRENT_UNIVERSE_FILTER",
        "universe_policy": "FULL_TEMPORAL_DEPTH_FULL_CROSS_SECTION_RESEARCH_COMPUTE_ONLY",
        "production_influence": 0,
    }
    if result["rows"] <= 0 or result["dates"] <= 0:
        raise RuntimeError("RESEARCH_PANEL_MATERIALIZATION_EMPTY")
    return result

def _metadata_time_bounds(db, paths: list[Path]) -> tuple[str | None, str | None]:
    if not paths:
        return None, None
    try:
        row = db.execute(f"""
            SELECT CAST(min(TRY_CAST(stats_min AS TIMESTAMP)) AS VARCHAR),
                   CAST(max(TRY_CAST(stats_max AS TIMESTAMP)) AS VARCHAR)
              FROM parquet_metadata({sql_list(paths)})
             WHERE lower(path_in_schema)='ts'
        """).fetchone()
        return (str(row[0])[:10] if row and row[0] else None,
                str(row[1])[:10] if row and row[1] else None)
    except Exception:
        return None, None


def refresh(data_dir: Path, operational_dsn: str, *, lock_wait_seconds: float = 1.0) -> dict:
    import duckdb

    layout = StorageLayout.from_data_dir(Path(data_dir))
    layout.ensure()
    candle_files = sorted((layout.curated_lake_dir / "candles").glob("timeframe=*/year=*/*.parquet"))
    delivery_files = sorted((layout.data_dir / "parquet" / "delivery").glob("trade_date=*/part-*.parquet"))
    nse_official_files = sorted((layout.curated_lake_dir / "nse_official").glob("*/trade_date=*/*.parquet"))
    if not candle_files:
        raise RuntimeError("Direct candle Parquet authority is empty")

    with interprocess_lock(layout.locks_dir / "research-catalog.lock", timeout_seconds=max(0.0, float(lock_wait_seconds))):
        with interprocess_lock(layout.locks_dir / "analytical-pipeline.lock", timeout_seconds=3600.0):
            instruments = project_instruments(layout, operational_dsn)
            corporate_actions = project_corporate_actions(
                layout, operational_dsn, instruments["current_equity_keys"]
            )
            source_catalogue = {
                "candles": file_catalogue(candle_files, layout.data_dir),
                "delivery": file_catalogue(delivery_files, layout.data_dir),
                "nse_official": file_catalogue(nse_official_files, layout.data_dir),
                "instrument_content_hash": instruments["content_hash"],
                "corporate_action_content_hash": corporate_actions["content_hash"],
                "catalog_version": CATALOG_VERSION,
            }
            catalogue_fingerprint = hashlib.sha256(canonical(source_catalogue).encode("utf-8")).hexdigest()
            manifest_path = layout.manifests_dir / "market-lake.json"
            try:
                prior = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                prior = {}
            if layout.analytics_db.is_file() and prior.get("catalogue_fingerprint") == catalogue_fingerprint:
                result = {
                    **prior,
                    **_production_manifest_safety(prior),
                    "ok": True,
                    "state": "RESEARCH_CATALOG_CURRENT",
                    "last_checked": now(),
                    "full_parquet_scan_performed": False,
                }
                atomic_write_json(manifest_path, result)
                return result

            layout.analytics_db.parent.mkdir(parents=True, exist_ok=True)
            # PL46 lock-contention fix: the parent service process may still hold a
            # short-lived read-only handle on this same file (curated reads close
            # per-call, but a request can be in flight at the exact moment this
            # writer starts). DuckDB requires exclusive access to open in write
            # mode, so on Windows that in-flight read surfaces as a hard
            # "file is being used by another process" IOException with no
            # automatic retry from the caller. Absorb that as a transient
            # condition instead of failing the whole catalogue refresh outright.
            db = None
            _connect_attempts = 5
            _connect_backoff_seconds = 1.5
            for _attempt in range(1, _connect_attempts + 1):
                try:
                    db = duckdb.connect(str(layout.analytics_db))
                    break
                except Exception as exc:
                    if _attempt >= _connect_attempts:
                        raise
                    _looks_like_lock_conflict = (
                        "being used by another process" in str(exc)
                        or "Conflicting lock" in str(exc)
                        or "Could not set lock" in str(exc)
                    )
                    if not _looks_like_lock_conflict:
                        raise
                    time.sleep(_connect_backoff_seconds * _attempt)
            try:
                db.execute("BEGIN TRANSACTION")
                _drop_relation(db, "curated_candles")
                db.execute(f"""
                    CREATE VIEW curated_candles AS
                    SELECT * EXCLUDE(rn,filename) FROM (
                      SELECT *, row_number() OVER (
                        PARTITION BY instrument_key,interval,ts
                        ORDER BY TRY_CAST(received_at AS TIMESTAMPTZ) DESC NULLS LAST, filename DESC
                      ) AS rn
                      FROM read_parquet({sql_list(candle_files)}, union_by_name=true, filename=true)
                    ) WHERE rn=1
                """)
                if delivery_files:
                    _drop_relation(db, "curated_delivery")
                    db.execute(f"""
                        CREATE VIEW curated_delivery AS
                        SELECT * EXCLUDE(rn,filename) FROM (
                          SELECT *, row_number() OVER (
                            PARTITION BY CAST(trade_date AS VARCHAR),upper(CAST(symbol AS VARCHAR))
                            ORDER BY TRY_CAST(published_at AS TIMESTAMPTZ) DESC NULLS LAST, filename DESC
                          ) AS rn
                          FROM read_parquet({sql_list(delivery_files)}, union_by_name=true, filename=true)
                        ) WHERE rn=1
                    """)
                else:
                    _drop_relation(db, "curated_delivery")
                if nse_official_files:
                    _drop_relation(db, "curated_nse_official_reports")
                    normalized_official_sql = normalized_nse_official_union_sql(db, nse_official_files)
                    db.execute(f"""
                        CREATE VIEW curated_nse_official_reports AS
                        SELECT * EXCLUDE(rn,filename) FROM (
                          SELECT *, row_number() OVER (
                            PARTITION BY source_key,CAST(trade_date AS VARCHAR),upper(CAST(symbol AS VARCHAR)),COALESCE(CAST(source_record_id AS VARCHAR),'')
                            ORDER BY TRY_CAST(published_at AS TIMESTAMPTZ) DESC NULLS LAST, filename DESC
                          ) AS rn
                           FROM ({normalized_official_sql})
                        ) WHERE rn=1
                    """)
                    _drop_relation(db, "curated_nse_daily_features")
                    db.execute("""
                        CREATE VIEW curated_nse_daily_features AS
                        SELECT CAST(trade_date AS VARCHAR) AS trade_date,
                               upper(CAST(symbol AS VARCHAR)) AS symbol,
                               max(TRY_CAST(turnover AS DOUBLE)) FILTER (WHERE TRY_CAST(source_key AS VARCHAR)='cm_udiff_bhavcopy') AS nse_turnover,
                               max(TRY_CAST(number_of_trades AS DOUBLE)) FILTER (WHERE TRY_CAST(source_key AS VARCHAR)='cm_udiff_bhavcopy') AS nse_number_of_trades,
                               max(TRY_CAST(delivery_pct AS DOUBLE)) FILTER (WHERE TRY_CAST(source_key AS VARCHAR)='security_delivery_positions') AS nse_delivery_pct,
                               max(TRY_CAST(daily_volatility AS DOUBLE)) FILTER (WHERE TRY_CAST(source_key AS VARCHAR)='daily_volatility_var_price_band') AS nse_daily_volatility,
                               max(TRY_CAST(var_margin AS DOUBLE)) FILTER (WHERE TRY_CAST(source_key AS VARCHAR)='daily_volatility_var_price_band') AS nse_var_margin,
                               max(TRY_CAST(impact_cost AS DOUBLE)) FILTER (WHERE TRY_CAST(source_key AS VARCHAR)='daily_volatility_var_price_band') AS nse_impact_cost,
                               max(TRY_CAST(price_band_low AS DOUBLE)) FILTER (WHERE TRY_CAST(source_key AS VARCHAR)='daily_volatility_var_price_band') AS nse_price_band_low,
                               max(TRY_CAST(price_band_high AS DOUBLE)) FILTER (WHERE TRY_CAST(source_key AS VARCHAR)='daily_volatility_var_price_band') AS nse_price_band_high,
                               max(TRY_CAST(index_weight AS DOUBLE)) FILTER (WHERE TRY_CAST(source_key AS VARCHAR)='index_snapshot_constituents_weights') AS nse_index_weight,
                               max(TRY_CAST(beta AS DOUBLE)) FILTER (WHERE TRY_CAST(source_key AS VARCHAR)='index_snapshot_constituents_weights') AS nse_beta,
                               max(TRY_CAST(market_cap AS DOUBLE)) FILTER (WHERE TRY_CAST(source_key AS VARCHAR)='index_snapshot_constituents_weights') AS nse_market_cap,
                               max(TRY_CAST(free_float_market_cap AS DOUBLE)) FILTER (WHERE TRY_CAST(source_key AS VARCHAR)='index_snapshot_constituents_weights') AS nse_free_float_market_cap,
                               sum(COALESCE(TRY_CAST(bulk_qty AS DOUBLE),0)) FILTER (WHERE TRY_CAST(source_key AS VARCHAR)='bulk_block_short_margin') AS nse_bulk_qty,
                               sum(COALESCE(TRY_CAST(block_qty AS DOUBLE),0)) FILTER (WHERE TRY_CAST(source_key AS VARCHAR)='bulk_block_short_margin') AS nse_block_qty,
                               sum(COALESCE(TRY_CAST(short_qty AS DOUBLE),0)) FILTER (WHERE TRY_CAST(source_key AS VARCHAR)='bulk_block_short_margin') AS nse_short_qty,
                               sum(COALESCE(TRY_CAST(margin_qty AS DOUBLE),0)) FILTER (WHERE TRY_CAST(source_key AS VARCHAR)='bulk_block_short_margin') AS nse_margin_qty,
                               sum(CASE WHEN TRY_CAST(source_key AS VARCHAR)='bulk_block_short_margin' AND upper(COALESCE(TRY_CAST(deal_side AS VARCHAR),'')) IN ('BUY','B') THEN COALESCE(TRY_CAST(bulk_qty AS DOUBLE),TRY_CAST(block_qty AS DOUBLE),0)
                                        WHEN TRY_CAST(source_key AS VARCHAR)='bulk_block_short_margin' AND upper(COALESCE(TRY_CAST(deal_side AS VARCHAR),'')) IN ('SELL','S') THEN -COALESCE(TRY_CAST(bulk_qty AS DOUBLE),TRY_CAST(block_qty AS DOUBLE),0)
                                        ELSE 0 END) AS nse_signed_deal_qty,
                               max(CASE WHEN TRY_CAST(source_key AS VARCHAR)='surveillance_52w_price_band_changes' AND
                                             (TRY_CAST(surveillance_flag AS VARCHAR) IS NOT NULL OR TRY_CAST(surveillance_category AS VARCHAR) IS NOT NULL) THEN 1 ELSE 0 END) AS nse_surveillance_flag,
                               max(TRY_CAST(high_52w AS DOUBLE)) FILTER (WHERE TRY_CAST(source_key AS VARCHAR)='surveillance_52w_price_band_changes') AS nse_high_52w,
                               min(TRY_CAST(low_52w AS DOUBLE)) FILTER (WHERE TRY_CAST(source_key AS VARCHAR)='surveillance_52w_price_band_changes') AS nse_low_52w,
                               max(TRY_CAST(price_band_change_pct AS DOUBLE)) FILTER (WHERE TRY_CAST(source_key AS VARCHAR)='surveillance_52w_price_band_changes') AS nse_price_band_change_pct,
                               max(TRY_CAST(revenue AS DOUBLE)) FILTER (WHERE TRY_CAST(source_key AS VARCHAR)='filings_results_announcements_shareholding') AS nse_revenue,
                               max(TRY_CAST(ebitda AS DOUBLE)) FILTER (WHERE TRY_CAST(source_key AS VARCHAR)='filings_results_announcements_shareholding') AS nse_ebitda,
                               max(TRY_CAST(net_profit AS DOUBLE)) FILTER (WHERE TRY_CAST(source_key AS VARCHAR)='filings_results_announcements_shareholding') AS nse_net_profit,
                               max(TRY_CAST(eps AS DOUBLE)) FILTER (WHERE TRY_CAST(source_key AS VARCHAR)='filings_results_announcements_shareholding') AS nse_eps,
                               max(TRY_CAST(promoter_holding_pct AS DOUBLE)) FILTER (WHERE TRY_CAST(source_key AS VARCHAR)='filings_results_announcements_shareholding') AS nse_promoter_holding_pct,
                               max(TRY_CAST(fii_holding_pct AS DOUBLE)) FILTER (WHERE TRY_CAST(source_key AS VARCHAR)='filings_results_announcements_shareholding') AS nse_fii_holding_pct,
                               max(TRY_CAST(dii_holding_pct AS DOUBLE)) FILTER (WHERE TRY_CAST(source_key AS VARCHAR)='filings_results_announcements_shareholding') AS nse_dii_holding_pct,
                               max(TRY_CAST(ownership_change_pct AS DOUBLE)) FILTER (WHERE TRY_CAST(source_key AS VARCHAR)='filings_results_announcements_shareholding') AS nse_ownership_change_pct,
                               max(CASE WHEN TRY_CAST(source_key AS VARCHAR)='cm_udiff_bhavcopy' THEN 1 ELSE 0 END) AS nse_has_bhavcopy,
                               max(CASE WHEN TRY_CAST(source_key AS VARCHAR)='security_delivery_positions' THEN 1 ELSE 0 END) AS nse_has_delivery,
                               max(CASE WHEN TRY_CAST(source_key AS VARCHAR)='mii_security_file' THEN 1 ELSE 0 END) AS nse_has_security_master,
                               max(CASE WHEN TRY_CAST(source_key AS VARCHAR)='daily_volatility_var_price_band' THEN 1 ELSE 0 END) AS nse_has_risk,
                               max(CASE WHEN TRY_CAST(source_key AS VARCHAR)='index_snapshot_constituents_weights' THEN 1 ELSE 0 END) AS nse_has_index_context,
                               max(CASE WHEN TRY_CAST(source_key AS VARCHAR)='bulk_block_short_margin' THEN 1 ELSE 0 END) AS nse_has_deal_events,
                               max(CASE WHEN TRY_CAST(source_key AS VARCHAR)='corporate_actions' THEN 1 ELSE 0 END) AS nse_has_corporate_action,
                               max(CASE WHEN TRY_CAST(source_key AS VARCHAR)='filings_results_announcements_shareholding' THEN 1 ELSE 0 END) AS nse_has_filings,
                               max(CASE WHEN TRY_CAST(source_key AS VARCHAR)='surveillance_52w_price_band_changes' THEN 1 ELSE 0 END) AS nse_has_surveillance,
                               count(DISTINCT TRY_CAST(source_key AS VARCHAR)) AS nse_source_family_count,
                               string_agg(DISTINCT TRY_CAST(content_hash AS VARCHAR), ',' ORDER BY TRY_CAST(content_hash AS VARCHAR)) AS nse_source_lineage
                          FROM curated_nse_official_reports
                         WHERE symbol IS NOT NULL AND trim(CAST(symbol AS VARCHAR))<>''
                         GROUP BY 1,2
                    """)
                else:
                    _drop_relation(db, "curated_nse_daily_features")
                    _drop_relation(db, "curated_nse_official_reports")
                current_sql = str(Path(instruments["current_file"]).resolve()).replace("\\", "/").replace("'", "''")
                history_sql = str(Path(instruments["history_file"]).resolve()).replace("\\", "/").replace("'", "''")
                _drop_relation(db, "current_instruments")
                _drop_relation(db, "point_in_time_security_master")
                db.execute(f"CREATE VIEW current_instruments AS SELECT * FROM read_parquet('{current_sql}', union_by_name=true)")
                db.execute(f"CREATE VIEW point_in_time_security_master AS SELECT * FROM read_parquet('{history_sql}', union_by_name=true)")
                if corporate_actions["actions_file"]:
                    _drop_relation(db, "corporate_actions")
                    action_sql = str(Path(corporate_actions["actions_file"]).resolve()).replace("\\", "/").replace("'", "''")
                    db.execute(f"CREATE OR REPLACE VIEW corporate_actions AS SELECT * FROM read_parquet('{action_sql}', union_by_name=true)")
                else:
                    _drop_relation(db, "corporate_actions")
                    db.execute("""CREATE TABLE corporate_actions(
                        instrument_key VARCHAR,exchange VARCHAR,trading_symbol VARCHAR,isin VARCHAR,
                        ex_date DATE,action_type VARCHAR,price_factor DOUBLE,volume_factor DOUBLE,
                        source_name VARCHAR,source_record_id VARCHAR,source_hash VARCHAR,
                        published_at TIMESTAMPTZ,verified BOOLEAN
                    )""")
                if corporate_actions["coverage_file"]:
                    _drop_relation(db, "corporate_action_coverage")
                    coverage_sql = str(Path(corporate_actions["coverage_file"]).resolve()).replace("\\", "/").replace("'", "''")
                    db.execute(f"CREATE OR REPLACE VIEW corporate_action_coverage AS SELECT * FROM read_parquet('{coverage_sql}', union_by_name=true)")
                else:
                    _drop_relation(db, "corporate_action_coverage")
                    db.execute("""CREATE TABLE corporate_action_coverage(
                        instrument_key VARCHAR,exchange VARCHAR,trading_symbol VARCHAR,
                        coverage_start DATE,coverage_end DATE,coverage_basis VARCHAR,
                        source_name VARCHAR,source_hash VARCHAR,complete BOOLEAN,verified_at TIMESTAMPTZ
                    )""")
                # PL42: adjustment authority is row-scoped. The view always exists;
                # each candle carries explicit verified coverage. An unresolved stock
                # stays unqualified without blocking unrelated stocks from WFA.
                _drop_relation(db, "curated_adjusted_candles")
                db.execute(
                    "CREATE VIEW curated_adjusted_candles AS "
                    + DEFAULT_CORPORATE_ACTION_ADJUSTMENT_AUTHORITY.duckdb_adjusted_candles_sql()
                )
                current_views = {row[0] for row in db.execute(
                    "SELECT table_name FROM information_schema.tables WHERE table_schema='main'"
                ).fetchall()}
                research_training_panel = _materialize_delivery_training_panel(db, views=current_views)
                candle_start_date, candle_end_date = _metadata_time_bounds(db, candle_files)
                db.execute("CREATE TABLE IF NOT EXISTS research_catalog_meta(key VARCHAR PRIMARY KEY,value VARCHAR,updated_at TIMESTAMP DEFAULT current_timestamp)")
                for key, value in (
                    ("catalog_version", CATALOG_VERSION),
                    ("catalogue_fingerprint", catalogue_fingerprint),
                    ("candle_start_date", candle_start_date or ""),
                    ("candle_end_date", candle_end_date or ""),
                    ("candle_file_count", str(len(candle_files))),
                    ("research_training_panel_version", RESEARCH_PANEL_VERSION),
                    ("research_training_panel_rows", str(research_training_panel.get("rows") or 0)),
                    ("research_training_panel_dates", str(research_training_panel.get("dates") or 0)),
                    ("refreshed_at", now()),
                ):
                    db.execute("INSERT OR REPLACE INTO research_catalog_meta VALUES (?, ?, current_timestamp)", [key, value])
                candle_rows = _metadata_rows(db, candle_files)
                delivery_rows = _metadata_rows(db, delivery_files)
                nse_official_rows = _metadata_rows(db, nse_official_files)
                db.execute("COMMIT")
            except Exception:
                try:
                    db.execute("ROLLBACK")
                except Exception:
                    pass
                raise
            finally:
                db.close()

            manifest = {
                **_production_manifest_safety(prior),
                "ok": True,
                "state": "RESEARCH_CATALOG_REFRESHED",
                "version": CATALOG_VERSION,
                "last_run": now(),
                "catalogue_fingerprint": catalogue_fingerprint,
                "full_parquet_scan_performed": False,
                "sqlite_source": False,
                "source_authorities": {
                    "candles": "DIRECT_PARQUET_FROM_QUESTDB_MARKET_DATA_PIPELINE",
                    "delivery": "DIRECT_PARQUET_DELIVERY_AUTHORITY",
                    "nse_official_reports": "CONTENT_ADDRESSED_OFFICIAL_REPORT_PARQUET",
                    "instruments": "OPERATIONAL_POSTGRESQL_POINT_IN_TIME_HISTORY",
                    "corporate_actions": "VERIFIED_POSTGRESQL_FACTOR_AND_COVERAGE_AUTHORITY",
                    "analytics": "READ_ONLY_DUCKDB_CATALOG",
                },
                "files": {
                    "candles": len(candle_files),
                    "delivery": len(delivery_files),
                    "nse_official_reports": len(nse_official_files),
                    "current_instruments": str(instruments["current_file"]),
                    "point_in_time_security_master": str(instruments["history_file"]),
                    "corporate_actions": str(corporate_actions["actions_file"] or ""),
                    "corporate_action_coverage": str(corporate_actions["coverage_file"] or ""),
                },
                "research_training_panel": research_training_panel,
                "rows": {
                    "candles_from_parquet_metadata": candle_rows,
                    "delivery_from_parquet_metadata": delivery_rows,
                    "nse_official_from_parquet_metadata": nse_official_rows,
                    "current_instruments": instruments["current_count"],
                    "point_in_time_instrument_records": instruments["history_count"],
                    "corporate_actions": corporate_actions["action_count"],
                    "corporate_action_coverage_records": corporate_actions["coverage_count"],
                },
                "corporate_action_authority": corporate_actions,
                "analytics_db": str(layout.analytics_db),
                "candle_date_bounds": {"start": candle_start_date, "end": candle_end_date},
                "training_policy": "INCREMENTAL_FEATURE_STORE_FROM_PARQUET_DUCKDB_ONLY",
                "data_quality_boundary": (
                    "point-in-time security-master history is authoritative; adjusted candles are enabled only when "
                    "every active equity has explicit complete coverage and every imported action is verified"
                ),
            }
            atomic_write_json(manifest_path, manifest)
            return manifest



def _drop_relation(db, name: str) -> None:
    """Drop a DuckDB relation using its actual catalog type."""
    safe = str(name).replace('"', '""')
    row = db.execute(
        "SELECT table_type FROM information_schema.tables WHERE table_schema='main' AND table_name=?",
        [name],
    ).fetchone()
    if not row:
        return
    relation_type = str(row[0] or '').upper()
    if relation_type == 'VIEW':
        db.execute(f'DROP VIEW "{safe}"')
    else:
        db.execute(f'DROP TABLE "{safe}"')

def main() -> int:
    parser = argparse.ArgumentParser(description="Refresh the Project Laddu production-authority research catalogue")
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--operational-dsn", default=os.environ.get("PROJECT_LADDU_OPERATIONAL_DSN", ""))
    parser.add_argument("--lock-wait-seconds", type=float, default=1.0, help="bounded wait for an existing catalogue owner before returning BUSY")
    args = parser.parse_args()
    try:
        result = refresh(args.data_dir, args.operational_dsn.strip(), lock_wait_seconds=args.lock_wait_seconds)
        print(json.dumps(result, indent=2, default=str))
        return 0
    except TimeoutError as exc:
        print(json.dumps({"ok": False, "state": "RESEARCH_CATALOG_BUSY", "error": str(exc)}, indent=2))
        return 3
    except Exception as exc:
        print(json.dumps({"ok": False, "state": "RESEARCH_CATALOG_BLOCKED", "error": f"{type(exc).__name__}: {exc}"}, indent=2))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
