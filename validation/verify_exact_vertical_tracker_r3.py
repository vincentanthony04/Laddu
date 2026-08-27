from __future__ import annotations
import json, sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'validation'))
from exact_vertical_tracker import update
OUT=Path('/mnt/data/LADDU_EXACT_VERTICAL_TRACKER_R3.json')
checks=[]
def check(name,ok,detail=None): checks.append({'name':name,'ok':bool(ok),'detail':detail}); print('FAIL',name,detail) if not ok else None

def live(open=True): return {'market_open':open,'trust':{'state':'TRUSTED','decision_admission_allowed':True}}
def ws(final=True): return {'coverage':{'intraday':{'complete':True},'delivery':{'complete':True}},'final_signals':[{'decision_id':'D1','signal_id':'S1','symbol':'ABC','mode':'intraday','instrument_key':'NSE_EQ|ABC','entry':100,'target':105,'stop':97,'rank_score':90,'generated_at':'2026-08-20T09:30:00+05:30'}] if final else []}
def model(status='OPEN',entry=100): return {'positions':[{'decision_id':'D1','source_decision_id':'D1','position_id':'P1','symbol':'ABC','mode':'intraday','status':status,'original_entry':entry,'original_target':105,'original_stop':97,'opened_at':'2026-08-20T09:35:00+05:30'}]}
def perf(after=None):
    r={'decision_id':'D1','settlement_id':'P1','symbol':'ABC','mode':'intraday','entry':100,'target':105,'stop':97,'exit':105,'exit_reason':'TARGET_HIT','signal_outcome':'SUCCESS','economic_outcome':'WIN','net_pnl':450,'realized_r':1.5,'closed_at':'2026-08-20T10:10:00+05:30','accuracy_eligible':True,'performance_eligible':True,'result_is_immutable':True}
    if after: r.update({'after':after,'after_state':after,'follow_through_state':after,'after_horizon':'60m'})
    return {'canonical_lifecycle':{'records':[r]}}
state,err=update({},live=live(),workspace=ws(),model={},performance={},expected_version='v131.0.0',expected_build='B')
check('captures one exact actionable decision',not err and state['stage']=='ACTIONABLE_OBSERVED' and state['decision_id']=='D1',state)
state,err=update(state,live=live(),workspace=ws(),model=model(),performance={},expected_version='v131.0.0',expected_build='B')
check('same decision opens Model Paper',not err and state['stage']=='MODEL_OPEN_OBSERVED' and state['position_id']=='P1',state)
state,err=update(state,live=live(False),workspace=ws(False),model=model('CLOSED'),performance=perf(),expected_version='v131.0.0',expected_build='B')
check('same decision settlement observed',not err and state['stage']=='SETTLED_OBSERVED' and state['result']=='TARGET_HIT',state)
state,err=update(state,live=live(False),workspace=ws(False),model=model('CLOSED'),performance=perf('CONTINUED'),expected_version='v131.0.0',expected_build='B')
check('same decision After observed separately',not err and state['stage']=='AFTER_OBSERVED' and state['after']=='CONTINUED',state)
state,err=update(state,live=live(False),workspace=ws(False),model=model('CLOSED'),performance=perf('CONTINUED'),expected_version='v131.0.0',expected_build='B',restart_proof={'before_boot_id':'BOOT1','after_boot_id':'BOOT2','same_settlement_persisted':True})
check('restart verification completes only same persisted settlement',not err and state['stage']=='RESTART_VERIFIED' and state['complete'] is True,state)
# A different historical settlement cannot substitute for the tracked decision.
badperf={'canonical_lifecycle':{'records':[{**perf('CONTINUED')['canonical_lifecycle']['records'][0],'decision_id':'OTHER'}]}}
prior={k:v for k,v in state.items() if k not in ('restart_proof','complete')}; prior['stage']='MODEL_OPEN_OBSERVED'
other,err=update(prior,live=live(False),workspace=ws(False),model=model(),performance=badperf,expected_version='v131.0.0',expected_build='B')
check('different settlement cannot make tracker green',other['stage']=='MODEL_OPEN_OBSERVED' and other.get('settlement_id')=='P1',other)
# Geometry drift is hard failure.
drift,err=update({'stage':'ACTIONABLE_OBSERVED','decision_id':'D1','expected_version':'v131.0.0','expected_build':'B','geometry':{'entry':100,'target':105,'stop':97},'events':[]},live=live(),workspace={'coverage':{'intraday':{'complete':True}},'final_signals':[{**ws()['final_signals'][0],'target':108}]},model={},performance={},expected_version='v131.0.0',expected_build='B')
check('canonical geometry drift fails hard','CANONICAL_GEOMETRY_DRIFT' in err,err)
# Build switch cannot silently continue a tracker.
switched,err=update(state,live=live(),workspace=ws(),model=model(),performance=perf('CONTINUED'),expected_version='v131.0.0',expected_build='OTHER')
check('tracker cannot cross build identity','TRACKER_BUILD_CHANGED' in err,err)
passed=sum(x['ok'] for x in checks); failed=len(checks)-passed
res={'ok':failed==0,'contract':'EXACT_VERTICAL_TRACKER_R3','passed':passed,'failed':failed,'checks':checks}; OUT.write_text(json.dumps(res,indent=2)+'\n'); print(json.dumps({k:v for k,v in res.items() if k!='checks'},indent=2)); raise SystemExit(0 if res['ok'] else 1)
