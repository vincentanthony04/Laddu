"""Content-addressed landing and canonicalisation for official NSE reports.

This service is deliberately transport-neutral: a scheduled collector may pass
bytes obtained from an official NSE report endpoint, a licensed EOD feed, or an
operator-supplied file.  The same content hash is never parsed or written twice.
Raw evidence is immutable; curated Parquet is partitioned by source and trade
session, and is consumed later by the read-only DuckDB research catalogue.

A GET/UI request never downloads data and model training never calls a provider.
"""
from __future__ import annotations

import csv
import gzip
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Dict, Iterable, Mapping, Sequence
import zipfile

from core.nse_cash_data_authority_service import SOURCES
from core.storage_layout import StorageLayout, atomic_write_json, interprocess_lock

SERVICE_VERSION = "nse-official-report-ingestion-2.2.0-governed-index-membership"
ALLOWED_SOURCE_KEYS = frozenset(str(row["key"]) for row in SOURCES)

CANONICAL_COLUMNS: tuple[str, ...] = (
    # immutable lineage and point-in-time identity
    "source_key", "trade_date", "source_record_id", "symbol", "series", "isin",
    "instrument_name", "exchange", "published_at", "observed_at",
    # cash facts and participation
    "open", "high", "low", "close", "volume", "turnover", "number_of_trades",
    "traded_qty", "deliverable_qty", "delivery_pct",
    # point-in-time security master
    "listing_status", "eligible_universe", "instrument_status", "listing_date",
    # risk, execution and surveillance
    "daily_volatility", "var_margin", "impact_cost", "price_band_low",
    "price_band_high", "price_band_change_pct", "surveillance_flag",
    "surveillance_category", "high_52w", "low_52w",
    # index / regime authority
    "index_name", "index_weight", "index_return", "market_cap",
    "free_float_market_cap", "beta", "sector_name",
    # deal/event authority
    "participant", "participant_category", "deal_side", "deal_type", "deal_price",
    "counterparty", "bulk_qty", "block_qty", "short_qty", "margin_qty",
    # corporate actions
    "ex_date", "record_date", "action_type", "purpose", "face_value", "price_factor", "volume_factor",
    # point-in-time filings and ownership
    "filing_type", "filing_period", "filing_timestamp", "announcement_category",
    "announcement_text", "revenue", "ebitda", "net_profit", "eps",
    "promoter_holding_pct", "fii_holding_pct", "dii_holding_pct", "ownership_change_pct",
    # source evidence
    "source_filename", "source_url", "content_hash",
)


