#!/usr/bin/env python3
"""Non-destructive exact-build target probes for installed Project Laddu.

These are target-runtime fault contracts, not production mutations.  The probes
exercise the installed Python/HTTP/PostgreSQL boundaries and emit machine-readable
scenario results consumed by VERIFY_INSTALLED_PRODUCT.ps1.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


def _http_json(base: str, path: str, timeout: float = 10.0) -> dict[str, Any]:
    with urllib.request.urlopen(base.rstrip("/") + path, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def postgres_transaction_resilience() -> dict[str, Any]:
    dsn = os.environ.get("PROJECT_LADDU_OPERATIONAL_DSN", "").strip()
    if not dsn:
        return {"ok": False, "state": "TARGET_PENDING", "reason": "PROJECT_LADDU_OPERATIONAL_DSN_NOT_LOADED"}
    from core.data_plane.postgres import PostgresAuthority

    authority = PostgresAuthority(dsn, role="installed-proof-rollback", min_size=1, max_size=1)
    initial = None
    after = None
    injected_error = None
    try:
        initial = authority.execute("SELECT 1 AS value", fetch="one")
        try:
            with authority.transaction(statement_timeout_ms=1500) as conn:
                with conn.cursor() as cur:
                    # Deterministic server-side error. No table/data mutation occurs.
                    cur.execute("SELECT 1/0 AS forced_failure")
        except Exception as exc:  # expected
            injected_error = type(exc).__name__
        after = authority.execute("SELECT 1 AS value", fetch="one")
        ok = bool(initial and after and injected_error)
        return {
            "ok": ok,
            "state": "PASS" if ok else "FAIL",
            "injected_error": injected_error,
            "before": initial,
            "after": after,
            "pool_health": authority.pool_health(),
            "non_destructive": True,
        }
    except Exception as exc:
        return {"ok": False, "state": "FAIL", "error": f"{type(exc).__name__}: {exc}", "non_destructive": True}
    finally:
        try:
            authority.close()
        except Exception:
            pass


def http_disconnect_resilience(base: str) -> dict[str, Any]:
    from urllib.parse import urlparse

    parsed = urlparse(base)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 8086
    request = (
        "GET /api/chart-data?symbol=TCS&interval=day&limit=520 HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        "Connection: close\r\n\r\n"
    ).encode("ascii")
    try:
        sock = socket.create_connection((host, port), timeout=5)
        sock.sendall(request)
        # Close immediately without reading the response: a real client disconnect.
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        sock.close()
        time.sleep(1.0)
        ready = _http_json(base, "/api/ready", 8)
        logs = _http_json(base, "/api/operations/logs?limit=250", 8)
        text = json.dumps(logs, ensure_ascii=False).lower()
        forbidden = [
            "cannot write to closing transport",
            "brokenpipeerror",
            "connectionreseterror",
            "headers already sent",
            "double response",
        ]
        hits = [token for token in forbidden if token in text]
        ok = ready.get("ready") is True and not hits
        return {
            "ok": ok,
            "state": "PASS" if ok else "FAIL",
            "ready_version": ready.get("version"),
            "forbidden_log_hits": hits,
            "non_destructive": True,
        }
    except Exception as exc:
        return {"ok": False, "state": "FAIL", "error": f"{type(exc).__name__}: {exc}", "non_destructive": True}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8086")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    scenarios = {
        "postgres_transaction_resilience": postgres_transaction_resilience(),
        "http_disconnect_resilience": http_disconnect_resilience(args.base_url),
    }
    payload = {
        "ok": all(row.get("ok") is True for row in scenarios.values()),
        "authority": "InstalledFaultContractProbe",
        "authority_version": "1.0.0",
        "captured_at_epoch": time.time(),
        "scenarios": scenarios,
        "production_change_allowed": False,
        "broker_authority": "NONE",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")
    print(json.dumps(payload, indent=2, default=str))
    return 0 if payload["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
