from __future__ import annotations
import hashlib, json, subprocess, sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.dont_write_bytecode=True

def sha(p): return hashlib.sha256(p.read_bytes()).hexdigest()

def main():
    checks=[]; failures=[]
    def ck(name, ok, detail):
        checks.append({'name':name,'ok':bool(ok),'detail':detail})
        if not ok: failures.append(name+':'+detail)
    ident=json.loads((ROOT/'RELEASE_IDENTITY.json').read_text(encoding='utf-8-sig'))
    freeze=json.loads((ROOT/'validation/pl20_frozen_pl17_trading_hashes.json').read_text())['files']
    changed=[rel for rel,d in freeze.items() if not (ROOT/rel).is_file() or sha(ROOT/rel)!=d]
    ck('PL22_IDENTITY','PL22_EVIDENCE_TRANSPORT_CLOSURE' in str(ident.get('acceptance_state') or ''),'PL22 acceptance state declared')
    ck('TRADING_CORE_FROZEN',not changed,'protected PL17 trading core unchanged: '+','.join(changed))

    strict=(ROOT/'backend/core/strict_json.py').read_text()
    ck('STRICT_JSON_MODULE','allow_nan' in strict and 'math.isfinite' in strict and 'return None' in strict,'non-finite reals normalize to JSON null and encoder forbids NaN')
    trainer=(ROOT/'backend/tools/train_nse_smart_model.py').read_text()
    ck('TRAINER_STRICT_TRANSPORT','safe_bundle = dict(json_safe(bundle) or {})' in trainer and 'strict_json_dumps(safe_bundle' in trainer and 'atomic_write_json(path, safe_bundle)' in trainer,'HTTP and durable outbox use strict sanitized bundle')
    pub=(ROOT/'backend/core/ai_training_publication_service.py').read_text()
    ck('PUBLICATION_BOUNDARY_SANITIZES_LEGACY','bundle = dict(json_safe(bundle) or {})' in pub and 'strict_json_dumps(validation_payload' in pub,'old outbox and compatibility projection normalized before persistence')
    repo=(ROOT/'backend/core/data_plane/model_governance_repository.py').read_text()
    ck('POSTGRES_JSONB_STRICT','return strict_json_dumps' in repo,'governance repository never emits NaN/Infinity to jsonb')

    refresh=(ROOT/'backend/tools/refresh_research_catalog.py').read_text()
    pit=(ROOT/'backend/core/historical_pit_sweep_service.py').read_text()
    ck('CATALOG_PEER_WAIT_CONFIGURABLE','--lock-wait-seconds' in refresh and 'lock_wait_seconds=args.lock_wait_seconds' in refresh,'catalogue refresher supports bounded peer-owner wait')
    ck('PIT_WAITS_BEHIND_PEER','"--lock-wait-seconds", "900"' in pit,'autonomous PIT waits up to 900s for genuine peer catalogue owner')
    ck('BUSY_REASON_VISIBLE','payload.get("error")' in pit,'catalogue BUSY error is surfaced instead of reason=null')
    status=(ROOT/'backend/core/evidence_pipeline_status_service.py').read_text()
    ck('STATUS_EXPOSES_TRANSPORT_GATES','publication_outbox_drained' in status and 'research_catalogue_ready' in status,'diagnostic shows outbox drain and catalogue readiness explicitly')

    # Functional strict-JSON regression using the exact legacy tokens seen on Windows.
    code=(
        "import json; from core.strict_json import strict_json_dumps; "
        "x=json.loads('{\\\"baseline_ic\\\":NaN,\\\"x\\\":Infinity,\\\"y\\\":-Infinity}'); "
        "s=strict_json_dumps(x,sort_keys=True,separators=(\",\",\":\")); "
        "assert 'NaN' not in s and 'Infinity' not in s; "
        "y=json.loads(s); assert y['baseline_ic'] is None and y['x'] is None and y['y'] is None; print(s)"
    )
    env={'PYTHONPATH':str(ROOT/'backend'),'PYTHONDONTWRITEBYTECODE':'1'}
    import os
    fullenv=dict(os.environ); fullenv.update(env)
    r=subprocess.run([sys.executable,'-c',code],cwd=str(ROOT),env=fullenv,capture_output=True,text=True)
    ck('LEGACY_NAN_FIXTURE',r.returncode==0,(r.stdout or r.stderr).strip()[:300])

    config=(ROOT/'backend/config.py').read_text()
    frontend=json.loads((ROOT/'frontend/release-identity.json').read_text(encoding='utf-8-sig'))
    index=(ROOT/'frontend/index.html').read_text(encoding='utf-8-sig')
    marker='production-usability-r8-pl22-evidence-transport-8086'
    ck('EXACT_BUILD_MARKER_SINGLE_AUTHORITY', marker in config and frontend.get('build_marker')==marker and f'data-build-marker="{marker}"' in index,'backend/frontend/index marker exact')
    ck('BROKER_AUTHORITY_NONE',ident.get('broker_authority')=='NONE','broker authority unchanged')
    result={'ok':not failures,'passed':sum(x['ok'] for x in checks),'failed':sum(not x['ok'] for x in checks),'checks':checks,'failures':failures,'broker_authority':'NONE'}
    print(json.dumps(result,indent=2))
    return 0 if not failures else 2
if __name__=='__main__': raise SystemExit(main())