# NSE has used several report schemas over time. Header aliases are normalised
# instead of binding the product to one spelling or one legacy Bhavcopy format.
ALIASES: dict[str, tuple[str, ...]] = {
    "trade_date": ("trade_date", "trading_date", "date", "date1", "bizdt", "timestamp", "mkt_dt", "as_on_date", "index_date"),
    "symbol": ("symbol", "tckrsymb", "security", "security_symbol", "sec_symbol", "security_id", "secid", "symbol_name"),
    "series": ("series", "sctysrs", "security_series"),
    "isin": ("isin", "isin_code", "isin_number", "isinnumber", "isin_no", "fininstrmid"),
    "instrument_name": ("instrument_name", "security_name", "sec_name", "fininstrmname", "company_name", "name_of_company", "security_description", "comp"),
    "open": ("open", "open_price", "opnpric"),
    "high": ("high", "high_price", "hghpric"),
    "low": ("low", "low_price", "lwpric"),
    "close": ("close", "close_price", "clspric", "last_price", "closing_price", "close_index_value", "closing_index_value"),
    "volume": ("volume", "ttltradgvol", "ttl_trd_qnty", "total_traded_quantity", "traded_volume"),
    "turnover": ("turnover", "ttltrfval", "total_traded_value", "turnover_lacs"),
    "number_of_trades": ("number_of_trades", "no_of_trades", "ttl_nb_trades", "ttl_nb_of_txs", "ttlnboftxsexctd", "ttltradgtrans", "no_of_trade"),
    "traded_qty": ("traded_qty", "traded_quantity", "ttltradgvol", "ttl_trd_qnty", "total_traded_quantity"),
    "deliverable_qty": ("deliverable_qty", "deliverable_quantity", "deliv_qty", "dlyqtto", "delivery_quantity", "deliverable_qty_to_traded_qty", "delivery_quantity_to_traded_quantity"),
    "delivery_pct": ("delivery_pct", "delivery_percentage", "deliv_per", "dlyqtto_traded_qty_pct", "percent_deliverable_qty", "dly_qt_to_traded_qty", "dly_qty_to_traded_qty", "percent_dly_qt_to_traded_qty"),
    "daily_volatility": ("daily_volatility", "volatility", "daily_sigma", "daily_volatility_percent", "daily_vol"),
    "var_margin": ("var_margin", "var", "value_at_risk"),
    "impact_cost": ("impact_cost", "impactcost"),
    "price_band_low": ("price_band_low", "lower_price_band", "lower_circuit", "lower_price_band_value", "lower_band"),
    "price_band_high": ("price_band_high", "upper_price_band", "upper_circuit", "upper_price_band_value", "upper_band"),
    "index_name": ("index_name", "index", "index_symbol", "index_name_", "name"),
    "index_weight": ("index_weight", "weight", "weightage"),
    "beta": ("beta", "security_beta"),
    "bulk_qty": ("bulk_qty", "bulk_quantity", "quantity_traded"),
    "block_qty": ("block_qty", "block_quantity"),
    "short_qty": ("short_qty", "short_selling_quantity", "short_quantity"),
    "margin_qty": ("margin_qty", "margin_trading_quantity", "mtf_quantity"),
    "surveillance_flag": ("surveillance_flag", "surveillance_indicator", "asm_gsm_indicator"),
    "high_52w": ("high_52w", "52_week_high", "week_52_high", "52_week_high_price", "week_high_price", "52w_h", "52w_high"),
    "low_52w": ("low_52w", "52_week_low", "week_52_low", "52_week_low_price", "week_low_price", "52w_l", "52w_low"),
    "published_at": ("published_at", "broadcast_date_time", "filing_timestamp", "updated_at"),
    "source_record_id": ("source_record_id", "record_id", "event_id", "announcement_id", "sr_no", "serial_no"),
    "exchange": ("exchange", "exchange_code", "exchng"),
    "observed_at": ("observed_at", "effective_at", "as_of", "timestamp"),
    "listing_status": ("listing_status", "status", "security_status", "listingstatus", "tradgstatus", "trading_status", "security_status_in_normal_market"),
    "eligible_universe": ("eligible_universe", "eligible", "equity_eligible", "is_eligible"),
    "instrument_status": ("instrument_status", "instrument_state", "security_state"),
    "listing_date": ("listing_date", "date_of_listing"),
    "price_band_change_pct": ("price_band_change_pct", "price_band_change", "band_change_pct"),
    "surveillance_category": ("surveillance_category", "asm_category", "gsm_category", "surveillance_stage"),
    "index_return": ("index_return", "return", "index_change_pct", "change_percent", "change", "percent_change", "perc_change"),
    "market_cap": ("market_cap", "market_capitalisation", "market_capitalization"),
    "free_float_market_cap": ("free_float_market_cap", "free_float_mcap", "ff_market_cap"),
    "sector_name": ("sector_name", "sector", "industry"),
    "participant": ("participant", "client_name", "buyer_seller_name", "name_of_client"),
    "participant_category": ("participant_category", "client_category", "category"),
    "deal_side": ("deal_side", "buy_sell", "buy_sell_indicator", "side"),
    "deal_type": ("deal_type", "transaction_type", "deal_category"),
    "deal_price": ("deal_price", "trade_price", "price"),
    "counterparty": ("counterparty", "counter_party"),
    "ex_date": ("ex_date", "exdate"),
    "record_date": ("record_date", "recorddate", "recdate", "rec_date"),
    "action_type": ("action_type", "corporate_action", "purpose_type"),
    "purpose": ("purpose", "subject", "description"),
    "face_value": ("face_value", "faceval", "face_val", "fv"),
    "price_factor": ("price_factor", "adjustment_factor", "price_adjustment_factor"),
    "volume_factor": ("volume_factor", "quantity_factor", "volume_adjustment_factor"),
    "filing_type": ("filing_type", "document_type", "submission_type"),
    "filing_period": ("filing_period", "period", "financial_period", "quarter"),
    "filing_timestamp": ("filing_timestamp", "broadcast_date_time", "submitted_at"),
    "announcement_category": ("announcement_category", "category", "subject"),
    "announcement_text": ("announcement_text", "announcement", "details", "description"),
    "revenue": ("revenue", "total_income", "sales"),
    "ebitda": ("ebitda", "operating_profit"),
    "net_profit": ("net_profit", "profit_after_tax", "pat"),
    "eps": ("eps", "earnings_per_share"),
    "promoter_holding_pct": ("promoter_holding_pct", "promoter_holding", "promoter_pct"),
    "fii_holding_pct": ("fii_holding_pct", "fpi_holding_pct", "foreign_institutional_holding"),
    "dii_holding_pct": ("dii_holding_pct", "domestic_institutional_holding"),
    "ownership_change_pct": ("ownership_change_pct", "holding_change_pct", "change_in_holding"),
}

