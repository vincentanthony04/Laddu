import hashlib, json, math, sys
from pathlib import Path
root=Path(__file__).resolve().parents[1]
checks=[]
def ck(name, cond, detail=''):
    checks.append({'name':name,'ok':bool(cond),'detail':detail})
def sha(rel):
    return hashlib.sha256((root/rel).read_bytes()).hexdigest()

# Freeze scanner/trading/WFA/research-training authorities from exact PL29.
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
for rel, expected in frozen.items():
    ck('frozen '+rel, sha(rel)==expected, sha(rel))

quant=(root/'backend/core/quant_edge_data_service.py').read_text()
repo=(root/'backend/core/data_plane/model_governance_repository.py').read_text()
index=(root/'frontend/index.html').read_text()
app=(root/'frontend/app.js').read_text()
css=(root/'frontend/ui-system.css').read_text()
config=(root/'backend/config.py').read_text()
front=json.loads((root/'frontend/release-identity.json').read_text())

ck('quant JSON-safe before snapshot hash', 'from core.strict_json import json_safe' in quant and 'safe_features = json_safe(features or {})' in quant)
ck('legacy hash compatibility semantic only', '_legacy_snapshot_hash_only_compatible' in repo and 'payload.pop("snapshot_hash", None)' in repo)
ck('genuine snapshot conflict still fail closed', 'raise RuntimeError("SELECTOR_FEATURE_SNAPSHOT_HASH_CONFLICT")' in repo)
ck('member immutable compatibility guarded', 'immutable_same = all([' in repo and 'envelope_same' in repo and 'MODEL_GOVERNANCE_IDEMPOTENCY_CONFLICT:research.selector_population_members' in repo)

# Functional proof: non-finite feature is normalised before hashing; JSONB-style
# strict round-trip is stable. An exact legacy hash-only mismatch is compatible,
# while a genuine content mutation is not.
sys.path.insert(0,str(root/'backend'))
from core.quant_edge_data_service import QuantEdgeDataService, _sha
from core.strict_json import strict_json_dumps
from core.data_plane.model_governance_repository import ProductionModelGovernanceRepository
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
features={
 'identity_verified':True,'exchange':'NSE','side':'LONG','rsi':float('nan'),'atr':20.0,'ltp':1500.0,
 'decision_ts':'2026-08-21T08:50:00Z','feature_as_of':'2026-08-21T08:49:59Z',
 'source_as_of':'2026-08-21T08:49:58Z','received_at':'2026-08-21T08:49:59Z',
 'universe_membership_as_of':'2026-08-21T00:00:00Z','market_regime':'RANGE','freshness_state':'FRESH',
}
snap=svc.record_snapshot(candidate_id='c1',population_fingerprint='p1',symbol='INFY',instrument_key='NSE_EQ|INE009A01021',mode='delivery',side='LONG',decision_ts='2026-08-21T08:50:00Z',universe_id='u1',dataset_fingerprint='d1',feature_manifest_hash='m1',feature_hash='f1',features=features,candidate_identity_version='candidate-identity-v2',_persist=False)
payload={k:v for k,v in snap.items() if k not in {'ok','inserted','snapshot_hash'}}
ck('non-finite becomes null', snap['features'].get('rsi') is None, repr(snap['features'].get('rsi')))
ck('strict roundtrip hash stable', _sha(json.loads(strict_json_dumps(payload,sort_keys=True,separators=(',',':'))))==snap['snapshot_hash'])
legacy_payload=json.loads(json.dumps(payload,default=str))
legacy_payload['features']['rsi']=float('nan')
legacy_hash=_sha(legacy_payload)
stored=json.loads(strict_json_dumps({**legacy_payload,'snapshot_hash':legacy_hash},sort_keys=True,separators=(',',':')))
incoming={**payload,'snapshot_hash':snap['snapshot_hash']}
ck('legacy PL29 hash-only mismatch accepted', legacy_hash!=snap['snapshot_hash'] and ProductionModelGovernanceRepository._legacy_snapshot_hash_only_compatible(stored,incoming))
mutated=json.loads(json.dumps(incoming)); mutated['features']['ltp']=1501.0
ck('genuine feature mutation rejected', not ProductionModelGovernanceRepository._legacy_snapshot_hash_only_compatible(stored,mutated))

# Trade Ready must be the first major workspace decision surface.
pos_trade=index.find('id="actionablePanel"')
pos_perf=index.find('id="r7PerformanceCockpit"')
pos_watch=index.find('id="workspaceSupportGrid"')
ck('Trade Ready precedes performance and research', 0 <= pos_trade < pos_perf < pos_watch, f'{pos_trade},{pos_perf},{pos_watch}')
for token in ('<th>Action</th>','<th>Qty</th>','<th>₹ Risk</th>','<th>Freshness</th>','<th>Rank</th>'):
    ck('Trade Ready column '+token, token in index)
ck('explicit no-trade-ready state', 'NO TRADE READY DECISIONS' in app)
ck('strongest ranked first preserved', 'ranked.sort((a,b)' in app and 'workspaceSignalScore' in app)
ck('no browser risk synthesis', "riskAmount=number(pick(row,'risk_amount','risk_rupees','position_risk','risk_budget_rupees','max_loss_rupees'))" in app and 'riskAmount===null' in app)
ck('prime semantic surface styling', '.trade-ready-prime' in css and 'var(--green)' in css and 'var(--amber)' in css)

marker='production-usability-r8-pl30-trade-ready-selector-hash-8086'
ck('PL30 marker', marker in config and front.get('build_marker')==marker and f'data-build-marker="{marker}"' in index)
ck('visible PL30 identity','v131 · R8 · PL30 · 8086' in index)
ck('broker authority unchanged', 'BROKER_ORDER_EXECUTION_ENABLED = False' in config)
failed=[x for x in checks if not x['ok']]
print(json.dumps({'contract':'PL30_TRADE_READY_SELECTOR_HASH_CLOSURE','ok':not failed,'passed':len(checks)-len(failed),'failed':len(failed),'checks':checks},indent=2,default=str))
raise SystemExit(1 if failed else 0)
