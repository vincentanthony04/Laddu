from __future__ import annotations
import hashlib,json,sys,types
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]; BACKEND=ROOT/'backend'
if str(BACKEND) not in sys.path: sys.path.insert(0,str(BACKEND))
sys.dont_write_bytecode=True
from core.historical_session_index_authority import HistoricalSessionIndexAuthority
import tools.train_nse_smart_model as trainer
checks=[]
def ck(name,cond,detail=''): checks.append({'name':name,'ok':bool(cond),'detail':detail})
def sha(rel): return hashlib.sha256((ROOT/rel).read_bytes()).hexdigest()

# Authority itself never infers unobserved weekdays/holidays.
a=HistoricalSessionIndexAuthority.from_session_dates(['2026-08-18','2026-08-20'],source='TEST_POSITIVE_ONLY')
ck('positive canonical session observed',a.observed_session('2026-08-18'))
ck('missing calendar date not inferred',not a.observed_session('2026-08-19'))
ck('missing-date evidence remains UNKNOWN',a.evidence('2026-08-19')['state']=='UNKNOWN')

class Result:
    def __init__(self,rows): self.rows=rows
    def fetchall(self): return self.rows
    def fetchone(self): return self.rows[0] if self.rows else None
class DB:
    def execute(self,sql,*args,**kwargs):
        q=' '.join(str(sql).split()).lower()
        if 'select distinct source_key from curated_nse_official_reports' in q:
            return Result([(k,) for k in sorted(trainer.REQUIRED_NSE_SOURCE_FAMILIES)])
        if 'string_agg(distinct content_hash' in q: return Result([('official-lineage',)])
        if "source_key as varchar)='cm_udiff_bhavcopy'" in q: return Result([('2026-08-20','bhavhash')])
        if 'select cast(key as varchar), cast(value as varchar) from research_catalog_meta' in q:
            return Result([('catalogue_fingerprint','catalog-fp')])
        if "from curated_candles" in q and "like 'nse_eq|%'" in q:
            return Result([('2026-08-18',),('2026-08-19',)])
        raise AssertionError('unexpected SQL '+str(sql))
    def close(self): pass
class Duck:
    @staticmethod
    def connect(*a,**k): return DB()
sys.modules['duckdb']=Duck
orig=trainer.lake_views
trainer.lake_views=lambda layout:{'curated_adjusted_candles','point_in_time_security_master','curated_nse_daily_features','curated_nse_official_reports','curated_candles','research_catalog_meta'}
try:
    out=trainer.data_quality_authority(types.SimpleNamespace(analytics_db=Path('/fake/catalog.duckdb')))
finally:
    trainer.lake_views=orig
ck('composite session authority created',out['historical_session_authority']=='HistoricalSessionIndexAuthority',json.dumps(out,default=str))
ck('canonical daily history extends bhavcopy sessions',out['historical_session_canonical_daily_count']==2,json.dumps(out,default=str))
ck('official bhavcopy remains represented',out['historical_session_official_count']==1,json.dumps(out,default=str))
ck('union has three distinct positive sessions',set(out['historical_session_dates'])=={'2026-08-18','2026-08-19','2026-08-20'},json.dumps(out,default=str))
ck('session policy explicitly forbids calendar inference',out['historical_session_source_policy']=='POSITIVE_OBSERVATIONS_ONLY_NO_CALENDAR_INFERENCE')
ck('data quality can authorize when every other authority is present',out['eligible'] is True,json.dumps(out,default=str))

src=(ROOT/'backend/tools/train_nse_smart_model.py').read_text(encoding='utf-8-sig')
ck('only NSE equity daily bars count as canonical session proof',"LIKE 'NSE_EQ|%'" in src and "('1d','day','1day')" in src)
ck('no weekday calendar fabrication added','weekday()' not in src and 'isoweekday()' not in src)
ck('composite provenance is explicit','COMPOSITE_NSE_OFFICIAL_BHAVCOPY_AND_CANONICAL_DAILY_OBSERVATIONS' in src)
ck('PL38 eligible forward cohort semantics retained','classify_forward_evidence' in (ROOT/'backend/core/selection_research_validation_service.py').read_text())
# Critical math/safety not part of this micro-fix.
frozen={
 'backend/core/decision_engine_service.py':'b963e5df7866699b42358d18a40e6f9b8aebcfd77be8b5eeda3a58e9d531fbaf',
 'backend/core/india_cost_model.py':'5d55e47a4286387f38785d672d9cd778114a29674d5a78020fd781ecfe11646a',
 'backend/core/walk_forward_validation_service.py':'fe22ca6c07c555a86b371e2f2c59199de28249354fad0357ddad123a86335fdb',
}
for rel,expected in frozen.items(): ck('frozen '+rel,sha(rel)==expected,sha(rel))
failed=[c for c in checks if not c['ok']]
print(json.dumps({'contract':'PL39_HISTORICAL_SESSION_LINEAGE_CLOSURE','ok':not failed,'passed':len(checks)-len(failed),'failed':len(failed),'checks':checks},indent=2))
raise SystemExit(1 if failed else 0)