NUMERIC_COLUMNS = frozenset({
    "open", "high", "low", "close", "volume", "turnover", "number_of_trades",
    "traded_qty", "deliverable_qty", "delivery_pct", "daily_volatility", "var_margin",
    "impact_cost", "price_band_low", "price_band_high", "price_band_change_pct",
    "index_weight", "index_return", "market_cap", "free_float_market_cap", "beta",
    "deal_price", "bulk_qty", "block_qty", "short_qty", "margin_qty",
    "face_value", "price_factor", "volume_factor", "revenue", "ebitda", "net_profit", "eps",
    "promoter_holding_pct", "fii_holding_pct", "dii_holding_pct", "ownership_change_pct",
    "high_52w", "low_52w",
})

SOURCE_REQUIRED_GROUPS: dict[str, tuple[tuple[str, ...], ...]] = {
    "cm_udiff_bhavcopy": (("symbol",), ("close",)),
    "security_delivery_positions": (("symbol",), ("delivery_pct", "deliverable_qty")),
    "mii_security_file": (("symbol",), ("isin", "listing_status")),
    "daily_volatility_var_price_band": (("symbol",), ("daily_volatility", "var_margin", "impact_cost", "price_band_low", "price_band_high")),
    "index_constituents": (("symbol",), ("index_name",)),
    "index_snapshot_constituents_weights": (("symbol", "index_name"), ("index_weight", "market_cap", "beta", "index_return")),
    "bulk_block_short_margin": (("symbol",), ("bulk_qty", "block_qty", "short_qty", "margin_qty", "deal_type")),
    "corporate_actions": (("symbol",), ("ex_date",), ("action_type", "purpose")),
    "filings_results_announcements_shareholding": (("symbol",), ("filing_timestamp", "published_at", "trade_date"), ("filing_type", "announcement_text", "revenue", "net_profit", "promoter_holding_pct")),
    "surveillance_52w_price_band_changes": (("symbol",), ("surveillance_flag", "surveillance_category", "high_52w", "low_52w", "price_band_change_pct")),
}


def _source_schema_status(source_key: str, rows: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    groups = SOURCE_REQUIRED_GROUPS.get(source_key, ())
    missing_groups: list[list[str]] = []
    for group in groups:
        if not any(any(row.get(field) not in (None, "") for field in group) for row in rows):
            missing_groups.append(list(group))
    field_coverage = {
        field: sum(1 for row in rows if row.get(field) not in (None, ""))
        for field in CANONICAL_COLUMNS
        if any(row.get(field) not in (None, "") for row in rows)
    }
    return {
        "state": "VALID" if not missing_groups else "SCHEMA_INCOMPLETE",
        "required_groups": [list(group) for group in groups],
        "missing_required_groups": missing_groups,
        "field_coverage": field_coverage,
    }



def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower()).strip("_")


def _normalise_header(value: str) -> str:
    return _slug(value).replace("__", "_")


