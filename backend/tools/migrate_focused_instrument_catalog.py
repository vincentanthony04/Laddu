from __future__ import annotations

"""Create the focused local instrument projection before production startup.

Upgrade path: deterministically migrate the persisted provider/legacy catalogue.
Fresh-install path: download both binding NSE/BSE exchange masters and commit
only after the complete v3 policy passes. No service is started with an empty or
partial identity authority.
"""

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


def _row_count(path: Path) -> int:
    if not path.exists():
        return 0
    try:
        conn = sqlite3.connect(str(path), timeout=5)
        try:
            table = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='instruments'").fetchone()
            return int(conn.execute("SELECT COUNT(*) FROM instruments").fetchone()[0]) if table else 0
        finally:
            conn.close()
    except Exception:
        return 0


def _fresh_bootstrap() -> dict:
    from storage import Store
    from laddu_upstox_rest_client import UpstoxClient
    from core.instrument_universe_policy import ACTIVE_UNIVERSE_REVISION

    store = Store()
    result = UpstoxClient(store).load_instruments(force=True, background=False)
    stats = dict(result.get("universe_stats") or {})
    valid = bool(
        result.get("loaded")
        and result.get("cache_usable")
        and result.get("universe_revision") == ACTIVE_UNIVERSE_REVISION
        and int(stats.get("nse_equities") or 0) > 0
        and int(stats.get("bse_only_equities") or 0) > 0
        and int(stats.get("indices") or 0) > 0
        and int(stats.get("derivatives") or 0) == 0
        and int(stats.get("out_of_policy_rows") or 0) == 0
    )
    if not valid:
        raise RuntimeError("fresh focused exchange-master bootstrap did not pass the complete identity gate: " + json.dumps(result, default=str)[:2000])
    return {
        "ok": True,
        "state": "FOCUSED_CATALOGUE_FRESH_BOOTSTRAP_READY",
        "source": "complete_nse_bse_exchange_download",
        "universe_revision": ACTIVE_UNIVERSE_REVISION,
        "after_rows": int(result.get("count") or 0),
        "universe_stats": stats,
        "policy_stats": result.get("policy_stats") or {},
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--install-dir", default=os.environ.get("PROJECT_LADDU_HOME", ""))
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    if args.install_dir:
        os.environ["PROJECT_LADDU_HOME"] = str(Path(args.install_dir).resolve())
    from config import DB_PATH
    from core.focused_instrument_migration import migrate_database

    path = Path(DB_PATH)
    try:
        if _row_count(path) > 0:
            result = migrate_database(path)
        else:
            result = _fresh_bootstrap()
        result = dict(result)
        result["database"] = str(path.resolve())
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(json.dumps(result, indent=2, sort_keys=True, default=str), encoding="utf-8")
        print(json.dumps(result, sort_keys=True, default=str))
        return 0
    except Exception as exc:
        failure = {
            "ok": False,
            "state": "FOCUSED_CATALOGUE_MIGRATION_FAILED",
            "error": str(exc),
            "database": str(path.resolve()),
            "fresh_bootstrap_attempted": _row_count(path) <= 0,
        }
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(json.dumps(failure, indent=2, sort_keys=True), encoding="utf-8")
        print(json.dumps(failure, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
