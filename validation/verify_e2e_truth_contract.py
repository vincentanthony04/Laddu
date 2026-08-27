from __future__ import annotations
import json, math, sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
BACKEND=ROOT/'backend'
sys.path.insert(0,str(BACKEND))

from core.trust_state_service import TrustStateService
from routes_get_system import _workspace_mode_coverage, r_trader_live_state, r_trader_workspace, r_ready
from routes_get_registry import ROUTES

OUT=Path('/mnt/data/LADDU_E2E_TRUTH_CONTRACT_PROOF.json')
checks=[]
def check(name, ok, detail=None):
    checks.append({'name':name,'ok':bool(ok),'detail':detail})
    if not ok: print('FAIL',name,detail)

class Static:
    def __init__(self,value): self.value=value
    def snapshot(self,*a,**k): return self.value

class Controller:
    def __init__(self,blockers=None): self.blockers=blockers or []
    def snapshot(self,refresh=False):
        return {'state':'RECOVERING' if self.blockers else 'RUNNING','blockers':self.blockers,'primary_blocker':self.blockers[0] if self.blockers else None}

class Governor:
    def __init__(self):
        self.value={'database_pressure':{'governance':{'pool_size':3,'pool_available':2,'requests_waiting':0,'recovering':False,'usable':True,'pressured':False},'interactive':{'pool_size':4,'pool_available':4,'requests_waiting':0,'saturated':False}}}
    def snapshot(self): return self.value

class Latency:
    def trading_snapshot(self): return {'customer_read_p95_ms':580,'customer_read_samples':13,'routes':{}}

class AppForTrust:
    def __init__(self,blockers=None):
        self.autonomic_controller=Controller(blockers)
        self.workload_governor=Governor()
        self.http_latency_monitor=Latency()

# Pending evidence must not block current trade trust; runtime failure must.
pending={'key':'exact_build_browser_workflows','state':'PENDING_EVIDENCE','component':'operator_read_models'}
trusted=TrustStateService(AppForTrust([pending])).snapshot()
check('pending evidence is not misclassified as runtime failure', trusted['state']=='TRUSTED' and trusted['decision_admission_allowed'] is True, trusted)
check('trust snapshot has comparable microsecond revision', isinstance(trusted.get('sequence_us'),int) and 0 < trusted['sequence_us'] < 9_007_199_254_740_991, trusted.get('sequence_us'))
check('trust snapshot has authoritative evaluated_at', bool(trusted.get('evaluated_at')), trusted.get('evaluated_at'))
blocked=TrustStateService(AppForTrust([{'key':'index','state':'NO_PROGRESS','component':'index_levels'}])).snapshot()
check('critical index NO_PROGRESS blocks admission', blocked['state']=='DO_NOT_TRUST' and blocked['decision_admission_allowed'] is False, blocked)

# Coverage/rank semantics must be numerically proven, not substring-inferred.
for state in ('INCOMPLETE','COMPLETE_WITH_EXPLICIT_BLOCKERS','CONTINUING_SWEEP','EXPECTED_IDLE'):
    row=_workspace_mode_coverage({'delivery':{'processed':2,'total':10,'state':state}})['delivery']
    check(f'{state} cannot claim full-universe rank at 2/10', row['complete'] is False and row['ranking_scope']=='EVALUATED_SUBSET_ONLY', row)
full=_workspace_mode_coverage({'delivery':{'processed':10,'total':10,'state':'RUNNING'}})['delivery']
check('numeric 10/10 coverage authorizes full-universe rank', full['complete'] is True and full['ranking_scope']=='FULL_UNIVERSE' and full['pct']==100.0, full)
for bad in (float('nan'),float('inf'),-1,True):
    row=_workspace_mode_coverage({'delivery':{'processed':bad,'total':10,'state':'RUNNING'}})['delivery']
    check(f'invalid coverage processed={bad!r} fails closed', row['complete'] is False and row['processed'] is None, row)

class TrustStub:
    def __init__(self): self.calls=0
    def snapshot(self):
        self.calls+=1
        return {'ok':True,'state':'TRUSTED','decision_admission_allowed':True,'evaluated_at':'2026-08-19T19:07:07+05:30','sequence_us':1_787_146_627_000_000,'reason':'current'}
class Hist:
    def snapshot(self): return {'state':'READY'}
class FakeApp:
    def __init__(self):
        self.trust_state_service=TrustStub(); self.historical_pit_sweep=Hist(); self.status={'service':'running'}
        self._market_radar_http_snapshot={}; self._market_radar_snapshot={}; self._coverage_quote_cache={}
        self.live_market=None; self.market_data=None
    def dashboard_cards_data(self,mode): return {'projection_state':'READY','time':'2026-08-19T19:07:07+05:30','final_signals':[],'active_positions':[],'discovery':{'near_qualified':[]},'watch_queue':[],'decision_list':[],'selected_memory':[]}
    def scanner_status(self):
        return {'service':'running','scanner':{'mode_scanners':{
          'delivery':{'processed':2560,'total':4137,'state':'CONTINUING_SWEEP'},
          'intraday':{'processed':2366,'total':3364,'state':'CLOSED_MARKET_READY'},
        }},'instruments':{'universe_count':4137}}
    def heatmap_snapshot(self): return []

app=FakeApp()
ready1=r_ready(app,{},'', 'all'); ready2=r_ready(app,{},'', 'all')
check('ready exposes stable process boot identity for restart proof',bool(ready1.get('process_boot_id')) and ready1.get('process_boot_id')==ready2.get('process_boot_id') and isinstance(ready1.get('process_id'),int) and bool(ready1.get('process_started_at')),ready1)
live=r_trader_live_state(app,{},'', 'all')
check('live-state endpoint registered', ROUTES.get('/api/trader-live-state') is r_trader_live_state, str(ROUTES.get('/api/trader-live-state')))
check('live-state uses current trust authority', live['trust']['state']=='TRUSTED' and live['trust']['sequence_us']==1_787_146_627_000_000, live)
check('live-state does not expose workspace rows', not any(k in live for k in ('final_signals','candidates','active','preparing')), list(live))

workspace=r_trader_workspace(app,{},'', 'all')
check('workspace v1.5 contract emitted', workspace['contract_version']=='trader-workspace-1.5.0-live-truth-and-ranking-scope', workspace.get('contract_version'))
check('workspace carries explicit current trust revision', workspace['trust']['state']=='TRUSTED' and workspace['trust']['sequence_us']==1_787_146_627_000_000, workspace['trust'])
check('workspace delivery rank scope truthful', workspace['coverage']['delivery']['ranking_scope']=='EVALUATED_SUBSET_ONLY' and abs(workspace['coverage']['delivery']['pct']-61.881)<0.01, workspace['coverage']['delivery'])
check('workspace intraday rank scope truthful', workspace['coverage']['intraday']['ranking_scope']=='EVALUATED_SUBSET_ONLY' and abs(workspace['coverage']['intraday']['pct']-70.333)<0.01, workspace['coverage']['intraday'])
check('workspace exposes server_time for generation freshness', bool(workspace.get('server_time')), workspace.get('server_time'))

passed=sum(c['ok'] for c in checks); failed=len(checks)-passed
payload={'ok':failed==0,'contract':'E2E_TRUTH_CONTRACT_R3','passed':passed,'failed':failed,'checks':checks}
OUT.write_text(json.dumps(payload,indent=2,allow_nan=False)+'\n')
print(json.dumps({k:v for k,v in payload.items() if k!='checks'},indent=2))
raise SystemExit(0 if payload['ok'] else 1)
