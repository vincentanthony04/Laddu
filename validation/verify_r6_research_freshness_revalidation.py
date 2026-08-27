from __future__ import annotations
import hashlib, json, sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'backend'))
import core.scan_orchestration_rows as scan_rows
from core.quant_scan_capture_service import QuantScanCaptureService
from core.quant_edge_data_service import QuantEdgeDataService, MIN_TRAINING_FEATURE_COVERAGE
from core.quant_research_orchestrator_service import QuantResearchOrchestratorService

checks=[]
def check(name, ok, detail=None): checks.append({'name':name,'ok':bool(ok),'detail':detail})

# Deterministic live quote boundary.
fixed=datetime(2026,8,20,4,40,0,tzinfo=timezone.utc)
scan_rows.india_now=lambda: fixed
scan_rows.is_india_market_open=lambda: True
instrument={'trading_symbol':'KAYNES','instrument_key':'NSE_EQ|KAYNES','exchange':'NSE'}
decision={
    'symbol':'KAYNES','mode':'intraday','side':'LONG',
    'planned_entry':5200.0,'planned_t1':5300.0,'planned_sl':5150.0,
    'decision_ts':'2026-08-20T04:40:00Z','market_regime':'RANGE',
    # Six deterministic Intraday selector families already present in the observed diagnostic path.
    'change_pct':1.2,'session_relative_volume':1.5,'vwap_distance_pct':0.20,
    'adx':24.0,'market_structure_score':70.0,'liquidity_score':7.2,
}
verified_quote={
    'ltp':5225.0,'identity_verified':True,
    'provider_timestamp':'2026-08-20T04:39:55Z',
    'received_at':'2026-08-20T04:39:56Z',
    'bid_price':5224.5,'ask_price':5225.5,
}
row=scan_rows.research_capture_row(decision,instrument,'intraday',verified_quote)
check('verified provider timestamp survives Research capture', row.get('provider_timestamp_verified') is True and row.get('source_as_of')=='2026-08-20T04:39:55Z', row)
check('verified live quote becomes explicit live freshness with quote age', row.get('freshness_state')=='live' and float(row.get('quote_age_seconds') or -1)==5.0, {k:row.get(k) for k in ('freshness_state','quote_age_seconds','quote_freshness_reason')})
check('real bid ask are preserved without fabrication', row.get('bid_price')==5224.5 and row.get('ask_price')==5225.5, {k:row.get(k) for k in ('bid_price','ask_price')})

prepared=QuantScanCaptureService._prepare(row,'2026-08-20T04:40:00Z')
obj=object.__new__(QuantEdgeDataService); obj.production_governance_required=False
snapshot=obj.record_snapshot(
    candidate_id='kaynes-r6', population_fingerprint='pop-r6', symbol='KAYNES',
    instrument_key='NSE_EQ|KAYNES', mode='intraday', side='LONG',
    decision_ts='2026-08-20T04:40:00Z', universe_id='u', dataset_fingerprint='d',
    feature_manifest_hash='manifest', feature_hash='feature', features=prepared, _persist=False,
)
check('training feature floor is not weakened', MIN_TRAINING_FEATURE_COVERAGE==0.60, MIN_TRAINING_FEATURE_COVERAGE)
check('verified live snapshot crosses COMPLETE only through real lineage/freshness', snapshot.get('snapshot_state')=='COMPLETE' and snapshot.get('lineage_state')=='VERIFIED' and float(snapshot.get('compact_feature_coverage') or 0)>=0.60 and snapshot.get('freshness_state')=='LIVE', {k:snapshot.get(k) for k in ('snapshot_state','lineage_state','compact_feature_coverage','freshness_state','missing_features')})

# Unverified quote remains fail-closed.
unverified={'ltp':5225.0,'identity_verified':True,'received_at':'2026-08-20T04:39:56Z'}
row2=scan_rows.research_capture_row(decision,instrument,'intraday',unverified)
prepared2=QuantScanCaptureService._prepare(row2,'2026-08-20T04:40:00Z')
snapshot2=obj.record_snapshot(
    candidate_id='kaynes-r6-u', population_fingerprint='pop-r6-u', symbol='KAYNES',
    instrument_key='NSE_EQ|KAYNES', mode='intraday', side='LONG',
    decision_ts='2026-08-20T04:40:00Z', universe_id='u', dataset_fingerprint='d',
    feature_manifest_hash='manifest', feature_hash='feature-u', features=prepared2, _persist=False,
)
check('missing provider timestamp remains PARTIAL', row2.get('provider_timestamp_verified') is False and snapshot2.get('snapshot_state')=='PARTIAL', {k:snapshot2.get(k) for k in ('snapshot_state','lineage_state','freshness_state')})

# Runtime source change must invalidate stale research failure even when data counts do not advance.
orch=object.__new__(QuantResearchOrchestratorService)
orch.analytics=SimpleNamespace(worker=ROOT/'backend/tools/quant_duckdb_lightgbm_worker.py')
orch.data=SimpleNamespace(status=lambda desk:{'labels':8315,'snapshots':3732})
orch._latest_cycle=lambda desk:{'completed_at':'2026-08-20T04:39:00Z','label_count':8315,'snapshot_count':3732,'result':{'runtime_fingerprint':'STALE_PRE_R6_RUNTIME'}}
called={}
orch.run_cycle=lambda **kwargs: called.update(kwargs) or {'ok':True,'state':'RERUN'}
rerun=orch.maybe_run_cycle(mode='intraday',trigger='scheduled-tournament')
check('worker source change forces one governed revalidation', rerun.get('state')=='RERUN' and called.get('trial_count')==1 and str(called.get('trigger') or '').endswith('runtime-source-revalidation'), called)

