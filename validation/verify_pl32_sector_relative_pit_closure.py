import hashlib, json, sys
from pathlib import Path
root=Path(__file__).resolve().parents[1]
checks=[]
def ck(name,cond,detail=''): checks.append({'name':name,'ok':bool(cond),'detail':detail})
def sha(rel): return hashlib.sha256((root/rel).read_bytes()).hexdigest()

# Freeze PL31 lineage and PL30 trading/model authorities.
frozen={
 'backend/core/scan_orchestration_rows.py':'b993df81eec2980b5ee28ceec91542704dcec5706712c0d92db0d6e7beb23739',
 'backend/core/research_lifecycle_reconciliation_service.py':'21ed7164b3b029c30d462473800fd3d4e3a5c0f428ba6fac9d83be4e06adc3f2',
 'backend/core/nse_cross_sectional_selector_service.py':'6a8a8730bdede43bca4cd6a79ee895b009efaf7b8ae3e8f7a81cf41537f750fc',
 'backend/core/quant_edge_data_service.py':'853016af004b6d47044e7349d954e80cbe3ae5373782f3e44a766608c49fad06',
 'backend/core/data_plane/model_governance_repository.py':'a20716ab419911d4d213f1e3e134cb25e652f8db7210e42a7f1c2751d82efd94',
 'frontend/app.js':'d466544f0210a42888ddba45b9652412dff6a43f62210ab5c953acb62dc8caa7',
 'frontend/app.css':'cbb3650112346d39955f28a75b318131b224a4b2388fff293c93be51972c2614',
}
for rel,expected in frozen.items(): ck('frozen '+rel,sha(rel)==expected,sha(rel))

sys.path.insert(0,str(root/'backend'))
from core.quant_scan_capture_service import QuantScanCaptureService, QUANT_SCAN_CAPTURE_VERSION
from core.quant_edge_data_service import QuantEdgeDataService, MIN_TRAINING_FEATURE_COVERAGE

class NoCandleStore: pass
svc=QuantScanCaptureService(NoCandleStore())
row={'instrument_key':'NSE_EQ|X','change_pct':2.4,'sector_change_pct':0.9,'sector_status':'supportive','decision_ts':'2026-08-21T09:00:00Z'}
out=svc._attach_adv_evidence(row)
ck('positive sector-relative derived',out.get('sector_relative_return')==1.5,repr(out.get('sector_relative_return')))
ck('PIT derivation source recorded',out.get('sector_relative_source')=='DECISION_STOCK_CHANGE_MINUS_MAPPED_SECTOR_INDEX_CHANGE')
ck('derivation works without candle getter',out.get('sector_relative_as_of')=='2026-08-21T09:00:00Z')
neg=svc._attach_adv_evidence({'instrument_key':'NSE_EQ|Y','change_pct':-1.2,'sector_change_pct':-0.4,'sector_status':'conflicting','observed_at':'2026-08-21T09:00:00Z'})
ck('negative sector-relative preserved',neg.get('sector_relative_return')==-0.8,repr(neg.get('sector_relative_return')))
unavailable=svc._attach_adv_evidence({'instrument_key':'NSE_EQ|Z','change_pct':3.0,'sector_change_pct':1.0,'sector_status':'unavailable'})
ck('unavailable sector not guessed','sector_relative_return' not in unavailable)
existing=svc._attach_adv_evidence({'instrument_key':'NSE_EQ|A','change_pct':3.0,'sector_change_pct':1.0,'sector_status':'supportive','sector_relative_return':7.7})
ck('existing sector-relative authority preserved',existing.get('sector_relative_return')==7.7)
ck('capture version advanced',QUANT_SCAN_CAPTURE_VERSION=='quant-scan-capture-3.1.0-sector-relative-pit')

# Delivery coverage example mirroring the observed blocker: 5 missing of 12 is 58.3%; adding only the legitimate sector-relative evidence lifts coverage over the unchanged 60% gate.
base={
 'relative_strength_20d':1.0,'relative_strength_60d':2.0,'relative_strength_120d':3.0,
 'relative_volume':1.4,'technical_score':70,'liquidity_score':8.0,'atr_pct':2.1,
}
coverage_before,missing_before=QuantEdgeDataService._feature_completeness('delivery',base)
after=dict(base); after['sector_relative_return']=1.5
coverage_after,missing_after=QuantEdgeDataService._feature_completeness('delivery',after)
ck('observed 5-missing delivery shape is below gate',coverage_before < MIN_TRAINING_FEATURE_COVERAGE and len(missing_before)==5,json.dumps({'coverage':coverage_before,'missing':missing_before}))
ck('one legitimate sector feature crosses unchanged gate',coverage_after >= MIN_TRAINING_FEATURE_COVERAGE and len(missing_after)==4,json.dumps({'coverage':coverage_after,'missing':missing_after,'gate':MIN_TRAINING_FEATURE_COVERAGE}))
ck('training completeness gate unchanged',MIN_TRAINING_FEATURE_COVERAGE==0.60,repr(MIN_TRAINING_FEATURE_COVERAGE))

index=(root/'frontend/index.html').read_text(); config=(root/'backend/config.py').read_text(); front=json.loads((root/'frontend/release-identity.json').read_text())
marker='production-usability-r8-pl32-sector-relative-pit-8086'
ck('PL32 marker',marker in config and marker in index and front.get('build_marker')==marker)
ck('visible PL32 identity','v131 · R8 · PL32 · 8086' in index)
ck('UI4 premium refinement inherited','data-ui-version="UI4"' in index and 'PL31_INHERITS_UI4' not in front.get('ui_refinement',''))
ck('broker authority unchanged','BROKER_ORDER_EXECUTION_ENABLED = False' in config)
failed=[x for x in checks if not x['ok']]
print(json.dumps({'contract':'PL32_SECTOR_RELATIVE_PIT_CLOSURE','ok':not failed,'passed':len(checks)-len(failed),'failed':len(failed),'checks':checks},indent=2,default=str))
raise SystemExit(1 if failed else 0)
