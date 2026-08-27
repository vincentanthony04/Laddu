from __future__ import annotations
import hashlib,json,subprocess,sys,os
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
EXPECTED_PARENT_SHA="781adfa2fae61cf6e7eb106a3c85948c4f1d4e6dd15112c2220e6fdbc440d8c1"
FROZEN=json.loads((ROOT/'validation/r46_frozen_r45_hashes.json').read_text(encoding='utf-8'))
FUNCTIONAL_ALLOWED={
 'backend/tools/refresh_research_catalog.py','backend/tools/train_nse_smart_model.py',
 'backend/core/historical_pit_sweep_service.py',
}
METADATA_ALLOWED={
 'RELEASE_IDENTITY.json','RELEASE_ATTESTATION.json','frontend/release-identity.json',
 'validation/package_allowlist.json','validation/package_manifest.sha256',
 'validation/validate_deployable_candidate.py','validation/r46_frozen_r45_hashes.json',
 'validation/verify_r46_research_data_engine_closure.py','docs/R46_RESEARCH_DATA_ENGINE_CLOSURE.md',
}
ALLOWED=FUNCTIONAL_ALLOWED|METADATA_ALLOWED

def sha(p:Path)->str:return hashlib.sha256(p.read_bytes()).hexdigest()

def main()->int:
 failures=[];checks=[]
 def check(name,ok,detail):
  checks.append({'gate':name,'state':'PASS' if ok else 'FAIL','detail':detail})
  if not ok:failures.append(f'{name}:{detail}')
 check('EXACT_R45_PARENT_ARCHIVE',FROZEN.get('parent_archive_sha256')==EXPECTED_PARENT_SHA,'R46 bound to exact R45 archive')
 missing=[];unexpected=[];changed=[]
 for rel,digest in dict(FROZEN.get('hashes') or {}).items():
  p=ROOT/rel
  if not p.is_file():missing.append(rel);continue
  if sha(p)!=digest:(changed if rel in ALLOWED else unexpected).append(rel)
 check('R45_NON_REGRESSION',not missing and not unexpected,f'missing={len(missing)} unexpected_changed={unexpected}')
 check('DECLARED_FUNCTIONAL_BOUNDARY',set(changed).issubset(ALLOWED),f'changed={sorted(changed)}')
 protected=['INSTALL_UPDATE.cmd','installer/install.ps1','installer/register_research_tasks.ps1','backend/core/intraday_session_policy.py','backend/core/selection_walk_forward_replay_service.py','frontend/app.js','frontend/index.html','frontend/ui-system.css']
 bad=[rel for rel in protected if rel in FROZEN['hashes'] and (ROOT/rel).is_file() and sha(ROOT/rel)!=FROZEN['hashes'][rel]]
 check('INSTALLER_UI_SESSION_SELECTOR_FROZEN',not bad,f'changed={bad}')
 refresh=(ROOT/'backend/tools/refresh_research_catalog.py').read_text(encoding='utf-8')
 trainer=(ROOT/'backend/tools/train_nse_smart_model.py').read_text(encoding='utf-8')
 hist=(ROOT/'backend/core/historical_pit_sweep_service.py').read_text(encoding='utf-8')
 check('MATERIALIZED_PANEL_AUTHORITY','CREATE TABLE research_delivery_training_panel AS' in refresh and 'research-delivery-training-panel-1.1.0' in refresh,'catalog refresh materializes reusable research panel')
 check('FULL_TEMPORAL_DEPTH_FULL_CROSS_SECTION','FULL_TEMPORAL_DEPTH_FULL_CROSS_SECTION_RESEARCH_COMPUTE_ONLY' in refresh and 'RESEARCH_PANEL_LIQUID_NAMES_PER_DATE' not in refresh and 'RESEARCH_PANEL_EXPLORATION_NAMES_PER_DATE' not in refresh,'every eligible historical date/symbol row is retained; no new research-universe cap')
 check('PIT_FALLBACK_EXPLICIT',"POINT_IN_TIME_SECURITY_MASTER" in refresh and "CURRENT_INSTRUMENTS_SHADOW_FALLBACK" in refresh,'row-level identity authority remains explicit')
 check('DUCKDB_JOIN_PUSHDOWN','LEFT JOIN curated_delivery d' in refresh and 'LEFT JOIN curated_nse_daily_features n' in refresh,'Delivery/NSE joins moved to materialized DuckDB projection')
 panel_fn=trainer[trainer.index('def load_panel_from_lake'):trainer.index('\n\ndef build_features',trainer.index('def load_panel_from_lake'))]
 check('TRAINER_READS_MATERIALIZED_PANEL','research_delivery_training_panel' in panel_fn and 'curated_delivery' not in panel_fn and 'curated_nse_daily_features' not in panel_fn,'trainer no longer performs separate giant delivery/official Pandas merges')
 check('NO_SILENT_PANEL_EXCEPTION','except Exception:\n        return None' not in panel_fn and 'ResearchPanelStageError' in trainer,'panel errors preserve stage instead of becoming empty')
 check('STRICT_ISO_DATE_NORMALIZATION','format="%Y-%m-%d"' in panel_fn and 'dayfirst=True' not in panel_fn,'known ISO/dayfirst warning path removed from panel loader')
 check('MATERIALIZED_PANEL_REQUIRED','research_delivery_training_panel' in trainer[trainer.index('def lake_training_available'):trainer.index('def data_quality_authority')],'trainer cannot silently fall back to old path')
 check('ONE_DECLARED_HISTORICAL_POLICY',trainer.count('trial_count=1,')>=2,'Delivery trainer uses one declared validation policy')
 check('WFA_DEPTH_NOT_WEAKENED','MIN_DATES = 504' in hist and '"--min-dates", str(self.MIN_DATES)' in hist,'504-date historical worker requirement retained')
 check('ML_PRODUCTION_INFLUENCE_ZERO','"production_weight": 0.0' in trainer and '"production_influence": 0' in refresh,'materialized research projection cannot gain production authority')
 # compile/import gate
 py=list((ROOT/'backend').rglob('*.py'))
 compile_failures=[]
 for source in py:
  try: compile(source.read_text(encoding='utf-8-sig'),str(source),'exec')
  except Exception as exc: compile_failures.append(f'{source.relative_to(ROOT)}:{type(exc).__name__}:{exc}')
 check('PYTHON_COMPILE',not compile_failures,f'files={len(py)} failures={compile_failures[:3]}')
 code="import os,sys;sys.path.insert(0,os.path.join(os.getcwd(),'backend'));import main;print(main.APP_VERSION)"
 env={**os.environ,'PROJECT_LADDU_DATA_PLANE_MODE':'test','PROJECT_LADDU_HOME':'/tmp/project-laddu-r46-import-home','PYTHONDONTWRITEBYTECODE':'1'}
 cp=subprocess.run([sys.executable,'-c',code],cwd=ROOT,text=True,capture_output=True,env=env)
 check('INSTALLER_EQUIVALENT_BACKEND_IMPORT',cp.returncode==0 and 'v131.0.0' in cp.stdout,f'rc={cp.returncode} stderr={cp.stderr[-240:]}')
 report={'ok':not failures,'scope':'R46_RESEARCH_DATA_ENGINE_CLOSURE','checks':checks,'failures':failures}
 print(json.dumps(report,indent=2))
 return 0 if not failures else 2
if __name__=='__main__':raise SystemExit(main())
