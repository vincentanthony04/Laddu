"""CLI for landing an official NSE report into the content-addressed lake."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

HERE = Path(__file__).resolve()
BACKEND = HERE.parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from core.nse_official_report_ingestion_service import NseOfficialReportIngestionService


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--source-key", required=True)
    parser.add_argument("--trade-date", required=True)
    parser.add_argument("--file", required=True)
    parser.add_argument("--source-url", default=None)
    args = parser.parse_args()
    path = Path(args.file)
    result = NseOfficialReportIngestionService.from_data_dir(Path(args.data_dir)).ingest_bytes(
        source_key=args.source_key,
        trade_date=args.trade_date,
        payload=path.read_bytes(),
        filename=path.name,
        source_url=args.source_url,
    )
    import json
    print(json.dumps(result, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
