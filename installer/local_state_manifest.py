"""Typed durable local-state preservation authority for the Windows installer.

PowerShell deliberately does not enumerate, normalize, hash, or compare the
preserved file collection.  This helper owns those deterministic semantics and
exchanges only bounded JSON summaries with the installer.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import time
from typing import Any, Iterable


SCHEMA_VERSION = "project-laddu-local-state-manifest-1.0.0"
MANAGED_MUTABLE = frozenset(
    {
        "secure/data-plane.admin.json",
        "secure/data-plane.connectivity.request.json",
        "secure/data-plane.env.ps1",
        "secure/data-plane.prepare-handoff.json",
        "secure/data-plane.prepare-handoff.sha256",
        "secure/data-plane.provision.request.json",
    }
)
COMPATIBILITY_PROJECTION = "data/runtime/compatibility/compatibility_projection.sqlite3"
EPHEMERAL_RUNTIME_LOCK_DIR = "data/runtime/locks"
READ_RETRY_ATTEMPTS = 8
READ_RETRY_SECONDS = 0.25


class ManifestError(RuntimeError):
    """A fail-closed preservation contract violation."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def canonical_sha256(value: Any) -> str:
    material = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def normalize_relative(parts: Iterable[str]) -> str:
    normalized = "/".join(str(part).replace("\\", "/").strip("/") for part in parts)
    while "//" in normalized:
        normalized = normalized.replace("//", "/")
    if not normalized or normalized.startswith("/") or normalized == ".." or "/../" in f"/{normalized}/":
        raise ManifestError(f"unsafe preservation path: {normalized!r}")
    return normalized


def is_excluded(relative: str) -> bool:
    folded = relative.casefold()
    if folded in {item.casefold() for item in MANAGED_MUTABLE}:
        return True
    lock_dir = EPHEMERAL_RUNTIME_LOCK_DIR.casefold()
    if folded.startswith(lock_dir + "/") and folded.endswith(".lock"):
        return True
    projection = COMPATIBILITY_PROJECTION.casefold()
    return folded == projection or folded.startswith(projection + "-")


def _reject_reparse_point(entry: os.DirEntry[str], info: os.stat_result, relative: str) -> None:
    attributes = int(getattr(info, "st_file_attributes", 0))
    reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    if entry.is_symlink() or attributes & reparse_flag:
        raise ManifestError(f"reparse points are forbidden in preserved state: {relative}")


def _hash_stable_file(path: Path, relative: str, initial: os.stat_result) -> tuple[int, str]:
    last_error: OSError | None = None
    for attempt in range(1, READ_RETRY_ATTEMPTS + 1):
        try:
            digest = hashlib.sha256()
            with path.open("rb") as handle:
                while chunk := handle.read(1024 * 1024):
                    digest.update(chunk)
            final = path.stat()
            before = (int(initial.st_size), int(initial.st_mtime_ns))
            after = (int(final.st_size), int(final.st_mtime_ns))
            before_inode = int(getattr(initial, "st_ino", 0))
            after_inode = int(getattr(final, "st_ino", 0))
            if before != after or (before_inode and after_inode and before_inode != after_inode):
                raise ManifestError(f"preserved file changed while hashing: {relative}")
            return int(final.st_size), digest.hexdigest()
        except ManifestError:
            raise
        except OSError as exc:
            last_error = exc
            retryable = isinstance(exc, PermissionError) or getattr(exc, "winerror", None) in {5, 32, 33}
            if not retryable or attempt >= READ_RETRY_ATTEMPTS:
                break
            time.sleep(READ_RETRY_SECONDS)
    raise ManifestError(
        f"cannot read preserved file {relative} after {READ_RETRY_ATTEMPTS} attempts: "
        f"{type(last_error).__name__}: {last_error}"
    ) from last_error


def snapshot(install_dir: Path) -> dict[str, Any]:
    root = install_dir.resolve(strict=False)
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    excluded: list[str] = []

    for authority in ("data", "secure"):
        authority_root = root / authority
        if not authority_root.exists():
            continue
        if not authority_root.is_dir():
            raise ManifestError(f"preserved authority root is not a directory: {authority_root}")
        pending: list[tuple[Path, tuple[str, ...]]] = [(authority_root, (authority,))]
        while pending:
            directory, prefix = pending.pop()
            try:
                entries = sorted(os.scandir(directory), key=lambda item: item.name.casefold())
            except OSError as exc:
                raise ManifestError(f"cannot enumerate preserved directory {directory}: {exc}") from exc
            for entry in entries:
                relative = normalize_relative((*prefix, entry.name))
                if is_excluded(relative):
                    excluded.append(relative)
                    continue
                try:
                    info = entry.stat(follow_symlinks=False)
                except OSError as exc:
                    raise ManifestError(f"cannot inspect preserved path {relative}: {exc}") from exc
                _reject_reparse_point(entry, info, relative)
                mode = info.st_mode
                if stat.S_ISDIR(mode):
                    pending.append((Path(entry.path), (*prefix, entry.name)))
                    continue
                if not stat.S_ISREG(mode):
                    raise ManifestError(f"unsupported preserved path type: {relative}")
                folded = relative.casefold()
                if folded in seen:
                    raise ManifestError(f"case-insensitive duplicate preservation path: {relative}")
                seen.add(folded)
                size, digest = _hash_stable_file(Path(entry.path), relative, info)
                rows.append({"path": relative, "size": size, "sha256": digest})

    rows.sort(key=lambda row: str(row["path"]).casefold())
    excluded.sort(key=str.casefold)
    return {
        "schema_version": SCHEMA_VERSION,
        "captured_at": utc_now(),
        "install_dir": str(root),
        "authorities": ["data", "secure"],
        "files": rows,
        "file_count": len(rows),
        "excluded_paths": excluded,
        "content_sha256": canonical_sha256(rows),
    }


