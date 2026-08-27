from __future__ import annotations
import hashlib, importlib.util, json, os, sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
BACKEND=ROOT/'backend'
if str(BACKEND) not in sys.path: sys.path.insert(0,str(BACKEND))
sys.dont_write_bytecode=True
checks=[]
def ck(name, cond, detail=''): checks.append({'name':name,'ok':bool(cond),'detail':detail})
def sha(rel): return hashlib.sha256((ROOT/rel).read_bytes()).hexdigest()

# PL37 must not change the established trading/risk/installer protections.
frozen={
 'backend/core/decision_engine_service.py':'b963e5df7866699b42358d18a40e6f9b8aebcfd77be8b5eeda3a58e9d531fbaf',
 'backend/core/india_cost_model.py':'5d55e47a4286387f38785d672d9cd778114a29674d5a78020fd781ecfe11646a',
 'backend/core/walk_forward_validation_service.py':'fe22ca6c07c555a86b371e2f2c59199de28249354fad0357ddad123a86335fdb',
 'backend/core/nse_cross_sectional_selector_service.py':'6a8a8730bdede43bca4cd6a79ee895b009efaf7b8ae3e8f7a81cf41537f750fc',
 'backend/core/quant_edge_data_service.py':'853016af004b6d47044e7349d954e80cbe3ae5373782f3e44a766608c49fad06',
 'backend/core/data_plane/model_governance_repository.py':'a20716ab419911d4d213f1e3e134cb25e652f8db7210e42a7f1c2751d82efd94',
 'installer/local_state_manifest.py':'c30550773e69f0c11eb0574350df5548cd1f06e987f53e7a19ec40fa5e8f3263',
 'validation/capture_authority_retention_evidence.py':'c9e13317377c7cf3d9b4679a31055ba1bb836290eb2bbc9fc6b5d64ee6e37223',
}
for rel,expected in frozen.items(): ck('frozen '+rel,sha(rel)==expected,sha(rel))

spec=importlib.util.spec_from_file_location('pl37_trainer',ROOT/'backend/tools/train_nse_smart_model.py')
mod=importlib.util.module_from_spec(spec); sys.modules[spec.name]=mod; spec.loader.exec_module(mod)
deep=mod.resolve_historical_training_policy(3719,horizon_days=10)
short=mod.resolve_historical_training_policy(400,horizon_days=10)
ck('default target resolves to 500 with retained deep history',deep.get('resolved_train_days')==500 and deep.get('state')=='TARGET_SATISFIED',json.dumps(deep))
ck('500 is target not brittle safety floor',short.get('ready') is True and 252 <= int(short.get('resolved_train_days') or 0) < 500,json.dumps(short))
ck('optional max is unbounded by default',deep.get('maximum_days')==0 and deep.get('maximum_policy')=='UNBOUNDED',json.dumps(deep))
dates=[f'd{i:04d}' for i in range(650)]
f1=mod.rolling_train_date_slice(dates,500,500); f2=mod.rolling_train_date_slice(dates,563,500)
ck('first WFA rolling fold contains 500 dates',len(f1)==500 and f1[0]=='d0000' and f1[-1]=='d0499')
ck('later WFA fold remains 500 not expanding',len(f2)==500 and f2[0]=='d0063' and f2[-1]=='d0562')

