from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PARENT_SHA = 'eb06bf659e837df9b157ab095b7ff08c841066dfb6d794d3b3ff05504e94c76f'
PROVEN_REGISTER_SHA = '449d34089ab79ddc4c22574a05fa547db55584cfcfe9818ddb560975b8d2c18e'


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check(name: str, ok: bool, detail: str, checks: list[dict], failures: list[str]) -> None:
    checks.append({'gate': name, 'state': 'PASS' if ok else 'FAIL', 'detail': detail})
    if not ok:
        failures.append(f'{name}:{detail}')


def main() -> int:
    checks: list[dict] = []
    failures: list[str] = []
    identity = json.loads((ROOT / 'RELEASE_IDENTITY.json').read_text(encoding='utf-8-sig'))
    frozen = json.loads((ROOT / 'validation/r36_frozen_r35_hashes.json').read_text(encoding='utf-8'))
    register = (ROOT / 'installer/register_research_tasks.ps1').read_text(encoding='utf-8-sig')
    verifier = (ROOT / 'validation/verify_authoritative_quant_research_lifecycle.py').read_text(encoding='utf-8-sig')
    training = (ROOT / 'train_ai_model.ps1').read_text(encoding='utf-8-sig')
    trainer = (ROOT / 'backend/tools/train_nse_smart_model.py').read_text(encoding='utf-8-sig')
    js = (ROOT / 'frontend/app.js').read_text(encoding='utf-8-sig')
    css = (ROOT / 'frontend/app.css').read_text(encoding='utf-8-sig')
    html = (ROOT / 'frontend/index.html').read_text(encoding='utf-8-sig')

    revision = str(identity.get('candidate_revision') or '').upper()
    approved_r37_delta = {'frontend/app.js','frontend/app.css'} if revision == 'R37' else set()
    missing, changed = [], []
    for item in frozen['files']:
        if item['path'] in approved_r37_delta:
            continue
        path = ROOT / item['path']
        if not path.is_file():
            missing.append(item['path'])
        elif sha(path) != item['sha256']:
            changed.append(item['path'])
    check('EXACT_R35_PROTECTED_PARENT', not missing and not changed,
          f"{len(frozen['files'])} R35 protected files unchanged; missing={len(missing)} changed={len(changed)}",
          checks, failures)
    check('EXACT_R35_PARENT_SHA', frozen.get('parent_archive_sha256') == PARENT_SHA,
          'exact failed R35 parent artifact is cryptographically bound', checks, failures)
    check('R36_IDENTITY', revision in {'R36','R37'},
          'candidate is R36 QC closure or bounded R37 browser-only descendant', checks, failures)

    # Reproduce and close the exact Windows failure from R35: the runtime verifier
    # expected train_ai_model.ps1 while the scheduled task pointed elsewhere.
    verifier_contract = '"ProjectLaddu-AI-Training": "train_ai_model.ps1"' in verifier
    register_contract = bool(re.search(
        r"Name='ProjectLaddu-AI-Training'.*?Script=\(Join-Path \$InstallDir 'train_ai_model\.ps1'\)",
        register,
    ))
    check('AUTHORITATIVE_VERIFIER_EXPECTATION', verifier_contract,
          'Research verifier requires ProjectLaddu-AI-Training -> train_ai_model.ps1', checks, failures)
    check('REGISTERED_AI_TASK_ACTION_MATCHES_VERIFIER', register_contract,
          'registered AI task action matches the verifier expectation exactly', checks, failures)
    check('R35_TASK_ACTION_MISMATCH_CLOSED', verifier_contract and register_contract and 'run_historical_pit_enrichment.ps1' not in register,
          'R35 TASK_ACTION_MISMATCH cannot recur from task registration', checks, failures)
    check('PROVEN_REGISTER_SCRIPT_RESTORED', sha(ROOT / 'installer/register_research_tasks.ps1') == PROVEN_REGISTER_SHA,
          'register_research_tasks.ps1 is exact proven R34/R33 bytes', checks, failures)
    check('NO_INSTALL_TIME_AI_AUTOSTART', "Start-ScheduledTask -TaskName 'ProjectLaddu-AI-Training'" not in register,
          'AI training is registered but never explicitly started by installer registration', checks, failures)

    # Historical enrichment stays inside the canonical trainer launcher rather
    # than changing task identity or inventing a parallel publication path.
    check('CANONICAL_TRAINER_504_MINIMUM', "@('--min-dates','504')" in training and "Join-Path $InstallDir 'backend\\tools\\train_nse_smart_model.py'" in training,
          'normal canonical AI training requires 504 governed historical dates', checks, failures)
    check('FIRST_MODE_REMAINS_SEPARATE', "if($FirstMode)" in training and "$args += '--first-mode'" in training,
          'diagnostic first-mode remains explicit and separate', checks, failures)
    check('PARQUET_DUCKDB_ONLY', 'TRAINING_SOURCE_POLICY = "PARQUET_DUCKDB_ONLY"' in trainer,
          'canonical trainer remains Parquet/DuckDB-only', checks, failures)
    check('SHADOW_PUBLICATION_BOUNDARY', '"lifecycle_state": "SHADOW"' in trainer and 'PRODUCTION_WEIGHT_POLICY' in trainer,
          'historical training remains governed shadow research, not broker authority', checks, failures)
    check('INERT_R35_PARALLEL_RUNNER', 'run_historical_pit_enrichment.ps1' not in register,
          'R35 parallel runner may remain packaged for lineage but is not scheduled or authoritative', checks, failures)

    # Preserve the actual R35 visual redesign while repairing only the QC escape.
    visual_number_ok = (all(x in js for x in ['odometerHtml','activateOdometers','data-odometer-target']) if revision == 'R36' else all(x in js for x in ['animatedNumberHtml','activateNumberAnimations','data-number-target']))
    check('R35_DASHBOARD_NUMBER_PRESENTATION_PRESERVED_OR_SAFELY_CORRECTED', visual_number_ok,
          'R36 odometer is retained on R36; R37 preserves smooth numbers through fail-safe real-text animation', checks, failures)
    check('R35_PREMIUM_DESK_PRESERVED', all(x in js + css for x in ['desk-card-v3','desk-v3-scan','desk-v3-kpis','desk-v3-context']),
          'compact desk dashboard preserved', checks, failures)
    check('R35_PREMIUM_MARKET_PRESERVED', all(x in js + css for x in ['market-card-v3','market-v3-price','market-v3-change']),
          'compact market tape preserved', checks, failures)
    check('R35_SR_AND_AXIS_PRESERVED', all(x in js for x in ['structuralLevelCandidates','MAJOR S','MAJOR R']) and 'Daily/weekly/monthly never mix' in js,
          'Major S/R and coherent time-axis implementation preserved', checks, failures)
    cache_ok = ('r36-qc-task-contract-historical-pit' in html) if revision == 'R36' else ('r37-workspace-resilience' in html)
    check('R36_OR_R37_CACHE_BINDING', cache_ok,
          'browser assets are cache-bound to the exact R36/R37 identity', checks, failures)

    node = subprocess.run(['node', '--check', str(ROOT / 'frontend/app.js')], capture_output=True, text=True)
    check('JS_SYNTAX', node.returncode == 0, 'frontend app parses cleanly', checks, failures)

    report = {
        'ok': not failures,
        'scope': 'R36_QC_TASK_CONTRACT_HISTORICAL_PIT_CLOSURE',
        'checks': checks,
        'passed': sum(x['state'] == 'PASS' for x in checks),
        'failed': sum(x['state'] == 'FAIL' for x in checks),
        'failures': failures,
        'production_ready': False,
        'broker_authority': 'NONE',
    }
    print(json.dumps(report, indent=2))
    return 0 if not failures else 2


if __name__ == '__main__':
    raise SystemExit(main())
