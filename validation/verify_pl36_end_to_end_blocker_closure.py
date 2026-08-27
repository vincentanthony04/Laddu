from __future__ import annotations
import hashlib, importlib.util, json, tempfile, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
checks=[]
def ck(name, cond, detail=''):
    checks.append({'name':name,'ok':bool(cond),'detail':detail})
def sha(rel):
    return hashlib.sha256((ROOT/rel).read_bytes()).hexdigest()

# Protected economics/trading/research-gate authorities must remain unchanged.
frozen={
 'backend/core/decision_engine_service.py':'b963e5df7866699b42358d18a40e6f9b8aebcfd77be8b5eeda3a58e9d531fbaf',
 'backend/core/india_cost_model.py':'5d55e47a4286387f38785d672d9cd778114a29674d5a78020fd781ecfe11646a',
 'backend/core/walk_forward_validation_service.py':'fe22ca6c07c555a86b371e2f2c59199de28249354fad0357ddad123a86335fdb',
 'backend/core/nse_cross_sectional_selector_service.py':'6a8a8730bdede43bca4cd6a79ee895b009efaf7b8ae3e8f7a81cf41537f750fc',
 'backend/core/quant_edge_data_service.py':'853016af004b6d47044e7349d954e80cbe3ae5373782f3e44a766608c49fad06',
 'backend/core/data_plane/model_governance_repository.py':'a20716ab419911d4d213f1e3e134cb25e652f8db7210e42a7f1c2751d82efd94',
 'frontend/app.js':'d466544f0210a42888ddba45b9652412dff6a43f62210ab5c953acb62dc8caa7',
 'frontend/app.css':'cbb3650112346d39955f28a75b318131b224a4b2388fff293c93be51972c2614',
 'frontend/ui-system.css':'eabc356debe583cdf0b683f8fa8ed706b0a9692713f0fcbf878262e2891d0a95',
 'installer/local_state_manifest.py':'c30550773e69f0c11eb0574350df5548cd1f06e987f53e7a19ec40fa5e8f3263',
}
for rel,expected in frozen.items(): ck('frozen '+rel, sha(rel)==expected, sha(rel))

# Exact installer regression: derived feature parquet may advance; raw canonical parquet may not mutate.
target=ROOT/'validation/capture_authority_retention_evidence.py'
spec=importlib.util.spec_from_file_location('pl36_retention',target)
mod=importlib.util.module_from_spec(spec); sys.modules[spec.name]=mod; spec.loader.exec_module(mod)
with tempfile.TemporaryDirectory() as td:
    install=Path(td)
    raw=install/'data/lake/curated/candles/timeframe=day/year=2026/part-a.parquet'
    feature=install/'data/lake/features/delivery/horizon=10/features.parquet'
    raw.parent.mkdir(parents=True); feature.parent.mkdir(parents=True)
    raw.write_bytes(b'canonical-raw')
    feature.write_bytes(b'derived-v1')
    before={'ok':True,'parquet':mod._parquet_plane(install),'content_sha256':'before'}
    feature.write_bytes(b'derived-v2-with-legitimate-growth')
    after={'ok':True,'parquet':mod._parquet_plane(install),'content_sha256':'after'}
    cmp1=mod.compare_evidence(before,after)
    ck('derived feature mutation allowed',cmp1.get('ok') is True,json.dumps(cmp1))
    ck('derived feature inventoried', 'lake/features/delivery/horizon=10/features.parquet' in after['parquet'].get('rebuildable_derived_parts',{}))
    raw.write_bytes(b'canonical-raw-MUTATED')
    after_bad={'ok':True,'parquet':mod._parquet_plane(install),'content_sha256':'afterbad'}
    cmp2=mod.compare_evidence(before,after_bad)
    ck('canonical parquet mutation fails closed',cmp2.get('ok') is False,json.dumps(cmp2))
    ck('canonical mutation classified',any(x.get('kind')=='parquet_immutable_part_changed' for x in cmp2.get('regressions',[])))

