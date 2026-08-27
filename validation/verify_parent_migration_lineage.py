from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "validation") not in sys.path:
    sys.path.insert(0, str(ROOT / "validation"))
from capture_authority_retention_evidence import EvidenceError, parse_data_plane_env, _questdb_query


class PlaneAccessError(RuntimeError):
    def __init__(self, failure_class: str, detail: str):
        super().__init__(f"{failure_class}:{detail}")
        self.failure_class = failure_class
        self.detail = detail


def _classify_postgres_error(exc: BaseException) -> str:
    name = type(exc).__name__
    if name == "InvalidPassword":
        return "AUTHENTICATION_FAILED"
    if name == "InvalidCatalogName":
        return "DATABASE_NOT_FOUND"
    if name in {
        "ConnectionTimeout",
        "OperationalError",
        "ConnectionFailure",
        "CannotConnectNow",
        "AdminShutdown",
        "CrashShutdown",
    }:
        return "DATABASE_UNREACHABLE"
    return "POSTGRES_ACCESS_FAILED"


def _connect_with_retry(psycopg, dsn: str, *, total_seconds: float = 30.0):
    deadline = time.monotonic() + total_seconds
    last: BaseException | None = None
    while time.monotonic() < deadline:
        try:
            return psycopg.connect(dsn, autocommit=True, connect_timeout=3)
        except Exception as exc:  # psycopg error hierarchy differs by failure mode
            last = exc
            failure_class = _classify_postgres_error(exc)
            if failure_class in {"AUTHENTICATION_FAILED", "DATABASE_NOT_FOUND"}:
                break
            time.sleep(1.0)
    assert last is not None
    raise PlaneAccessError(_classify_postgres_error(last), type(last).__name__) from last


def postgres_ledger(dsn: str, entries: list[dict]) -> tuple[list[dict], list[str]]:
    try:
        import psycopg
    except Exception as exc:
        raise PlaneAccessError("PSYCOPG_UNAVAILABLE", f"{type(exc).__name__}:{exc}") from exc
    evidence: list[dict] = []
    failures: list[str] = []
    try:
        conn = _connect_with_retry(psycopg, dsn)
        with conn:
            with conn.cursor() as cur:
                cur.execute("SET statement_timeout='4000ms'")
                cur.execute("SELECT to_regclass('runtime_control.schema_migrations')")
                if cur.fetchone()[0] is None:
                    return evidence, ["SCHEMA_MIGRATION_LEDGER_MISSING"]
                for entry in entries:
                    if not entry.get("parent_required", False):
                        continue
                    cur.execute(
                        "SELECT name,content_sha256 FROM runtime_control.schema_migrations WHERE version=%s",
                        (int(entry["version"]),),
                    )
                    row = cur.fetchone()
                    if row is None:
                        failures.append(f"MISSING:{entry['version']}:{entry['name']}")
                        evidence.append({"version": int(entry["version"]), "name": entry["name"], "state": "missing"})
                        continue
                    name, digest = str(row[0]), str(row[1])
                    ok = name == entry["name"] and digest == entry["sha256"]
                    evidence.append({"version": int(entry["version"]), "name": name, "sha256": digest, "ok": ok})
                    if not ok:
                        failures.append(f"IMMUTABILITY_MISMATCH:{entry['version']}:{name}:{digest}")
    except PlaneAccessError:
        raise
    except Exception as exc:
        raise PlaneAccessError(_classify_postgres_error(exc), type(exc).__name__) from exc
    return evidence, failures


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--install-dir", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    plan = json.loads((ROOT / "infra/postgres/MIGRATION_PLAN.json").read_text(encoding="utf-8"))
    env = parse_data_plane_env(args.env_file)

    operational: list[dict] = []
    governance: list[dict] = []
    op_fail: list[str] = []
    gov_fail: list[str] = []
    access_failures: list[str] = []

    try:
        operational, op_fail = postgres_ledger(env["PROJECT_LADDU_OPERATIONAL_DSN"], plan["operational"])
    except PlaneAccessError as exc:
        access_failures.append(f"operational:{exc.failure_class}:{exc.detail}")
    try:
        governance, gov_fail = postgres_ledger(env["PROJECT_LADDU_GOVERNANCE_DSN"], plan["governance"])
    except PlaneAccessError as exc:
        access_failures.append(f"governance:{exc.failure_class}:{exc.detail}")

    quest_fail: list[str] = []
    quest_tables: list[str] = []
    try:
        _questdb_query(
            env["PROJECT_LADDU_QUESTDB_HTTP_URL"],
            "select table_name from tables() where table_name in ('market_ticks','market_bars','market_data_quality_events')",
            env,
        )
        for table in ("market_ticks", "market_bars", "market_data_quality_events"):
            _questdb_query(env["PROJECT_LADDU_QUESTDB_HTTP_URL"], f"SELECT count() AS row_count FROM {table}", env)
            quest_tables.append(table)
    except Exception as exc:
        quest_fail.append(f"QUESTDB_PARENT_SCHEMA_MISSING_OR_UNREADABLE:{type(exc).__name__}:{exc}")

    ledger_failures = [f"operational:{x}" for x in op_fail] + [f"governance:{x}" for x in gov_fail]
    failures = access_failures + ledger_failures + quest_fail
    if access_failures:
        failure_class = "DATABASE_CONNECTIVITY_OR_AUTHORITY"
        exit_code = 20
    elif ledger_failures:
        failure_class = "MIGRATION_LEDGER_OR_IMMUTABILITY"
        exit_code = 30
    elif quest_fail:
        failure_class = "QUESTDB_PARENT_SCHEMA"
        exit_code = 31
    else:
        failure_class = None
        exit_code = 0

    result = {
        "ok": not failures,
        "contract_version": "parent-migration-lineage-verification-2.1.0",
        "authoritative_parent": plan["authoritative_parent"],
        "operational": operational,
        "governance": governance,
        "questdb_required_tables": quest_tables,
        "failure_class": failure_class,
        "failures": failures,
        "secrets_emitted": False,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(result, indent=2, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
