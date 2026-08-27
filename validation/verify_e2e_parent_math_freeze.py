from __future__ import annotations
import hashlib, json, subprocess, sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
PARENT=Path('/mnt/data/laddu_ui_r2_fresh_extract/Project-Laddu-v131.0.0-MATHEMATICS-GREEN-TERMINAL-ACTIONABLE-UI-R2-INSTALLATION-CANDIDATE-NOT-ACCEPTED-NOT-RELEASE')
OUT=Path('/mnt/data/LADDU_E2E_PARENT_MATH_FREEZE_PROOF.json')
allowed_backend={
 'backend/core/trust_state_service.py',
 'backend/routes_get_system.py',
 'backend/routes_get_registry.py',
 'backend/core/follow_through_projection_service.py',
 'backend/core/performance_evidence_authority.py',
}
recertified_math_backend={
 'backend/core/follow_through_projection_service.py',
 'backend/core/performance_evidence_authority.py',
}

def sha(p): return hashlib.sha256(p.read_bytes()).hexdigest()
def inventory(root, prefix='backend/'):
    out={}
    for p in root.rglob('*'):
        if not p.is_file() or '__pycache__' in p.parts or p.suffix in {'.pyc','.pyo'}: continue
        rel=p.relative_to(root).as_posix()
        if rel.startswith(prefix): out[rel]=sha(p)
    return out
pa=inventory(PARENT); ch=inventory(ROOT)
allkeys=sorted(set(pa)|set(ch))
changed=[k for k in allkeys if pa.get(k)!=ch.get(k)]
unexpected=[k for k in changed if k not in allowed_backend]
missing=[k for k in pa if k not in ch]
added=[k for k in ch if k not in pa and k.startswith('backend/')]
math_tokens=('indicator','technical','candle','risk','cost','settlement','lifecycle','factor','strategy','alpha','walk_forward','wfa','support','resistance','vwap','rsi','atr','adx','macd','supertrend','entry','geometry','performance','accuracy','model_paper','decision_repository','admission_policy')
math_sensitive=[k for k in changed if any(t in k.lower() for t in math_tokens) and k not in allowed_backend]
recert=subprocess.run([sys.executable,str(ROOT/'validation/verify_follow_through_projection_r3.py')],cwd=ROOT,capture_output=True,text=True,timeout=60)
recert_ok=recert.returncode==0
checks=[
 {'name':'all backend changes are inside explicit E2E/recertified follow-through boundary','ok':not unexpected,'detail':unexpected},
 {'name':'no parent backend files were removed','ok':not missing,'detail':missing},
 {'name':'only recertified follow-through backend file was added','ok':set(added).issubset(recertified_math_backend),'detail':added},
 {'name':'no unrecertified mathematics-sensitive backend authority changed','ok':not math_sensitive,'detail':math_sensitive},
 {'name':'follow-through mathematics recertification passes','ok':recert_ok,'detail':(recert.stdout+recert.stderr)[-1200:]},
 {'name':'exact expected backend change set','ok':set(changed)==allowed_backend,'detail':changed},
]
passed=sum(x['ok'] for x in checks); failed=len(checks)-passed
payload={'ok':failed==0,'contract':'E2E_PARENT_MATH_FREEZE_R3','parent':'R2 installed candidate','parent_math_gate':'720 PASS / 0 FAIL / P0=0','changed_backend_files':changed,'unexpected_backend_changes':unexpected,'math_sensitive_changes':math_sensitive,'passed':passed,'failed':failed,'checks':checks}
OUT.write_text(json.dumps(payload,indent=2)+'\n')
print(json.dumps({k:v for k,v in payload.items() if k!='checks'},indent=2))
raise SystemExit(0 if payload['ok'] else 1)