def _text(value: Any) -> str | None:
    raw = str(value or "").strip()
    return raw if raw and raw.lower() not in {"na", "n/a", "null", "none", "-", "--"} else None


def _number(value: Any) -> float | None:
    raw = _text(value)
    if raw is None:
        return None
    raw = raw.replace(",", "").replace("%", "").replace("₹", "").strip()
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _date(value: Any, fallback: str) -> str:
    raw = _text(value)
    if not raw:
        return fallback
    raw = raw[:19]
    for pattern in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y%m%d", "%d-%b-%Y", "%d %b %Y"):
        try:
            return datetime.strptime(raw[:11].strip(), pattern).date().isoformat()
        except ValueError:
            pass
    match = re.search(r"(20\d{2})[-/]?(\d{2})[-/]?(\d{2})", raw)
    if match:
        return f"{match.group(1)}-{match.group(2)}-{match.group(3)}"
    return fallback


def _csv_payloads(payload: bytes, filename: str) -> list[tuple[str, bytes]]:
    lower_name = str(filename or "").lower()
    if payload[:2] == b"\x1f\x8b" or lower_name.endswith(".gz"):
        try:
            expanded = gzip.decompress(payload)
        except (OSError, EOFError) as exc:
            raise ValueError("Invalid GZIP NSE report") from exc
        member = Path(filename[:-3] if lower_name.endswith(".gz") else filename + ".csv").name
        return [(member, expanded)]
    if zipfile.is_zipfile(io.BytesIO(payload)):
        output: list[tuple[str, bytes]] = []
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            for name in sorted(archive.namelist()):
                if name.lower().endswith((".csv", ".txt")) and not name.endswith("/"):
                    output.append((Path(name).name, archive.read(name)))
        if not output:
            raise ValueError("ZIP contains no CSV/TXT report")
        return output
    return [(filename, payload)]


def _decode(data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _find_value(row: Mapping[str, Any], canonical: str) -> Any:
    for alias in ALIASES.get(canonical, (canonical,)):
        if alias in row and _text(row.get(alias)) is not None:
            return row.get(alias)
    return None


def canonicalise_rows(
    source_key: str,
    trade_date: str,
    payload: bytes,
    filename: str,
    content_hash: str,
    source_url: str | None = None,
    source_metadata: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for member_name, member_payload in _csv_payloads(payload, filename):
        text = _decode(member_payload)
        sample = text[:8192]
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",|;\t")
        except csv.Error:
            dialect = csv.excel
        reader = csv.DictReader(io.StringIO(text), dialect=dialect)
        if not reader.fieldnames:
            continue
        for raw in reader:
            normalised = {_normalise_header(key): value for key, value in raw.items() if key is not None}
            symbol = _text(_find_value(normalised, "symbol"))
            if symbol:
                symbol = symbol.upper()
            canonical: dict[str, Any] = {column: None for column in CANONICAL_COLUMNS}
            canonical.update({
                "source_key": source_key,
                "trade_date": _date(_find_value(normalised, "trade_date"), trade_date),
                "source_record_id": _text(_find_value(normalised, "source_record_id")),
                "symbol": symbol,
                "source_filename": member_name,
                "content_hash": content_hash,
                "source_url": source_url,
                "published_at": _text(_find_value(normalised, "published_at")),
                "observed_at": _text(_find_value(normalised, "observed_at")),
            })
            for column in CANONICAL_COLUMNS:
                if column in {"source_key", "trade_date", "source_record_id", "symbol", "source_filename", "source_url", "content_hash", "published_at", "observed_at"}:
                    continue
                value = _find_value(normalised, column)
                canonical[column] = _number(value) if column in NUMERIC_COLUMNS else _text(value)
            metadata = dict(source_metadata or {})
            if not canonical.get("index_name") and metadata.get("index_name"):
                canonical["index_name"] = _text(metadata.get("index_name"))
            if not canonical.get("exchange") and metadata.get("exchange"):
                canonical["exchange"] = _text(metadata.get("exchange"))
            # Legacy index-level close/snapshot reports can use the index name as
            # their canonical identity.  Constituent membership may NOT do this:
            # a membership row must contain an actual constituent Symbol from the
            # official artifact, otherwise an index-level row could be projected as
            # a fake constituent.
            if (
                source_key == "index_snapshot_constituents_weights"
                and not canonical["symbol"]
                and canonical["index_name"]
            ):
                canonical["symbol"] = str(canonical["index_name"]).upper()
            for date_field in ("ex_date", "record_date", "listing_date"):
                if canonical.get(date_field):
                    canonical[date_field] = _date(canonical[date_field], canonical["trade_date"])
            if canonical["symbol"]:
                if not canonical.get("source_record_id"):
                    identity = "|".join(str(canonical.get(key) or "") for key in (
                        "source_key", "trade_date", "symbol", "isin", "index_name",
                        "deal_type", "deal_side", "ex_date", "action_type", "purpose", "filing_timestamp", "source_filename",
                    ))
                    canonical["source_record_id"] = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]
                rows.append(canonical)
    return rows


