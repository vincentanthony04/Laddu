"""Bind installed Project Laddu runtime to this exact sealed package.

The installer records immutable package hashes in DEPLOY_MANIFEST.json after the
payload swap.  This verifier independently recomputes the same backend/frontend
tree hashes plus release identity, attestation, and package-manifest hashes from
the candidate and from the installed target.  Runtime data/log/secure directories
are intentionally outside this identity proof.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

VERSION = "installed-package-binding-1.0.0"


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tree_hash(root: Path) -> str:
    material: list[str] = []
    for path in sorted((p for p in root.rglob("*") if p.is_file()), key=lambda p: p.as_posix().lower()):
        rel = path.relative_to(root).as_posix()
        if "__pycache__" in path.parts or path.suffix.lower() in {".pyc", ".pyo"}:
            continue
        material.append(f"{rel}\t{sha(path).upper()}")
    return hashlib.sha256("\n".join(material).encode("utf-8")).hexdigest().upper()


def verify(package_root: Path, install_dir: Path) -> dict[str, Any]:
    identity = json.loads((package_root / "RELEASE_IDENTITY.json").read_text(encoding="utf-8-sig"))
    deploy_path = install_dir / "DEPLOY_MANIFEST.json"
    if not deploy_path.is_file():
        return {"ok": False, "version": VERSION, "blockers": ["INSTALLED_DEPLOY_MANIFEST_MISSING"]}
    deploy = json.loads(deploy_path.read_text(encoding="utf-8-sig"))
    checks: dict[str, bool] = {}
    details: dict[str, Any] = {}
    package_identity = sha(package_root / "RELEASE_IDENTITY.json")
    package_attestation = sha(package_root / "RELEASE_ATTESTATION.json")
    package_manifest = sha(package_root / "validation" / "package_manifest.sha256")
    package_backend = tree_hash(package_root / "backend")
    package_frontend = tree_hash(package_root / "frontend")
    installed_identity = sha(install_dir / "RELEASE_IDENTITY.json") if (install_dir / "RELEASE_IDENTITY.json").is_file() else ""
    installed_attestation = sha(install_dir / "RELEASE_ATTESTATION.json") if (install_dir / "RELEASE_ATTESTATION.json").is_file() else ""
    installed_manifest = sha(install_dir / "validation" / "package_manifest.sha256") if (install_dir / "validation" / "package_manifest.sha256").is_file() else ""
    installed_backend = tree_hash(install_dir / "backend") if (install_dir / "backend").is_dir() else ""
    installed_frontend = tree_hash(install_dir / "frontend") if (install_dir / "frontend").is_dir() else ""
    expected_version = str(identity.get("version") or "")
    checks["source_version_exact"] = str(deploy.get("source_version") or "") == expected_version
    checks["release_identity_exact"] = package_identity == installed_identity == str(deploy.get("release_identity_hash") or "").lower()
    checks["release_attestation_exact"] = package_attestation == installed_attestation == str(deploy.get("release_attestation_hash") or "").lower()
    checks["package_manifest_exact"] = package_manifest == installed_manifest == str(deploy.get("package_manifest_hash") or "").lower()
    checks["backend_tree_exact"] = package_backend == installed_backend == str(deploy.get("backend_hash") or "").upper()
    checks["frontend_tree_exact"] = package_frontend == installed_frontend == str(deploy.get("frontend_hash") or "").upper()
    checks["frontend_owner_exact"] = str(deploy.get("frontend_owner") or "") == f"standalone-{expected_version}"
    details.update({
        "expected_version": expected_version,
        "deploy_source_version": deploy.get("source_version"),
        "package_manifest_sha256": package_manifest,
        "installed_manifest_sha256": installed_manifest,
        "package_backend_tree_sha256": package_backend,
        "installed_backend_tree_sha256": installed_backend,
        "package_frontend_tree_sha256": package_frontend,
        "installed_frontend_tree_sha256": installed_frontend,
    })
    blockers = [name for name, ok in checks.items() if not ok]
    return {
        "ok": not blockers,
        "version": VERSION,
        "checks": checks,
        "blockers": blockers,
        "details": details,
        "install_dir": str(install_dir),
        "broker_authority": "NONE",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--package-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--install-dir", type=Path, default=Path(r"C:\ProgramData\ProjectLaddu"))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        result = verify(args.package_root.resolve(), args.install_dir)
    except Exception as exc:
        result = {"ok": False, "version": VERSION, "blockers": [f"{type(exc).__name__}:{exc}"]}
    text = json.dumps(result, indent=2, default=str)
    print(text)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", encoding="utf-8")
    return 0 if result.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
