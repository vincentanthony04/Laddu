from __future__ import annotations

import argparse
import json
from pathlib import Path
import time


def classify(exc: BaseException) -> str:
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


def prove_plane(psycopg, name: str, spec: dict[str, str], total_seconds: float) -> dict[str, object]:
    deadline = time.monotonic() + total_seconds
    last: BaseException | None = None
    while time.monotonic() < deadline:
        try:
            with psycopg.connect(spec["dsn"], autocommit=True, connect_timeout=3) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1,current_database(),current_user")
                    one, database, user = cur.fetchone()
                    if int(one) != 1:
                        raise RuntimeError(f"SELECT 1 returned {one!r}")
                    if str(database) != spec["database"] or str(user) != spec["user"]:
                        raise RuntimeError(f"identity mismatch database={database} user={user}")
            return {
                "plane": name,
                "state": "PASS",
                "database": spec["database"],
                "user": spec["user"],
                "failure_class": None,
            }
        except Exception as exc:
            last = exc
            failure_class = classify(exc)
            if failure_class in {"AUTHENTICATION_FAILED", "DATABASE_NOT_FOUND"}:
                break
            time.sleep(1.0)
    assert last is not None
    return {
        "plane": name,
        "state": "FAIL",
        "failure_class": classify(last),
        "error_type": type(last).__name__,
        "detail": str(last)[:240],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("request", type=Path)
    parser.add_argument("--retry-seconds", type=float, default=35.0)
    args = parser.parse_args()

    try:
        import psycopg
    except Exception as exc:
        print(json.dumps({
            "state": "FAIL",
            "failure_class": "PSYCOPG_UNAVAILABLE",
            "error_type": type(exc).__name__,
            "detail": str(exc)[:240],
            "secrets_emitted": False,
        }, sort_keys=True))
        return 21

    request = json.loads(args.request.read_text(encoding="utf-8-sig"))
    results = [
        prove_plane(psycopg, "operational", request["operational"], args.retry_seconds),
        prove_plane(psycopg, "governance", request["governance"], args.retry_seconds),
    ]
    failures = [item for item in results if item["state"] != "PASS"]
    if failures:
        failure_classes = sorted({str(item.get("failure_class") or "POSTGRES_ACCESS_FAILED") for item in failures})
        print(json.dumps({
            "state": "FAIL",
            "failure_class": "+".join(failure_classes),
            "results": results,
            "secrets_emitted": False,
        }, sort_keys=True))
        return 20

    print(json.dumps({
        "state": "PASS",
        "planes": 2,
        "results": results,
        "secrets_emitted": False,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
