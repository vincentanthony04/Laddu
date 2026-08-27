from __future__ import annotations
import json, sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'backend'))
from core.follow_through_projection_service import FollowThroughProjectionService
from core.india_time import INDIA_TZ

OUT=Path('/mnt/data/LADDU_FOLLOW_THROUGH_PROJECTION_R3.json')
checks=[]
def check(name, ok, detail=None):
    checks.append({'name':name,'ok':bool(ok),'detail':detail})
    if not ok: print('FAIL',name,detail)

class Resolver:
    def resolve(self,symbol,prefer_index=False): return {'instrument_key':f'NSE_EQ|{symbol}'}
class Market:
    def __init__(self,by): self.by=by
    def stored_candles(self,key,interval,limit=5000): return self.by.get((key,interval),[])
class Repo:
    def __init__(self,rows): self.rows=rows
    def settled_learning_rows(self,limit=10000): return self.rows
class App:
    def __init__(self,by,rows): self.instrument_resolver=Resolver(); self.market_data=Market(by); self.model_portfolio_repository=Repo(rows)

def bar(ts,close): return {'timestamp':ts.isoformat(),'open':close,'high':close,'low':close,'close':close,'volume':100}

# Intraday: TARGET HIT at 10:00, 60m later price continued by +1R.
closed=datetime(2026,8,19,10,0,tzinfo=INDIA_TZ)
minute=[]
for i in range(0,122):
    start=closed+timedelta(minutes=i)
    minute.append(bar(start,105 + i/60*3))
settled={'decision_id':'D1','source_signal_id':'S1','symbol':'ABC','mode':'intraday','side':'LONG','entry_price':100,'original_stop':97,'exit_price':105,'exit_reason':'TARGET_HIT','closed_at':closed.isoformat()}
life={'decision_id':'D1','symbol':'ABC','mode':'intraday','side':'LONG','entry':100,'stop':97,'exit':105,'exit_reason':'TARGET_HIT','closed_at':closed.isoformat(),'settled':True,'accuracy_eligible':True,'performance_eligible':True}
svc=FollowThroughProjectionService(App({('NSE_EQ|ABC','1minute'):minute},[settled]))
out=svc.enrich_record(life,settled,now=closed+timedelta(hours=2,minutes=2))
check('intraday result remains immutable',out['exit_reason']=='TARGET_HIT' and out['result_is_immutable'] is True,out)
check('intraday 60m follow-through complete',out['follow_through']['horizons']['60m']['complete'] is True,out['follow_through'])
check('intraday target-hit continuation classified',out['after']=='CONTINUED' and out['after_horizon']=='60m',out['follow_through'])

# Intraday stop hit then price recovers above entry -> RECOVERED.
settled2={**settled,'decision_id':'D2','symbol':'XYZ','exit_price':97,'exit_reason':'SL_HIT'}
life2={**life,'decision_id':'D2','symbol':'XYZ','exit':97,'exit_reason':'SL_HIT'}
minute2=[]
for i in range(0,90): minute2.append(bar(closed+timedelta(minutes=i),97 + i/60*4.5))
svc2=FollowThroughProjectionService(App({('NSE_EQ|XYZ','1minute'):minute2},[settled2]))
out2=svc2.enrich_record(life2,settled2,now=closed+timedelta(minutes=91))
check('stop-hit recovery classified separately',out2['after']=='RECOVERED' and out2['exit_reason']=='SL_HIT',out2['follow_through'])

# Missing 60m horizon must remain pending, never FLAT/zero.
minute_short=[bar(closed+timedelta(minutes=i),105.0) for i in range(10)]
svc3=FollowThroughProjectionService(App({('NSE_EQ|ABC','1minute'):minute_short},[settled]))
out3=svc3.enrich_record(life,settled,now=closed+timedelta(minutes=10))
check('incomplete horizon remains pending',out3['follow_through']['state']=='EVIDENCE_PENDING' and out3['after'] is None and out3['after_state']=='PENDING',out3['follow_through'])
# A sparse bar much later than the exact horizon cannot substitute for the missing 60m observation.
sparse=[bar(closed+timedelta(minutes=121),120.0)]
svc_sparse=FollowThroughProjectionService(App({('NSE_EQ|ABC','1minute'):sparse},[settled]))
out_sparse=svc_sparse.enrich_record(life,settled,now=closed+timedelta(minutes=123))
check('sparse later intraday bar cannot fake exact 60m horizon',out_sparse['follow_through']['horizons']['60m'].get('complete') is not True,out_sparse['follow_through'])

# Delivery: fifth subsequent completed trading-day close is the canonical primary horizon.
dclosed=datetime(2026,8,10,12,0,tzinfo=INDIA_TZ)
days=[]
for i,day in enumerate((11,12,13,14,17,18,19,20,21,24),start=1):
    days.append(bar(datetime(2026,8,day,0,0,tzinfo=INDIA_TZ),110+i))
dset={'decision_id':'D3','symbol':'DEF','mode':'delivery','side':'LONG','entry_price':100,'original_stop':95,'exit_price':110,'exit_reason':'TARGET_HIT','closed_at':dclosed.isoformat()}
dlife={'decision_id':'D3','symbol':'DEF','mode':'delivery','side':'LONG','entry':100,'stop':95,'exit':110,'exit_reason':'TARGET_HIT','closed_at':dclosed.isoformat(),'settled':True,'accuracy_eligible':True,'performance_eligible':True}
svc4=FollowThroughProjectionService(App({('NSE_EQ|DEF','day'):days},[dset]))
out4=svc4.enrich_record(dlife,dset,now=datetime(2026,8,25,17,0,tzinfo=INDIA_TZ))
check('delivery primary horizon is 5D',out4['after_horizon']=='5D',out4['follow_through'])
check('delivery five-day continuation classified',out4['after']=='CONTINUED',out4['follow_through'])
# Missing a required trading-session candle must make the horizon incomplete rather than silently counting later bars.
missing_day=[x for x in days if not str(x['timestamp']).startswith('2026-08-12')]
svc_missing=FollowThroughProjectionService(App({('NSE_EQ|DEF','day'):missing_day},[dset]))
out_missing=svc_missing.enrich_record(dlife,dset,now=datetime(2026,8,25,17,0,tzinfo=INDIA_TZ))
check('missing delivery trading session cannot shift 5D horizon',out_missing['follow_through']['horizons']['5D'].get('complete') is not True,out_missing['follow_through'])

# Lifecycle enrichment must bind the same decision id and not another settlement.
app=App({('NSE_EQ|ABC','1minute'):minute},[settled,{**settled,'decision_id':'OTHER','symbol':'NOPE'}])
svc5=FollowThroughProjectionService(app)
payload=svc5.enrich_lifecycle({'ok':True,'records':[life]})
rec=payload['records'][0]
check('lifecycle enrichment uses exact decision lineage',rec['decision_id']=='D1' and rec['follow_through']['instrument_key']=='NSE_EQ|ABC',rec)
check('follow-through projection declares immutable policy',payload['follow_through_projection']['records_enriched']==1 and rec['result_is_immutable'] is True,payload['follow_through_projection'])

passed=sum(x['ok'] for x in checks); failed=len(checks)-passed
result={'ok':failed==0,'contract':'FOLLOW_THROUGH_PROJECTION_R3','passed':passed,'failed':failed,'checks':checks}
OUT.write_text(json.dumps(result,indent=2)+'\n')
print(json.dumps({k:v for k,v in result.items() if k!='checks'},indent=2))
raise SystemExit(0 if result['ok'] else 1)
