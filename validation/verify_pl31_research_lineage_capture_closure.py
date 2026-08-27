import hashlib, json, sys
from datetime import datetime, timezone
from pathlib import Path
root=Path(__file__).resolve().parents[1]
checks=[]
def ck(name, cond, detail=''):
    checks.append({'name':name,'ok':bool(cond),'detail':detail})
def sha(rel):
    return hashlib.sha256((root/rel).read_bytes()).hexdigest()

# Freeze production decision/trading/WFA authorities from PL30.
frozen={
 'backend/core/scan_orchestration_lifecycle.py':'97fa6c7e1cabe30c6f88caa7719cea6dc90c07694ee5159e79584ff908940afc',
 'backend/core/walk_forward_validation_service.py':'fe22ca6c07c555a86b371e2f2c59199de28249354fad0357ddad123a86335fdb',
 'backend/tools/train_nse_smart_model.py':'7f4d837de209ed64a333644f7c444fa4126207db35725520a5d9b146e1233b15',
 'backend/core/india_cost_model.py':'5d55e47a4286387f38785d672d9cd778114a29674d5a78020fd781ecfe11646a',
 'backend/core/production_ranking_service.py':'2e7665f30ca04a336c689401e903688212532f54865f9430e46af074965af35f',
 'backend/core/today_entries_lifecycle_projection_service.py':'1e3e1892e915473a55acd4b621768d1dd66cc561f1400d68fd39564709e8c8f6',
 'backend/core/decision_surface_reconciliation_service.py':'56e85b66f4c062bf98ebf8f7601a25f392b6481faa39381ba46325b259306053',
 'backend/tools/refresh_research_catalog.py':'80c1c68ee3f2c9f8b86410f7ff5c1bee58044ad93d8a6f9efe2e64fcf1c03015',
}
for rel,expected in frozen.items(): ck('frozen '+rel,sha(rel)==expected,sha(rel))


# PL30 cumulative contract remains frozen underneath the lineage-only delta.
pl30_frozen={
 'backend/core/quant_edge_data_service.py':'853016af004b6d47044e7349d954e80cbe3ae5373782f3e44a766608c49fad06',
 'backend/core/data_plane/model_governance_repository.py':'a20716ab419911d4d213f1e3e134cb25e652f8db7210e42a7f1c2751d82efd94',
 'frontend/app.js':'d466544f0210a42888ddba45b9652412dff6a43f62210ab5c953acb62dc8caa7',
 'frontend/app.css':'cbb3650112346d39955f28a75b318131b224a4b2388fff293c93be51972c2614',
}
for rel,expected in pl30_frozen.items(): ck('PL30 cumulative frozen '+rel,sha(rel)==expected,sha(rel))
quant=(root/'backend/core/quant_edge_data_service.py').read_text()
repo=(root/'backend/core/data_plane/model_governance_repository.py').read_text()
index=(root/'frontend/index.html').read_text()
app=(root/'frontend/app.js').read_text()
ck('PL30 JSON-safe snapshot hash retained','safe_features = json_safe(features or {})' in quant)
ck('PL30 legacy semantic hash compatibility retained','_legacy_snapshot_hash_only_compatible' in repo and 'SELECTOR_FEATURE_SNAPSHOT_HASH_CONFLICT' in repo)
pos_trade=index.find('id="actionablePanel"'); pos_perf=index.find('id="r7PerformanceCockpit"'); pos_watch=index.find('id="workspaceSupportGrid"')
ck('PL30 Trade Ready remains first major workspace decision surface',0 <= pos_trade < pos_perf < pos_watch,f'{pos_trade},{pos_perf},{pos_watch}')
ck('PL30 no browser risk synthesis',"riskAmount=number(pick(row,'risk_amount','risk_rupees','position_risk','risk_budget_rupees','max_loss_rupees'))" in app and 'riskAmount===null' in app)

sys.path.insert(0,str(root/'backend'))
import core.scan_orchestration_rows as rows
from core.quant_scan_capture_service import QuantScanCaptureService
from core.quant_edge_data_service import QuantEdgeDataService

# Exact quote receipt must replace a stale generic receipt carried by the decision row.
rows.india_now=lambda: datetime(2026,8,20,9,0,10,tzinfo=timezone.utc)
rows.is_india_market_open=lambda: True
decision={
 'symbol':'MCX','observed_at':'2026-08-20T09:00:10Z','received_at':'2026-08-20T08:59:00Z',
 'side':'LONG','planned_entry':100.0,'planned_t1':102.0,'planned_sl':99.0,'identity_verified':True,
}
instrument={'trading_symbol':'MCX','instrument_key':'NSE_EQ|INE745G01043','exchange':'NSE'}
quote={
 'ltp':100.5,'identity_verified':True,
 'provider_timestamp':'2026-08-20T09:00:08Z','received_at':'2026-08-20T09:00:08.400000Z',
}
captured=rows.research_capture_row(decision,instrument,'intraday',quote)
ck('exact quote receipt captured',captured.get('quote_received_at')==quote['received_at'])
ck('stale generic receipt replaced by exact quote receipt',captured.get('received_at')==quote['received_at'],repr(captured.get('received_at')))
prepared=QuantScanCaptureService._prepare(captured,'2026-08-20T09:00:10Z')
ck('quant capture prefers quote receipt',prepared.get('received_at')==quote['received_at'],repr(prepared.get('received_at')))

