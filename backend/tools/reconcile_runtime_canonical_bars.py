from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--install-dir", default=os.environ.get("PROJECT_LADDU_HOME", ""))
    args = parser.parse_args()
    if args.install_dir:
        os.environ["PROJECT_LADDU_HOME"] = str(Path(args.install_dir).resolve())
    from config import RUNTIME_DB_PATH
    from core.runtime_market_state_store import RuntimeMarketStateStore
    try:
        store = RuntimeMarketStateStore(RUNTIME_DB_PATH)
        result = store.reconcile_derived_bars_from_1m()
        result["database"] = str(RUNTIME_DB_PATH)
        print(json.dumps(result, sort_keys=True, default=str))
        return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "state": "CANONICAL_RECONCILIATION_FAILED", "error": str(exc), "database": str(RUNTIME_DB_PATH)}, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
