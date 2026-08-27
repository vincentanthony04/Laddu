from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from core.data_plane.candle_lake_repository import CandleLakeRepository  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Build Project Laddu protected candle file catalogue.")
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--force", action="store_true", help="rebuild even when the installed catalogue matches the retained lake")
    args = parser.parse_args()
    repository = CandleLakeRepository(args.data_dir, start_catalog_builder=False)
    unchanged = (not args.force) and (not repository._catalog_needs_rebuild(verify_disk_signature=True))
    result = repository.catalog_status() if unchanged else repository.rebuild_catalog()
    ok = bool(result.get("usable")) and result.get("state") == "READY" and not result.get("error")
    payload = {
        "ok": bool(ok),
        "state": result.get("state"),
        "action": "reused_verified_catalog" if unchanged else "rebuilt_atomic_catalog",
        "catalog": result,
    }
    print(json.dumps(payload, indent=2, default=str))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