def load_manifest(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ManifestError(f"cannot read preservation manifest {path}: {exc}") from exc
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise ManifestError(f"unsupported preservation manifest schema: {payload.get('schema_version')!r}")
    rows = payload.get("files")
    if not isinstance(rows, list) or payload.get("file_count") != len(rows):
        raise ManifestError("preservation manifest file collection/count mismatch")
    if payload.get("content_sha256") != canonical_sha256(rows):
        raise ManifestError("preservation manifest content hash mismatch")
    return payload


def compare(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    before_rows = {str(row["path"]).casefold(): row for row in before["files"]}
    after_rows = {str(row["path"]).casefold(): row for row in after["files"]}
    missing: list[str] = []
    changed: list[str] = []
    for key, expected in before_rows.items():
        current = after_rows.get(key)
        if current is None:
            missing.append(str(expected["path"]))
        elif int(current["size"]) != int(expected["size"]) or str(current["sha256"]) != str(expected["sha256"]):
            changed.append(str(expected["path"]))
    added = [str(row["path"]) for key, row in after_rows.items() if key not in before_rows]
    missing.sort(key=str.casefold)
    changed.sort(key=str.casefold)
    added.sort(key=str.casefold)
    failures = [*(f"MISSING:{path}" for path in missing), *(f"CHANGED:{path}" for path in changed)]
    return {
        "schema_version": "project-laddu-local-state-proof-1.0.0",
        "proved_at": utc_now(),
        "ok": not failures,
        "before_content_sha256": before["content_sha256"],
        "after_content_sha256": after["content_sha256"],
        "preserved_files": len(before_rows),
        "checked_after": len(after_rows),
        "added_count": len(added),
        "added": added,
        "missing": missing,
        "changed": changed,
        "failures": failures,
    }


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    material = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    try:
        temporary.write_text(material, encoding="utf-8", newline="\n")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def command_presence(args: argparse.Namespace) -> dict[str, Any]:
    root = args.install_dir
    present = False
    for authority in ("data", "secure"):
        path = root / authority
        if path.is_dir() and next(path.iterdir(), None) is not None:
            present = True
            break
    return {"ok": True, "schema_version": SCHEMA_VERSION, "present": present}


def command_snapshot(args: argparse.Namespace) -> dict[str, Any]:
    payload = snapshot(args.install_dir)
    write_json(args.output, payload)
    return {
        "ok": True,
        "schema_version": SCHEMA_VERSION,
        "output": str(args.output.resolve()),
        "file_count": payload["file_count"],
        "excluded_count": len(payload["excluded_paths"]),
        "content_sha256": payload["content_sha256"],
    }


def command_verify(args: argparse.Namespace) -> dict[str, Any]:
    before = load_manifest(args.before)
    after = snapshot(args.install_dir)
    proof = compare(before, after)
    write_json(args.after_output, after)
    write_json(args.proof_output, proof)
    if not proof["ok"]:
        raise ManifestError("preserved local state changed: " + ", ".join(proof["failures"]))
    return {
        "ok": True,
        "schema_version": proof["schema_version"],
        "after_output": str(args.after_output.resolve()),
        "proof_output": str(args.proof_output.resolve()),
        "preserved_files": proof["preserved_files"],
        "checked_after": proof["checked_after"],
        "added_count": proof["added_count"],
    }


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser()
    sub = result.add_subparsers(dest="command", required=True)
    presence = sub.add_parser("presence")
    presence.add_argument("--install-dir", required=True, type=Path)
    presence.set_defaults(handler=command_presence)
    capture = sub.add_parser("snapshot")
    capture.add_argument("--install-dir", required=True, type=Path)
    capture.add_argument("--output", required=True, type=Path)
    capture.set_defaults(handler=command_snapshot)
    verify = sub.add_parser("verify")
    verify.add_argument("--install-dir", required=True, type=Path)
    verify.add_argument("--before", required=True, type=Path)
    verify.add_argument("--after-output", required=True, type=Path)
    verify.add_argument("--proof-output", required=True, type=Path)
    verify.set_defaults(handler=command_verify)
    return result


def main() -> int:
    args = parser().parse_args()
    try:
        result = args.handler(args)
    except (ManifestError, OSError, KeyError, TypeError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
