from __future__ import annotations
import hashlib, json, os, subprocess, sys, tempfile
from pathlib import Path
sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
checks=[]
def ck(name, ok, detail=None): checks.append({'name':name,'ok':bool(ok),'detail':detail})
def sha(rel): return hashlib.sha256((ROOT/rel).read_bytes()).hexdigest()
FROZEN={
 'backend/tools/refresh_research_catalog.py':'80c1c68ee3f2c9f8b86410f7ff5c1bee58044ad93d8a6f9efe2e64fcf1c03015',
 'backend/core/historical_pit_sweep_service.py':'0ae6df1f98539dadea14802a3af96e1c609e274632f22c7192e8f722b4d3fb60',
 'backend/core/workload_governor.py':'c669d874ee995232dc6099d97e07e84bd021fa29a92e76e648796bdd131b4d1c',
 'backend/core/walk_forward_validation_service.py':'fe22ca6c07c555a86b371e2f2c59199de28249354fad0357ddad123a86335fdb',
 'backend/core/incremental_feature_store.py':'8080dfc613b2b69d51fcfcf6d97bc2b77e2d18029f3dd196eacd5c3c0d79efe3',
 'backend/core/factor_authority_service.py':'ab0ea8e5f07b4578d59f79dd286dca4a67733b4fb92cd48c5c60076f2ff789da',
 'backend/core/factor_dedup_service.py':'053f54c5b395e3f08a7c57f8bf87f732cb2ed540d2fd6aa7e778c630535b5a3d',
 'backend/core/factors/ic_ir_runner.py':'4fe6393d358cbee68b9050688e56c646a4a1fe01dc339b3e60853db4e866cd4e',
 'backend/core/scan_orchestration_service.py':'16621831e9dbefce1d497e96a9728861de900eb110407e1391dbe687cb65b654',
 'backend/core/decision_engine_service.py':'e035a0e3c36521ed2150a2ed9fcc18ef8e352b328a1b4a8f99be1a22e7c4cc69',
 'backend/core/trade_geometry_authority.py':'517c265231fd0da0142329780d93e1895e1350e917e8c5f8d7f9b254853e3525',
 'backend/core/exact_broker_cash_cost_authority.py':'70f463a37021fc2422b3d3195b11df13b1a8e59ca88f10d000459d99f723a727',
}
for rel, expected in FROZEN.items(): ck('frozen PL27 authority: '+rel, sha(rel)==expected, sha(rel))
trainer=(ROOT/'backend/tools/train_nse_smart_model.py').read_text(encoding='utf-8')
ck('PL27 stale rank schema fix retained','EXCLUDE(research_liquidity_value,research_liquidity_rank)' not in trainer and 'SELECT * EXCLUDE(research_liquidity_value)' in trainer)
ck('finite-target policy declared','NONFINITE_TARGETS_TO_MISSING_1.0.0_PL28' in trainer)
ck('target sanitizer covers all supervised labels','SUPERVISED_TARGET_COLUMNS = ("forward_return", "forward_equilibrium_atr20", "reverted_to_equilibrium")' in trainer)
ck('post-store sanitation protects retained PL27 feature stores','featured, target_sanitation = sanitize_supervised_targets(featured)' in trainer and trainer.index('featured, target_sanitation = sanitize_supervised_targets(featured)') < trainer.index('labelled = featured.dropna(subset=["forward_return"])'))
ck('model spec hashes target policy','"supervised_target_policy": SUPERVISED_TARGET_POLICY' in trainer)
ck('no target clipping introduced','clip(' not in trainer[trainer.index('def sanitize_supervised_targets'):trainer.index('def attach_historical_regime_labels')])
try:
 import numpy as np, pandas as pd
 from tools.train_nse_smart_model import sanitize_supervised_targets, SUPERVISED_TARGET_POLICY
 src=pd.DataFrame({
  'forward_return':[0.25, np.inf, -np.inf, -0.95, 12.5],
  'forward_equilibrium_atr20':[1.2, -np.inf, 0.0, np.inf, -3.0],
  'reverted_to_equilibrium':[1.0,0.0,1.0,np.inf,0.0],
 })
 clean,audit=sanitize_supervised_targets(src)
 ck('nonfinite labels become missing only', int(clean['forward_return'].isna().sum())==2 and int(clean['forward_equilibrium_atr20'].isna().sum())==2 and int(clean['reverted_to_equilibrium'].isna().sum())==1, audit)
 ck('finite extreme returns are preserved exactly', float(clean.loc[4,'forward_return'])==12.5 and float(clean.loc[3,'forward_return'])==-0.95, clean['forward_return'].tolist())
 ck('sanitation audit is explicit', audit.get('policy')==SUPERVISED_TARGET_POLICY and audit.get('nonfinite_removed')==5, audit)
 # Reproduce sklearn's reported failure, then prove the PL28 boundary admits only finite y.
 from sklearn.ensemble import HistGradientBoostingRegressor
 failed_as_expected=False
 try:
  HistGradientBoostingRegressor(max_iter=2, random_state=1).fit([[0.],[1.],[2.]], [0.1, np.inf, 0.2])
 except ValueError:
  failed_as_expected=True
 ck('Windows infinity fit failure reproduced',failed_as_expected)
 finite=clean.dropna(subset=['forward_return'])
 X=[[float(i)] for i in range(len(finite))]
 HistGradientBoostingRegressor(max_iter=2, random_state=1).fit(X, finite['forward_return'])
 ck('sanitized supervised target fits without infinity error',True,{'rows':len(finite)})
except Exception as exc:
 ck('finite-target functional proof',False,f'{type(exc).__name__}: {exc}')
marker='production-usability-r8-pl28-finite-targets-8086'
config=(ROOT/'backend/config.py').read_text(encoding='utf-8')
front=json.loads((ROOT/'frontend/release-identity.json').read_text(encoding='utf-8'))
index=(ROOT/'frontend/index.html').read_text(encoding='utf-8')
ck('PL28 exact build marker', marker in config and front.get('build_marker')==marker and f'data-build-marker="{marker}"' in index)
def run(rel):
 env=dict(os.environ); env['PYTHONDONTWRITEBYTECODE']='1'; env['PYTHONPATH']=str(ROOT/'backend')
 p=subprocess.run([sys.executable,str(ROOT/rel)],cwd=ROOT,text=True,capture_output=True,timeout=120,env=env)
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
print(json.dumps({'contract':'PL28_FINITE_TARGET_TRAINING_CLOSURE','ok':not failed,'passed':len(checks)-len(failed),'failed':len(failed),'checks':checks},indent=2,default=str))
raise SystemExit(0 if not failed else 1)
