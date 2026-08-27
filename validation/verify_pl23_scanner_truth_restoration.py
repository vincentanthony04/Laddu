from __future__ import annotations
import hashlib, json, subprocess, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FROZEN = {
 'backend/core/scan_orchestration_service.py':'16621831e9dbefce1d497e96a9728861de900eb110407e1391dbe687cb65b654',
 'backend/core/scan_orchestration_modes.py':'0afd0d00a577c4edda94b79724f72787526c93fbf1ed9d0bd76c70e7ade9d07f',
 'backend/core/scan_orchestration_coverage.py':'9c6b3ae36640982e8548b2960610301b2fe28c4720d828c84d58ee16eb3773df',
 'backend/core/scan_orchestration_lifecycle.py':'97fa6c7e1cabe30c6f88caa7719cea6dc90c07694ee5159e79584ff908940afc',
 'backend/core/scan_orchestration_fast_lane.py':'0a5c02a1877f5c18878249839311c3bfbeba9d37b0541476e13c68783ecc5ffe',
 'backend/core/scan_orchestration_rows.py':'cd37c69edfe178b961cd1d91f6db19262dfae63ed813acccf000875c61608675',
 'backend/core/scan_orchestration_discovery.py':'6e7735c80d1457ce86deb3f44ff46045b3e7ab5ec0608f45f143e9c61f596dc4',
 'backend/core/desk_analysis_executor_router.py':'0db0b9e47d6c05525f332993ca3351fe854d183c83debcbcd570a177b0297720',
}
checks=[]
def check(name, ok, detail=None): checks.append({'name':name,'ok':bool(ok),'detail':detail})
for rel, expected in FROZEN.items():
    got=hashlib.sha256((ROOT/rel).read_bytes()).hexdigest()
    check(f'PL22 scanner engine frozen: {rel}', got==expected, got)

# Preserve the PL22 evidence-transport fixes while changing only operator projection.
strict=(ROOT/'backend/core/strict_json.py').read_text(encoding='utf-8')
pit=(ROOT/'backend/core/historical_pit_sweep_service.py').read_text(encoding='utf-8')
pub=(ROOT/'backend/core/ai_training_publication_service.py').read_text(encoding='utf-8')
check('PL22 strict JSON transport retained', 'allow_nan' in strict and 'math.isfinite' in strict and 'return None' in strict and 'bundle = dict(json_safe(bundle) or {})' in pub)
check('PL22 bounded catalogue arbitration retained', '\"--lock-wait-seconds\", \"900\"' in pit)
config=(ROOT/'backend/config.py').read_text(encoding='utf-8')
front=json.loads((ROOT/'frontend/release-identity.json').read_text(encoding='utf-8'))
index=(ROOT/'frontend/index.html').read_text(encoding='utf-8')
marker='production-usability-r8-pl23-scanner-truth-8086'
check('PL23 exact build marker single authority', marker in config and front.get('build_marker')==marker and f'data-build-marker="{marker}"' in index)

ops=(ROOT/'backend/core/operations_control_service.py').read_text(encoding='utf-8')
app=(ROOT/'frontend/app.js').read_text(encoding='utf-8')
check('coverage and analysis titles are separate', all(x in ops for x in ['"delivery_scanner": "Delivery Deep Analysis"','"delivery_coverage": "Delivery Universe Sweep"','"intraday_scanner": "Intraday Live Analysis"','"intraday_coverage": "Intraday Universe Sweep"']))
check('coverage projection carries retained full-sweep proof', all(x in ops for x in ['"last_completed_sweep_count"','"last_completed_at"','"full_sweep_proven"','component.endswith("_coverage")']))
check('quick cards use whole-universe coverage authorities', "map.get('loop:intraday_coverage')" in app and "map.get('loop:delivery_coverage')" in app)
check('UI distinguishes current sweep from last full sweep', 'current ${formatNumber(done,0)}/${formatNumber(total,0)} · last full ${formatNumber(lastFull,0)}/${formatNumber(total,0)}' in app)

def run(rel):
    p=subprocess.run([sys.executable, str(ROOT/rel)], cwd=ROOT, text=True, capture_output=True, timeout=90)
    return p.returncode, (p.stdout or p.stderr).strip()
rc,out=run('validation/validate_delivery_coverage_scheduler.py')
try: d=json.loads(out.splitlines()[-1])
except Exception: d={}
check('Delivery immutable 4137 sweep remains monotonic/full', rc==0 and d.get('full_sweep') is True and d.get('strictly_monotonic') is True and d.get('population')==4137, d)
rc,out=run('validation/verify_r40_intraday_authority_closure.py')
try: d=json.loads(out)
except Exception: d={}
check('Intraday one-authority/bounded-executor contract remains green', rc==0 and d.get('ok') is True, {'ok':d.get('ok'),'failed':len(d.get('failures') or [])})
failed=[x for x in checks if not x['ok']]
print(json.dumps({'contract':'PL23_SCANNER_TRUTH_RESTORATION','ok':not failed,'passed':len(checks)-len(failed),'failed':len(failed),'checks':checks},indent=2))
raise SystemExit(0 if not failed else 1)
