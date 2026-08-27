"""Canonical feature-matrix construction for every shadow model family.

SQLite and DuckDB are storage/query implementations.  They must both feed the
same feature extraction, coverage, label and fingerprint semantics defined
here so adding a nonlinear challenger cannot silently create a second
backtest/live feature path.
"""
from __future__ import annotations

import hashlib
import json
import math
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence, Tuple

from core.nse_cross_sectional_selector_service import (
    DELIVERY_FEATURES,
    FEATURE_MANIFEST_HASH,
    INTRADAY_FEATURES,
)
from core.production_mode_policy import require_production_mode


DATASET_SERVICE_VERSION = "quant-research-dataset-1.0.0"
MIN_FEATURE_COVERAGE = 0.60


def _number(value: Any) -> Optional[float]:
    try:
        parsed = float(value)
        return parsed if math.isfinite(parsed) else None
    except (TypeError, ValueError):
        return None


def feature_value(features: Mapping[str, Any], aliases: Sequence[str]) -> Optional[float]:
    for alias in aliases:
        value = _number(features.get(alias))
        if value is not None:
            return value
    return None


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


class QuantResearchDatasetService:
    """Build one governed model matrix from storage-neutral joined rows."""

    @staticmethod
    def specs(mode: str):
        desk = require_production_mode(mode)
        return DELIVERY_FEATURES if desk == "delivery" else INTRADAY_FEATURES

    @classmethod
    def build(
        cls,
        rows: Iterable[Mapping[str, Any]],
        *,
        mode: str,
    ) -> Tuple[list[str], list[Dict[str, Any]], Dict[str, Any]]:
        desk = require_production_mode(mode)
        specs = cls.specs(desk)
        names = [name for name, _aliases, _weight, _higher in specs]
        dataset: list[Dict[str, Any]] = []
        rejected = {
            "feature_manifest_mismatch": 0,
            "missing_hash_lineage": 0,
            "insufficient_feature_coverage": 0,
            "invalid_label": 0,
        }
        fingerprint_material = []
        for raw in rows:
            row = dict(raw or {})
            if str(row.get("feature_manifest_hash") or "") != FEATURE_MANIFEST_HASH:
                rejected["feature_manifest_mismatch"] += 1
                continue
            if not row.get("feature_hash") or not row.get("record_hash"):
                rejected["missing_hash_lineage"] += 1
                continue
            features = row.get("features")
            if not isinstance(features, Mapping):
                try:
                    features = json.loads(str(row.get("feature_json") or "{}"))
                except (TypeError, ValueError, json.JSONDecodeError):
                    features = {}
            values = [feature_value(features, aliases) for _name, aliases, _weight, _higher in specs]
            coverage = sum(value is not None for value in values) / len(values) if values else 0.0
            if coverage < MIN_FEATURE_COVERAGE:
                rejected["insufficient_feature_coverage"] += 1
                continue
            net_return = _number(row.get("net_return_bps"))
            stressed_return = _number(row.get("net_return_plus_20bps"))
            if net_return is None or stressed_return is None:
                rejected["invalid_label"] += 1
                continue
            decision_ts = str(row.get("decision_ts") or row.get("observed_at") or "")
            item = {
                "candidate_id": str(row.get("candidate_id") or ""),
                "symbol": str(row.get("symbol") or "").upper(),
                "date": decision_ts[:10],
                "observed_at": decision_ts,
                "settled_at": str(row.get("settled_at") or ""),
                "regime": str(row.get("label_regime") or row.get("market_regime") or "UNKNOWN").upper(),
                "values": values,
                "coverage": round(coverage, 6),
                "label": 1 if net_return > 0 else 0,
                "net_return_bps": net_return,
                "net_return_plus_20bps": stressed_return,
                "target_before_stop": row.get("target_before_stop"),
                "mae_bps": _number(row.get("mae_bps")),
                "mfe_bps": _number(row.get("mfe_bps")),
                "time_to_outcome_bars": _number(row.get("time_to_outcome_bars")),
                "dataset_fingerprint": str(row.get("dataset_fingerprint") or ""),
                "feature_hash": str(row.get("feature_hash") or ""),
                "record_hash": str(row.get("record_hash") or ""),
            }
            dataset.append(item)
            fingerprint_material.append({
                "candidate_id": item["candidate_id"],
                "observed_at": item["observed_at"],
                "feature_hash": item["feature_hash"],
                "record_hash": item["record_hash"],
            })
        dataset.sort(key=lambda row: (row["observed_at"], row["symbol"], row["candidate_id"]))
        fingerprint_material.sort(key=lambda row: (
            row["observed_at"], row["candidate_id"], row["feature_hash"], row["record_hash"]
        ))
        fingerprint = hashlib.sha256(_canonical(fingerprint_material).encode("utf-8")).hexdigest()
        evidence = {
            "ok": True,
            "version": DATASET_SERVICE_VERSION,
            "mode": desk,
            "feature_manifest_hash": FEATURE_MANIFEST_HASH,
            "feature_names": names,
            "accepted_rows": len(dataset),
            "rejected_rows": rejected,
            "dataset_fingerprint": fingerprint,
        }
        return names, dataset, evidence