current_fp=orch._runtime_fingerprint()
orch2=object.__new__(QuantResearchOrchestratorService)
orch2.analytics=orch.analytics
orch2.data=SimpleNamespace(status=lambda desk:{'labels':8315,'snapshots':3732})
orch2._latest_cycle=lambda desk:{'completed_at':'2026-08-20T04:39:00Z','label_count':8315,'snapshot_count':3732,'result':{'runtime_fingerprint':current_fp}}
orch2.run_cycle=lambda **kwargs: (_ for _ in ()).throw(AssertionError('unchanged runtime must not rerun'))
suppressed=orch2.maybe_run_cycle(mode='intraday',trigger='scheduled-tournament')
check('unchanged runtime plus unchanged evidence remains cadence suppressed', suppressed.get('state') in {'NOT_DUE','NO_NEW_EVIDENCE'} and suppressed.get('runtime_fingerprint')==current_fp, suppressed)

# Static wiring proof: quote is passed at both Intraday and Delivery capture call sites.
modes=(ROOT/'backend/core/scan_orchestration_modes.py').read_text(encoding='utf-8')
rows_source=(ROOT/'backend/core/scan_orchestration_rows.py').read_text(encoding='utf-8')
check('scanner passes exact quote into Research capture at both desk paths', modes.count('_research_capture_row(d,')>=2 and 'quote_by_key.get' in modes, None)
check('Research capture reuses pure quote integrity authority', 'classify_quote' in rows_source and 'receipt time is never promoted' in rows_source.lower(), None)

# Exact R5 backend freeze outside declared R6 research freshness/revalidation boundary.
frozen=json.loads((ROOT/'validation/r6_frozen_r5_backend_hashes.json').read_text(encoding='utf-8'))
parent=dict(frozen.get('hashes') or {})
current={}
for p in sorted((ROOT/'backend').rglob('*')):
    if p.is_file() and '__pycache__' not in p.parts and p.suffix not in {'.pyc','.pyo'}:
        current[p.relative_to(ROOT).as_posix()]=hashlib.sha256(p.read_bytes()).hexdigest()
changed=sorted(k for k in set(parent)|set(current) if parent.get(k)!=current.get(k))
r8_identity="R8_PRODUCTION_USABILITY_CLOSURE" in (ROOT/'RELEASE_IDENTITY.json').read_text(encoding='utf-8')
allowed={
 'backend/core/quant_analytics_service.py',
 'backend/core/quant_research_orchestrator_service.py',
 'backend/core/scan_orchestration_modes.py',
 'backend/core/scan_orchestration_rows.py',
 'backend/tools/quant_duckdb_lightgbm_worker.py',
}
if r8_identity:
    allowed.add('backend/core/persistent_research_history_service.py')
unexpected=sorted(set(changed)-allowed)
check('R6 backend delta is limited to freshness lineage, runtime revalidation and the declared R8 Research projection', not unexpected and set(changed)==allowed, {'changed':changed,'unexpected':unexpected,'r8_identity':r8_identity})
protected=[
 'backend/core/decision_engine_service.py','backend/core/evidence_engine_service.py','backend/core/trade_geometry_authority.py',
 'backend/core/intraday_session_structure_authority.py','backend/core/structural_trade_map_service.py','backend/core/exact_broker_cash_cost_authority.py',
 'backend/core/model_paper_lifecycle_authority.py','backend/core/outcome_accuracy_taxonomy.py','backend/core/vectorized_evidence_screening_service.py',
 'backend/core/persistent_research_history_service.py','backend/core/trust_state_service.py',
]
if r8_identity:
    protected.remove('backend/core/persistent_research_history_service.py')
mism=[x for x in protected if parent.get(x)!=current.get(x)]
check('decision/math/geometry/cost/outcome and healthy cadence authorities remain byte-identical', not mism, mism)
check('exact R5 parent archive is bound', frozen.get('parent_sha256')=='5952092f712e1b8e51cc023b153a086565c41815166c8968bdfb88ad9378ce76', frozen.get('parent_sha256'))

worker=(ROOT/'backend/tools/quant_duckdb_lightgbm_worker.py').read_text(encoding='utf-8')
check('LightGBM local-name shadowing defect remains absent', 'production_ready = bool(readiness["production_validation_ready"])' in worker and '\n    production_validation_ready = bool(' not in worker, None)
check('worker identity makes R6 revalidation visible', 'duckdb-lightgbm-worker-1.1.1-r6-revalidated' in worker, None)

passed=sum(1 for c in checks if c['ok']); failed=len(checks)-passed
payload={'ok':failed==0,'contract':'R6_RESEARCH_FRESHNESS_REVALIDATION','passed':passed,'failed':failed,'checks':checks}
print(json.dumps(payload,indent=2,default=str))
raise SystemExit(0 if payload['ok'] else 1)
