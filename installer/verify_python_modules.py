from __future__ import annotations

import argparse
import contextlib
import importlib
import io
import json
import sys
from collections.abc import Iterable


PROFILES: dict[str, tuple[str, ...]] = {
    "runtime": ("requests", "duckdb", "upstox_client", "psycopg", "psycopg_pool"),
    "research": (
        "duckdb",
        "lightgbm",
        "numpy",
        "pandas",
        "scipy",
        "sklearn",
        "pandas_ta_classic",
        "ta",
        "talib",
        "backtesting",
        "statsmodels",
        "arch",
        "skfolio",
        "smartmoneyconcepts",
        "qlib",
    ),
}


def verify_modules(module_names: Iterable[str]) -> dict[str, object]:
    names = tuple(str(name).strip() for name in module_names if str(name).strip())
    if not names:
        raise ValueError("no module names were supplied")

    loaded: dict[str, object] = {}
    sink = io.StringIO()
    with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
        for name in names:
            loaded[name] = importlib.import_module(name)

    versions = {
        name: getattr(module, "__version__", None)
        for name, module in loaded.items()
    }
    return {
        "ok": True,
        "modules": list(names),
        "versions": versions,
        "suppressed_import_output_chars": len(sink.getvalue()),
    }


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Verify Project Laddu Python dependencies.")
    parser.add_argument("--profile", choices=sorted(PROFILES), help="Named dependency profile.")
    parser.add_argument(
        "--modules",
        help="Comma-separated module names. Overrides --profile and is mainly for validation.",
    )
    args = parser.parse_args(argv)
    if not args.profile and not args.modules:
        parser.error("either --profile or --modules is required")
    return args


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(list(sys.argv[1:] if argv is None else argv))
    modules = (
        tuple(part.strip() for part in args.modules.split(",") if part.strip())
        if args.modules
        else PROFILES[args.profile]
    )
    try:
        result = verify_modules(modules)
    except Exception as exc:  # installer needs one concise machine-readable failure
        print(
            json.dumps(
                {
                    "ok": False,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "modules": list(modules),
                },
                ensure_ascii=True,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1

    print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
