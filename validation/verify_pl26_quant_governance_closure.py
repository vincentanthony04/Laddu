from __future__ import annotations
import hashlib, json, sqlite3, subprocess, sys, tempfile, threading
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
BACKEND=ROOT/'backend'
sys.path.insert(0,str(BACKEND))
checks=[]
def ck(name, ok, detail=None): checks.append({'name':name,'ok':bool(ok),'detail':detail})
def sha(rel): return hashlib.sha256((ROOT/rel).read_bytes()).hexdigest()

# PL26 must not solve market-hour contention by altering protected runtime/WFA/math.
FROZEN={
 'backend/core/historical_pit_sweep_service.py':'0ae6df1f98539dadea14802a3af96e1c609e274632f22c7192e8f722b4d3fb60',
 'backend/core/workload_governor.py':'c669d874ee995232dc6099d97e07e84bd021fa29a92e76e648796bdd131b4d1c',
 'backend/core/research_catalogue_evidence_service.py':'621b7670b3ffbab46fab44a713a0611b11fe625d345159ae2adaefa78c5b042f',
 'backend/core/walk_forward_validation_service.py':'fe22ca6c07c555a86b371e2f2c59199de28249354fad0357ddad123a86335fdb',
 'backend/core/selection_walk_forward_replay_service.py':'f409edeaf0525122cf368c0c0a91d84667127272d7adc74feddf9e85fb650bba',
 'backend/core/factors/factor_thresholds.py':'97d81ed475d272ac496364ba638ac33c9e040563f0f79b644151ae1264174216',
 'backend/core/factor_authority_service.py':'ab0ea8e5f07b4578d59f79dd286dca4a67733b4fb92cd48c5c60076f2ff789da',
 'backend/core/factor_dedup_service.py':'053f54c5b395e3f08a7c57f8bf87f732cb2ed540d2fd6aa7e778c630535b5a3d',
 'backend/core/factors/ic_ir_runner.py':'4fe6393d358cbee68b9050688e56c646a4a1fe01dc339b3e60853db4e866cd4e',
 'backend/core/scan_orchestration_service.py':'16621831e9dbefce1d497e96a9728861de900eb110407e1391dbe687cb65b654',
 'backend/core/decision_engine_service.py':'e035a0e3c36521ed2150a2ed9fcc18ef8e352b328a1b4a8f99be1a22e7c4cc69',
 'backend/core/trade_geometry_authority.py':'517c265231fd0da0142329780d93e1895e1350e917e8c5f8d7f9b254853e3525',
 'backend/core/exact_broker_cash_cost_authority.py':'70f463a37021fc2422b3d3195b11df13b1a8e59ca88f10d000459d99f723a727',
}
for rel, expected in FROZEN.items(): ck('frozen PL25 authority: '+rel, sha(rel)==expected, sha(rel))

marker='production-usability-r8-pl26-quant-governance-8086'
config=(ROOT/'backend/config.py').read_text(encoding='utf-8')
front=json.loads((ROOT/'frontend/release-identity.json').read_text(encoding='utf-8'))
index=(ROOT/'frontend/index.html').read_text(encoding='utf-8')
ck('PL26 exact build marker', marker in config and front.get('build_marker')==marker and f'data-build-marker="{marker}"' in index)

capital=(ROOT/'backend/core/capital_readiness_service.py').read_text(encoding='utf-8')
trainer=(ROOT/'backend/tools/train_nse_smart_model.py').read_text(encoding='utf-8')
publisher=(ROOT/'backend/core/ai_training_publication_service.py').read_text(encoding='utf-8')
challenger=(ROOT/'backend/core/model_challenger_governance_service.py').read_text(encoding='utf-8')
audit=(ROOT/'tools/AUDIT_QUANT_ML_ALPHA.ps1').read_text(encoding='utf-8')
ck('capital-readiness undefined desk defect removed', 'paper_desk = ' in capital and 'shadow_desk = ' not in capital)
ck('trainer writes measured factor registry', all(t in trainer for t in ['persist_factor_governance','upsert_factor_registry','DEFAULT_ALIVE_IC_THRESHOLD','FactorDedupService().audit_frame']))
ck('formula/prod authority never fabricated', 'formula_class="UNVERIFIED"' in trainer and 'production_influence=0' in trainer)
ck('factor evidence published to compatibility registry', 'factor_registry = [dict(item or {})' in publisher and "VALUES(?,?,?,?,?,?,?,?,?,?,?,'UNVERIFIED',NULL,?,0)" in publisher)
ck('publication preserves separately verified formula authority', 'formula_class=excluded.formula_class' not in publisher and 'production_influence=excluded.production_influence' not in publisher)
ck('HistGradientBoosting governed family declared', '"hist_gradient_boosting"' in challenger and '"model_family": "hist_gradient_boosting"' in trainer)
ck('dead shadow endpoint removed from audit', '/api/shadow-portfolio' not in audit)
ck('forward selector remains prospective only', 'record_selector_population' not in trainer and 'record_selector_outcome' not in trainer)

