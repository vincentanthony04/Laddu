"""Incremental, versioned feature-store authority for isolated ML training.

Raw candle history is not recalculated on every training cycle.  The store
reuses immutable feature rows, reopens only the label-maturation tail, and
loads a bounded rolling context for newly appended sessions.  Heavy imports are
inside research-only functions so the live runtime dependency boundary remains
clean.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Callable, Dict, Iterable, Mapping, Optional, Sequence

from core.storage_layout import StorageLayout, atomic_write_json


FEATURE_STORE_VERSION = "incremental-feature-store-1.0.0"
DEFAULT_LOOKBACK_CALENDAR_DAYS = 430


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _date(value: Any) -> str:
    return str(value or "")[:10]


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def feature_store_path(layout: StorageLayout, *, mode: str, horizon: int) -> Path:
    return layout.feature_lake_dir / str(mode).lower() / f"horizon={int(horizon)}" / "features.parquet"


def feature_manifest_path(layout: StorageLayout, *, mode: str, horizon: int) -> Path:
    return layout.manifests_dir / "feature_stores" / f"{str(mode).lower()}-h{int(horizon)}.json"


def read_manifest(path: Path) -> Dict[str, Any]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return {}


def plan_incremental_update(
    *,
    existing_dates: Sequence[str],
    source_dates: Sequence[str],
    horizon: int,
    lookback_calendar_days: int = DEFAULT_LOOKBACK_CALENDAR_DAYS,
) -> Dict[str, Any]:
    """Return an append/reopen plan without touching storage.

    The last horizon plus two sessions are replaced because their labels may
    mature when new sessions arrive.  A wider calendar context is queried only
    to calculate long rolling features for that replacement tail.
    """
    source = sorted({_date(value) for value in source_dates if _date(value)})
    existing = sorted({_date(value) for value in existing_dates if _date(value)})
    if not source:
        return {"state": "SOURCE_EMPTY", "requires_build": False}
    if not existing:
        return {
            "state": "FULL_BUILD",
            "requires_build": True,
            "source_start": source[0],
            "source_end": source[-1],
            "query_start": source[0],
            "replace_from": source[0],
        }
    if source[-1] <= existing[-1] and len(source) <= len(existing):
        return {
            "state": "CURRENT",
            "requires_build": False,
            "source_start": source[0],
            "source_end": source[-1],
            "feature_through": existing[-1],
        }
    reopen = max(0, int(horizon)) + 2
    replace_index = max(0, len(existing) - reopen)
    replace_from = existing[replace_index]
    query_start = (
        datetime.fromisoformat(replace_from).date() - timedelta(days=max(30, int(lookback_calendar_days)))
    ).isoformat()
    return {
        "state": "INCREMENTAL_APPEND",
        "requires_build": True,
        "source_start": source[0],
        "source_end": source[-1],
        "feature_through": existing[-1],
        "query_start": query_start,
        "replace_from": replace_from,
        "reopened_sessions": len(existing) - replace_index,
    }


def _read_parquet(path: Path):
    import duckdb

    db = duckdb.connect()
    try:
        escaped = str(Path(path).resolve()).replace("\\", "/").replace("'", "''")
        return db.execute(f"SELECT * FROM read_parquet('{escaped}', union_by_name=true)").fetchdf()
    finally:
        db.close()


def _atomic_write_parquet(frame: Any, path: Path) -> None:
    import duckdb

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp.parquet", dir=str(path.parent))
    os.close(fd)
    temp = Path(temp_name)
    temp.unlink(missing_ok=True)
    try:
        db = duckdb.connect()
        try:
            db.register("feature_frame", frame)
            destination = str(temp.resolve()).replace("\\", "/").replace("'", "''")
            db.execute(f"COPY feature_frame TO '{destination}' (FORMAT PARQUET, COMPRESSION ZSTD)")
        finally:
            db.close()
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def feature_frame_fingerprint(frame: Any, *, feature_names: Sequence[str], horizon: int) -> tuple[str, Dict[str, Any]]:
    import pandas as pd

    if frame is None or frame.empty:
        basis = {"rows": 0, "symbols": 0, "dates": 0, "horizon": int(horizon), "features": list(feature_names)}
    else:
        dates = pd.to_datetime(frame["date"], errors="coerce")
        labelled = frame[frame.get("forward_return").notna()] if "forward_return" in frame.columns else frame
        basis = {
            "rows": int(len(frame)),
            "labelled_rows": int(len(labelled)),
            "symbols": int(frame["symbol"].nunique()),
            "dates": int(dates.dt.strftime("%Y-%m-%d").nunique()),
            "start": dates.min().strftime("%Y-%m-%d") if dates.notna().any() else None,
            "end": dates.max().strftime("%Y-%m-%d") if dates.notna().any() else None,
            "labelled_end": (
                pd.to_datetime(labelled["date"], errors="coerce").max().strftime("%Y-%m-%d")
                if len(labelled) else None
            ),
            "horizon": int(horizon),
            "features": list(feature_names),
            "close_sum": round(float(frame["close"].fillna(0).sum()), 6) if "close" in frame.columns else None,
            "forward_sum": round(float(labelled["forward_return"].fillna(0).sum()), 8) if "forward_return" in labelled.columns else None,
        }
    return hashlib.sha256(_canonical(basis).encode("utf-8")).hexdigest(), basis


def materialize_feature_store(
    layout: StorageLayout,
    *,
    mode: str,
    horizon: int,
    feature_names: Sequence[str],
    feature_definition_hash: str,
    source_dates: Sequence[str],
    source_watermark: Mapping[str, Any],
    load_panel: Callable[[Optional[str]], Any],
    build_features: Callable[[Any, int], Any],
) -> tuple[Any, Dict[str, Any]]:
    """Load the current feature store or update only its mutable tail."""
    import pandas as pd

    target = feature_store_path(layout, mode=mode, horizon=horizon)
    manifest_file = feature_manifest_path(layout, mode=mode, horizon=horizon)
    manifest = read_manifest(manifest_file)
    reusable = (
        target.is_file()
        and manifest.get("feature_store_version") == FEATURE_STORE_VERSION
        and manifest.get("feature_definition_hash") == feature_definition_hash
        and int(manifest.get("horizon") or -1) == int(horizon)
    )
    existing = _read_parquet(target) if reusable else pd.DataFrame()
    existing_dates = (
        pd.to_datetime(existing["date"], errors="coerce").dropna().dt.strftime("%Y-%m-%d").unique().tolist()
        if reusable and not existing.empty else []
    )
    plan = plan_incremental_update(
        existing_dates=existing_dates,
        source_dates=source_dates,
        horizon=horizon,
    )
    same_watermark = manifest.get("source_watermark") == dict(source_watermark)
    if reusable and plan["state"] == "CURRENT" and same_watermark:
        result = {
            **manifest,
            "state": "FEATURE_STORE_CURRENT",
            "last_checked_at": _now(),
            "history_reused_rows": int(len(existing)),
            "features_recomputed_rows": 0,
            "plan": plan,
        }
        return existing, result

    if reusable and plan.get("state") == "CURRENT" and not same_watermark:
        source = sorted({_date(value) for value in source_dates if _date(value)})
        plan = {
            "state": "SOURCE_CORRECTION_REBUILD",
            "requires_build": True,
            "source_start": source[0] if source else None,
            "source_end": source[-1] if source else None,
            "query_start": source[0] if source else None,
            "replace_from": source[0] if source else None,
            "reason": "source catalogue fingerprint changed without a new terminal session",
        }

    query_start = None if plan.get("state") in {"FULL_BUILD", "SOURCE_CORRECTION_REBUILD"} else plan.get("query_start")
    panel = load_panel(query_start)
    if panel is None or panel.empty:
        raise RuntimeError("The authoritative research panel is empty for the planned feature window")
    panel_attrs = dict(getattr(panel, "attrs", {}) or {})
    panel_authority = {
        "point_in_time_universe": bool(panel_attrs.get("point_in_time_universe")),
        "universe_join_authority": str(panel_attrs.get("universe_join_authority") or "UNKNOWN"),
        "price_basis": str(panel_attrs.get("price_basis") or "UNKNOWN"),
    }
    recomputed = build_features(panel, horizon)
    if recomputed is None or recomputed.empty:
        raise RuntimeError("Feature calculation produced no rows")
    recomputed["date"] = pd.to_datetime(recomputed["date"], errors="coerce")
    recomputed = recomputed.dropna(subset=["date", "symbol"]).sort_values(["date", "symbol"])
    if reusable and not existing.empty and plan.get("replace_from"):
        existing["date"] = pd.to_datetime(existing["date"], errors="coerce")
        boundary = pd.Timestamp(plan["replace_from"])
        retained = existing[existing["date"] < boundary].copy()
        replacement = recomputed[recomputed["date"] >= boundary].copy()
        combined = pd.concat([retained, replacement], ignore_index=True, sort=False)
        reused_rows = int(len(retained))
        recomputed_rows = int(len(replacement))
    else:
        combined = recomputed
        reused_rows = 0
        recomputed_rows = int(len(recomputed))
    combined = combined.drop_duplicates(["symbol", "date"], keep="last").sort_values(["date", "symbol"]).reset_index(drop=True)
    fingerprint, basis = feature_frame_fingerprint(combined, feature_names=feature_names, horizon=horizon)
    _atomic_write_parquet(combined, target)
    result = {
        "ok": True,
        "state": "FEATURE_STORE_BUILT" if not reusable else "FEATURE_STORE_INCREMENTALLY_UPDATED",
        "feature_store_version": FEATURE_STORE_VERSION,
        "mode": str(mode).lower(),
        "horizon": int(horizon),
        "feature_definition_hash": feature_definition_hash,
        "dataset_fingerprint": fingerprint,
        "dataset_basis": basis,
        "feature_path": str(target),
        "source_watermark": dict(source_watermark),
        "history_reused_rows": reused_rows,
        "features_recomputed_rows": recomputed_rows,
        "query_start": query_start,
        "replace_from": plan.get("replace_from"),
        "feature_through": basis.get("end"),
        "labelled_through": basis.get("labelled_end"),
        "updated_at": _now(),
        "plan": plan,
        "panel_authority": panel_authority,
    }
    atomic_write_json(manifest_file, result)
    return combined, result


def training_is_current(
    latest_run: Mapping[str, Any],
    *,
    dataset_fingerprint: str,
    model_spec_hash: str,
    labelled_through: str,
) -> bool:
    return bool(
        latest_run
        and latest_run.get("ok") is True
        and str(latest_run.get("dataset_fingerprint") or "") == str(dataset_fingerprint)
        and str(latest_run.get("model_spec_hash") or "") == str(model_spec_hash)
        and str(latest_run.get("labelled_through") or "") == str(labelled_through)
        and str(latest_run.get("state") or "") not in {"PUBLICATION_PENDING", "TRAINING_BLOCKED"}
    )
