from __future__ import annotations
import hashlib, json, sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
PARENT_HASHES = ROOT/'validation/r5_frozen_r3_backend_hashes.json'
sys.path.insert(0, str(ROOT/'backend'))
from core.persistent_research_history_service import PersistentResearchHistoryService
from core.trust_state_service import TrustStateService

checks=[]
def check(name, ok, detail=None): checks.append({'name':name,'ok':bool(ok),'detail':detail})

class FakeStore:
    def __init__(self): self.quotes={}
    def latest_quotes_by_symbol(self, symbols): return {s:self.quotes[s] for s in symbols if s in self.quotes}

class FakePortfolio:
    def __init__(self): self.rows={}
    def research_rows(self, limit=1000): return sorted(self.rows.values(), key=lambda r:r['occurred_at'], reverse=True)[:limit]
    def _research(self, candidate, disposition, at, price=None):
        signal=str(candidate.get('signal_id') or candidate.get('source_signal_id') or '')
        sym=str(candidate.get('symbol') or candidate.get('stock') or '').upper(); mode=str(candidate.get('mode') or '').lower()
        rid=hashlib.sha256(f'{signal}|{sym}|{mode}|{disposition}'.encode()).hexdigest()[:28]
        self.rows[rid]={'research_id':rid,'source_signal_id':signal or None,'symbol':sym,'mode':mode,'disposition':disposition,'observed_price':price,'occurred_at':at.astimezone(timezone.utc).isoformat().replace('+00:00','Z'),'payload':dict(candidate)}
        return {'state':'RESEARCH','disposition':disposition,'symbol':sym,'mode':mode}

store=FakeStore(); portfolio=FakePortfolio(); svc=PersistentResearchHistoryService(store, portfolio)
kaynes={'symbol':'KAYNES','exchange':'NSE','mode':'intraday','side':'LONG','source_signal_id':'kaynes-20260820-a','entry':5200.0,'target':5300.0,'sl':5150.0,'ltp':5225.0}
other={'symbol':'RELIANCE','exchange':'NSE','mode':'intraday','side':'LONG','source_signal_id':'reliance-a','entry':1400.0,'target':1420.0,'sl':1390.0,'ltp':1405.0}
r1=svc.publish_many([kaynes],scope_mode='intraday')
h1=svc.history()
check('KAYNES customer publication persists', any(r['symbol']=='KAYNES' and r['research_lifecycle']=='RESEARCH_ACTIVE' for r in h1), h1)
r2=svc.publish_many([other],scope_mode='intraday')
h2=svc.history()
k2=next((r for r in h2 if r['symbol']=='KAYNES'),None)
check('KAYNES never disappears after scanner rerank', k2 is not None, h2)
check('KAYNES becomes history rather than falsely active', bool(k2 and k2['research_lifecycle']=='RESEARCH_HISTORY' and k2['result']=='RERANKED_OUT'), k2)
first_seen=k2.get('first_seen_at') if k2 else None
# Re-enter with same identity, then settle on target.
svc.publish_many([kaynes],scope_mode='intraday')
h3=svc.history(); k3=next(r for r in h3 if r['symbol']=='KAYNES')
check('same KAYNES identity resumes on re-entry', k3['research_candidate_id']=='kaynes-20260820-a' and k3['first_seen_at']==first_seen and k3['research_lifecycle']=='RESEARCH_ACTIVE', k3)
store.quotes['KAYNES']={'ltp':5310.0}
settle=svc.mark_quotes({'KAYNES':{'ltp':5310.0,'verified':True,'fresh':True,'executable':True}})
h4=svc.history(); k4=next(r for r in h4 if r['symbol']=='KAYNES')
perf=svc.performance(h4)
check('Research target settles SUCCESS independently', settle['settled']==1 and k4['signal_outcome']=='SUCCESS' and k4['result']=='TARGET_HIT' and k4['research_lifecycle']=='RESEARCH_SETTLED', k4)
check('Research performance has separate authority and no Final P&L impact', perf['authority']=='PERSISTENT_RESEARCH_COUNTERFACTUAL_ONLY' and perf['included_in_final_performance'] is False and perf['success']==1, perf)
check('Research first seen survives lifecycle', k4['first_seen_at']==first_seen, k4['first_seen_at'])

class Snapshot:
    def __init__(self,data): self.data=data
    def snapshot(self,*args,**kwargs): return self.data

def trust_app(*, idle=True, p95=250.0, failed_blocker=True):
    sup_state={'intraday_scanner':{'alive':True,'stale':False,'state':'EXPECTED_IDLE' if idle else 'FAILED','expected_idle':idle,'waiting_on':'next scheduled cycle in 74s','heartbeat_age_sec':1.2},
               'delivery_scanner':{'alive':True,'stale':False,'state':'EXPECTED_IDLE','expected_idle':True,'waiting_on':'next scheduled cycle','heartbeat_age_sec':1.1}}
    controller={'state':'DEGRADED' if failed_blocker else 'READY','blockers':([{'component':'intraday_scanner','state':'FAILED'}] if failed_blocker else [])}
    pressure={'database_pressure':{'governance':{'pool_available':3,'pool_size':4,'requests_waiting':0,'usable':True,'recovering':False,'pressured':False},'interactive':{'saturated':False,'requests_waiting':0}}}
    latency={'customer_read_p95_ms':p95,'customer_read_samples':20,'routes':{}}
    return SimpleNamespace(autonomic_controller=Snapshot(controller), workload_governor=Snapshot(pressure), http_latency_monitor=SimpleNamespace(trading_snapshot=lambda:latency), supervisor=Snapshot(sup_state), status={'worker_health':{'intraday_scanner':{'last_completed_at':'2026-08-20T04:28:41Z','next_run_at':'2026-08-20T04:30:11Z','seconds_to_next':74}}})

