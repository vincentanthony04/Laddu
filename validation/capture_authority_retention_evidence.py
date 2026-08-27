from __future__ import annotations

"""Direct, read-only upgrade retention evidence for Project Laddu.

This tool deliberately does not import the application runtime.  Upgrade safety
must be provable even when the old HTTP process is slow, unhealthy or unable to
serve a derived read model.  It reads only the retained production authorities:
operational PostgreSQL, governance PostgreSQL, QuestDB and immutable Parquet.

No DSN, password or secure environment value is emitted into evidence.
"""

import argparse
import base64
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any, Iterable
from urllib import parse, request


VERSION = "authority-retention-evidence-2.1.0-derived-parquet-aware"


@dataclass(frozen=True)
class TableSpec:
    name: str
    high_water: str | None = None
    extra_sql: str | None = None


OPERATIONAL_TABLES: tuple[TableSpec, ...] = (
    TableSpec(
        "trading.canonical_decisions",
        "updated_at",
        "COUNT(*) FILTER (WHERE active) AS active_count, "
        "COUNT(*) FILTER (WHERE NOT active) AS terminal_count",
    ),
    TableSpec("trading.canonical_decision_events", "occurred_at", "MAX(event_id) AS max_event_id"),
    TableSpec(
        "trading.model_paper_positions",
        "updated_at",
        "COUNT(*) FILTER (WHERE status='OPEN') AS open_count, "
        "COUNT(*) FILTER (WHERE status='CLOSED') AS closed_count",
    ),
    TableSpec("trading.outcome_learning", "created_at"),
    TableSpec("risk.candidate_admissions", "occurred_at"),
    TableSpec("accounting.journal_entries", "created_at"),
    TableSpec("accounting.journal_postings", None, "MAX(posting_id) AS max_posting_id"),
    TableSpec("integration.transactional_outbox", "created_at", "MAX(outbox_id) AS max_outbox_id"),
    TableSpec("runtime_control.schema_migrations", "applied_at"),
)

GOVERNANCE_TABLES: tuple[TableSpec, ...] = (
    TableSpec("model_registry.models", "registered_at"),
    TableSpec("research.regime_observations", "created_at"),
    TableSpec("research.model_paper_observations", "created_at"),
    TableSpec("research.ranking_populations", "created_at"),
    TableSpec("research.feature_snapshots", "frozen_at"),
    TableSpec("research.predictions", "frozen_at"),
    TableSpec("research.prediction_outcomes", "created_at"),
    TableSpec("research.experiments", "started_at"),
    TableSpec("research.experiment_predictions", "assigned_at"),
    TableSpec("research.experiment_folds", "test_end"),
    TableSpec("research.experiment_metrics", "computed_at"),
    TableSpec("research.training_publications", "created_at"),
    TableSpec("research.shadow_predictions", "created_at"),
    TableSpec("research.factor_decay_observations", "created_at"),
    TableSpec("research.training_publication_events", "occurred_at"),
    TableSpec("research.selector_populations", "created_at"),
    TableSpec("research.selector_population_members", "created_at"),
    TableSpec("research.selector_arm_predictions", "created_at"),
    TableSpec("research.selector_outcomes", "created_at"),
    TableSpec("research.forward_maturity_checkpoints", "created_at"),
    TableSpec("deployment.promotion_decisions", "decided_at"),
    TableSpec("deployment.assignments", "effective_from"),
    TableSpec("runtime_control.schema_migrations", "applied_at"),
)

QUESTDB_TABLES: tuple[tuple[str, str], ...] = (
    ("market_ticks", "provider_ts"),
    ("market_bars", "bar_end_ts"),
    ("market_data_quality_events", "event_ts"),
)


# Single-file feature/prediction stores are intentionally rewritten atomically by
# the research runtime after startup. They are derived from retained canonical
# candle/delivery/NSE/PostgreSQL authorities and must not be compared as immutable
# retained bytes during install activation. Their inventory remains visible in
# evidence, while immutable/raw parquet parts remain fail-closed protected.
REBUILDABLE_DERIVED_PARQUET_PREFIXES: tuple[str, ...] = (
    "lake/features/",
    "lake/predictions/",
)

def _is_rebuildable_derived_parquet(relative: str) -> bool:
    folded = str(relative or "").replace("\\", "/").lower().lstrip("/")
    return any(folded.startswith(prefix) for prefix in REBUILDABLE_DERIVED_PARQUET_PREFIXES)


class EvidenceError(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _json_default(value: Any) -> str:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z") if value.tzinfo else value.isoformat()
    return str(value)


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=_json_default)


