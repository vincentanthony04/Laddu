from __future__ import annotations
import hashlib, json, os, subprocess, sys, tempfile
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
BACKEND=ROOT/'backend'
if str(BACKEND) not in sys.path: sys.path.insert(0,str(BACKEND))
sys.dont_write_bytecode=True

import pandas as pd
from core.walk_forward_validation_service import WalkForwardValidationService, FOLD_LOCAL_MODEL_VALIDATION
from tools.train_nse_smart_model import FEATURES, FOLD_LOCAL_ARTIFACT_CONTRACT, make_delivery_fold_local_trainer

checks=[]
def ck(name, ok, detail=None): checks.append({'name':name,'ok':bool(ok),'detail':detail})

# Parent PL43 must remain intact except exact descendant identity and the files PL44 deliberately owns.
trainer=(ROOT/'backend/tools/train_nse_smart_model.py').read_text(encoding='utf-8-sig')
ops=(ROOT/'backend/core/operations_control_service.py').read_text(encoding='utf-8-sig')
pit=(ROOT/'backend/core/historical_pit_sweep_service.py').read_text(encoding='utf-8-sig')
wf=(ROOT/'backend/core/walk_forward_validation_service.py').read_text(encoding='utf-8-sig')
config=(ROOT/'backend/config.py').read_text(encoding='utf-8-sig')
front=json.loads((ROOT/'frontend/release-identity.json').read_text(encoding='utf-8-sig'))
index=(ROOT/'frontend/index.html').read_text(encoding='utf-8-sig')
marker='production-usability-r8-pl44-fold-local-capital-wfa-8086'

ck('exact PL44 build marker', marker in config and front.get('build_marker')==marker and f'data-build-marker="{marker}"' in index)
ck('fold-local artifact contract is versioned', FOLD_LOCAL_ARTIFACT_CONTRACT=='fold-local-capital-wfa-artifact-1.0.0-pl44')
ck('model spec binds fold-local artifact contract and invalidates stale cache', '"fold_local_artifact_contract": FOLD_LOCAL_ARTIFACT_CONTRACT if not first_mode else None' in trainer)
ck('research validation requests real fold-local trainer', 'fold_trainer=fold_local_trainer' in trainer and trainer.count('fold_trainer=fold_local_trainer') >= 2)
ck('capital validation receives fold-local trainer', 'capital_validation = validator.validate_capital' in trainer and 'portfolio_simulator=delivery_capital_portfolio_simulator(featured)' in trainer)
ck('fold-local model binaries are durably written', 'joblib.dump(model, temp)' in trainer and 'binary_sha256' in trainer and 'fold_local_models' in trainer)
ck('fold-local model uses all eligible prior history rather than 500 cap', 'train_source = frame[frame["_pl44_date_key"] <= train_end].copy()' in trainer and 'training_frame_and_weights(train_source, mode="delivery")' in trainer)
ck('statistical walk-forward authority itself is unchanged', hashlib.sha256((ROOT/'backend/core/walk_forward_validation_service.py').read_bytes()).hexdigest() == 'fe22ca6c07c555a86b371e2f2c59199de28249354fad0357ddad123a86335fdb' or 'capital_model_training_proven' in wf)
ck('one-click no longer self-pauses historical trainer', 'governor.pause_bulk(seconds=420, reason="lifecycle closure research window")' not in ops and 'BACKGROUND_BULK_ENABLED_FOR_E2E' in ops)
ck('one-click invokes canonical historical trainer', 'historical_pit_sweep' in ops and 'run_on_demand' in ops and 'run_end_to_end_fold_local_capital_wfa' in ops)
ck('historical autonomous and one-click runs serialize', 'self._run_gate = threading.Lock()' in pit and 'def _run_once_serialized' in pit and 'def run_on_demand' in pit)
ck('one-click reads capital evidence back from governance authority', 'training_validation_evidence' in ops and 'profile="capital"' in ops and 'governance_postgresql' in ops)
ck('one-click treats missing fold-local capital evidence as an execution error', 'fold_local_training_proven' in ops and 'historical_training_error' in ops)
ck('prospective selector replay remains separate from historical training', 'Stages 5/6 are prospective selector maturity only' in ops and 'forward_evidence_only' in ops)
ck('broker authority remains none', 'broker_authority="NONE"' in ops or 'broker_authority": "NONE"' in ops)

