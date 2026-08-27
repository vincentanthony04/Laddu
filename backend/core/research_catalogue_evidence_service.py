"""Bounded read-only proof of the persisted DuckDB research catalogue.

This authority exists so a valid materialized historical research panel can be reused
for an evidence-publication/WFA cycle without waiting behind a redundant catalogue
refresh lock. It never fabricates rows, never writes DuckDB and never changes
production influence.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

from config import DATA_DIR, ML_DELIVERY_TRAIN_MIN_DAYS, ML_DELIVERY_TRAIN_REFERENCE_DAYS

VERSION = "research-catalogue-evidence-1.1.0-pl42-adaptive-history"


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        return dict(value) if isinstance(value, dict) else {}
    except Exception:
        return {}


class ResearchCatalogueEvidenceService:
    @staticmethod
    def probe(*, data_dir: str | Path = DATA_DIR, min_dates: int = ML_DELIVERY_TRAIN_MIN_DAYS) -> Dict[str, Any]:
        root = Path(data_dir)
        manifest = _read_json(root / "manifests" / "market-lake.json")
        db_path = root / "analytics" / "project_laddu_quant.duckdb"
        base = {
            "ok": True,
            "version": VERSION,
            "ready": False,
            "state": "PERSISTED_RESEARCH_PANEL_NOT_PROVEN",
            "analytics_db": str(db_path),
            "analytics_db_exists": db_path.is_file(),
            "manifest_state": manifest.get("state"),
            "manifest_version": manifest.get("version"),
            "manifest_last_run": manifest.get("last_run"),
            "min_dates_required": int(min_dates),
            "reference_days": int(ML_DELIVERY_TRAIN_REFERENCE_DAYS),
            "reference_semantics": "STABILITY_REFERENCE_NOT_CAP",
            "history_policy": "ALL_ELIGIBLE_BY_STOCK_AND_MODE",
            "production_influence": 0,
            "broker_authority": "NONE",
        }
        if not db_path.is_file():
            return base
        try:
            import duckdb
            db = duckdb.connect(str(db_path), read_only=True)
        except Exception as exc:
            return {**base, "state": "PERSISTED_RESEARCH_PANEL_BUSY_OR_UNAVAILABLE", "error": f"{type(exc).__name__}: {exc}"[:400]}
        try:
            relations = {str(row[0]) for row in db.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema='main'"
            ).fetchall()}
            if "research_delivery_training_panel" not in relations:
                return {**base, "state": "PERSISTED_RESEARCH_PANEL_MISSING", "relations_checked": True}
            sample = db.execute(
                """SELECT 1 FROM research_delivery_training_panel
                     WHERE date IS NOT NULL AND symbol IS NOT NULL AND close IS NOT NULL LIMIT 1"""
            ).fetchone()
            if not sample:
                return {**base, "state": "PERSISTED_RESEARCH_PANEL_EMPTY"}
            row = db.execute(
                """SELECT count(*) AS rows,
                          count(DISTINCT date) AS dates,
                          CAST(min(date) AS VARCHAR) AS start_date,
                          CAST(max(date) AS VARCHAR) AS end_date
                     FROM research_delivery_training_panel"""
            ).fetchone()
            rows = int(row[0] or 0) if row else 0
            dates = int(row[1] or 0) if row else 0
            meta = {}
            if "research_catalog_meta" in relations:
                try:
                    meta = {str(k): str(v) for k, v in db.execute(
                        "SELECT key,value FROM research_catalog_meta"
                    ).fetchall()}
                except Exception:
                    meta = {}
            ready = rows > 0 and dates >= int(min_dates)
            return {
                **base,
                "ready": ready,
                "state": "PERSISTED_RESEARCH_PANEL_READY" if ready else "PERSISTED_RESEARCH_PANEL_INSUFFICIENT_DEPTH",
                "rows": rows,
                "dates": dates,
                "reference_satisfied": dates >= int(ML_DELIVERY_TRAIN_REFERENCE_DAYS),
                "start": str(row[2])[:10] if row and row[2] else None,
                "end": str(row[3])[:10] if row and row[3] else None,
                "catalogue_fingerprint": meta.get("catalogue_fingerprint"),
                "catalog_version": meta.get("catalog_version") or manifest.get("version"),
                "panel_version": meta.get("research_training_panel_version"),
                "proof": "READ_ONLY_DUCKDB_MATERIALIZED_PANEL",
            }
        except Exception as exc:
            return {**base, "state": "PERSISTED_RESEARCH_PANEL_PROBE_FAILED", "error": f"{type(exc).__name__}: {exc}"[:400]}
        finally:
            try:
                db.close()
            except Exception:
                pass
