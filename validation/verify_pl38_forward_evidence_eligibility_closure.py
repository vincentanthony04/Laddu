from __future__ import annotations
import hashlib, json, sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
BACKEND=ROOT/'backend'
if str(BACKEND) not in sys.path: sys.path.insert(0,str(BACKEND))
sys.dont_write_bytecode=True
from core.forward_evidence_eligibility import classify_forward_evidence
from core.level5_forward_maturity_service import Level5ForwardMaturityService
checks=[]
def ck(name,cond,detail=''): checks.append({'name':name,'ok':bool(cond),'detail':detail})
def sha(rel): return hashlib.sha256((ROOT/rel).read_bytes()).hexdigest()

def rows(candidate='c1', pop='p1', observed='2026-08-20T09:30:00+05:30', prediction='2026-08-20T09:31:00+05:30', settled='2026-08-21T15:30:00+05:30'):
    return [
        {'arm':arm,'candidate_id':candidate,'population_fingerprint':pop,'outcome_population_fingerprint':pop,
         'observed_at':observed,'prediction_at':prediction,'settled_at':settled,'market_regime':'BULL'}
        for arm in ('heuristic','quant','hybrid')
    ]

valid=classify_forward_evidence(rows())
ck('valid three-arm prospective candidate admitted',valid['eligible_candidate_count']==1 and valid['eligible_row_count']==3,json.dumps(valid,default=str))
ck('valid candidate has zero exclusions',valid['excluded_candidate_count']==0,json.dumps(valid,default=str))

bad_time=classify_forward_evidence(rows(candidate='old',settled='2026-08-20T09:29:00+05:30'))
ck('non-future legacy outcome excluded',bad_time['eligible_candidate_count']==0 and bad_time['exclusion_reason_counts'].get('OUTCOME_NOT_STRICTLY_FUTURE')==1,json.dumps(bad_time,default=str))

bad_prediction_early=classify_forward_evidence(rows(candidate='early',prediction='2026-08-20T09:29:00+05:30'))
ck('prediction before candidate observation excluded',bad_prediction_early['exclusion_reason_counts'].get('PREDICTION_PRECEDES_CANDIDATE_OBSERVATION')==1,json.dumps(bad_prediction_early,default=str))

bad_prediction_late=classify_forward_evidence(rows(candidate='late',prediction='2026-08-21T15:30:00+05:30'))
ck('prediction not before settlement excluded',bad_prediction_late['exclusion_reason_counts'].get('PREDICTION_NOT_BEFORE_SETTLEMENT')==1,json.dumps(bad_prediction_late,default=str))

incomplete=classify_forward_evidence(rows(candidate='inc')[:2])
ck('incomplete three-arm set excluded',incomplete['exclusion_reason_counts'].get('INCOMPLETE_OR_DUPLICATE_THREE_ARM_SET')==1,json.dumps(incomplete,default=str))

mismatch=rows(candidate='mis')
mismatch[0]['outcome_population_fingerprint']='other'
mis=classify_forward_evidence(mismatch)
ck('population mismatch excluded',mis['exclusion_reason_counts'].get('POPULATION_FINGERPRINT_MISMATCH')==1,json.dumps(mis,default=str))

mixed=classify_forward_evidence(rows(candidate='good',pop='goodpop')+rows(candidate='legacy',pop='oldpop',settled='2026-08-20T09:29:00+05:30'))
ck('invalid legacy cohort cannot poison valid future cohort',mixed['eligible_candidate_count']==1 and mixed['excluded_candidate_count']==1,json.dumps(mixed,default=str))
ck('eligible rows remain exact three arms',len(mixed['rows'])==3 and {r['arm'] for r in mixed['rows']}=={'heuristic','quant','hybrid'})

svc=object.__new__(Level5ForwardMaturityService)
integ=svc._integrity('delivery','20d',report={'forward_eligibility':{k:v for k,v in mixed.items() if k!='rows'}})
ck('Level5 integrity passes with valid eligible cohort despite retained invalid history',integ['passed'] is True and integ['eligible_candidates']==1 and integ['excluded_candidates']==1,json.dumps(integ))
integ2=svc._integrity('delivery','20d',report={'forward_eligibility':{k:v for k,v in bad_time.items() if k!='rows'}})
ck('Level5 integrity fails closed when no eligible cohort exists',integ2['passed'] is False,json.dumps(integ2))

sel=(ROOT/'backend/core/selection_research_validation_service.py').read_text(encoding='utf-8-sig')
replay=(ROOT/'backend/core/selection_walk_forward_replay_service.py').read_text(encoding='utf-8-sig')
repo=(ROOT/'backend/core/data_plane/model_governance_repository.py').read_text(encoding='utf-8-sig')
l5=(ROOT/'backend/core/level5_forward_maturity_service.py').read_text(encoding='utf-8-sig')
ck('selector report applies governed eligibility filter','eligibility = classify_forward_evidence(raw_joined)' in sel)
ck('selector report excludes UNKNOWN regime from regime count','"UNKNOWN", "NONE", "UNAVAILABLE"' in sel)
ck('selector local join binds outcome to prediction population','AND o.population_fingerprint=p.population_fingerprint' in sel)
ck('WFA replay consumes same eligibility policy','self._eligibility_summary' in replay and 'classify_forward_evidence(rows)' in replay)
ck('governance joined rows expose immutable prediction time','p.prediction_at' in repo and 'outcome_population_fingerprint' in repo)
ck('governance joined rows bind all joins by population','AND m.population_fingerprint=p.population_fingerprint' in repo and 'AND o.population_fingerprint=p.population_fingerprint' in repo)
ck('Level5 service version advances eligibility semantics','level5-forward-maturity-1.3.0-eligible-forward-cohorts' in l5)
ck('Level5 direct all-history poison query removed','settled_at<=o.observed_at' not in l5 and 'FROM selector_candidate_outcomes o' not in l5)

frozen={
 'backend/core/decision_engine_service.py':'b963e5df7866699b42358d18a40e6f9b8aebcfd77be8b5eeda3a58e9d531fbaf',
 'backend/core/india_cost_model.py':'5d55e47a4286387f38785d672d9cd778114a29674d5a78020fd781ecfe11646a',
 'backend/tools/train_nse_smart_model.py':'1d001020fa2058a6e94a8b8c26e3ba67de71821c38d836d2b79af565034f4a46',
 'backend/core/walk_forward_validation_service.py':'fe22ca6c07c555a86b371e2f2c59199de28249354fad0357ddad123a86335fdb',
 'installer/local_state_manifest.py':'c30550773e69f0c11eb0574350df5548cd1f06e987f53e7a19ec40fa5e8f3263',
 'validation/capture_authority_retention_evidence.py':'c9e13317377c7cf3d9b4679a31055ba1bb836290eb2bbc9fc6b5d64ee6e37223',
}
for rel,expected in frozen.items(): ck('frozen '+rel,sha(rel)==expected,sha(rel))
failed=[c for c in checks if not c['ok']]
print(json.dumps({'contract':'PL38_FORWARD_EVIDENCE_ELIGIBILITY_CLOSURE','ok':not failed,'passed':len(checks)-len(failed),'failed':len(failed),'checks':checks},indent=2))
raise SystemExit(1 if failed else 0)
