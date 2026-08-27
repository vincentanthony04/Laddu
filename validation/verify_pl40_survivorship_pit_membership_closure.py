from __future__ import annotations
import hashlib,json,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]; BACKEND=ROOT/'backend'
if str(BACKEND) not in sys.path: sys.path.insert(0,str(BACKEND))
sys.dont_write_bytecode=True
from tools.train_nse_smart_model import resolve_survivorship_authority
checks=[]
def ck(name,cond,detail=''): checks.append({'name':name,'ok':bool(cond),'detail':detail})
def sha(rel): return hashlib.sha256((ROOT/rel).read_bytes()).hexdigest()

for name,values,expected,state in [
 ('exact PIT',{'POINT_IN_TIME_SECURITY_MASTER'},True,'POINT_IN_TIME_SECURITY_MASTER'),
 ('canonical observed membership',{'CANONICAL_DAILY_CANDLE_OBSERVED_MEMBERSHIP'},True,'PIT_PLUS_CANONICAL_OBSERVED_MEMBERSHIP'),
 ('mixed PIT plus canonical observed',{'POINT_IN_TIME_SECURITY_MASTER','CANONICAL_DAILY_CANDLE_OBSERVED_MEMBERSHIP'},True,'PIT_PLUS_CANONICAL_OBSERVED_MEMBERSHIP'),
 ('current-universe fallback rejected',{'CURRENT_INSTRUMENTS_SHADOW_FALLBACK'},False,'UNCONTROLLED_OR_CURRENT_UNIVERSE_FALLBACK'),
 ('empty evidence rejected',set(),False,'UNCONTROLLED_OR_CURRENT_UNIVERSE_FALLBACK'),
]:
 r=resolve_survivorship_authority(values); ck(name,r['controlled'] is expected and r['state']==state,json.dumps(r))

refresh=(ROOT/'backend/tools/refresh_research_catalog.py').read_text(encoding='utf-8-sig')
trainer=(ROOT/'backend/tools/train_nse_smart_model.py').read_text(encoding='utf-8-sig')
ck('current instruments is no longer membership filter','LEFT JOIN current_instruments cur' in refresh and 'JOIN current_instruments cur ON cur.instrument_key=c.instrument_key' not in refresh.replace('LEFT JOIN current_instruments cur ON cur.instrument_key=c.instrument_key',''))
ck('historical identity map does not decide membership','historical_identity AS' in refresh and 'Membership is proven by' in refresh)
ck('canonical daily candle is explicit membership authority',"CANONICAL_DAILY_CANDLE_OBSERVED_MEMBERSHIP" in refresh)
ck('cash segment filter remains NSE equity only',"LIKE 'NSE_EQ|%'" in refresh)
ck('PIT exact interval join retained','pit.active_from' in refresh and 'pit.active_to' in refresh)
ck('current fallback cannot certify in trainer','CURRENT_INSTRUMENTS_SHADOW_FALLBACK' not in str(resolve_survivorship_authority({'CURRENT_INSTRUMENTS_SHADOW_FALLBACK'}).get('authorities',[])) or not resolve_survivorship_authority({'CURRENT_INSTRUMENTS_SHADOW_FALLBACK'})['controlled'])
ck('trainer reports exact authority set','universe_join_authorities' in trainer)
ck('PL39 positive-session closure retained','POSITIVE_OBSERVATIONS_ONLY_NO_CALENDAR_INFERENCE' in trainer)
ck('PL38 eligible-forward closure retained','classify_forward_evidence' in (ROOT/'backend/core/selection_research_validation_service.py').read_text())

frozen={
 'backend/core/decision_engine_service.py':'b963e5df7866699b42358d18a40e6f9b8aebcfd77be8b5eeda3a58e9d531fbaf',
 'backend/core/india_cost_model.py':'5d55e47a4286387f38785d672d9cd778114a29674d5a78020fd781ecfe11646a',
 'backend/core/walk_forward_validation_service.py':'fe22ca6c07c555a86b371e2f2c59199de28249354fad0357ddad123a86335fdb',
}
for rel,expected in frozen.items(): ck('frozen '+rel,sha(rel)==expected,sha(rel))
failed=[c for c in checks if not c['ok']]
print(json.dumps({'contract':'PL40_SURVIVORSHIP_PIT_MEMBERSHIP_CLOSURE','ok':not failed,'passed':len(checks)-len(failed),'failed':len(failed),'checks':checks},indent=2))
raise SystemExit(1 if failed else 0)
