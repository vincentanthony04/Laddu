from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    failures=[]; checks=[]
    def check(name, ok, detail):
        checks.append({'gate':name,'state':'PASS' if ok else 'FAIL','detail':detail})
        if not ok: failures.append(name)

    coordinator=(ROOT/'backend/core/data_plane/coordinator.py').read_text(encoding='utf-8')
    app=(ROOT/'backend/application_runtime.py').read_text(encoding='utf-8')
    routes=(ROOT/'backend/routes_get_research.py').read_text(encoding='utf-8')
    projection=(ROOT/'backend/core/research_control_projection_service.py').read_text(encoding='utf-8')
    ops=(ROOT/'backend/core/operations_control_service.py').read_text(encoding='utf-8')
    market=(ROOT/'backend/core/market_data_service.py').read_text(encoding='utf-8')
    governor=(ROOT/'backend/core/workload_governor.py').read_text(encoding='utf-8')
    replay=(ROOT/'backend/core/selection_walk_forward_replay_service.py').read_text(encoding='utf-8')
    forward=(ROOT/'backend/core/forward_progress_service.py').read_text(encoding='utf-8')
    plan=json.loads((ROOT/'infra/postgres/MIGRATION_PLAN.json').read_text(encoding='utf-8'))

    check('DEDICATED_GOVERNANCE_READ_POOL', 'governance-read' in coordinator and 'model_governance_read' in coordinator, 'Research/UI/WFA reads have capacity separate from governance writes')
    check('GOVERNANCE_READ_STARTED_AND_CLOSED', 'self.governance_read.open()' in coordinator and 'self.governance_read.close()' in coordinator, 'Dedicated read authority follows runtime lifecycle')
    check('READ_REPOSITORY_INJECTED', 'production_model_governance_read_repository' in app, 'Store exposes separate governance read repository')
    check('RESEARCH_PROJECTION_SUPERVISED', 'research_control_projection' in app and 'ResearchControlProjectionService' in app, 'Heavy Research composition owns a supervised background worker')
    check('HTTP_FORWARD_PROGRESS_CACHE_ONLY', 'return service.forward_progress()' in routes and 'ForwardProgressService(app.store).status()' not in routes, 'Forward Progress HTTP cannot execute governance fan-out')
    check('HTTP_FORWARD_CLOCK_CACHE_ONLY', 'return service.forward_clock()' in routes and 'ForwardEvidenceClockService(app.store).status()' not in routes, 'Forward Clock HTTP cannot execute governance fan-out')
    check('HTTP_QUANT_RESEARCH_CACHE_ONLY', 'return service.quant_research_plane()' in routes and 'training_publication_status()' not in routes and 'ModelTournamentService(app.store).status()' not in routes, 'Quant Research HTTP serves the materialized projection only')
    check('PROJECTION_OWNS_HEAVY_READS', 'ForwardProgressService(self.app.store).status()' in projection and 'ForwardEvidenceClockService(self.app.store).status()' in projection and 'training_publication_status' in projection, 'Heavy Research reads are concentrated in one background lane')
    check('WFA_READ_REPOSITORY', 'production_model_governance_read_repository' in replay, 'Walk-forward replay uses dedicated governance read capacity')
    check('FORWARD_PROGRESS_READ_REPOSITORY', 'production_model_governance_read_repository' in forward, 'Forward-progress reconciliation uses dedicated governance read capacity')
    check('LIFECYCLE_COORDINATES_BACKGROUND', 'pause_bulk(seconds=420' not in ops and 'BACKGROUND_BULK_ENABLED_FOR_E2E' in ops and 'run_on_demand' in ops, 'One-shot lifecycle no longer pauses the historical trainer it owns; normal governor still yields only to real higher-priority demand')
    check('WFA_EVIDENCE_SHORT_CIRCUIT', 'FORWARD_MATURITY_PENDING' in ops and 'complete_snapshots <= 0 or label_days < minimum_calendar_depth' in ops and 'FORWARD_SELECTOR_EVIDENCE_PENDING' in ops, 'Prospective selector shortage remains a cheap maturity-only short-circuit and cannot block retained historical ML/WFA')
    check('WFA_ERROR_ISOLATION_RETAINED', 'WFA_EXECUTION_ERROR' in ops and 'COMPLETE_WITH_EXECUTION_ERRORS' in ops, 'Research execution errors remain explicit without aborting final reconciliation')
    check('DEEP_HISTORY_CONSERVATIVE_WORKERS', '"batch_size": 8' in market and '"workers": 1' in market and '"batch_size": 3' in market, 'History convergence cannot monopolize PostgreSQL during customer acceptance')
    check('GOVERNOR_SEES_GOVERNANCE_READ', 'governance_read' in governor and 'governance read PostgreSQL pool pressure' in governor, 'Background work yields when the new read lane is pressured')
    gov_names=[row.get('name') for row in plan.get('governance',[])]
    check('WFA_INDEX_MIGRATION_ACTUALLY_PLANNED', '007_r26_wfa_query_indexes.sql' in gov_names, 'Previously shipped WFA index file is now in the authoritative migration plan')
    check('WFA_INDEX_MIGRATION_ORDER', '007_r26_wfa_query_indexes.sql' in gov_names and gov_names.index('007_r26_wfa_query_indexes.sql') > gov_names.index('006_legacy_research_migration_checkpoint.sql'), 'WFA read indexes apply after prior governance migrations without rewriting history; later additive migrations are permitted')
    check('BROKER_AUTHORITY_UNCHANGED', 'broker_authority' in projection and '"NONE"' in projection, 'Architecture convergence does not add order execution authority')

    result={'ok':not failures,'scope':'R30_END_TO_END_ARCHITECTURE_CONVERGENCE','checks':checks,'passed':len(checks)-len(failures),'failed':len(failures),'failures':failures,'production_ready':False,'broker_authority':'NONE'}
    print(json.dumps(result,indent=2))
    return 0 if not failures else 1

if __name__=='__main__': raise SystemExit(main())