# Functional CapitalReadiness proof: reaching assess() must no longer NameError.
try:
 import core.capital_readiness_service as cr
 old=cr.ProductionRiskAuthorityService
 class DummyRisk:
  def __init__(self,*a,**k): pass
  def status(self): return {'operator_stop':{'enabled':False},'account_loss_state':{'measured':True},'portfolio':{'portfolio_heat_pct':0},'limits':{'max_portfolio_heat_pct':1}}
 cr.ProductionRiskAuthorityService=DummyRisk
 class X(cr.CapitalReadinessService):
  def _latest_capital_validation(self, model_id): return {}
  def _latest_fairness(self, desk): return {}
  def _model_paper_evidence(self): return {'by_desk':{d:{'closed_count':0,'observation_days':0,'cost_versions':[]} for d in self.DESKS}}
  def _reconciliation(self): return {'state':'measured','duplicate_open_model_paper_positions':0,'duplicate_open_signal_theses':0}
  def _worker_health(self): return {'healthy':True}
 result=X(object()).assess()
 ck('functional capital-readiness assess returns structured result', result.get('ok') is True and 'desk_gates' in result, result.get('maturity'))
 cr.ProductionRiskAuthorityService=old
except Exception as exc:
 ck('functional capital-readiness assess',False,f'{type(exc).__name__}: {exc}')

# Functional local IC/IR + redundancy publication proof on synthetic data whose
# signal is deliberately known. This tests plumbing, not claimed market alpha.
try:
 import numpy as np, pandas as pd
 from tools import train_nse_smart_model as t
 rng=np.random.default_rng(4510)
 dates=pd.date_range('2025-01-01',periods=45,freq='B')
 symbols=[f'S{i:02d}' for i in range(12)]
 sig=rng.normal(size=(len(dates),len(symbols)))
 close=np.full((len(dates),len(symbols)),100.0)
 for i in range(len(dates)-1): close[i+1]=close[i]*(1+0.006*sig[i]+rng.normal(scale=.001,size=len(symbols)))
 rows=[]
 for di,date in enumerate(dates):
  for si,symbol in enumerate(symbols):
   row={'date':date,'symbol':symbol,'close':close[di,si]}
   for name in t.FEATURES: row[name]=float(sig[di,si] if name=='ret_1' else rng.normal())
   rows.append(row)
 frame=pd.DataFrame(rows)
 with tempfile.TemporaryDirectory() as td:
  conn=sqlite3.connect(str(Path(td)/'factor.sqlite3')); conn.row_factory=sqlite3.Row
  decay,registry,dedup=t.persist_factor_governance(frame,conn,1,'pl26-functional-proof')
  r1=next(row for row in registry if row['factor_name']=='ret_1')
  ck('functional factor registry covers exact model feature set',len(registry)==len(t.FEATURES)==53,{'registry':len(registry),'features':len(t.FEATURES)})
  ck('functional factor decay covers exact model feature set',len(decay)==53,len(decay))
  ck('functional known signal measured alive',r1['status']=='alive' and float(r1['ic_score'])>0.02,{'status':r1['status'],'ic':r1['ic_score']})
  ck('functional registry remains non-production/unverified',all(r['formula_class']=='UNVERIFIED' and int(r['production_influence'])==0 for r in registry))
  ck('functional redundancy audit measured',dedup.get('ok') is True and dedup.get('state')=='MEASURED',{'state':dedup.get('state'),'rows':dedup.get('sample_rows')})
