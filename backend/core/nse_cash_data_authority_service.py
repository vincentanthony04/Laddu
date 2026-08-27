"""Read-only NSE cash-market data authority catalogue.

The catalogue makes the model-data contract explicit.  It does not download
files from an operator GET request and it never grants model or broker
production authority.  Source ingestion remains a scheduled, content-hashed,
incremental process whose target run status is projected when available.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import json
from typing import Any, Dict, Iterable, Mapping
from core.india_time import INDIA_TZ as IST
from core.storage_layout import atomic_write_json

SERVICE_VERSION = "nse-cash-data-authority-2.3.0-materialized-cache-authority"
AUTHORITY_CONTRACT_VERSION = "nse-cash-data-authority-contract-3.0.0-tiered-freshness-readiness"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _expected_report_date(now: datetime | None = None) -> str:
    from core.official_report_publication_policy import DEFAULT_OFFICIAL_REPORT_PUBLICATION_POLICY
    evidence = DEFAULT_OFFICIAL_REPORT_PUBLICATION_POLICY.latest_eligible_trade_date(now)
    trade_date = evidence.get("trade_date")
    if not trade_date:
        raise RuntimeError("official NSE report date unavailable: exchange calendar is unverified")
    return str(trade_date)


SOURCES: tuple[dict[str, Any], ...] = (
    {
        "key": "cm_udiff_bhavcopy",
        "name": "CM UDiFF Common Bhavcopy Final",
        "domain": "PRICE_AND_ACTIVITY",
        "cadence": "TRADING_DAY",
        "access": "NSE_PUBLIC_REPORT",
        "target": "PARQUET_DUCKDB",
        "fields": ["OHLC", "close", "volume", "turnover", "number_of_trades"],
        "feature_families": ["returns", "liquidity", "activity", "execution_cost"],
    },
    {
        "key": "security_delivery_positions",
        "name": "Security-wise Delivery Positions / Security Deliverable Data",
        "domain": "PARTICIPATION",
        "cadence": "TRADING_DAY",
        "access": "NSE_PUBLIC_REPORT",
        "target": "PARQUET_DUCKDB",
        "fields": ["deliverable_quantity", "delivery_percentage", "traded_quantity"],
        "feature_families": ["delivery_participation", "conviction", "accumulation_distribution"],
    },
    {
        "key": "mii_security_file",
        "name": "MII Security File",
        "domain": "POINT_IN_TIME_IDENTITY",
        "cadence": "TRADING_DAY",
        "access": "NSE_PUBLIC_REPORT",
        "target": "POSTGRESQL_HISTORY",
        "fields": ["symbol", "series", "ISIN", "listing_status", "eligible_universe"],
        "feature_families": ["survivorship_control", "point_in_time_universe", "identity"],
    },
    {
        "key": "daily_volatility_var_price_band",
        "name": "Daily Volatility, VaR, Impact Cost and Price Bands",
        "domain": "RISK_AND_EXECUTABILITY",
        "cadence": "TRADING_DAY",
        "access": "NSE_PUBLIC_REPORT",
        "target": "POSTGRESQL_AND_PARQUET",
        "fields": ["daily_volatility", "VaR", "impact_cost", "price_band"],
        "feature_families": ["volatility_regime", "liquidity_risk", "slippage", "admission_risk"],
    },
    {
        "key": "index_constituents",
        "name": "Official NSE/NSE Indices Constituent Lists",
        "domain": "MARKET_AND_SECTOR_MEMBERSHIP",
        "cadence": "RECONSTITUTION_SNAPSHOT",
        "access": "NSE_OR_NSE_INDICES_PUBLIC_REPORT",
        "target": "POSTGRESQL_AND_PARQUET",
        "fields": ["index_name", "symbol", "series", "ISIN", "sector_name"],
        "feature_families": ["point_in_time_index_membership", "breadth", "sector_rotation", "market_regime"],
    },
    {
        "key": "index_snapshot_constituents_weights",
        "name": "Legacy Rich Index Snapshot / Weights Evidence",
        "domain": "MARKET_AND_SECTOR_CONTEXT",
        "cadence": "TRADING_DAY",
        "access": "OPERATOR_OR_VERIFIED_NSE_REPORT",
        "target": "POSTGRESQL_AND_PARQUET",
        "fields": ["constituent", "weight", "index_return", "market_cap", "beta"],
        "feature_families": ["relative_strength", "breadth", "sector_rotation", "market_regime"],
    },
    {
        "key": "bulk_block_short_margin",
        "name": "Bulk Deals, Block Deals, Short Selling and Margin Trading",
        "domain": "EVENT_AND_PARTICIPATION",
        "cadence": "TRADING_DAY",
        "access": "NSE_PUBLIC_REPORT",
        "target": "POSTGRESQL_EVENTS",
        "fields": ["participant", "quantity", "price", "deal_type", "short_quantity", "margin_position"],
        "feature_families": ["institutional_participation", "event_pressure", "crowding"],
    },
    {
        "key": "corporate_actions",
        "name": "Corporate Actions",
        "domain": "ADJUSTMENT_AUTHORITY",
        "cadence": "EVENT_DRIVEN",
        "access": "NSE_CORPORATE_FILINGS",
        "target": "POSTGRESQL_HISTORY",
        "fields": ["ex_date", "record_date", "purpose", "price_factor", "volume_factor"],
        "feature_families": ["adjusted_history", "return_integrity", "eligibility"],
    },
    {
        "key": "filings_results_announcements_shareholding",
        "name": "Financial Results, Announcements and Shareholding Patterns",
        "domain": "POINT_IN_TIME_FUNDAMENTAL_AND_EVENT",
        "cadence": "EVENT_DRIVEN",
        "access": "NSE_CORPORATE_FILINGS_OR_LICENSED_EOD",
        "target": "POSTGRESQL_HISTORY",
        "fields": ["filing_timestamp", "period", "financial_metrics", "announcement", "ownership"],
        "feature_families": ["fundamental_change", "earnings_event", "ownership_change", "event_risk"],
    },
    {
        "key": "surveillance_52w_price_band_changes",
        "name": "Surveillance Indicators, 52-week High/Low and Price-band Changes",
        "domain": "SURVEILLANCE_AND_EXTREMES",
        "cadence": "TRADING_DAY",
        "access": "NSE_PUBLIC_REPORT",
        "target": "POSTGRESQL_EVENTS",
        "fields": ["surveillance_indicator", "high_52w", "low_52w", "price_band_change"],
        "feature_families": ["surveillance_veto", "breakout_context", "tail_risk"],
    },
)

CRITICAL_EVIDENCE_START_SOURCES = (
    "cm_udiff_bhavcopy",
    "security_delivery_positions",
    "mii_security_file",
)

RESEARCH_ENHANCEMENT_SOURCES = (
    "corporate_actions",
    "daily_volatility_var_price_band",
    "index_constituents",
    "index_snapshot_constituents_weights",
    "surveillance_52w_price_band_changes",
)

MATURITY_CERTIFICATION_SOURCES = (
    "bulk_block_short_margin",
    "filings_results_announcements_shareholding",
)

RUN_ALIASES: dict[str, tuple[str, ...]] = {
    "cm_udiff_bhavcopy": ("bhavcopy", "cm_udiff_bhavcopy", "cash_bhavcopy"),
    "security_delivery_positions": ("delivery_data", "security_delivery_positions"),
    "mii_security_file": ("instrument_master", "mii_security_file", "security_master"),
    "daily_volatility_var_price_band": ("daily_volatility", "var", "price_band"),
    "index_constituents": ("index_constituents", "index_membership", "index_members"),
    "index_snapshot_constituents_weights": ("index_snapshot", "index_weights"),
    "bulk_block_short_margin": ("bulk_deals", "block_deals", "short_selling", "margin_trading"),
    "corporate_actions": ("corporate_actions",),
    "filings_results_announcements_shareholding": ("corporate_filings", "financial_results", "shareholding"),
    "surveillance_52w_price_band_changes": ("surveillance", "price_band_changes", "52_week_high_low"),
}


def _normalise_runs(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for row in rows or []:
        result.append({str(key): value for key, value in dict(row).items()})
    return result


def _latest_for(source_key: str, runs: list[dict[str, Any]]) -> dict[str, Any] | None:
    aliases = RUN_ALIASES.get(source_key, (source_key,))
    aliases = tuple(value.lower() for value in aliases)
    for row in runs:
        job = str(row.get("job_name") or "").lower()
        if any(alias in job for alias in aliases):
            return row
    return None


class NseCashDataAuthorityService:
    """Projects explicit source, storage and model-admission truth."""

    def __init__(self, store: Any = None, data_dir: Path | str | None = None):
        self.store = store
        self.data_dir = Path(data_dir) if data_dir else None

    def _runs(self) -> list[dict[str, Any]]:
        getter = getattr(self.store, "reference_run_status", None)
        if not callable(getter):
            return []
        try:
            return _normalise_runs(getter())
        except Exception:
            return []

    def _manifest_for(self, source_key: str) -> dict[str, Any] | None:
        if self.data_dir is None:
            return None
        root = self.data_dir / "manifests" / "nse_official" / source_key
        if not root.is_dir():
            return None
        admitted: list[dict[str, Any]] = []
        for path in root.rglob("*.json"):
            try:
                row = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                continue
            if row.get("ok") is True and row.get("content_hash"):
                admitted.append(row)
        if not admitted:
            return None
        admitted.sort(key=lambda row: str(row.get("ingested_at") or row.get("trade_date") or ""), reverse=True)
        return admitted[0]

    def _catalogue_sources(self) -> set[str]:
        if self.data_dir is None:
            return set()
        try:
            import duckdb
            from core.storage_layout import StorageLayout
            analytics_db = StorageLayout.from_data_dir(self.data_dir).analytics_db
            if not analytics_db.is_file():
                return set()
            db = duckdb.connect(str(analytics_db), read_only=True)
            try:
                views = {row[0] for row in db.execute(
                    "SELECT table_name FROM information_schema.tables WHERE table_schema='main'"
                ).fetchall()}
                if "curated_nse_official_reports" not in views:
                    return set()
                return {str(row[0]) for row in db.execute(
                    "SELECT DISTINCT source_key FROM curated_nse_official_reports WHERE source_key IS NOT NULL"
                ).fetchall()}
            finally:
                db.close()
        except Exception:
            return set()

    def _last_cycle(self) -> dict[str, Any]:
        if self.data_dir is None:
            return {}
        path = self.data_dir / "manifests" / "nse_official" / "last-cycle.json"
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except Exception:
            return {}

    def _compute_status(self) -> Dict[str, Any]:
        runs = self._runs()
        expected_report_date = _expected_report_date()
        last_cycle = self._last_cycle()
        catalogue_sources = self._catalogue_sources()
        sources: list[dict[str, Any]] = []
        ready_count = 0
        failed_count = 0
        for definition in SOURCES:
            row = dict(definition)
            manifest = self._manifest_for(row["key"])
            latest = _latest_for(row["key"], runs)
            if manifest:
                target = str(row.get("target") or "")
                projection = dict(manifest.get("postgres_projection") or {})
                schema_valid = (manifest.get("schema_status") or {}).get("state") == "VALID"
                parquet_ready = bool(manifest.get("curated_path"))
                postgres_required = "POSTGRESQL" in target
                postgres_ready = (not postgres_required) or projection.get("state") == "PROJECTED"
                catalogue_ready = row["key"] in catalogue_sources
                run_date = str(manifest.get("trade_date") or "")
                cadence = str(row.get("cadence") or "").upper()
                fresh = cadence != "TRADING_DAY" or run_date == expected_report_date
                if schema_valid and parquet_ready and postgres_ready and catalogue_ready and fresh:
                    state = "CURRENT"
                elif schema_valid and parquet_ready and postgres_ready and catalogue_ready:
                    state = "STALE"
                else:
                    state = "WIRED_INCOMPLETE"
                row["coverage"] = {
                    "state": state,
                    "run_date": run_date or None,
                    "expected_report_date": expected_report_date if cadence == "TRADING_DAY" else None,
                    "fresh": fresh,
                    "rows_written": int(manifest.get("row_count") or 0),
                    "finished_at": manifest.get("ingested_at"),
                    "content_hash": manifest.get("content_hash"),
                    "error": None,
                }
                row["wire_status"] = {
                    "raw_evidence": bool(manifest.get("raw_path")),
                    "schema_valid": schema_valid,
                    "curated_parquet": parquet_ready,
                    "postgres_projection": postgres_ready,
                    "research_catalogue": catalogue_ready,
                    "analysis_consumer": catalogue_ready,
                    "ml_feature_consumer": catalogue_ready,
                    "walk_forward_lineage": catalogue_ready,
                    "production_influence": 0.0,
                }
                if state == "CURRENT":
                    ready_count += 1
            elif latest:
                raw_state = str(latest.get("status") or "UNKNOWN").upper()
                cadence = str(row.get("cadence") or "").upper()
                run_date = str(latest.get("run_date") or "")
                fresh = cadence != "TRADING_DAY" or run_date == expected_report_date
                state = "CURRENT" if raw_state in {"OK", "COMPLETE", "CURRENT"} and fresh else "STALE" if raw_state in {"OK", "COMPLETE", "CURRENT"} else raw_state
                row["coverage"] = {
                    "state": state,
                    "run_date": run_date or None,
                    "expected_report_date": expected_report_date if cadence == "TRADING_DAY" else None,
                    "fresh": fresh,
                    "rows_written": int(latest.get("rows_written") or 0),
                    "finished_at": latest.get("finished_at"),
                    "error": latest.get("error") or None,
                }
                if state == "CURRENT" and not manifest:
                    ready_count += 1
                if state == "FAILED":
                    failed_count += 1
            else:
                row["coverage"] = {
                    "state": "TARGET_DATA_REQUIRED",
                    "run_date": None,
                    "expected_report_date": expected_report_date if str(row.get("cadence") or "").upper() == "TRADING_DAY" else None,
                    "fresh": False,
                    "rows_written": 0,
                    "finished_at": None,
                    "error": None,
                }
            row.setdefault("wire_status", {
                "raw_evidence": False, "schema_valid": False, "curated_parquet": False,
                "postgres_projection": False, "research_catalogue": False,
                "analysis_consumer": False, "ml_feature_consumer": False,
                "walk_forward_lineage": False, "production_influence": 0.0,
            })
            sources.append(row)

        total = len(sources)
        all_current = ready_count == total
        state_by_key = {str(row.get("key")): str((row.get("coverage") or {}).get("state") or "").upper() for row in sources}
        def tier(keys):
            current = sum(1 for key in keys if state_by_key.get(key) == "CURRENT")
            return {
                "keys": list(keys),
                "current": current,
                "total": len(keys),
                "ready": current == len(keys),
                "missing": [key for key in keys if state_by_key.get(key) != "CURRENT"],
            }
        tiers = {
            "evidence_start": tier(CRITICAL_EVIDENCE_START_SOURCES),
            "research_enhancement": tier(RESEARCH_ENHANCEMENT_SOURCES),
            "maturity_certification": tier(MATURITY_CERTIFICATION_SOURCES),
        }
        evidence_start_ready = bool(tiers["evidence_start"]["ready"])
        research_enhancement_ready = bool(tiers["research_enhancement"]["ready"])
        authority_state = "FULL_MATURITY_CURRENT" if all_current else "EVIDENCE_START_READY" if evidence_start_ready else "DATA_AUTHORITY_INCOMPLETE"
        return {
            "ok": True,
            "version": SERVICE_VERSION,
            "authority_contract_version": AUTHORITY_CONTRACT_VERSION,
            "observed_at": _now(),
            "state": authority_state,
            "expected_report_date": expected_report_date,
            "summary": {
                "source_count": total,
                "current_count": ready_count,
                "failed_count": failed_count,
                "target_data_required_count": total - ready_count,
                "critical_current": tiers["evidence_start"]["current"],
                "critical_total": tiers["evidence_start"]["total"],
                "enhancement_current": tiers["research_enhancement"]["current"],
                "enhancement_total": tiers["research_enhancement"]["total"],
                "maturity_current": tiers["maturity_certification"]["current"],
                "maturity_total": tiers["maturity_certification"]["total"],
                "expected_report_date": expected_report_date,
                "history_policy": "DOWNLOAD_ONCE_CONTENT_HASH_INCREMENTAL_APPEND",
                "feature_policy": "VERSIONED_INCREMENTAL_FEATURE_STORE_REOPEN_MATURING_TAIL_ONLY",
                "training_policy": "NO_DATA_CHANGE_NO_READ_NO_FIT",
                "model_authority": "SHADOW_ONLY_UNTIL_POINT_IN_TIME_QUALITY_AND_FORWARD_PROOF",
                "tiers": tiers,
                "evidence_start_ready": evidence_start_ready,
                "research_enhancement_ready": research_enhancement_ready,
                "all_source_maturity_ready": all_current,
                "research_activation_allowed": evidence_start_ready,
            },
            "last_cycle": {
                "state": last_cycle.get("state"),
                "trade_date": last_cycle.get("trade_date"),
                "finished_at": last_cycle.get("finished_at"),
                "duration_ms": last_cycle.get("duration_ms"),
                "ingested_or_current": last_cycle.get("ingested_or_current"),
                "required_failures": last_cycle.get("required_failures"),
                "results": last_cycle.get("results") or [],
            },
            "sources": sources,
            "feature_families": [
                {"name": "Participation", "inputs": ["delivery percentage", "deliverable quantity", "bulk/block activity", "short selling"]},
                {"name": "Liquidity & execution", "inputs": ["turnover", "trades", "impact cost", "VaR", "price bands"]},
                {"name": "Market & regime", "inputs": ["index constituents", "weights", "breadth", "beta", "sector dispersion"]},
                {"name": "Point-in-time fundamentals", "inputs": ["financial results", "announcements", "shareholding", "corporate actions"]},
                {"name": "Safety & surveillance", "inputs": ["surveillance indicators", "price-band changes", "52-week extremes"]},
            ],
            "admission_gates": [
                "content hash and schema validated",
                "point-in-time timestamp preserved",
                "corporate-action adjustment coverage complete",
                "survivorship-safe universe membership present",
                "training snapshot immutable and reproducible",
                "purged walk-forward and forward Model Paper evidence pass",
            ],
            "production_influence": 0.0 if not all_current else None,
            "broker_authority": "NONE",
        }


    @property
    def snapshot_path(self) -> Path | None:
        if self.data_dir is None:
            return None
        return self.data_dir / "manifests" / "nse_official" / "authority-status.json"

    def refresh(self) -> Dict[str, Any]:
        """Compute the heavy authority projection off the HTTP path and persist it atomically."""
        payload = self._compute_status()
        path = self.snapshot_path
        if path is not None:
            persisted = dict(payload)
            persisted["materialized_at"] = _now()
            persisted["read_authority"] = "MATERIALIZED_SNAPSHOT"
            atomic_write_json(path, persisted)
            return persisted
        return payload

    def cached_status(self) -> Dict[str, Any]:
        """Return only the last immutable authority snapshot; never trigger DB/DuckDB recounts."""
        path = self.snapshot_path
        if path is None or not path.is_file():
            return {
                "ok": False,
                "version": SERVICE_VERSION,
                "authority_contract_version": AUTHORITY_CONTRACT_VERSION,
                "state": "AUTHORITY_SNAPSHOT_WARMING",
                "observed_at": _now(),
                "materialized_at": None,
                "read_authority": "MATERIALIZED_SNAPSHOT",
                "summary": {
                    "source_count": len(SOURCES),
                    "current_count": None,
                    "failed_count": None,
                    "critical_current": None,
                    "critical_total": len(CRITICAL_EVIDENCE_START_SOURCES),
                    "enhancement_current": None,
                    "enhancement_total": len(RESEARCH_ENHANCEMENT_SOURCES),
                    "maturity_current": None,
                    "maturity_total": len(MATURITY_CERTIFICATION_SOURCES),
                    "evidence_start_ready": False,
                    "research_activation_allowed": False,
                },
                "sources": [],
                "broker_authority": "NONE",
            }
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            return {
                "ok": False,
                "version": SERVICE_VERSION,
                "authority_contract_version": AUTHORITY_CONTRACT_VERSION,
                "state": "AUTHORITY_SNAPSHOT_UNREADABLE",
                "observed_at": _now(),
                "materialized_at": None,
                "read_authority": "MATERIALIZED_SNAPSHOT",
                "summary": {"source_count": len(SOURCES), "current_count": None},
                "sources": [],
                "error": f"{type(exc).__name__}: {exc}"[:300],
                "broker_authority": "NONE",
            }
        if not isinstance(payload, dict):
            payload = {}
        payload = dict(payload)
        payload["read_authority"] = "MATERIALIZED_SNAPSHOT"
        payload["served_at"] = _now()
        return payload

    def status(self) -> Dict[str, Any]:
        """Compatibility/heavy computation API for background workers and deterministic tests."""
        return self._compute_status()