trainer=(ROOT/'backend/tools/train_nse_smart_model.py').read_text(encoding='utf-8-sig')
config=(ROOT/'backend/config.py').read_text(encoding='utf-8-sig')
pit=(ROOT/'backend/core/historical_pit_sweep_service.py').read_text(encoding='utf-8-sig')
ops=(ROOT/'backend/core/operations_control_service.py').read_text(encoding='utf-8-sig')
eps=(ROOT/'backend/core/evidence_pipeline_status_service.py').read_text(encoding='utf-8-sig')
catalogue=(ROOT/'backend/core/research_catalogue_evidence_service.py').read_text(encoding='utf-8-sig')
priority=(ROOT/'backend/core/priority_pipeline_service.py').read_text(encoding='utf-8-sig')
lock=(ROOT/'installer/local_state_manifest.py').read_text(encoding='utf-8-sig')
ret=(ROOT/'validation/capture_authority_retention_evidence.py').read_text(encoding='utf-8-sig')
ck('target/min/max are operator configuration','PROJECT_LADDU_ML_TRAIN_TARGET_DAYS' in config and 'PROJECT_LADDU_ML_TRAIN_MIN_DAYS' in config and 'PROJECT_LADDU_ML_TRAIN_MAX_DAYS' in config)
ck('default target is 500','"PROJECT_LADDU_ML_TRAIN_TARGET_DAYS", 500' in config)
ck('model-spec identity includes depth policy','"historical_train_target_days": int(ML_HISTORICAL_TRAIN_TARGET_DAYS)' in trainer and '"historical_train_minimum_days": int(ML_HISTORICAL_TRAIN_MIN_DAYS)' in trainer and '"historical_train_maximum_days": int(ML_HISTORICAL_TRAIN_MAX_DAYS)' in trainer)
ck('OOF path uses rolling window helper','rolling_train_date_slice(dates, train_end, train_window_days)' in trainer and 'train_window_days=None if first_mode else adaptive_train_dates' in trainer)
ck('final return model uses same governed window','final_training, final_training_dates = recent_date_window(labelled, adaptive_train_dates)' in trainer and 'final_model.fit(final_training[FEATURES], final_training["forward_return"])' in trainer)
ck('companion models share final date window','final_training_date_set = set(final_training_dates)' in trainer and 'isin(final_training_date_set)' in trainer)
ck('PIT supervisor consumes configured target','TRAIN_TARGET_DAYS = ML_HISTORICAL_TRAIN_TARGET_DAYS' in pit and '--min-dates", str(self.TRAIN_TARGET_DAYS)' in pit)
ck('catalogue/evidence status consume configured target','min_dates: int = ML_HISTORICAL_TRAIN_TARGET_DAYS' in catalogue and 'min_dates=ML_HISTORICAL_TRAIN_TARGET_DAYS' in eps)
ck('operations projects configurable policy','CONFIGURABLE_ROLLING_TARGET_WITH_SAFETY_FLOOR' in ops and 'historical_training_minimum_days' in ops)
ck('forward maturity still separate','FORWARD_MATURITY_PENDING' in ops and 'FORWARD_SELECTOR_EVIDENCE_PENDING' in ops and 'INSUFFICIENT_SELECTOR_EVIDENCE' not in ops)
ck('PL36 stale priority recovery retained','automatic_stale_lease_exhaustion' in priority and '_auto_reconciled_exhausted' in priority)
ck('PL35 ephemeral lock exclusion retained','EPHEMERAL_RUNTIME_LOCK_DIR = "data/runtime/locks"' in lock and 'folded.endswith(".lock")' in lock)
ck('PL36 derived feature retention classification retained','rebuildable_derived_parts' in ret and 'lake/features/' in ret and 'lake/predictions/' in ret)
index=(ROOT/'frontend/index.html').read_text(encoding='utf-8-sig'); front=json.loads((ROOT/'frontend/release-identity.json').read_text())
marker='production-usability-r8-pl37-configurable-rolling-ml-wfa-8086'
ck('PL37 exact build identity',marker in config and marker in index and front.get('build_marker')==marker)
ck('PL37 visible version','v131 · R8 · PL37 · 8086' in index)
ck('broker execution remains disabled','BROKER_ORDER_EXECUTION_ENABLED = False' in config)
failed=[x for x in checks if not x['ok']]
print(json.dumps({'contract':'PL37_CONFIGURABLE_ROLLING_ML_WFA','ok':not failed,'passed':len(checks)-len(failed),'failed':len(failed),'checks':checks},indent=2))
raise SystemExit(1 if failed else 0)
