from __future__ import annotations

"""Verify that an isolated Python environment exactly satisfies pinned requirements."""

import argparse
from importlib import metadata
import json
from pathlib import Path
import re


PIN = re.compile(r"^([A-Za-z0-9_.-]+)==([^\s;]+)$")


def normalise(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def expected_versions(path: Path) -> dict[str, tuple[str, str]]:
    out: dict[str, tuple[str, str]] = {}
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = PIN.fullmatch(line)
        if not match:
            raise RuntimeError(f"NON_EXACT_REQUIREMENT:{line}")
        name, version = match.groups()
        out[normalise(name)] = (name, version)
    if not out:
        raise RuntimeError("EMPTY_REQUIREMENTS")
    return out


def verify(path: Path) -> dict:
    expected = expected_versions(path)
    installed = {normalise(dist.metadata["Name"]): dist.version for dist in metadata.distributions() if dist.metadata.get("Name")}
    mismatches = []
    for key, (name, version) in expected.items():
        actual = installed.get(key)
        if actual != version:
            mismatches.append({"package": name, "expected": version, "actual": actual})
    return {
        "ok": not mismatches,
        "requirements": str(path),
        "expected_count": len(expected),
        "mismatches": mismatches,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--requirements", required=True)
    args = parser.parse_args()
    result = verify(Path(args.requirements))
    print(json.dumps(result, sort_keys=True))
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