except Exception as exc:
 ck('functional factor governance',False,f'{type(exc).__name__}: {exc}')

# Functional publication projection must update empirical evidence while refusing
# to overwrite independently verified formula/production authority.
try:
 from core.ai_training_publication_service import AITrainingPublicationService
 from core.factors.factor_store import ensure_factor_tables, FactorRegistryRow, upsert_factor_registry
 class S: pass
 store=S(); store.conn=sqlite3.connect(':memory:'); store.conn.row_factory=sqlite3.Row; store.write_lock=threading.Lock(); store.production_model_governance_repository=None; store.production_model_governance_required=False
 ensure_factor_tables(store.conn)
 upsert_factor_registry(store.conn,FactorRegistryRow('f1','legacy',.1,.2,'alive','2026-08-21T00:00:00Z',formula_class='EXACT',formula_verification_hash='verified',empirical_qualification_hash='old',production_influence=1))
 bundle={'publication_id':'pl26-functional','model':{'model_id':'m1','model_version':'1','framework':'HistGradientBoosting','model_family':'hist_gradient_boosting','horizon_days':10,'lifecycle_state':'SHADOW','feature_manifest_hash':'fh','dataset_fingerprint':'df','trained_through':'2026-08-20','training_data_source':'PARQUET_DUCKDB'},'predictions':[],'factor_decay':[],'factor_registry':[{'factor_name':'f1','family':'nse','ic_score':.03,'ir_score':.2,'status':'alive','last_validated':'2026-08-21T00:00:00Z','redundancy_status':'CANONICAL','canonical_factor_name':'f1','redundancy_correlation':1.0,'dedup_version':'d','dedup_measured_at':'2026-08-21T00:00:00Z','formula_class':'EXACT','formula_verification_hash':'untrusted','empirical_qualification_hash':'new','production_influence':1}]}
 published=AITrainingPublicationService(store).publish(bundle)
 row=dict(store.conn.execute('SELECT * FROM factor_registry WHERE factor_name="f1"').fetchone())
 ck('functional factor publication updates empirical evidence',published.get('ok') is True and row['ic_score']==.03 and row['empirical_qualification_hash']=='new',row)
 ck('functional offline publication cannot elevate/replace formula authority',row['formula_class']=='EXACT' and row['formula_verification_hash']=='verified' and row['production_influence']==1,{'formula_class':row['formula_class'],'formula_hash':row['formula_verification_hash'],'production_influence':row['production_influence']})
except Exception as exc:
 ck('functional factor publication',False,f'{type(exc).__name__}: {exc}')

try:
 from core.model_challenger_governance_service import ModelChallengerGovernanceService
 result=ModelChallengerGovernanceService().assess({'framework':'equilibrium-aware HistGradientBoosting'}, {})
 ck('functional legacy HistGradientBoosting family recognized',result.get('family')=='hist_gradient_boosting' and result.get('stage')!='UNKNOWN_MODEL_FAMILY',{'family':result.get('family'),'stage':result.get('stage')})
except Exception as exc:
 ck('functional challenger family recognition',False,f'{type(exc).__name__}: {exc}')

# Reprove full scanner engines.
def run(rel):
 p=subprocess.run([sys.executable,str(ROOT/rel)],cwd=ROOT,text=True,capture_output=True,timeout=120)
 return p.returncode,(p.stdout or p.stderr).strip()
rc,out=run('validation/validate_delivery_coverage_scheduler.py')
try:d=json.loads(out.splitlines()[-1])
except Exception:d={}
ck('Delivery 4137 full sweep still proven',rc==0 and d.get('full_sweep') is True and d.get('population')==4137 and d.get('strictly_monotonic') is True,d)
rc,out=run('validation/verify_r40_intraday_authority_closure.py')
try:d=json.loads(out)
except Exception:d={}
ck('Intraday bounded authority still proven',rc==0 and d.get('ok') is True,{'ok':d.get('ok'),'failures':len(d.get('failures') or [])})

failed=[x for x in checks if not x['ok']]
print(json.dumps({'contract':'PL26_QUANT_GOVERNANCE_CLOSURE','ok':not failed,'passed':len(checks)-len(failed),'failed':len(failed),'checks':checks},indent=2,default=str))
raise SystemExit(0 if not failed else 1)
