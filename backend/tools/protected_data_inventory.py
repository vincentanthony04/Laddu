"""Capture and compare Project Laddu protected data assets.

This tool is intentionally read-only.  It gives the transactional Windows
installer a bounded proof that an application update did not delete or reduce
historical Parquet, analytical DuckDB, model artefacts, or PostgreSQL coverage
metadata.  It does not hash every large market-data file because that would
turn an update into a multi-hour operation; content integrity remains the
responsibility of the existing market-lake manifests and database authorities.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

SERVICE_VERSION = "protected-data-inventory-1.0.0"

_TRANSIENT_PARTS = {
    "runtime/training_scratch",
    "snapshots/training",
    "tmp",
    "temp",
    "cache",
}


def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _is_transient(relative: str) -> bool:
    value = relative.replace("\\", "/").lower().strip("/")
    return any(value == item or value.startswith(item + "/") for item in _TRANSIENT_PARTS)


def _scan_root(path: Path) -> dict[str, Any]:
    result: dict[str, Any] = {
        "path": str(path),
        "exists": path.is_dir(),
        "files": 0,
        "bytes": 0,
        "parquet_files": 0,
        "parquet_bytes": 0,
        "duckdb_files": 0,
        "duckdb_bytes": 0,
        "model_files": 0,
        "earliest_mtime": None,
        "latest_mtime": None,
    }
    if not path.is_dir():
        return result
    earliest: float | None = None
    latest: float | None = None
    for file in path.rglob("*"):
        if not file.is_file():
            continue
        try:
            relative = file.relative_to(path).as_posix()
            if _is_transient(relative):
                continue
            stat = file.stat()
        except OSError:
            continue
        size = int(stat.st_size)
        suffix = file.suffix.lower()
        result["files"] += 1
        result["bytes"] += size
        earliest = stat.st_mtime if earliest is None else min(earliest, stat.st_mtime)
        latest = stat.st_mtime if latest is None else max(latest, stat.st_mtime)
        if suffix == ".parquet":
            result["parquet_files"] += 1
            result["parquet_bytes"] += size
        if suffix in {".duckdb", ".ddb"}:
            result["duckdb_files"] += 1
            result["duckdb_bytes"] += size
        if suffix in {".pkl", ".pickle", ".joblib", ".onnx", ".pt", ".pth", ".model", ".ubj", ".txt", ".json"}:
            result["model_files"] += 1
    result["earliest_mtime"] = _iso(earliest)
    result["latest_mtime"] = _iso(latest)
    return result


def _run_text(command: Iterable[str], timeout: int = 12) -> str | None:
    try:
        completed = subprocess.run(
            list(command), check=True, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = completed.stdout.strip()
    return value or None


def _postgres_coverage() -> dict[str, Any]:
    sql = """
    SELECT json_build_object(
      'rows', count(*),
      'instruments', count(DISTINCT security_id),
      'intervals', count(DISTINCT interval),
      'accepted', count(*) FILTER (WHERE quality_state IN ('ACCEPTED','REPAIRED')),
      'earliest', min(earliest_stored_ts),
      'latest', max(latest_stored_ts),
      'missing_ranges', COALESCE(sum(jsonb_array_length(missing_ranges)),0)
    )::text
    FROM market_data.coverage;
    """
    raw = _run_text([
        "docker", "exec", "project-laddu-operational-postgres",
        "psql", "-At", "-v", "ON_ERROR_STOP=1", "-U", "laddu_admin",
        "-d", "laddu_operational", "-c", " ".join(sql.split()),
    ])
    if raw is None:
        return {"available": False}
    try:
        parsed = json.loads(raw.splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        return {"available": False, "error": "unparseable coverage response"}
    parsed["available"] = True
    return parsed


def capture(install_dir: Path) -> dict[str, Any]:
    data = install_dir / "data"
    return {
        "ok": True,
        "service_version": SERVICE_VERSION,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "install_dir": str(install_dir),
        "policy": "application updates may add protected data but may not delete or reduce it",
        "roots": {
            "lake": _scan_root(data / "lake"),
            "analytics": _scan_root(data / "analytics"),
            "models": _scan_root(data / "models"),
            "manifests": _scan_root(data / "manifests"),
        },
        "postgres_coverage": _postgres_coverage(),
    }


def _metric(report: dict[str, Any], root: str, key: str) -> int:
    try:
        return int(report["roots"][root][key] or 0)
    except (KeyError, TypeError, ValueError):
        return 0


def compare(before: dict[str, Any], after: dict[str, Any]) -> list[dict[str, Any]]:
    regressions: list[dict[str, Any]] = []
    checks = (
        ("lake", "parquet_files"),
        ("lake", "parquet_bytes"),
        ("analytics", "duckdb_files"),
        ("models", "model_files"),
    )
    for root, key in checks:
        old = _metric(before, root, key)
        new = _metric(after, root, key)
        if old > 0 and new < old:
            regressions.append({
                "code": "HISTORICAL_COVERAGE_REGRESSION",
                "asset": f"{root}.{key}", "before": old, "after": new,
            })
    old_pg = before.get("postgres_coverage") or {}
    new_pg = after.get("postgres_coverage") or {}
    if old_pg.get("available") and new_pg.get("available"):
        for key in ("rows", "instruments", "accepted"):
            old = int(old_pg.get(key) or 0)
            new = int(new_pg.get(key) or 0)
            if old > 0 and new < old:
                regressions.append({
                    "code": "HISTORICAL_COVERAGE_REGRESSION",
                    "asset": f"postgres_coverage.{key}", "before": old, "after": new,
                })
        old_earliest = str(old_pg.get("earliest") or "")
        new_earliest = str(new_pg.get("earliest") or "")
        if old_earliest and new_earliest and new_earliest > old_earliest:
            regressions.append({
                "code": "HISTORICAL_COVERAGE_REGRESSION",
                "asset": "postgres_coverage.earliest", "before": old_earliest, "after": new_earliest,
            })
    return regressions


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--install-dir", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--baseline")
    parser.add_argument("--fail-on-regression", action="store_true")
    args = parser.parse_args()

    report = capture(Path(args.install_dir))
    if args.baseline:
        baseline_path = Path(args.baseline)
        if not baseline_path.is_file():
            report.update(ok=False, state="BASELINE_MISSING", regressions=[{
                "code": "HISTORICAL_COVERAGE_BASELINE_MISSING", "path": str(baseline_path),
            }])
        else:
            baseline = json.loads(baseline_path.read_text(encoding="utf-8-sig"))
            regressions = compare(baseline, report)
            report.update(
                ok=not regressions,
                state="PROTECTED_DATA_PRESERVED" if not regressions else "HISTORICAL_COVERAGE_REGRESSION",
                baseline=str(baseline_path),
                regressions=regressions,
            )
    else:
        report["state"] = "PROTECTED_DATA_BASELINE_CAPTURED"

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
    print(json.dumps({
        "ok": report.get("ok"), "state": report.get("state"),
        "lake_parquet_files": _metric(report, "lake", "parquet_files"),
        "lake_parquet_bytes": _metric(report, "lake", "parquet_bytes"),
        "coverage_rows": (report.get("postgres_coverage") or {}).get("rows"),
        "regressions": report.get("regressions") or [],
        "report": str(output),
    }, indent=2, default=str))
    return 2 if args.fail_on_regression and not report.get("ok") else 0


if __name__ == "__main__":
    raise SystemExit(main())