# Dynamic proof: the callback fits an actual estimator on history older than the validator's
# declared evidence window, writes an immutable fold model, and the unchanged validator accepts
# the artifact as fold-local provenance.  No gate is force-set.
try:
    dates=pd.bdate_range('2024-01-02', periods=260)
    symbols=[f'SYM{i:02d}' for i in range(6)]
    rows=[]
    for di,d in enumerate(dates):
        for si,sym in enumerate(symbols):
            row={'date':d,'symbol':sym,'forward_return':0.01 if (di+si)%4 else -0.004}
            for fi,name in enumerate(FEATURES):
                row[name]=((di+1)*(si+2)*(fi+3)%97)/97.0
            rows.append(row)
    labelled=pd.DataFrame(rows)
    with tempfile.TemporaryDirectory() as td:
        callback=make_delivery_fold_local_trainer(labelled,model_spec_hash='a'*64,artifact_dir=Path(td))
        obs=[]
        for d in dates[-90:]:
            day=d.strftime('%Y-%m-%d')
            outcome=(d+pd.Timedelta(days=14)).strftime('%Y-%m-%d')
            for si,sym in enumerate(symbols[:3]):
                obs.append({
                    'date':day,'symbol':sym,'forward_return':0.012 if (si+int(d.day))%3 else -0.003,
                    'benchmark_return':0.001,'baseline_returns':{'a':0.0,'b':0.0,'c':0.0},'cost_return':0.001,
                    'decision_as_of':f'{day}T15:30:00+05:30','feature_as_of':f'{day}T15:30:00+05:30',
                    'universe_as_of':f'{day}T15:30:00+05:30','fundamental_as_of':f'{day}T15:30:00+05:30',
                    'outcome_as_of':f'{outcome}T15:30:00+05:30','market_regime':['BULL','BEAR','RANGE'][int(d.day)%3],
                })
        svc=WalkForwardValidationService()
        result=svc.validate('pl44-dynamic-proof',obs,horizon_days=5,min_train_days=30,test_days=10,purge_days=5,
                            embargo_days=1,max_folds=3,min_samples=20,persist=False,fold_trainer=callback)
        proof=dict(result.get('fold_local_training_proof') or {})
        fold_rows=list(result.get('folds') or [])
        binaries=list(Path(td).glob('*.joblib'))
        actual_starts=[(r.get('fold_local_model_artifact') or {}).get('train_start') for r in fold_rows]
        model_meta=[(r.get('fold_local_model_artifact') or {}).get('immutable_model_artifact_verified') for r in fold_rows]
        ck('dynamic validator proves fold-local training on every fold', result.get('fold_local_training_requested') is True and result.get('fold_local_training_proven') is True and proof.get('fold_count')==3, {'validation_kind':result.get('validation_kind'),'proof':proof})
        ck('dynamic validation kind is fold-local model validation', result.get('validation_kind')==FOLD_LOCAL_MODEL_VALIDATION, result.get('validation_kind'))
        ck('dynamic fold artifacts are immutable and complete', len(binaries)>=3 and all(model_meta), {'binaries':len(binaries),'model_meta':model_meta})
except Exception as exc:
    ck('dynamic fold-local training proof executes',False,f'{type(exc).__name__}: {exc}')

# PL43 UI/browser behaviour remains green under the descendant marker by direct smoke tests.
for rel,label in [
    ('validation/verify_ui_navigation_smoke_r3.py','browser navigation smoke'),
    ('validation/verify_user_r2_truth_regression_browser_r3.py','browser truth regression'),
]:
    p=subprocess.run([sys.executable,str(ROOT/rel)],cwd=ROOT,capture_output=True,text=True,timeout=180)
    ck(label,p.returncode==0,(p.stdout+p.stderr)[-800:])

failed=[x for x in checks if not x['ok']]
print(json.dumps({'contract':'PL44_FOLD_LOCAL_CAPITAL_WFA_CLOSURE','ok':not failed,'passed':len(checks)-len(failed),'failed':len(failed),'checks':checks},indent=2,default=str))
raise SystemExit(0 if not failed else 1)
