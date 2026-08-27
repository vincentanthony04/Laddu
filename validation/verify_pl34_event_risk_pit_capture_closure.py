import hashlib, json, sys
from pathlib import Path
root=Path(__file__).resolve().parents[1]
checks=[]
def ck(name,cond,detail=''): checks.append({'name':name,'ok':bool(cond),'detail':detail})
def sha(rel): return hashlib.sha256((root/rel).read_bytes()).hexdigest()

# Freeze PL33 official/fundamental wiring, PL32/PL31 lineage, selector/model and UI4.
frozen={
 'backend/core/decision_engine_service.py':'b963e5df7866699b42358d18a40e6f9b8aebcfd77be8b5eeda3a58e9d531fbaf',
 'backend/core/nse_official_evidence_service.py':'b906f9c01b6c70cf789cb2ab4c1f4edab6ed038e7132a2365d2ff301031d03fb',
 'backend/core/scan_orchestration_rows.py':'b993df81eec2980b5ee28ceec91542704dcec5706712c0d92db0d6e7beb23739',
 'backend/core/nse_cross_sectional_selector_service.py':'6a8a8730bdede43bca4cd6a79ee895b009efaf7b8ae3e8f7a81cf41537f750fc',
 'backend/core/quant_edge_data_service.py':'853016af004b6d47044e7349d954e80cbe3ae5373782f3e44a766608c49fad06',
 'backend/core/data_plane/model_governance_repository.py':'a20716ab419911d4d213f1e3e134cb25e652f8db7210e42a7f1c2751d82efd94',
 'backend/core/event_risk_policy_service.py':'00c6369da4d12534b3d689c3baaf4b4188aa50281576b3d3010e6ad85a770166',
 'frontend/app.js':'d466544f0210a42888ddba45b9652412dff6a43f62210ab5c953acb62dc8caa7',
 'frontend/app.css':'cbb3650112346d39955f28a75b318131b224a4b2388fff293c93be51972c2614',
 'frontend/ui-system.css':'eabc356debe583cdf0b683f8fa8ed706b0a9692713f0fcbf878262e2891d0a95',
}
for rel,expected in frozen.items(): ck('frozen '+rel,sha(rel)==expected,sha(rel))

sys.path.insert(0,str(root/'backend'))
from core.quant_scan_capture_service import QuantScanCaptureService, QUANT_SCAN_CAPTURE_VERSION
from core.quant_edge_data_service import QuantEdgeDataService, MIN_TRAINING_FEATURE_COVERAGE
class EmptyStore: pass
svc=QuantScanCaptureService(EmptyStore())

base={
 'mode':'delivery','decision_as_of':'2026-08-21T10:00:00+05:30',
 'relative_strength_20d':1,'relative_strength_60d':1,'relative_strength_120d':1,
 'relative_volume':1.2,'technical_score':70,'liquidity_score':8,'atr_pct':2,
 'sector_relative_return':1.5,'delivery_pct_zscore':1.25,'delivered_qty_zscore':-0.75,
 'fundamental_score':72,
}
flagged=svc._attach_adv_evidence(dict(base,event_risk={'flag':True,'nearest_event_date':'2026-08-22'}))
clear=svc._attach_adv_evidence(dict(base,event_risk={'flag':False}))
missing=svc._attach_adv_evidence(dict(base))
malformed=svc._attach_adv_evidence(dict(base,event_risk={'flag':'false'}))
preexisting=svc._attach_adv_evidence(dict(base,event_risk={'flag':True},event_risk_score=0.25))
ck('PL34 capture version',QUANT_SCAN_CAPTURE_VERSION=='quant-scan-capture-3.2.0-event-risk-pit',QUANT_SCAN_CAPTURE_VERSION)
ck('flagged canonical event maps to penalty 1',flagged.get('event_risk_score')==1.0,repr(flagged.get('event_risk_score')))
ck('clear canonical event maps to penalty 0',clear.get('event_risk_score')==0.0,repr(clear.get('event_risk_score')))
ck('flagged event source explicit',flagged.get('event_risk_source')=='CANONICAL_DECISION_EVENT_RISK_AUTHORITY')
ck('flagged nearest date retained',flagged.get('event_risk_nearest_date')=='2026-08-22')
ck('missing authority stays missing','event_risk_score' not in missing)
ck('malformed flag stays missing','event_risk_score' not in malformed)
ck('preexisting event authority preserved',preexisting.get('event_risk_score')==0.25)
coverage0,missing0=QuantEdgeDataService._feature_completeness('delivery',base)
coverage1,missing1=QuantEdgeDataService._feature_completeness('delivery',flagged)
ck('PL33 observed shape remains 11 of 12',coverage0==0.916667 and missing0==['event_risk_penalty'],json.dumps({'coverage':coverage0,'missing':missing0}))
ck('authoritative event projection completes 12 of 12',coverage1==1.0 and missing1==[],json.dumps({'coverage':coverage1,'missing':missing1}))
ck('completeness gate unchanged',MIN_TRAINING_FEATURE_COVERAGE==0.60,repr(MIN_TRAINING_FEATURE_COVERAGE))

index=(root/'frontend/index.html').read_text(); config=(root/'backend/config.py').read_text(); front=json.loads((root/'frontend/release-identity.json').read_text())
marker='production-usability-r8-pl34-event-risk-pit-capture-8086'
ck('PL34 marker',marker in config and marker in index and front.get('build_marker')==marker)
ck('visible PL34 identity','v131 · R8 · PL34 · 8086' in index)
ck('UI4 premium refinement inherited','data-ui-version="UI4"' in index)
ck('broker authority unchanged','BROKER_ORDER_EXECUTION_ENABLED = False' in config)
failed=[x for x in checks if not x['ok']]
print(json.dumps({'contract':'PL34_EVENT_RISK_PIT_CAPTURE_CLOSURE','ok':not failed,'passed':len(checks)-len(failed),'failed':len(failed),'checks':checks},indent=2,default=str))
raise SystemExit(1 if failed else 0)
