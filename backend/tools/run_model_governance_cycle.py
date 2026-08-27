from __future__ import annotations
import argparse, json, os
from pathlib import Path
import sys
BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
from core.data_plane.model_governance_repository import ProductionModelGovernanceRepository
from core.data_plane.postgres import PostgresAuthority


def main() -> int:
    parser=argparse.ArgumentParser()
    parser.add_argument('--limit',type=int,default=20)
    parser.add_argument('--report',type=Path)
    args=parser.parse_args()
    dsn=os.environ.get('PROJECT_LADDU_GOVERNANCE_DSN','').strip()
    if not dsn: raise RuntimeError('PROJECT_LADDU_GOVERNANCE_DSN is required')
    authority=PostgresAuthority(dsn,role='governance-cycle',min_size=1,max_size=2)
    try:
        authority.open()
        results=ProductionModelGovernanceRepository(authority).evaluate_ready_experiments(args.limit)
    finally:
        authority.close()
    report={'ok':True,'evaluated':len(results),'results':results}
    if args.report:
        args.report.parent.mkdir(parents=True,exist_ok=True)
        args.report.write_text(json.dumps(report,indent=2,default=str),encoding='utf-8')
    print(json.dumps(report,indent=2,default=str))
    return 0
if __name__=='__main__': raise SystemExit(main())