trainer=(ROOT/'backend/tools/train_nse_smart_model.py').read_text()
config=(ROOT/'backend/config.py').read_text()
pit=(ROOT/'backend/core/historical_pit_sweep_service.py').read_text()
eps=(ROOT/'backend/core/evidence_pipeline_status_service.py').read_text()
ops=(ROOT/'backend/core/operations_control_service.py').read_text()
proj=(ROOT/'backend/core/research_control_projection_service.py').read_text()
priority=(ROOT/'backend/core/priority_pipeline_service.py').read_text()
conveyor=(ROOT/'backend/core/data_conveyor_runtime_service.py').read_text()

ck('500-day governed trainer target config','PROJECT_LADDU_ML_TRAIN_TARGET_DAYS", 500' in config and 'ML_HISTORICAL_TRAIN_TARGET_DAYS' in trainer)
ck('training policy participates in model-spec cache identity','"historical_train_target_days": int(ML_HISTORICAL_TRAIN_TARGET_DAYS)' in trainer and '"historical_train_minimum_days": int(ML_HISTORICAL_TRAIN_MIN_DAYS)' in trainer)
ck('500 effective train plus purge','oof_start_dates = adaptive_train_dates if first_mode else adaptive_train_dates + int(horizon)' in trainer)
ck('historical PIT depth is config-driven','TRAIN_TARGET_DAYS = ML_HISTORICAL_TRAIN_TARGET_DAYS' in pit)
ck('evidence catalogue depth is config-driven','min_dates=ML_HISTORICAL_TRAIN_TARGET_DAYS' in eps)
ck('forward evidence does not block historical WFA','FORWARD_MATURITY_PENDING' in ops and 'FORWARD_SELECTOR_EVIDENCE_PENDING' in ops)
ck('old selector blocker removed','INSUFFICIENT_SELECTOR_EVIDENCE' not in ops)
ck('historical train target projected from config','primary["historical_training_days"] = int(ML_HISTORICAL_TRAIN_TARGET_DAYS)' in ops)
ck('projection separates selector maturity','selector_forward_maturity_state' in proj and 'PERSISTED_HISTORICAL_WFA' in proj)
ck('stale exhausted priority jobs auto reconcile','automatic_stale_lease_exhaustion' in priority and '_auto_reconciled_exhausted' in priority)
ck('incomplete official authority retries quickly','official_interval = 6 * 3600 if critical_ready else 2 * 60' in conveyor)

# PL35 exact runtime-lock fix must remain intact.
lock_source=(ROOT/'installer/local_state_manifest.py').read_text()
ck('PL35 transient lock exclusion retained','EPHEMERAL_RUNTIME_LOCK_DIR = "data/runtime/locks"' in lock_source and 'folded.endswith(".lock")' in lock_source)

config=(ROOT/'backend/config.py').read_text(); index=(ROOT/'frontend/index.html').read_text(); front=json.loads((ROOT/'frontend/release-identity.json').read_text())
marker=front.get('build_marker')
ck('PL36-or-descendant build marker', marker in {'production-usability-r8-pl36-end-to-end-blocker-closure-8086','production-usability-r8-pl37-configurable-rolling-ml-wfa-8086'} and marker in config and marker in index)
ck('PL36-or-descendant visible identity',('v131 · R8 · PL36 · 8086' in index) or ('v131 · R8 · PL37 · 8086' in index))
ck('UI4 premium refinement retained','data-ui-version="UI4"' in index)
ck('broker authority remains disabled','BROKER_ORDER_EXECUTION_ENABLED = False' in config)

failed=[x for x in checks if not x['ok']]
print(json.dumps({'contract':'PL36_END_TO_END_BLOCKER_CLOSURE','ok':not failed,'passed':len(checks)-len(failed),'failed':len(failed),'checks':checks},indent=2,default=str))
raise SystemExit(1 if failed else 0)
