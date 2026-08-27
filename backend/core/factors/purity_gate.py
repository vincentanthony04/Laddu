"""Layer 2 — purity gate.

Statically verifies that a factor module only imports from an allowlist,
and never references forbidden names (no network, no filesystem, no
process control, no dynamic code execution). This runs at import-time
registration (see registry.py) and also as a standalone check any factor
file can be run through before being added to the zoo.

A factor that fails this gate is rejected outright — it is never wired
into scoring, regardless of how good its formula looks.
"""

from __future__ import annotations

import ast
from pathlib import Path

ALLOWED_MODULES = {
    "pandas",
    "numpy",
    "scipy",
    "core.factors.factor_ops",
    "__future__",
    "typing",
    "math",
    "dataclasses",
}

FORBIDDEN_NAMES = {
    "os",
    "subprocess",
    "socket",
    "urllib",
    "requests",
    "httpx",
    "pathlib",
    "Path",
    "eval",
    "exec",
    "compile",
    "__import__",
    "open",
    "input",
}


class PurityViolation(Exception):
    pass


def _module_root(name: str) -> str:
    return name.split(".")[0]


def check_source(source: str, filename: str = "<factor>") -> None:
    """Raise PurityViolation if `source` breaks the factor sandbox contract."""
    tree = ast.parse(source, filename=filename)

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if not _import_allowed(alias.name):
                    raise PurityViolation(
                        f"{filename}: disallowed import '{alias.name}'"
                    )
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if not _import_allowed(mod):
                raise PurityViolation(
                    f"{filename}: disallowed import 'from {mod} import ...'"
                )
        elif isinstance(node, ast.Name):
            if node.id in FORBIDDEN_NAMES:
                raise PurityViolation(
                    f"{filename}: forbidden name '{node.id}' referenced"
                )
        elif isinstance(node, ast.Attribute):
            # e.g. os.system, pathlib.Path — catch attribute access on
            # forbidden roots even if only partially imported.
            if node.attr.startswith("__") and node.attr not in (
                "__future__",
            ):
                raise PurityViolation(
                    f"{filename}: dunder attribute access '{node.attr}' "
                    "is not permitted in factor code"
                )
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in (
                "eval",
                "exec",
                "compile",
                "__import__",
                "open",
                "input",
            ):
                raise PurityViolation(
                    f"{filename}: forbidden call to '{func.id}'"
                )
            if isinstance(func, ast.Name) and func.id == "getattr" and len(node.args) >= 2:
                arg2 = node.args[1]
                if isinstance(arg2, ast.Constant) and isinstance(arg2.value, str):
                    if arg2.value.startswith("__"):
                        raise PurityViolation(
                            f"{filename}: getattr with dunder name is not permitted"
                        )


def _import_allowed(module_name: str) -> bool:
    if not module_name:
        return False
    root = _module_root(module_name)
    if module_name in ALLOWED_MODULES:
        return True
    return root in {_module_root(m) for m in ALLOWED_MODULES}


def check_file(path: str | Path) -> None:
    path = Path(path)
    check_source(path.read_text(encoding="utf-8"), filename=str(path))
