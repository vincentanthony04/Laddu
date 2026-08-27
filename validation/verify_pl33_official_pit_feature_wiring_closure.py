import hashlib, json, sys
from pathlib import Path
root=Path(__file__).resolve().parents[1]
checks=[]
def ck(name,cond,detail=''): checks.append({'name':name,'ok':bool(cond),'detail':detail})
def sha(rel): return hashlib.sha256((root/rel).read_bytes()).hexdigest()

# Freeze PL32/PL31 feature-lineage and core selector/model/trading authorities.
frozen={
 'backend/core/nse_official_evidence_service.py':'b906f9c01b6c70cf789cb2ab4c1f4edab6ed038e7132a2365d2ff301031d03fb',
 'backend/core/quant_scan_capture_service.py':'6773c78c4a4f3f2953f7285ed97d889c87f691fc214598bc476951fcf236b5ca',
 'backend/core/scan_orchestration_rows.py':'b993df81eec2980b5ee28ceec91542704dcec5706712c0d92db0d6e7beb23739',
 'backend/core/nse_cross_sectional_selector_service.py':'6a8a8730bdede43bca4cd6a79ee895b009efaf7b8ae3e8f7a81cf41537f750fc',
 'backend/core/quant_edge_data_service.py':'853016af004b6d47044e7349d954e80cbe3ae5373782f3e44a766608c49fad06',
 'backend/core/data_plane/model_governance_repository.py':'a20716ab419911d4d213f1e3e134cb25e652f8db7210e42a7f1c2751d82efd94',
 'frontend/app.js':'d466544f0210a42888ddba45b9652412dff6a43f62210ab5c953acb62dc8caa7',
 'frontend/app.css':'cbb3650112346d39955f28a75b318131b224a4b2388fff293c93be51972c2614',
 'frontend/ui-system.css':'eabc356debe583cdf0b683f8fa8ed706b0a9692713f0fcbf878262e2891d0a95',
}
for rel,expected in frozen.items(): ck('frozen '+rel,sha(rel)==expected,sha(rel))

sys.path.insert(0,str(root/'backend'))
from core.decision_engine_service import DecisionEngineService
from core.quant_edge_data_service import QuantEdgeDataService, MIN_TRAINING_FEATURE_COVERAGE
svc=DecisionEngineService()
# Canonical sync projects only finite existing authority values; preserves preexisting authority.
d={'mode':'delivery','decision_as_of':'2026-08-21T10:00:00+05:30'}
ctx={
 'official_nse_evidence':{'ok':True,'state':'POINT_IN_TIME_OFFICIAL_EVIDENCE_READY','as_of':'2026-08-20','decision_features':{'delivery_pct_surprise':1.25,'delivered_quantity_surprise':-0.75}},
 'fundamentals':{'ok':True,'score':72.0,'as_of':'2026-08-20'},
 'heat_context':{}, 'freshness':{},
}
out=svc.sync_decision_context(d,ctx,is_market_open_fn=lambda:False)
ck('official delivery pct surprise projected',out.get('delivery_pct_zscore')==1.25,repr(out.get('delivery_pct_zscore')))
ck('official delivered quantity surprise projected signed',out.get('delivered_qty_zscore')==-0.75,repr(out.get('delivered_qty_zscore')))
ck('official PIT as-of retained',out.get('official_nse_as_of')=='2026-08-20')
ck('verified fundamental score projected',out.get('fundamental_score')==72.0)
pre={'mode':'delivery','delivery_pct_zscore':9.0,'fundamental_score':88.0}
preout=svc.sync_decision_context(pre,ctx,is_market_open_fn=lambda:False)
ck('preexisting delivery authority preserved',preout.get('delivery_pct_zscore')==9.0)
ck('preexisting fundamental authority preserved',preout.get('fundamental_score')==88.0)
missing=svc.sync_decision_context({'mode':'delivery'},{'official_nse_evidence':{'ok':False,'decision_features':{'delivery_pct_surprise':None,'delivered_quantity_surprise':None}},'fundamentals':{'ok':False,'score':0},'heat_context':{},'freshness':{}},is_market_open_fn=lambda:False)
ck('missing official evidence stays missing','delivery_pct_zscore' not in missing and 'delivered_qty_zscore' not in missing)
ck('unverified fundamentals stay missing','fundamental_score' not in missing)
# Existing official service is invoked for Delivery as well as Intraday by market_context.
class FakeOfficial:
 def __init__(self): self.calls=[]
 def latest(self,symbol,as_of=None): self.calls.append((symbol,as_of)); return {'ok':True,'state':'POINT_IN_TIME_OFFICIAL_EVIDENCE_READY','as_of':'2026-08-20','decision_features':{}}
fake=FakeOfficial(); svc._nse_official=fake
def safe(name,fn,default=None):
 return fn() if name=='layer_official_nse' else default
kwargs=dict(safe_section_fn=safe,resolve_sector_key_fn=lambda row:None,heatmap_snapshot_fn=lambda:[],sector_context_for_row_fn=lambda row,heat:{},fundamental_context_fn=lambda inst,use_api=False:{},mode_intelligence_foundation_fn=lambda:{},price_action_intelligence_fn=lambda candles,mode:{},is_market_open_fn=lambda:False,minutes_to_close_fn=lambda:None)
mc=svc.market_context({'trading_symbol':'DWARKESH','instrument_key':'NSE_EQ|D'},'delivery',[],{'timestamp':'2026-08-21T10:00:00+05:30'},**kwargs)
ck('official PIT authority invoked for Delivery',fake.calls==[('DWARKESH','2026-08-21T10:00:00+05:30')],repr(fake.calls))
ck('official payload retained in Delivery context',(mc.get('official_nse_evidence') or {}).get('ok') is True)
# Exact observed feature shape: PL32 sector + official delivery + fundamental can add evidence, but gate stays 60%.
base={'relative_strength_20d':1,'relative_strength_60d':1,'relative_strength_120d':1,'relative_volume':1.2,'technical_score':70,'liquidity_score':8,'atr_pct':2,'sector_relative_return':1.5}
coverage0,missing0=QuantEdgeDataService._feature_completeness('delivery',base)
enriched=dict(base,delivery_pct_zscore=1.25,delivered_qty_zscore=-0.75,fundamental_score=72)
coverage1,missing1=QuantEdgeDataService._feature_completeness('delivery',enriched)
ck('official wiring increases real feature coverage',coverage1>coverage0,json.dumps({'before':coverage0,'after':coverage1,'missing_before':missing0,'missing_after':missing1}))
ck('completeness gate unchanged',MIN_TRAINING_FEATURE_COVERAGE==0.60,repr(MIN_TRAINING_FEATURE_COVERAGE))
index=(root/'frontend/index.html').read_text(); config=(root/'backend/config.py').read_text(); front=json.loads((root/'frontend/release-identity.json').read_text())
marker='production-usability-r8-pl33-official-pit-feature-wiring-8086'
ck('PL33 marker',marker in config and marker in index and front.get('build_marker')==marker)
ck('visible PL33 identity','v131 · R8 · PL33 · 8086' in index)
ck('UI4 premium refinement inherited','data-ui-version="UI4"' in index)
ck('broker authority unchanged','BROKER_ORDER_EXECUTION_ENABLED = False' in config)
failed=[x for x in checks if not x['ok']]
print(json.dumps({'contract':'PL33_OFFICIAL_PIT_FEATURE_WIRING_CLOSURE','ok':not failed,'passed':len(checks)-len(failed),'failed':len(failed),'checks':checks},indent=2,default=str))
raise SystemExit(1 if failed else 0)