def _write_parquet(rows: Sequence[Mapping[str, Any]], destination: Path) -> None:
    if not rows:
        raise ValueError("Official report produced no canonical rows")
    import duckdb

    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="laddu-nse-official-") as temp_dir:
        stage = Path(temp_dir) / destination.name
        target = str(stage.resolve()).replace("\\", "/").replace("'", "''")
        db = duckdb.connect()
        try:
            definitions = ",".join(
                f'"{column}" {"DOUBLE" if column in NUMERIC_COLUMNS else "VARCHAR"}'
                for column in CANONICAL_COLUMNS
            )
            db.execute(f"CREATE TABLE canonical_official_report({definitions})")
            placeholders = ",".join("?" for _ in CANONICAL_COLUMNS)
            values = [tuple(dict(row).get(column) for column in CANONICAL_COLUMNS) for row in rows]
            db.executemany(
                f"INSERT INTO canonical_official_report VALUES ({placeholders})",
                values,
            )
            db.execute(
                f"COPY canonical_official_report TO '{target}' (FORMAT PARQUET, COMPRESSION ZSTD)"
            )
        finally:
            db.close()
        os.replace(stage, destination)


@dataclass
class NseOfficialReportIngestionService:
    layout: StorageLayout

    @classmethod
    def from_data_dir(cls, data_dir: Path) -> "NseOfficialReportIngestionService":
        layout = StorageLayout.from_data_dir(Path(data_dir))
        layout.ensure()
        return cls(layout)

    def ingest_bytes(
        self,
        *,
        source_key: str,
        trade_date: str,
        payload: bytes,
        filename: str,
        source_url: str | None = None,
        source_metadata: Mapping[str, Any] | None = None,
    ) -> Dict[str, Any]:
        source_key = _slug(source_key)
        if source_key not in ALLOWED_SOURCE_KEYS:
            raise ValueError(f"Unsupported NSE source key: {source_key}")
        trade_date = _date(trade_date, trade_date)
        if not re.fullmatch(r"20\d{2}-\d{2}-\d{2}", trade_date):
            raise ValueError("trade_date must resolve to YYYY-MM-DD")
        if not isinstance(payload, (bytes, bytearray)) or not payload:
            raise ValueError("payload must contain report bytes")
        content_hash = hashlib.sha256(bytes(payload)).hexdigest()
        suffix = Path(filename or "report.csv").suffix.lower() or ".bin"
        raw_dir = self.layout.raw_lake_dir / "nse_official" / source_key / f"trade_date={trade_date}"
        curated_dir = self.layout.curated_lake_dir / "nse_official" / source_key / f"trade_date={trade_date}"
        raw_path = raw_dir / f"{content_hash}{suffix}"
        curated_path = curated_dir / f"part-{content_hash[:20]}.parquet"
        manifest_path = self.layout.manifests_dir / "nse_official" / source_key / f"trade_date={trade_date}" / f"{content_hash}.json"
        lock_path = self.layout.locks_dir / f"nse-official-{source_key}-{trade_date}.lock"

        with interprocess_lock(lock_path, timeout_seconds=60.0):
            try:
                prior = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                prior = {}
            if (
                prior.get("content_hash") == content_hash
                and raw_path.is_file()
                and curated_path.is_file()
            ):
                projection = dict(prior.get("postgres_projection") or {})
                dsn = os.environ.get("PROJECT_LADDU_OPERATIONAL_DSN") or os.environ.get("DATABASE_URL")
                if dsn and str(projection.get("state") or "").upper() != "PROJECTED":
                    # Content identity is unchanged, but admission is not complete.
                    # Re-parse the immutable payload only to repair the missing
                    # PostgreSQL projection; never rewrite raw/Parquet bytes.
                    rows = canonicalise_rows(
                        source_key, trade_date, bytes(payload), filename, content_hash, source_url,
                        source_metadata=source_metadata or prior.get("source_metadata") or {},
                    )
                    schema_status = _source_schema_status(source_key, rows)
                    if schema_status["state"] != "VALID":
                        raise ValueError(f"{source_key} schema incomplete during reprojection: {schema_status['missing_required_groups']}")
                    from core.nse_official_postgres_repository import NseOfficialPostgresRepository
                    projection = NseOfficialPostgresRepository(dsn).project(
                        source_key=source_key, trade_date=trade_date, content_hash=content_hash,
                        source_url=source_url or prior.get("source_url"),
                        source_filename=filename or prior.get("source_filename"), rows=rows,
                    )
                    repaired = {
                        **prior,
                        "postgres_projection": projection,
                        "schema_status": schema_status,
                        "projection_repaired_at": _now(),
                    }
                    atomic_write_json(manifest_path, repaired)
                    return {
                        **repaired,
                        "ok": True,
                        "state": "UNCHANGED_CONTENT_REPROJECTED",
                        "raw_write_performed": False,
                        "parse_performed": True,
                    }
                return {
                    **prior,
                    "ok": True,
                    "state": "UNCHANGED_CONTENT_SKIPPED",
                    "raw_write_performed": False,
                    "parse_performed": False,
                }

            raw_dir.mkdir(parents=True, exist_ok=True)
            curated_dir.mkdir(parents=True, exist_ok=True)
            if not raw_path.exists():
                temp = raw_path.with_suffix(raw_path.suffix + ".tmp")
                temp.write_bytes(bytes(payload))
                os.replace(temp, raw_path)
            rows = canonicalise_rows(
                source_key, trade_date, bytes(payload), filename, content_hash, source_url,
                source_metadata=source_metadata,
            )
            schema_status = _source_schema_status(source_key, rows)
            if schema_status["state"] != "VALID":
                raise ValueError(f"{source_key} schema incomplete: {schema_status['missing_required_groups']}")
            _write_parquet(rows, curated_path)
            projection = {"state": "POSTGRES_NOT_CONFIGURED", "rows_projected": 0}
            dsn = os.environ.get("PROJECT_LADDU_OPERATIONAL_DSN") or os.environ.get("DATABASE_URL")
            if dsn:
                from core.nse_official_postgres_repository import NseOfficialPostgresRepository
                projection = NseOfficialPostgresRepository(dsn).project(
                    source_key=source_key, trade_date=trade_date, content_hash=content_hash,
                    source_url=source_url, source_filename=filename, rows=rows,
                )
            schema_hash = hashlib.sha256(",".join(CANONICAL_COLUMNS).encode()).hexdigest()
            result = {
                "ok": True,
                "state": "INGESTED",
                "version": SERVICE_VERSION,
                "source_key": source_key,
                "trade_date": trade_date,
                "source_url": source_url,
                "source_filename": filename,
                "source_metadata": dict(source_metadata or {}),
                "content_hash": content_hash,
                "schema_hash": schema_hash,
                "row_count": len(rows),
                "schema_status": schema_status,
                "postgres_projection": projection,
                "analysis_ready": schema_status["state"] == "VALID",
                "training_ready": schema_status["state"] == "VALID",
                "raw_path": str(raw_path),
                "curated_path": str(curated_path),
                "ingested_at": _now(),
                "raw_write_performed": True,
                "parse_performed": True,
                "history_policy": "IMMUTABLE_RAW_CONTENT_HASH_AND_PARTITIONED_CURATED_PARQUET",
            }
            atomic_write_json(manifest_path, result)
            return result
