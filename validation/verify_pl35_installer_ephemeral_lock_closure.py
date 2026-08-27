from __future__ import annotations
import importlib.util
import json
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "installer" / "local_state_manifest.py"


def load_module():
    spec = importlib.util.spec_from_file_location("pl35_local_state_manifest", TARGET)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load local_state_manifest")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    failures: list[str] = []
    mod = load_module()
    cases = {
        "data/runtime/locks/analytical-pipeline.lock": True,
        "data/runtime/locks/ai-training.lock": True,
        "data/runtime/locks/nse-official-source-cycle.lock": True,
        "data/runtime/locks/research-catalog.lock": True,
        "data/runtime/locks/durable.json": False,
        "data/other.lock": False,
        "secure/operator.lock": False,
    }
    for rel, expected in cases.items():
        actual = bool(mod.is_excluded(rel))
        if actual != expected:
            failures.append(f"is_excluded mismatch {rel}: {actual} != {expected}")

    # Exact Windows failure regression: a runtime lock file must never enter the
    # preserved-file hash path, while adjacent durable data remains hashed.
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "data/runtime/locks").mkdir(parents=True)
        (root / "secure").mkdir(parents=True)
        lock = root / "data/runtime/locks/analytical-pipeline.lock"
        lock.write_bytes(b"transient-lock")
        durable = root / "data/runtime/durable-state.json"
        durable.parent.mkdir(parents=True, exist_ok=True)
        durable.write_text('{"durable":true}', encoding="utf-8")
        secure = root / "secure/retained.secret"
        secure.write_text("retained", encoding="utf-8")

        original_hash = mod._hash_stable_file
        attempted: list[str] = []
        def guarded_hash(path, relative, initial):
            attempted.append(relative)
            if relative.casefold() == "data/runtime/locks/analytical-pipeline.lock":
                raise PermissionError(13, "Permission denied")
            return original_hash(path, relative, initial)
        mod._hash_stable_file = guarded_hash
        snap = mod.snapshot(root)
        paths = {row["path"] for row in snap["files"]}
        excluded = set(snap["excluded_paths"])
        if "data/runtime/locks/analytical-pipeline.lock" not in excluded:
            failures.append("analytical-pipeline.lock not recorded as excluded")
        if "data/runtime/locks/analytical-pipeline.lock" in attempted:
            failures.append("ephemeral analytical lock reached preserved hash path")
        if "data/runtime/durable-state.json" not in paths:
            failures.append("adjacent durable runtime data was excluded")
        if "secure/retained.secret" not in paths:
            failures.append("secure retained file was excluded")

    source = TARGET.read_text(encoding="utf-8")
    for token in (
        'EPHEMERAL_RUNTIME_LOCK_DIR = "data/runtime/locks"',
        'folded.startswith(lock_dir + "/") and folded.endswith(".lock")',
        'READ_RETRY_ATTEMPTS = 8',
    ):
        if token not in source:
            failures.append("contract token missing: " + token)

    installer = (ROOT / "installer/install.ps1").read_text(encoding="utf-8-sig")
    if "ephemeral runtime lock files" not in installer:
        failures.append("installer preservation message does not declare runtime-lock exclusion")

    out = {
        "ok": not failures,
        "version": "pl35-installer-ephemeral-lock-closure-1.0.0",
        "failures": failures,
        "policy": "exclude only *.lock beneath data/runtime/locks; preserve adjacent durable data and all other lock-named files",
    }
    print(json.dumps(out, indent=2))
    return 0 if not failures else 2

if __name__ == "__main__":
    raise SystemExit(main())
