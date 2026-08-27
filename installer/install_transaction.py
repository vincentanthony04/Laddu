"""Durable, release-bound installer transaction state machine."""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
from typing import Any


SCHEMA_VERSION = "project-laddu-install-transaction-1.0.0"
PHASES = (
    "BEGIN",
    "PACKAGE_PROOF",
    "ENVIRONMENT_PROOF",
    "DATA_AUTHORITY_PROOF",
    "RETENTION_SNAPSHOT",
    "STAGED",
    "RUNTIME_QUIESCED",
    "DURABLE_STATE_PROOF",
    "SCHEMA_APPLIED",
    "RETENTION_PROOF",
    "RESEARCH_GOVERNANCE_MIGRATED",
    "PAYLOAD_ACTIVATED",
    "SECURE_DATA_PRESERVED",
    "SERVICE_STARTED",
    "BACKEND_READY",
    "FRONTEND_IDENTITY",
    "OPERATIONAL_PROOF",
    "COMMIT",
)


class TransactionError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def digest(payload: dict[str, Any]) -> str:
    body = dict(payload)
    body.pop("content_sha256", None)
    material = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def write_journal(path: Path, payload: dict[str, Any]) -> None:
    payload["content_sha256"] = digest(payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def load_journal(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TransactionError(f"cannot read transaction journal {path}: {exc}") from exc
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise TransactionError("transaction journal schema mismatch")
    if payload.get("content_sha256") != digest(payload):
        raise TransactionError("transaction journal content hash mismatch")
    return payload


def event(state: str, detail: str = "") -> dict[str, str]:
    return {"state": state, "recorded_at": utc_now(), "detail": detail}


def ensure_active(payload: dict[str, Any]) -> None:
    if payload.get("status") != "ACTIVE":
        raise TransactionError(f"transaction is not active: {payload.get('status')}")


def command_begin(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    if args.journal.exists():
        raise TransactionError(f"refusing to overwrite transaction journal: {args.journal}")
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "transaction_id": args.transaction_id,
        "release_version": args.release_version,
        "release_identity_sha256": args.release_identity_sha256.lower(),
        "package_root": str(args.package_root.resolve()),
        "install_dir": str(args.install_dir.resolve(strict=False)),
        "previous_version": args.previous_version,
        "prior_runtime_running": bool(args.prior_runtime_running),
        "status": "ACTIVE",
        "current_phase": "BEGIN",
        "events": [event("BEGIN", "transaction created before target mutation")],
    }
    write_journal(args.journal, payload)
    return summary(payload, args.journal), 0


def command_advance(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    payload = load_journal(args.journal)
    ensure_active(payload)
    current = str(payload["current_phase"])
    try:
        expected = PHASES[PHASES.index(current) + 1]
    except (ValueError, IndexError) as exc:
        raise TransactionError(f"no legal phase follows {current}") from exc
    if args.phase != expected or args.phase == "COMMIT":
        raise TransactionError(f"illegal installer transition {current} -> {args.phase}; expected {expected}")
    payload["current_phase"] = args.phase
    payload["events"].append(event(args.phase, args.detail))
    write_journal(args.journal, payload)
    result = summary(payload, args.journal)
    if args.fault_after and args.fault_after.upper() == args.phase:
        result["fault_injected"] = True
        return result, 86
    return result, 0


def command_commit(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    payload = load_journal(args.journal)
    ensure_active(payload)
    if payload.get("current_phase") != "OPERATIONAL_PROOF":
        raise TransactionError("COMMIT requires OPERATIONAL_PROOF")
    payload["current_phase"] = "COMMIT"
    payload["status"] = "COMMITTED"
    payload["events"].append(event("COMMIT", args.detail or "all required target gates passed"))
    write_journal(args.journal, payload)
    return summary(payload, args.journal), 0


def command_fail(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    payload = load_journal(args.journal)
    ensure_active(payload)
    if payload.get("current_phase") == "COMMIT":
        raise TransactionError("committed transaction cannot be failed or rolled back")
    rollback = args.rollback_state.upper()
    if rollback not in {"NOT_REQUIRED", "PRIOR_RUNTIME_RESTORED", "CLEAN_TARGET_RESTORED", "PARTIAL"}:
        raise TransactionError(f"unsupported rollback state: {rollback}")
    if args.failure_b64:
        try:
            failure = base64.b64decode(args.failure_b64.encode("ascii"), validate=True).decode("utf-8")
        except (ValueError, UnicodeDecodeError) as exc:
            raise TransactionError(f"invalid --failure-b64 payload: {exc}") from exc
    else:
        failure = args.failure
    if not failure:
        raise TransactionError("failure reason is required")
    payload["failed_phase"] = payload["current_phase"]
    payload["failure"] = failure
    payload["rollback_state"] = rollback
    payload["status"] = "ROLLBACK_FAILED" if rollback == "PARTIAL" else "ABORTED_SAFE"
    payload["events"].append(event("FAILURE", failure))
    payload["events"].append(event("ROLLBACK", rollback))
    write_journal(args.journal, payload)
    return summary(payload, args.journal), 0


def command_status(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    return summary(load_journal(args.journal), args.journal), 0


def summary(payload: dict[str, Any], path: Path) -> dict[str, Any]:
    return {
        "ok": payload.get("status") != "ROLLBACK_FAILED",
        "schema_version": SCHEMA_VERSION,
        "transaction_id": payload.get("transaction_id"),
        "status": payload.get("status"),
        "current_phase": payload.get("current_phase"),
        "failed_phase": payload.get("failed_phase"),
        "rollback_state": payload.get("rollback_state"),
        "journal": str(path.resolve()),
        "content_sha256": payload.get("content_sha256"),
    }


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser()
    sub = result.add_subparsers(dest="command", required=True)
    begin = sub.add_parser("begin")
    begin.add_argument("--journal", required=True, type=Path)
    begin.add_argument("--transaction-id", required=True)
    begin.add_argument("--release-version", required=True)
    begin.add_argument("--release-identity-sha256", required=True)
    begin.add_argument("--package-root", required=True, type=Path)
    begin.add_argument("--install-dir", required=True, type=Path)
    begin.add_argument("--previous-version", default="")
    begin.add_argument("--prior-runtime-running", action="store_true")
    begin.set_defaults(handler=command_begin)
    advance = sub.add_parser("advance")
    advance.add_argument("--journal", required=True, type=Path)
    advance.add_argument("--phase", required=True, choices=PHASES[1:-1])
    advance.add_argument("--detail", default="")
    advance.add_argument("--fault-after", default="")
    advance.set_defaults(handler=command_advance)
    commit = sub.add_parser("commit")
    commit.add_argument("--journal", required=True, type=Path)
    commit.add_argument("--detail", default="")
    commit.set_defaults(handler=command_commit)
    fail = sub.add_parser("fail")
    fail.add_argument("--journal", required=True, type=Path)
    failure_group = fail.add_mutually_exclusive_group(required=True)
    failure_group.add_argument("--failure")
    failure_group.add_argument("--failure-b64", default="")
    fail.add_argument("--rollback-state", required=True)
    fail.set_defaults(handler=command_fail)
    status = sub.add_parser("status")
    status.add_argument("--journal", required=True, type=Path)
    status.set_defaults(handler=command_status)
    return result


def main() -> int:
    args = parser().parse_args()
    try:
        result, code = args.handler(args)
    except (TransactionError, OSError, KeyError, TypeError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
