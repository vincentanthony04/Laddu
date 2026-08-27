#!/usr/bin/env python3
"""Merge shipped active NSE transports into the installed retained plan."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.nse_official_plan_service import merge_nse_source_plan


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--default-plan", type=Path, default=ROOT / "resources" / "nse_official_sources.example.json")
    parser.add_argument("--target-plan", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = merge_nse_source_plan(args.default_plan, args.target_plan)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(result, sort_keys=True))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
