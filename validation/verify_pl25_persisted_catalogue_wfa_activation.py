from __future__ import annotations
import hashlib, json, subprocess, sys, tempfile
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
checks=[]
def ck(name, ok, detail=None): checks.append({'name':name,'ok':bool(ok),'detail':detail})
def sha(rel): return hashlib.sha256((ROOT/rel).read_bytes()).hexdigest()

FROZEN={
 'backend/core/scan_orchestration_service.py':'16621831e9dbefce1d497e96a9728861de900eb110407e1391dbe687cb65b654',
 'backend/core/scan_orchestration_modes.py':'0afd0d00a577c4edda94b79724f72787526c93fbf1ed9d0bd76c70e7ade9d07f',
 'backend/core/scan_orchestration_coverage.py':'9c6b3ae36640982e8548b2960610301b2fe28c4720d828c84d58ee16eb3773df',
 'backend/core/scan_orchestration_lifecycle.py':'97fa6c7e1cabe30c6f88caa7719cea6dc90c07694ee5159e79584ff908940afc',
 'backend/core/scan_orchestration_fast_lane.py':'0a5c02a1877f5c18878249839311c3bfbeba9d37b0541476e13c68783ecc5ffe',
 'backend/core/scan_orchestration_rows.py':'cd37c69edfe178b961cd1d91f6db19262dfae63ed813acccf000875c61608675',
 'backend/core/scan_orchestration_discovery.py':'6e7735c80d1457ce86deb3f44ff46045b3e7ab5ec0608f45f143e9c61f596dc4',
 'backend/core/desk_analysis_executor_router.py':'0db0b9e47d6c05525f332993ca3351fe854d183c83debcbcd570a177b0297720',
 'backend/core/decision_engine_service.py':'e035a0e3c36521ed2150a2ed9fcc18ef8e352b328a1b4a8f99be1a22e7c4cc69',
 'backend/core/trade_geometry_authority.py':'517c265231fd0da0142329780d93e1895e1350e917e8c5f8d7f9b254853e3525',
 'backend/core/exact_broker_cash_cost_authority.py':'70f463a37021fc2422b3d3195b11df13b1a8e59ca88f10d000459d99f723a727',
 'backend/core/model_paper_lifecycle_authority.py':'2d0a69453d7568aab420191df675a4677cd5a6407eb045c4eab3e7ad7b1aac8c',
 'backend/core/outcome_accuracy_taxonomy.py':'2c000a60c2570c341416ae7bf6e5fbf53d72b618a19da05fcd1e2ae0a04eb470',
 'backend/core/intraday_session_structure_authority.py':'b296c8a3972ab7eacee85da2c2242fcb27e781180267cb9b87741aa6a97f06eb',
 'backend/core/structural_trade_map_service.py':'f3d0b139947ba79ec7a3ccb8a65f29fb5ba76bc98e57393c62199f4aa694d0c9',
 'backend/core/evidence_engine_service.py':'f0c33451e1293ce4c53ed13ae0375b9caa00ca15e2915d5f5b1cb60126333c9e',
}
for rel,expected in FROZEN.items(): ck('frozen scanner/trading core: '+rel,sha(rel)==expected,sha(rel))

marker='production-usability-r8-pl25-catalogue-wfa-8086'
config=(ROOT/'backend/config.py').read_text(encoding='utf-8')
front=json.loads((ROOT/'frontend/release-identity.json').read_text(encoding='utf-8'))
index=(ROOT/'frontend/index.html').read_text(encoding='utf-8')
ck('PL25 exact build marker',marker in config and front.get('build_marker')==marker and f'data-build-marker="{marker}"' in index)

