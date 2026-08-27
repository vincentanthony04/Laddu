"""Run one governed NSE official-source acquisition and catalogue cycle."""
from __future__ import annotations

import argparse
from datetime import date
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from core.nse_official_source_cycle_service import NseOfficialSourceCycleService  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--trade-date", default=date.today().isoformat())
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--inbox-only", action="store_true")
    args = parser.parse_args()
    service = NseOfficialSourceCycleService(args.data_dir, plan_path=args.plan)
    result = service.run(trade_date=args.trade_date, inbox_only=args.inbox_only)
    if result.get("catalog_refresh_required"):
        command = [sys.executable, str(BACKEND / "tools" / "refresh_research_catalog.py"), "--data-dir", str(args.data_dir)]
        dsn = os.environ.get("PROJECT_LADDU_OPERATIONAL_DSN", "").strip()
        if dsn:
            command.extend(["--operational-dsn", dsn])
        completed = subprocess.run(command, cwd=ROOT, text=True, capture_output=True)
        result["catalog_refresh"] = {
            "returncode": completed.returncode,
            "state": "REFRESHED" if completed.returncode == 0 else "FAILED",
            "stdout_tail": completed.stdout[-2000:],
            "stderr_tail": completed.stderr[-2000:],
        }
        if completed.returncode:
            result["ok"] = False
            result["state"] = "CATALOG_REFRESH_FAILED"
    print(json.dumps(result, indent=2, default=str))
    return 0 if result.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