def _sha(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def parse_data_plane_env(path: Path) -> dict[str, str]:
    if not path.exists():
        raise EvidenceError(f"DATA_PLANE_ENV_MISSING:{path}")
    text = path.read_text(encoding="utf-8-sig")
    values: dict[str, str] = {}
    single = re.compile(r"\$env:([A-Za-z0-9_]+)\s*=\s*'((?:''|[^'])*)'")
    double = re.compile(r'\$env:([A-Za-z0-9_]+)\s*=\s*"((?:`"|[^"])*)"')
    for match in single.finditer(text):
        values[match.group(1)] = match.group(2).replace("''", "'")
    for match in double.finditer(text):
        values.setdefault(match.group(1), match.group(2).replace('`"', '"'))
    required = (
        "PROJECT_LADDU_OPERATIONAL_DSN",
        "PROJECT_LADDU_GOVERNANCE_DSN",
        "PROJECT_LADDU_QUESTDB_HTTP_URL",
    )
    missing = [name for name in required if not values.get(name)]
    if missing:
        raise EvidenceError("DATA_PLANE_ENV_INCOMPLETE:" + ",".join(missing))
    return values


def _serialise_row(columns: Iterable[str], row: Iterable[Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in zip(columns, row):
        if isinstance(value, datetime):
            value = _json_default(value)
        out[str(key)] = value
    return out


def _postgres_plane(dsn: str, specs: tuple[TableSpec, ...], *, include_control_state: bool = False) -> dict[str, Any]:
    try:
        import psycopg  # type: ignore
    except Exception as exc:  # pragma: no cover - target runtime contract
        raise EvidenceError(f"PSYCOPG_UNAVAILABLE:{type(exc).__name__}:{exc}") from exc

    tables: dict[str, Any] = {}
    with psycopg.connect(dsn, autocommit=True, connect_timeout=4) as conn:
        with conn.cursor() as cur:
            cur.execute("SET statement_timeout = '4000ms'")
            cur.execute("SET lock_timeout = '1000ms'")
            for spec in specs:
                fields = ["COUNT(*) AS row_count"]
                if spec.high_water:
                    fields.append(f"MAX({spec.high_water}) AS high_water")
                if spec.extra_sql:
                    fields.append(spec.extra_sql)
                sql = f"SELECT {', '.join(fields)} FROM {spec.name}"
                cur.execute(sql)
                row = cur.fetchone()
                names = [desc.name for desc in cur.description]
                tables[spec.name] = _serialise_row(names, row)
            control_state: dict[str, Any] | None = None
            if include_control_state:
                cur.execute(
                    "SELECT singleton_id,operator_stop,reason,updated_by,external_daily_pnl,"
                    "external_equity,equity_peak,account_as_of,updated_at "
                    "FROM risk.control_state WHERE singleton_id=1"
                )
                row = cur.fetchone()
                if row is None:
                    raise EvidenceError("RISK_CONTROL_STATE_MISSING")
                names = [desc.name for desc in cur.description]
                control_state = _serialise_row(names, row)
    return {"ok": True, "tables": tables, "control_state": control_state}


def _questdb_query(base_url: str, sql: str, env: dict[str, str]) -> dict[str, Any]:
    query = parse.urlencode({"query": sql})
    headers: dict[str, str] = {}
    username = env.get("PROJECT_LADDU_QUESTDB_USERNAME")
    password = env.get("PROJECT_LADDU_QUESTDB_PASSWORD")
    if username is not None and password is not None:
        token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
        headers["Authorization"] = "Basic " + token
    req = request.Request(base_url.rstrip("/") + "/exec?" + query, headers=headers)
    with request.urlopen(req, timeout=4.0) as response:
        if response.status != 200:
            raise EvidenceError(f"QUESTDB_HTTP_{response.status}")
        payload = json.loads(response.read().decode("utf-8") or "{}")
    if payload.get("error"):
        raise EvidenceError("QUESTDB_QUERY_ERROR:" + str(payload.get("error"))[:300])
    columns = [str(item.get("name") if isinstance(item, dict) else item) for item in payload.get("columns") or []]
    dataset = payload.get("dataset") or []
    return _serialise_row(columns, dataset[0]) if dataset else {}


def _questdb_plane(base_url: str, env: dict[str, str]) -> dict[str, Any]:
    tables: dict[str, Any] = {}
    for table, ts_col in QUESTDB_TABLES:
        row = _questdb_query(base_url, f"SELECT count() AS row_count,max({ts_col}) AS high_water FROM {table}", env)
        tables[table] = row
    return {"ok": True, "tables": tables}


def _parquet_plane(install_dir: Path) -> dict[str, Any]:
    data_dir = install_dir / "data"
    parts: dict[str, int] = {}
    rebuildable_parts: dict[str, int] = {}
    if data_dir.exists():
        for path in sorted(data_dir.rglob("*.parquet")):
            if not path.is_file():
                continue
            try:
                size = int(path.stat().st_size)
            except OSError as exc:
                raise EvidenceError(f"PARQUET_STAT_FAILED:{path}:{exc}") from exc
            relative = path.relative_to(data_dir).as_posix()
            if _is_rebuildable_derived_parquet(relative):
                rebuildable_parts[relative] = size
            else:
                parts[relative] = size
    total_bytes = sum(parts.values())

    catalog_path = data_dir / "manifests" / "candle-file-catalog.json"
    catalog: dict[str, Any] = {
        "present": False,
        "catalog_version": None,
        "generated_at": None,
        "root_file_count": 0,
        "durable_rows": 0,
        "durable_series": 0,
        "durable_first": None,
        "durable_latest": None,
        "unreadable_files": 0,
    }
    if catalog_path.exists():
        payload = json.loads(catalog_path.read_text(encoding="utf-8-sig"))
        series = [dict(row or {}) for row in dict(payload.get("series") or {}).values()]
        first = min((str(row.get("first")) for row in series if row.get("first")), default=None)
        latest = max((str(row.get("last")) for row in series if row.get("last")), default=None)
        catalog = {
            "present": True,
            "catalog_version": payload.get("catalog_version"),
            "generated_at": payload.get("generated_at"),
            "root_file_count": int(payload.get("root_file_count") or 0),
            "durable_rows": sum(max(0, int(row.get("count") or 0)) for row in series),
            "durable_series": len(series),
            "durable_first": first,
            "durable_latest": latest,
            "unreadable_files": len(payload.get("unreadable_files") or []),
        }
    return {
        "ok": True,
        "authority": "IMMUTABLE_RETAINED_PARQUET_PLUS_CANDLE_CATALOG",
        "part_count": len(parts),
        "total_bytes": total_bytes,
        "parts": parts,
        "parts_inventory_sha256": _sha(parts),
        "rebuildable_derived_part_count": len(rebuildable_parts),
        "rebuildable_derived_parts": rebuildable_parts,
        "rebuildable_policy": "INVENTORIED_NOT_IMMUTABLE_RETENTION_AUTHORITY",
        "candle_catalog": catalog,
    }


def capture(install_dir: Path, env_path: Path, *, label: str) -> dict[str, Any]:
    env = parse_data_plane_env(env_path)
    evidence = {
        "ok": True,
        "version": VERSION,
        "label": label,
        "captured_at": _now(),
        "install_dir": str(install_dir),
        "authority_policy": "DIRECT_READ_ONLY_NO_APPLICATION_HTTP",
        "operational_postgres": _postgres_plane(env["PROJECT_LADDU_OPERATIONAL_DSN"], OPERATIONAL_TABLES, include_control_state=True),
        "governance_postgres": _postgres_plane(env["PROJECT_LADDU_GOVERNANCE_DSN"], GOVERNANCE_TABLES),
        "questdb": _questdb_plane(env["PROJECT_LADDU_QUESTDB_HTTP_URL"], env),
        "parquet": _parquet_plane(install_dir),
        "secrets_emitted": False,
    }
    material = dict(evidence)
    material.pop("captured_at", None)
    evidence["content_sha256"] = _sha(material)
    return evidence


def clean_install_evidence(install_dir: Path, *, label: str) -> dict[str, Any]:
    payload = {
        "ok": True,
        "version": VERSION,
        "label": label,
        "captured_at": _now(),
        "install_dir": str(install_dir),
        "state": "CLEAN_INSTALL_NO_PRIOR_AUTHORITY",
        "authority_policy": "DIRECT_READ_ONLY_NO_APPLICATION_HTTP",
        "secrets_emitted": False,
    }
    material = dict(payload)
    material.pop("captured_at", None)
    payload["content_sha256"] = _sha(material)
    return payload


def compare_evidence(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    regressions: list[dict[str, Any]] = []
    if before.get("state") == "CLEAN_INSTALL_NO_PRIOR_AUTHORITY":
        return {"ok": True, "state": "CLEAN_INSTALL_NO_PRIOR_AUTHORITY", "regressions": []}
    if not before.get("ok") or not after.get("ok"):
        regressions.append({"kind": "capture_not_ok", "before_ok": bool(before.get("ok")), "after_ok": bool(after.get("ok"))})

    for plane in ("operational_postgres", "governance_postgres"):
        before_tables = dict((before.get(plane) or {}).get("tables") or {})
        after_tables = dict((after.get(plane) or {}).get("tables") or {})
        for table, old in before_tables.items():
            new = after_tables.get(table)
            if new is None:
                regressions.append({"kind": "postgres_table_missing", "plane": plane, "table": table})
                continue
            old_count = int((old or {}).get("row_count") or 0)
            new_count = int((new or {}).get("row_count") or 0)
            if new_count < old_count:
                regressions.append({"kind": "postgres_row_regression", "plane": plane, "table": table, "before": old_count, "after": new_count})

    before_control = dict((before.get("operational_postgres") or {}).get("control_state") or {})
    after_control = dict((after.get("operational_postgres") or {}).get("control_state") or {})
    if before_control:
        if not after_control:
            regressions.append({"kind": "risk_control_state_missing"})
        elif bool(before_control.get("operator_stop")) and not bool(after_control.get("operator_stop")):
            regressions.append({
                "kind": "operator_stop_was_cleared",
                "before_reason": before_control.get("reason"),
                "after_reason": after_control.get("reason"),
            })

    before_q = dict((before.get("questdb") or {}).get("tables") or {})
    after_q = dict((after.get("questdb") or {}).get("tables") or {})
    for table, old in before_q.items():
        new = after_q.get(table)
        if new is None:
            regressions.append({"kind": "questdb_table_missing", "table": table})
            continue
        old_count = int((old or {}).get("row_count") or 0)
        new_count = int((new or {}).get("row_count") or 0)
        if new_count < old_count:
            regressions.append({"kind": "questdb_row_regression", "table": table, "before": old_count, "after": new_count})

    before_p = dict(before.get("parquet") or {})
    after_p = dict(after.get("parquet") or {})
    before_parts = dict(before_p.get("parts") or {})
    after_parts = dict(after_p.get("parts") or {})
    for path, old_size in before_parts.items():
        if path not in after_parts:
            regressions.append({"kind": "parquet_part_missing", "path": path})
        elif int(after_parts[path]) != int(old_size):
            regressions.append({"kind": "parquet_immutable_part_changed", "path": path, "before_size": int(old_size), "after_size": int(after_parts[path])})
    before_catalog = dict(before_p.get("candle_catalog") or {})
    after_catalog = dict(after_p.get("candle_catalog") or {})
    if before_catalog.get("present"):
        if not after_catalog.get("present"):
            regressions.append({"kind": "candle_catalog_missing"})
        elif int(after_catalog.get("durable_rows") or 0) < int(before_catalog.get("durable_rows") or 0):
            regressions.append({
                "kind": "candle_catalog_row_regression",
                "before": int(before_catalog.get("durable_rows") or 0),
                "after": int(after_catalog.get("durable_rows") or 0),
            })

    return {
        "ok": not regressions,
        "state": "AUTHORITIES_PRESERVED" if not regressions else "AUTHORITY_RETENTION_REGRESSION",
        "regressions": regressions,
        "before_content_sha256": before.get("content_sha256"),
        "after_content_sha256": after.get("content_sha256"),
    }


def _write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, sort_keys=True, indent=2, default=_json_default) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--install-dir", required=True)
    parser.add_argument("--env-file")
    parser.add_argument("--output", required=True)
    parser.add_argument("--label", default="authority-retention")
    parser.add_argument("--compare-before")
    parser.add_argument("--clean-install", action="store_true")
    args = parser.parse_args()

    install_dir = Path(args.install_dir)
    output = Path(args.output)
    try:
        if args.clean_install:
            payload = clean_install_evidence(install_dir, label=args.label)
        else:
            env_file = Path(args.env_file) if args.env_file else install_dir / "secure" / "data-plane.env.ps1"
            payload = capture(install_dir, env_file, label=args.label)
        if args.compare_before:
            before = json.loads(Path(args.compare_before).read_text(encoding="utf-8-sig"))
            comparison = compare_evidence(before, payload)
            payload["comparison"] = comparison
            payload["ok"] = bool(payload.get("ok")) and bool(comparison.get("ok"))
        _write(output, payload)
        if not payload.get("ok"):
            print(json.dumps({"ok": False, "output": str(output), "comparison": payload.get("comparison")}, default=_json_default))
            return 2
        print(json.dumps({"ok": True, "output": str(output), "content_sha256": payload.get("content_sha256"), "comparison": payload.get("comparison")}, default=_json_default))
        return 0
    except Exception as exc:
        failure = {
            "ok": False,
            "version": VERSION,
            "label": args.label,
            "captured_at": _now(),
            "error": f"{type(exc).__name__}:{exc}",
            "secrets_emitted": False,
        }
        _write(output, failure)
        print(json.dumps(failure))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