t1=TrustStateService(trust_app(idle=True,p95=250.0,failed_blocker=True)).snapshot()
check('healthy scanner sleep does not inherit stale FAILED trust blocker', t1['state']=='TRUSTED' and t1['controller']['runtime_blocker_count']==0, t1)
check('sleeping scanner exposes last/next/countdown', t1['scanner_cadence']['intraday_scanner']['state']=='SLEEPING' and t1['scanner_cadence']['intraday_scanner']['next_cycle_at'] and t1['scanner_cadence']['intraday_scanner']['seconds_to_next']==74, t1['scanner_cadence']['intraday_scanner'])
t2=TrustStateService(trust_app(idle=False,p95=250.0,failed_blocker=True)).snapshot()
check('genuine scanner failure still blocks trust', t2['state']=='DO_NOT_TRUST' and t2['controller']['runtime_blocker_count']==1, t2)
t3=TrustStateService(trust_app(idle=True,p95=5800.0,failed_blocker=True)).snapshot()
check('real customer read p95 failure remains DO_NOT_TRUST while scanner sleeps healthily', t3['state']=='DO_NOT_TRUST' and t3['scanner_cadence']['intraday_scanner']['state']=='SLEEPING' and '5.8s' in t3['reason'], t3)

# Static customer-contract proof.
html=(ROOT/'frontend/index.html').read_text(encoding='utf-8')
js=(ROOT/'frontend/app.js').read_text(encoding='utf-8')
life=(ROOT/'backend/core/scan_orchestration_lifecycle.py').read_text(encoding='utf-8')
ops=(ROOT/'backend/core/operations_control_service.py').read_text(encoding='utf-8')
check('Research page has persistent history surface', 'PERSISTENT RESEARCH HISTORY' in html and 'researchCandidateHistoryRows' in html and 'Published candidates never disappear' in html)
check('top trust strip exposes scanner cadence', 'trustCadence' in html and 'scanner_cadence?.intraday_scanner' in js and 'Intraday ${cadenceState' in js)
check('Operations customer copy treats cadence sleep as healthy', 'healthy / sleeping jobs' in js and 'Scheduled cadence is healthy; no trust penalty' in js)
check('scanner lifecycle records next run timestamp and countdown', 'next_run_at' in life and 'seconds_to_next' in life and '"state": "sleeping"' in life)
check('operations read model exposes SLEEPING display state', 'display_state' in ops and 'SLEEPING' in ops and 'next_cycle_at' in ops)

# Parent R3 freeze: every backend byte outside the intentional R5 read/lifecycle projection boundary stays identical.
allowed={
 'backend/core/decision_quote_projection_service.py',
 'backend/core/operations_control_service.py',
 'backend/core/persistent_research_history_service.py',
 'backend/core/portfolio_workspace_service.py',
 'backend/core/scan_orchestration_discovery.py',
 'backend/core/scan_orchestration_lifecycle.py',
 'backend/core/supervisor.py',
 'backend/core/trust_state_service.py',
}
def inv(root):
    out={}
    for p in (root/'backend').rglob('*'):
        if p.is_file() and '__pycache__' not in p.parts and p.suffix not in {'.pyc','.pyo'}:
            rel=p.relative_to(root).as_posix(); out[rel]=hashlib.sha256(p.read_bytes()).hexdigest()
    return out
try:
    frozen=json.loads(PARENT_HASHES.read_text(encoding='utf-8'))
    pa=dict(frozen.get('hashes') or {}); ch=inv(ROOT)
    changed=sorted(k for k in set(pa)|set(ch) if pa.get(k)!=ch.get(k))
    unexpected=sorted(set(changed)-allowed)
    check('frozen exact R3 parent manifest is bound to expected artifact', frozen.get('parent_sha256')=='903b4190666ed08ab0bb4e63dda2f60f9b509fc5269903c8b91a86529be8c620' and int(frozen.get('backend_file_count') or 0)==len(pa), {'parent_sha256':frozen.get('parent_sha256'),'count':len(pa)})
    check('R3 backend frozen outside explicit Research/cadence projection boundary', not unexpected and set(changed)==allowed, {'changed':changed,'unexpected':unexpected})
    protected=[
      'backend/core/decision_engine_service.py','backend/core/evidence_engine_service.py','backend/core/trade_geometry_authority.py',
      'backend/core/intraday_session_structure_authority.py','backend/core/structural_trade_map_service.py','backend/core/exact_broker_cash_cost_authority.py',
      'backend/core/model_paper_lifecycle_authority.py','backend/core/outcome_accuracy_taxonomy.py','backend/core/vectorized_evidence_screening_service.py'
    ]
    mism=[x for x in protected if pa.get(x)!=ch.get(x)]
    check('decision/math/geometry/cost/outcome authorities remain byte-identical to R3', not mism, mism)
except Exception as exc:
    check('frozen exact R3 parent hash manifest is readable', False, f'{type(exc).__name__}:{exc}')

passed=sum(1 for c in checks if c['ok']); failed=len(checks)-passed
payload={'ok':failed==0,'contract':'R5_PERSISTENT_RESEARCH_HEALTHY_CADENCE','passed':passed,'failed':failed,'checks':checks}
# Read-only by default so executing validation never mutates the sealed package.
# An operator may explicitly request an external evidence file.
import os
proof_output = str(os.environ.get('PROJECT_LADDU_R5_PROOF_OUTPUT') or '').strip()
if proof_output:
    Path(proof_output).write_text(json.dumps(payload,indent=2,default=str)+'\n',encoding='utf-8')
print(json.dumps(payload,indent=2,default=str))
raise SystemExit(0 if payload['ok'] else 1)
