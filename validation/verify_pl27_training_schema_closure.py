from __future__ import annotations
import hashlib, json, os, subprocess, sys, tempfile
from pathlib import Path
from types import SimpleNamespace
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
 'backend/core/factor_authority_service.py':'ab0ea8e5f07b4578d59f79dd286dca4a67733b4fb92cd48c5c60076f2ff789da',
 'backend/core/factor_dedup_service.py':'053f54c5b395e3f08a7c57f8bf87f732cb2ed540d2fd6aa7e778c630535b5a3d',
 'backend/core/factors/ic_ir_runner.py':'4fe6393d358cbee68b9050688e56c646a4a1fe01dc339b3e60853db4e866cd4e',
 'backend/core/scan_orchestration_service.py':'16621831e9dbefce1d497e96a9728861de900eb110407e1391dbe687cb65b654',
 'backend/core/decision_engine_service.py':'e035a0e3c36521ed2150a2ed9fcc18ef8e352b328a1b4a8f99be1a22e7c4cc69',
 'backend/core/trade_geometry_authority.py':'517c265231fd0da0142329780d93e1895e1350e917e8c5f8d7f9b254853e3525',
 'backend/core/exact_broker_cash_cost_authority.py':'70f463a37021fc2422b3d3195b11df13b1a8e59ca88f10d000459d99f723a727',
}
for rel, expected in FROZEN.items(): ck('frozen PL26 authority: '+rel, sha(rel)==expected, sha(rel))
trainer=(ROOT/'backend/tools/train_nse_smart_model.py').read_text(encoding='utf-8')
refresh=(ROOT/'backend/tools/refresh_research_catalog.py').read_text(encoding='utf-8')
ck('stale missing rank exclusion removed','EXCLUDE(research_liquidity_value,research_liquidity_rank)' not in trainer)
ck('actual materializer auxiliary exclusion retained','SELECT * EXCLUDE(research_liquidity_value)' in trainer)
ck('materializer creates liquidity value not phantom rank','AS research_liquidity_value' in refresh and 'research_liquidity_rank' not in refresh)
marker='production-usability-r8-pl27-training-schema-8086'
config=(ROOT/'backend/config.py').read_text(encoding='utf-8')
front=json.loads((ROOT/'frontend/release-identity.json').read_text(encoding='utf-8'))
index=(ROOT/'frontend/index.html').read_text(encoding='utf-8')
ck('PL27 exact build marker', marker in config and front.get('build_marker')==marker and f'data-build-marker="{marker}"' in index)
try:
 import types, pandas as pd
 class FakeDB:
  def __init__(self): self.last=''
  def execute(self, query, params=None):
   self.last=str(query)
   if 'research_liquidity_rank' in self.last:
    raise RuntimeError('Binder Error: Column "research_liquidity_rank" in EXCLUDE list not found in FROM clause')
   return self
  def fetchall(self):
   if 'information_schema.tables' in self.last: return [('research_delivery_training_panel',)]
   return []
  def fetchdf(self):
   return pd.DataFrame([{'date':'2026-08-20','symbol':'TEST','instrument_key':'NSE_EQ|TEST','open':100.0,'high':102.0,'low':99.0,'close':101.0,'volume':100000.0,'oi':0.0,'universe_join_authority':'CURRENT_INSTRUMENTS_SHADOW_FALLBACK','traded_qty':100000.0,'deliverable_qty':50000.0,'delivery_pct':50.0}])
  def close(self): pass
 fake=types.SimpleNamespace(connect=lambda *a,**k: FakeDB())
 sys.modules['duckdb']=fake
 from tools.train_nse_smart_model import load_panel_from_lake
 with tempfile.TemporaryDirectory() as td:
  dbp=Path(td)/'quant.duckdb'; dbp.write_bytes(b'fake')
  panel=load_panel_from_lake(SimpleNamespace(analytics_db=dbp))
  ck('functional PL27 materialized panel load succeeds',len(panel)==1 and panel.iloc[0]['symbol']=='TEST',list(panel.columns))
  ck('auxiliary liquidity column excluded from model frame','research_liquidity_value' not in panel.columns and 'research_liquidity_rank' not in panel.columns,list(panel.columns))
except Exception as exc:
 ck('functional PL27 materialized panel load succeeds',False,f'{type(exc).__name__}: {exc}')
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
print(json.dumps({'contract':'PL27_TRAINING_SCHEMA_CLOSURE','ok':not failed,'passed':len(checks)-len(failed),'failed':len(failed),'checks':checks},indent=2,default=str))
raise SystemExit(0 if not failed else 1)