probe=(ROOT/'backend/core/research_catalogue_evidence_service.py').read_text(encoding='utf-8')
status=(ROOT/'backend/core/evidence_pipeline_status_service.py').read_text(encoding='utf-8')
pit=(ROOT/'backend/core/historical_pit_sweep_service.py').read_text(encoding='utf-8')
runner=(ROOT/'RUN_EVIDENCE_PIPELINE_STATUS.ps1').read_text(encoding='utf-8')
trainer=(ROOT/'backend/tools/train_nse_smart_model.py').read_text(encoding='utf-8')
repo=(ROOT/'backend/core/data_plane/model_governance_repository.py').read_text(encoding='utf-8')
ck('read-only persisted panel probe exists', all(t in probe for t in ['read_only=True','research_delivery_training_panel','count(DISTINCT date)','production_influence": 0']))
ck('status uses direct persisted catalogue evidence', 'ResearchCatalogueEvidenceService.probe' in status and '"catalogue_evidence": catalogue_evidence' in status and 'persisted_catalogue_ready = bool(catalogue_evidence.get("ready"))' in status)
ck('PIT activates trainer only for missing capital WFA', all(t in pit for t in ['capital_missing','activate_from_persisted','PERSISTED_RESEARCH_PANEL_REUSED','direct DuckDB panel proof satisfied']))
ck('normal refresh remains after capital WFA exists', 'and capital_missing' in pit and 'for phase, command in commands' in pit)
ck('PL24 immutable capital publication preserved', 'for profile, evidence in (("research", validation), ("capital", capital_validation))' in repo and 'capital-wfa-postgres-1.0.0-pl24' in trainer)
ck('forward selector remains prospective only', 'record_selector_population' not in trainer and 'record_selector_outcome' not in trainer and 'PROSPECTIVE_FORWARD_SELECTOR_ONLY_NOT_HISTORICAL_WFA' in status)
ck('status runner reads actual result', '$fwd = $result.forward_selector_evidence_depth' in runner and '$fwd = $r.forward_selector_evidence_depth' not in runner)

# Functional probe logic with a deterministic DuckDB-compatible read-only stub.
try:
 import types
 class _Result:
  def __init__(self, rows): self.rows=rows
  def fetchall(self): return self.rows
  def fetchone(self): return self.rows[0] if self.rows else None
 class _DB:
  def __init__(self, dates): self.dates=dates
  def execute(self, sql):
   q=' '.join(str(sql).split()).lower()
   if 'information_schema.tables' in q: return _Result([('research_delivery_training_panel',),('research_catalog_meta',)])
   if 'select 1 from research_delivery_training_panel' in q: return _Result([(1,)])
   if 'count(*) as rows' in q: return _Result([(600,self.dates,'2022-01-01','2023-08-23')])
   if 'select key,value from research_catalog_meta' in q: return _Result([('catalogue_fingerprint','abc'),('catalog_version','v'),('research_training_panel_version','p')])
   raise RuntimeError(q)
  def close(self): pass
 sys.modules['duckdb']=types.SimpleNamespace(connect=lambda path,read_only=False:_DB(600))
 with tempfile.TemporaryDirectory() as td:
  root=Path(td); (root/'analytics').mkdir(); (root/'manifests').mkdir(); (root/'analytics'/'project_laddu_quant.duckdb').write_bytes(b'x')
  sys.path.insert(0,str(ROOT/'backend'))
  from core.research_catalogue_evidence_service import ResearchCatalogueEvidenceService
  good=ResearchCatalogueEvidenceService.probe(data_dir=root,min_dates=504)
  ck('functional persisted panel sufficient-depth proof',good.get('ready') is True and good.get('rows')==600 and good.get('dates')==600,good)
  sys.modules['duckdb']=types.SimpleNamespace(connect=lambda path,read_only=False:_DB(100))
  shallow=ResearchCatalogueEvidenceService.probe(data_dir=root,min_dates=504)
  ck('functional persisted panel shallow-depth rejection',shallow.get('ready') is False and shallow.get('state')=='PERSISTED_RESEARCH_PANEL_INSUFFICIENT_DEPTH',shallow)
except Exception as exc:
 ck('functional persisted panel proof',False,f'{type(exc).__name__}: {exc}')

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
print(json.dumps({'contract':'PL25_PERSISTED_CATALOGUE_WFA_ACTIVATION','ok':not failed,'passed':len(checks)-len(failed),'failed':len(failed),'checks':checks},indent=2))
raise SystemExit(0 if not failed else 1)
