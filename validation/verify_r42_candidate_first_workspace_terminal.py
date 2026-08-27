from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
FROZEN = json.loads((ROOT / 'validation/r42_frozen_r41_hashes.json').read_text(encoding='utf-8'))
EXPECTED_PARENT_SHA = 'e33131a39e6373aa9adb2e88df96f905d2f75eb71b8cfb443f595fa527b36538'

FUNCTIONAL_ALLOWED = {
    'frontend/app.js',
    'frontend/index.html',
    'frontend/ui-system.css',
}
METADATA_ALLOWED = {
    'RELEASE_IDENTITY.json', 'RELEASE_ATTESTATION.json', 'frontend/release-identity.json',
    'validation/package_allowlist.json', 'validation/package_manifest.sha256',
    'validation/r42_frozen_r41_hashes.json',
    'validation/verify_r42_candidate_first_workspace_terminal.py',
    'validation/validate_deployable_candidate.py',
    'docs/R42_CANDIDATE_FIRST_WORKSPACE_TERMINAL.md',
}
ALLOWED = FUNCTIONAL_ALLOWED | METADATA_ALLOWED


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    failures: list[str] = []
    checks: list[dict[str, str]] = []

    def check(name: str, ok: bool, detail: str) -> None:
        checks.append({'gate': name, 'state': 'PASS' if ok else 'FAIL', 'detail': detail})
        if not ok:
            failures.append(f'{name}:{detail}')

    check('EXACT_R41_PARENT_ARCHIVE', FROZEN.get('parent_archive_sha256') == EXPECTED_PARENT_SHA,
          'R42 is bound to the exact R41 archive')

    missing, unexpected, allowed_changed = [], [], []
    for rel, digest in dict(FROZEN.get('hashes') or {}).items():
        path = ROOT / rel
        if not path.is_file():
            missing.append(rel)
            continue
        if sha(path) != digest:
            if rel in ALLOWED:
                allowed_changed.append(rel)
            else:
                unexpected.append(rel)
    check('R41_NON_REGRESSION', not missing and not unexpected,
          f'missing={len(missing)} unexpected_changed={unexpected}')
    check('FRONTEND_ONLY_FUNCTIONAL_BOUNDARY', set(allowed_changed).issubset(ALLOWED),
          f'allowed_changed={sorted(allowed_changed)}')

    protected = [
        'INSTALL_UPDATE.cmd', 'installer/install.ps1', 'installer/register_research_tasks.ps1',
        'backend/core/scan_orchestration_lifecycle.py', 'backend/core/scan_orchestration_fast_lane.py',
        'backend/core/scan_orchestration_coverage.py', 'backend/core/historical_pit_sweep_service.py',
        'backend/core/trust_state_service.py', 'backend/core/selection_walk_forward_replay_service.py',
    ]
    bad = []
    for rel in protected:
        expected = (FROZEN.get('hashes') or {}).get(rel)
        path = ROOT / rel
        if not expected or not path.is_file() or sha(path) != expected:
            bad.append(rel)
    check('BACKEND_INSTALLER_R41_FROZEN', not bad, f'changed={bad}')

    index = (ROOT / 'frontend/index.html').read_text(encoding='utf-8')
    app = (ROOT / 'frontend/app.js').read_text(encoding='utf-8')
    css = (ROOT / 'frontend/ui-system.css').read_text(encoding='utf-8')

    check('CANDIDATES_FIRST_IN_DOM',
          index.index('candidate-focus-panel') < index.index('scanner-panel') < index.index('market-panel') < index.index('sector-panel'),
          'selected candidates precede desks and market/sector support rails')
    check('NO_MARKET_SECTOR_DESCRIPTIVE_HEADINGS',
          'MARKET PULSE' not in index and 'SECTOR PULSE' not in index and 'Verified benchmark context' not in index and 'NSE sector/index participation from the same market authority' not in index,
          'support context no longer spends vertical space on explanatory headings')
    check('SELECTED_CANDIDATES_PRIMARY',
          'SELECTED CANDIDATES' in index and 'Open full list' in index and 'candidate-focus-table' in index,
          'Workspace names and emphasizes the selected candidate list')
    check('CANDIDATE_POOL_NOT_FINAL_ONLY',
          "selectedPool = ['intraday','delivery'].flatMap(desk => dedupeDeskCandidates(payload.candidates, desk))" in app,
          'candidate focus is populated from ranked published candidates rather than final-only attention rows')
    check('CANDIDATE_RANKING',
          'candidateStageWeight(a)' in app and 'evidenceScoreValue(a)' in app and '.slice(0,8)' in app,
          'candidate focus is ranked stage-first then evidence/recency')
    check('DESK_LINEAR_ROW_MODEL',
          'desk-card-v4' in app and 'desk-row-main' in app and 'grid-template-columns:125px 190px minmax(190px,1fr) repeat(5,88px) 95px' in css,
          'each desk is one linear row with coverage, live analysis, KPIs and pace')
    check('NO_R41_LARGE_DESK_LAYOUT_IN_ACTIVE_MARKUP',
          'desk-card desk-card-v3' not in app,
          'R41 tall desk-card markup is no longer emitted')
    check('MARKET_SUPPORT_SINGLE_RAIL',
          'support-rail' in index and 'height:42px!important' in css and 'grid-template-columns:repeat(6,minmax(155px,1fr))' in css,
          'market context is a compact one-line support rail')
    check('SECTOR_SUPPORT_SINGLE_RAIL',
          'display:flex!important' in css and 'flex:0 0 145px!important' in css and 'support-breadth' in index,
          'sector context is a compact horizontally scrollable support rail')
    check('R42_CACHE_IDENTITY',
          '/ui-system.css?v=131.0.0-r42' in index and '/app.js?v=131.0.0-r42' in index and '/app.css?v=131.0.0-r42' in index,
          'browser cache identity is R42 across all frontend assets')

    node = subprocess.run(['node', '--check', str(ROOT / 'frontend/app.js')], capture_output=True, text=True, timeout=20)
    check('FRONTEND_JS_SYNTAX', node.returncode == 0,
          node.stderr.strip()[-220:] if node.returncode else 'node --check PASS')

    report = {
        'ok': not failures,
        'scope': 'R42_CANDIDATE_FIRST_WORKSPACE_TERMINAL',
        'checks': checks,
        'passed': sum(c['state'] == 'PASS' for c in checks),
        'failed': sum(c['state'] == 'FAIL' for c in checks),
        'failures': failures,
        'functional_change_boundary': sorted(FUNCTIONAL_ALLOWED),
        'production_ready': False,
        'broker_authority': 'NONE',
    }
    print(json.dumps(report, indent=2))
    return 0 if not failures else 2


if __name__ == '__main__':
    raise SystemExit(main())
