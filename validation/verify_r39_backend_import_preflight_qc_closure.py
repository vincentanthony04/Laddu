from __future__ import annotations
import hashlib, json, os, subprocess, sys, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PARENT = json.loads((ROOT/'validation/r39_frozen_r38_hashes.json').read_text(encoding='utf-8'))
EXPECTED_PARENT_SHA = 'b8cf1d52db201038b4a06939ba84be85950e16b350b4ed56e14b4a2a42f0dc82'
ALLOWED = {
    'backend/core/historical_pit_sweep_service.py',
    'RELEASE_IDENTITY.json','RELEASE_ATTESTATION.json','frontend/release-identity.json',
    'validation/package_allowlist.json','validation/package_manifest.sha256','validation/validate_deployable_candidate.py',
    'validation/r39_frozen_r38_hashes.json','validation/verify_r39_backend_import_preflight_qc_closure.py',
    'docs/R39_BACKEND_IMPORT_PREFLIGHT_QC_CLOSURE.md',
}

def sha(p: Path) -> str: return hashlib.sha256(p.read_bytes()).hexdigest()

def main() -> int:
    failures=[]; checks=[]
    def check(name, ok, detail):
        checks.append({'gate':name,'state':'PASS' if ok else 'FAIL','detail':detail})
        if not ok: failures.append(f'{name}:{detail}')

    check('EXACT_R38_PARENT_ARCHIVE', PARENT.get('parent_archive_sha256') == EXPECTED_PARENT_SHA,
          'R39 is bound to exact R38 archive SHA')
    missing=[]; changed=[]
    for rel,digest in dict(PARENT.get('hashes') or {}).items():
        if rel in ALLOWED: continue
        p=ROOT/rel
        if not p.is_file(): missing.append(rel)
        elif sha(p)!=digest: changed.append(rel)
    check('R38_NON_REGRESSION', not missing and not changed,
          f'{len(PARENT.get("hashes") or {})-len(ALLOWED)}+ parent members protected; missing={len(missing)} changed={len(changed)}')

    service=(ROOT/'backend/core/historical_pit_sweep_service.py').read_text(encoding='utf-8')
    config=(ROOT/'backend/config.py').read_text(encoding='utf-8')
    check('PORT_AUTHORITY_CORRECT', 'from config import DATA_DIR, DEFAULT_PORT' in service and '{DEFAULT_PORT}' in service,
          'historical PIT uses config.DEFAULT_PORT, the declared package port authority')
    check('INVALID_PORT_IMPORT_ABSENT', 'from config import DATA_DIR, PORT' not in service,
          'R38 ImportError source is absent')
    check('DEFAULT_PORT_DECLARED', 'DEFAULT_PORT = int(os.environ.get("PROJECT_LADDU_PORT", "8086"))' in config,
          'config exports DEFAULT_PORT from PROJECT_LADDU_PORT')

    # Reproduce the Windows installer preflight semantics against the packaged backend.
    with tempfile.TemporaryDirectory(prefix='laddu-r39-import-') as td:
        env=dict(os.environ)
        env['PROJECT_LADDU_HOME']=td
        env['PROJECT_LADDU_DATA_PLANE_MODE']='test'
        env['PROJECT_LADDU_BACKEND_DIR']=str(ROOT/'backend')
        env['PYTHONDONTWRITEBYTECODE']='1'
        code=("import os,sys; sys.path.insert(0,os.environ['PROJECT_LADDU_BACKEND_DIR']); "
              "import main; print(main.APP_VERSION)")
        proc=subprocess.run([sys.executable,'-c',code],cwd=str(ROOT),env=env,capture_output=True,text=True,timeout=60)
        check('INSTALLER_EQUIVALENT_BACKEND_IMPORT', proc.returncode==0 and 'v131.0.0' in proc.stdout,
              f'returncode={proc.returncode}; stdout={proc.stdout.strip()[-120:]}; stderr={proc.stderr.strip()[-240:]}')

    # Import the changed module independently to catch symbol/circular-import mistakes.
    with tempfile.TemporaryDirectory(prefix='laddu-r39-pit-') as td:
        env=dict(os.environ); env['PROJECT_LADDU_HOME']=td; env['PROJECT_LADDU_DATA_PLANE_MODE']='test'; env['PYTHONDONTWRITEBYTECODE']='1'
        code=(f"import sys; sys.path.insert(0,{str(ROOT/'backend')!r}); "
              "from core.historical_pit_sweep_service import HistoricalPitSweepService; print(HistoricalPitSweepService.VERSION)")
        proc=subprocess.run([sys.executable,'-c',code],cwd=str(ROOT),env=env,capture_output=True,text=True,timeout=30)
        check('CHANGED_MODULE_IMPORT', proc.returncode==0 and 'historical-pit-sweep' in proc.stdout,
              f'returncode={proc.returncode}; stderr={proc.stderr.strip()[-240:]}')

    report={'ok':not failures,'scope':'R39_BACKEND_IMPORT_PREFLIGHT_QC_CLOSURE','checks':checks,
            'passed':sum(c['state']=='PASS' for c in checks),'failed':sum(c['state']=='FAIL' for c in checks),
            'failures':failures,'production_ready':False,'broker_authority':'NONE'}
    print(json.dumps(report,indent=2))
    return 0 if not failures else 2

if __name__=='__main__': raise SystemExit(main())