# Record-snapshot lineage must no longer fail merely because the decision row carried an older receipt.
class Repo:
    authority=object()
    def record_selector_feature_snapshot(self,*a,**k): raise AssertionError('not used')
    def selector_feature_snapshot(self,*a,**k): return None
    def record_selector_label(self,*a,**k): return None
    def selector_evidence_status(self,*a,**k): return None
    def quant_training_rows(self,*a,**k): return []
class Store:
    production_model_governance_required=True
    production_model_governance_repository=Repo()
svc=QuantEdgeDataService(Store())
features=dict(prepared)
features.update({
 'identity_verified':True,'market_regime':'RANGE','freshness_state':'FRESH',
 'change_pct':1.2,'session_relative_volume':1.7,'vwap_distance_pct':0.3,'adx':28,
 'technical_score':78,'expected_net_move_bps':55,'spread_bps':4,'quote_age_seconds':2,
 'liquidity_score':90,
})
snap=svc.record_snapshot(candidate_id='pl31-mcx',population_fingerprint='pl31-pop',symbol='MCX',instrument_key=instrument['instrument_key'],mode='intraday',side='LONG',decision_ts='2026-08-20T09:00:10Z',universe_id='u',dataset_fingerprint='d',feature_manifest_hash='m',feature_hash='f',features=features,_persist=False)
ck('corrected lineage verifies',snap.get('lineage_state')=='VERIFIED',repr(snap.get('lineage_state')))
ck('corrected snapshot can be complete',snap.get('snapshot_state')=='COMPLETE',json.dumps({'coverage':snap.get('compact_feature_coverage'),'missing':snap.get('missing_features'),'freshness':snap.get('freshness_state')}))

# Genuine bad provider/receipt ordering must remain fail-closed.
bad=dict(features); bad['source_as_of']='2026-08-20T09:00:09Z'; bad['quote_as_of']=bad['source_as_of']; bad['received_at']='2026-08-20T09:00:08Z'; bad['quote_received_at']=bad['received_at']
bad_snap=svc.record_snapshot(candidate_id='pl31-bad',population_fingerprint='pl31-pop2',symbol='MCX',instrument_key=instrument['instrument_key'],mode='intraday',side='LONG',decision_ts='2026-08-20T09:00:10Z',universe_id='u',dataset_fingerprint='d',feature_manifest_hash='m',feature_hash='f2',features=bad,_persist=False)
ck('genuine timestamp inversion still rejected',bad_snap.get('lineage_state')=='INVALID_TIMESTAMP_ORDER' and bad_snap.get('snapshot_state')=='PARTIAL',repr((bad_snap.get('lineage_state'),bad_snap.get('snapshot_state'))))

# Missing quote receipt is not fabricated by the row builder.
quote_no_receipt={'ltp':100.5,'identity_verified':True,'provider_timestamp':'2026-08-20T09:00:08Z'}
no_receipt=rows.research_capture_row({'symbol':'MCX','observed_at':'2026-08-20T09:00:10Z'},instrument,'intraday',quote_no_receipt)
ck('missing quote receipt not fabricated','quote_received_at' not in no_receipt)

# Diagnostics now expose the exact times and coverage needed for operator triage.
recon=(root/'backend/core/research_lifecycle_reconciliation_service.py').read_text()
for token in ('"compact_feature_coverage": snapshot.get("compact_feature_coverage")','"source_as_of": snapshot.get("source_as_of")','"received_at": snapshot.get("received_at")','"freshness_eligible_for_training": snapshot.get("freshness_eligible_for_training")'):
    ck('diagnostic '+token,token in recon)

config=(root/'backend/config.py').read_text(); index=(root/'frontend/index.html').read_text(); front=json.loads((root/'frontend/release-identity.json').read_text())
marker='production-usability-r8-pl31-research-lineage-8086'
ck('PL31 marker',marker in config and marker in index and front.get('build_marker')==marker)
ck('visible PL31 identity','v131 · R8 · PL31 · 8086' in index)
ck('broker authority unchanged','BROKER_ORDER_EXECUTION_ENABLED = False' in config)
failed=[x for x in checks if not x['ok']]
print(json.dumps({'contract':'PL31_RESEARCH_LINEAGE_CAPTURE_CLOSURE','ok':not failed,'passed':len(checks)-len(failed),'failed':len(failed),'checks':checks},indent=2,default=str))
raise SystemExit(1 if failed else 0)
