from __future__ import annotations
import hashlib,json,sys,types
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];BACKEND=ROOT/'backend'
if str(BACKEND) not in sys.path:sys.path.insert(0,str(BACKEND))
sys.dont_write_bytecode=True
import tools.train_nse_smart_model as trainer
checks=[]
def ck(name,cond,detail=''):checks.append({'name':name,'ok':bool(cond),'detail':detail})
def sha(rel):return hashlib.sha256((ROOT/rel).read_bytes()).hexdigest()
ck('core WFA sources are narrowly defined',trainer.CORE_NSE_WFA_SOURCE_FAMILIES=={'cm_udiff_bhavcopy','security_delivery_positions'},repr(trainer.CORE_NSE_WFA_SOURCE_FAMILIES))
ck('optional enrichment set remains explicit',len(trainer.OPTIONAL_NSE_ENRICHMENT_SOURCE_FAMILIES)==7,repr(trainer.OPTIONAL_NSE_ENRICHMENT_SOURCE_FAMILIES))
ck('all nine source families remain modeled',len(trainer.ALL_NSE_SOURCE_FAMILIES)==9)
ck('availability features remain in model',len(trainer.NSE_OFFICIAL_AVAILABILITY_FEATURES)==9)

class Result:
 def __init__(self,rows):self.rows=rows
 def fetchall(self):return self.rows
 def fetchone(self):return self.rows[0] if self.rows else None
class DB:
 def execute(self,sql,*args,**kwargs):
  q=' '.join(str(sql).split()).lower()
  if 'select distinct source_key from curated_nse_official_reports' in q:return Result([(x,) for x in sorted(trainer.CORE_NSE_WFA_SOURCE_FAMILIES)])
  if 'string_agg(distinct content_hash' in q:return Result([('core-lineage',)])
  if "source_key as varchar)='cm_udiff_bhavcopy'" in q:return Result([('2026-08-20','bhav')])
  if 'select cast(key as varchar), cast(value as varchar) from research_catalog_meta' in q:return Result([('catalogue_fingerprint','cat')])
  if 'from curated_candles' in q and "like 'nse_eq|%'" in q:return Result([('2026-08-18',),('2026-08-19',),('2026-08-20',)])
  raise AssertionError(q)
 def close(self):pass
class Duck:
 @staticmethod
 def connect(*a,**k):return DB()
sys.modules['duckdb']=Duck
orig=trainer.lake_views
trainer.lake_views=lambda layout:{'curated_adjusted_candles','point_in_time_security_master','curated_nse_daily_features','curated_nse_official_reports','curated_candles','research_catalog_meta'}
try:out=trainer.data_quality_authority(types.SimpleNamespace(analytics_db=Path('/fake.duckdb')))
finally:trainer.lake_views=orig
ck('optional NSE enrichment absence does not block WFA authority',out['eligible'] is True,json.dumps(out,default=str))
ck('core source completeness is exact',out['official_nse_core_source_count']==2 and out['official_nse_core_required_count']==2,json.dumps(out,default=str))
ck('optional missing sources remain visible',len(out['official_nse_optional_missing_sources'])==7,json.dumps(out,default=str))
ck('optional enrichment coverage is measured',out['official_nse_optional_enrichment_coverage']==0.0,json.dumps(out,default=str))

wfa=(ROOT/'backend/core/walk_forward_validation_service.py').read_text(encoding='utf-8-sig')
tr=(ROOT/'backend/tools/train_nse_smart_model.py').read_text(encoding='utf-8-sig')
ck('capital gate requires core source completeness','official_nse_core_source_family_coverage_complete' in wfa)
ck('obsolete all-nine capital gate removed','official_nse_source_family_coverage_complete' not in wfa)
ck('optional coverage is diagnostic not capital gate','optional_nse_enrichment_coverage' in wfa and '"optional_nse_enrichment_coverage": optional_nse_enrichment_coverage' in wfa)
ck('all-nine coverage remains a model feature','clip(0, 9) / 9.0' in tr)
ck('PL40 survivorship policy retained','CANONICAL_DAILY_CANDLE_OBSERVED_MEMBERSHIP' in tr)
ck('PL39 session policy retained','POSITIVE_OBSERVATIONS_ONLY_NO_CALENDAR_INFERENCE' in tr)
ck('PL38 eligibility retained','classify_forward_evidence' in (ROOT/'backend/core/selection_research_validation_service.py').read_text())
# Core trading/cost are untouched.
frozen={
 'backend/core/decision_engine_service.py':'b963e5df7866699b42358d18a40e6f9b8aebcfd77be8b5eeda3a58e9d531fbaf',
 'backend/core/india_cost_model.py':'5d55e47a4286387f38785d672d9cd778114a29674d5a78020fd781ecfe11646a',
}
for rel,expected in frozen.items():ck('frozen '+rel,sha(rel)==expected,sha(rel))
failed=[x for x in checks if not x['ok']]
print(json.dumps({'contract':'PL41_OFFICIAL_SOURCE_QUALIFICATION_POLICY','ok':not failed,'passed':len(checks)-len(failed),'failed':len(failed),'checks':checks},indent=2))
raise SystemExit(1 if failed else 0)
