"""Fail-closed package inventory, manifest and lineage-attestation verification."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
from typing import Iterable

MANIFEST_REL = "validation/package_manifest.sha256"
ATTESTATION_REL = "RELEASE_ATTESTATION.json"
SOURCE_METADATA_EXCLUSIONS = {ATTESTATION_REL, "validation/package_allowlist.json", MANIFEST_REL}


def eligible_source_files(root: Path) -> list[Path]:
    """Return the deterministic source-attestation scope for this package.

    Generated package metadata is intentionally excluded to avoid circular hashes.
    Runtime bytecode/log artefacts are never valid source members.
    """
    root = Path(root).resolve()
    files: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if rel in SOURCE_METADATA_EXCLUSIONS or ".git" in path.relative_to(root).parts:
            continue
        if "__pycache__" in path.parts or path.suffix.lower() in {".pyc", ".pyo", ".log"}:
            continue
        files.append(path)
    return sorted(files, key=lambda path: path.relative_to(root).as_posix())


def digest_material(files: Iterable[Path], root: Path) -> tuple[str, str, int]:
    """Recompute the attested source-tree and inventory hashes."""
    root = Path(root).resolve()
    tree = hashlib.sha256()
    inventory = hashlib.sha256()
    count = 0
    for path in sorted((Path(p).resolve() for p in files), key=lambda p: p.relative_to(root).as_posix()):
        rel = path.relative_to(root).as_posix()
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        tree.update(f"{digest}  {rel}\n".encode("utf-8"))
        inventory.update(f"{rel}\n".encode("utf-8"))
        count += 1
    return tree.hexdigest(), inventory.hexdigest(), count


def _safe_rel(value: str) -> str:
    rel = str(value or "").replace("\\", "/").strip()
    posix = PurePosixPath(rel)
    if not rel or posix.is_absolute() or ".." in posix.parts or (posix.parts and ":" in posix.parts[0]):
        raise RuntimeError(f"unsafe package path: {value!r}")
    return posix.as_posix()


def parse_manifest(root: Path) -> dict[str, str]:
    manifest = root / MANIFEST_REL
    if not manifest.is_file():
        raise RuntimeError("package integrity manifest is missing")
    entries: dict[str, str] = {}
    for line_no, line in enumerate(manifest.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        parts = line.split("  ", 1)
        if len(parts) != 2 or len(parts[0].strip()) != 64:
            raise RuntimeError(f"malformed package manifest line {line_no}")
        digest = parts[0].strip().lower()
        rel = _safe_rel(parts[1])
        if rel == MANIFEST_REL:
            raise RuntimeError("manifest must not self-hash")
        if rel in entries:
            raise RuntimeError(f"duplicate package manifest member: {rel}")
        entries[rel] = digest
    if not entries:
        raise RuntimeError("package manifest is empty")
    return entries


def actual_files(root: Path) -> set[str]:
    return {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and ".git" not in path.relative_to(root).parts
    }


def verify_exact_inventory_and_hashes(root: Path) -> dict[str, object]:
    entries = parse_manifest(root)
    expected = set(entries) | {MANIFEST_REL}
    actual = actual_files(root)
    missing = sorted(expected - actual)
    extras = sorted(actual - expected)
    if missing:
        raise RuntimeError("package inventory missing files: " + ", ".join(missing[:20]))
    if extras:
        raise RuntimeError("package inventory contains unmanifested files: " + ", ".join(extras[:20]))
    for rel, expected_digest in entries.items():
        path = root / rel
        actual_digest = hashlib.sha256(path.read_bytes()).hexdigest().lower()
        if actual_digest != expected_digest:
            raise RuntimeError(f"package checksum mismatch: {rel}")

    # The generated allowlist must equal manifest membership exactly.  This
    # prevents a stale allowlist from being used to package a different tree.
    identity = json.loads((root / "RELEASE_IDENTITY.json").read_text(encoding="utf-8-sig"))
    allowlist_path = root / "validation" / "package_allowlist.json"
    if not allowlist_path.is_file():
        raise RuntimeError("current release allowlist is missing")
    allowlist = json.loads(allowlist_path.read_text(encoding="utf-8-sig"))
    allow_files = {_safe_rel(item) for item in list(allowlist.get("files") or [])}
    if allow_files != set(entries):
        omitted = sorted(set(entries) - allow_files)
        stale = sorted(allow_files - set(entries))
        raise RuntimeError(
            "allowlist/manifest membership mismatch"
            + (f" omitted={omitted[:10]}" if omitted else "")
            + (f" stale={stale[:10]}" if stale else "")
        )
    return {
        "manifest_files": len(entries),
        "actual_files": len(actual),
        "allowlist_files": len(allow_files),
        "manifest_sha256": hashlib.sha256((root / MANIFEST_REL).read_bytes()).hexdigest(),
        "allowlist_sha256": hashlib.sha256(allowlist_path.read_bytes()).hexdigest(),
    }


def verify_attestation(root: Path, *, verify_source_tree: bool = True) -> dict[str, object]:
    identity = json.loads((root / "RELEASE_IDENTITY.json").read_text(encoding="utf-8-sig"))
    attestation = json.loads((root / ATTESTATION_REL).read_text(encoding="utf-8-sig"))
    if attestation.get("version") != identity.get("version"):
        raise RuntimeError("release attestation version mismatch")
    if attestation.get("artifact_type") != identity.get("artifact_type"):
        raise RuntimeError("release attestation artifact type mismatch")
    if bool(attestation.get("installable")) != bool(identity.get("installable")):
        raise RuntimeError("release attestation installability mismatch")
    if bool(attestation.get("production_ready")) != bool(identity.get("production_ready")):
        raise RuntimeError("release attestation production-ready mismatch")
    if str(attestation.get("installation_purpose") or "") != str(identity.get("installation_purpose") or ""):
        raise RuntimeError("release attestation installation-purpose mismatch")
    if identity.get("artifact_type") == "INSTALLATION_CANDIDATE":
        certification = dict(attestation.get("certification") or {})
        if certification.get("SOURCE_SEALED") != "PASS" or certification.get("current_level") != "SOURCE_SEALED":
            raise RuntimeError("installation candidate is not source-sealed")
        if certification.get("INSTALLABLE") != "PENDING_INSTALLED_PROOF":
            raise RuntimeError("installation candidate overstates installed proof")
        if bool(attestation.get("production_ready")):
            raise RuntimeError("installation candidate cannot be production-ready")
    if dict(attestation.get("parent") or {}) != dict(identity.get("parent") or {}):
        raise RuntimeError("release attestation parent mismatch")
    result = {
        "attestation_sha256": hashlib.sha256((root / ATTESTATION_REL).read_bytes()).hexdigest(),
        "source_tree_sha256": attestation.get("source_tree_sha256"),
        "source_inventory_sha256": attestation.get("source_inventory_sha256"),
        "source_file_count": int(attestation.get("source_file_count_excluding_generated_package_metadata") or -1),
        "source_tree_recomputed": bool(verify_source_tree),
    }
    if verify_source_tree:
        source_tree, inventory, count = digest_material(eligible_source_files(root), root)
        if source_tree != attestation.get("source_tree_sha256"):
            raise RuntimeError("release attestation source-tree hash mismatch")
        if inventory != attestation.get("source_inventory_sha256"):
            raise RuntimeError("release attestation source-inventory hash mismatch")
        if count != int(attestation.get("source_file_count_excluding_generated_package_metadata") or -1):
            raise RuntimeError("release attestation source-file count mismatch")
        result.update({"source_tree_sha256": source_tree, "source_inventory_sha256": inventory, "source_file_count": count})
    return result
