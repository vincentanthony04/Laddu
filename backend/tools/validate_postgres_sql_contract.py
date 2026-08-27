from __future__ import annotations

"""Static fail-fast validation for Psycopg pyformat SQL.

The operational installer runs this from the package source before stopping an
existing installation. It catches malformed literal percent signs such as
``LIKE 'INF%'`` when parameters are also supplied to Psycopg.
"""

import argparse
import ast
import json
from pathlib import Path
import sys
from typing import Any

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from core.data_plane.postgres import validate_psycopg_pyformat


def _literal_sql(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts: list[str] = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
            elif isinstance(value, ast.FormattedValue):
                parts.append("{expression}")
            else:
                return None
        return "".join(parts)
    return None


def validate_tree(root: Path) -> dict[str, Any]:
    files = 0
    parameterised_calls = 0
    literal_calls = 0
    failures: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        source = path.read_text(encoding="utf-8")
        # SQLite repositories also use execute(sql, params) with literal LIKE
        # percent signs. Only files that explicitly depend on Psycopg or the
        # Project Laddu PostgreSQL authority belong to this contract.
        if not any(marker in source for marker in ("psycopg", "PostgresAuthority", "PostgresUnavailable")):
            continue
        files += 1
        try:
            tree = ast.parse(source, filename=str(path))
        except Exception as exc:
            failures.append({"file": str(path), "line": 0, "error": f"SYNTAX:{exc}"})
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr not in {"execute", "executemany"} or not node.args:
                continue
            sql_text = _literal_sql(node.args[0])
            if sql_text is None:
                continue
            literal_calls += 1
            has_params = len(node.args) >= 2 or any(
                keyword.arg in {"params", "parameters"} for keyword in node.keywords if keyword.arg
            )
            if not has_params:
                continue
            parameterised_calls += 1
            try:
                validate_psycopg_pyformat(sql_text, ())
            except Exception as exc:
                failures.append(
                    {
                        "file": str(path.relative_to(root)),
                        "line": int(getattr(node, "lineno", 0) or 0),
                        "error": str(exc),
                    }
                )
    return {
        "ok": not failures,
        "service_version": "postgres-sql-contract-validator-1.0.0",
        "root": str(root),
        "files_scanned": files,
        "literal_execute_calls": literal_calls,
        "parameterised_literal_calls": parameterised_calls,
        "failures": failures,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend-root", type=Path, default=BACKEND)
    args = parser.parse_args()
    report = validate_tree(args.backend_root.resolve())
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
