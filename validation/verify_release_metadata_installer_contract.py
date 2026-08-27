from __future__ import annotations
import json,re,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]

def need(obj,path):
    cur=obj
    for part in path.split('.'):
        if not isinstance(cur,dict) or part not in cur:
            raise AssertionError(f'missing property: {path}')
        cur=cur[part]
    if cur is None:
        raise AssertionError(f'null property: {path}')
    return cur

def main():
    ident=json.loads((ROOT/'RELEASE_IDENTITY.json').read_text(encoding='utf-8-sig'))
    att=json.loads((ROOT/'RELEASE_ATTESTATION.json').read_text(encoding='utf-8-sig'))
    required_ident=[
        'installable','artifact_type','production_ready','installation_purpose','broker_authority','version',
        'parent.version','parent.release_identity_sha256','parent.archive_sha256'
    ]
    required_att=[
        'installable','artifact_type','production_ready','installation_purpose','version',
        'certification.current_level','certification.SOURCE_SEALED','certification.INSTALLABLE','certification.END_TO_END_ACCEPTED',
        'parent.version','parent.release_identity_sha256','parent.archive_sha256'
    ]
    failures=[]
    for p in required_ident:
        try: need(ident,p)
        except Exception as e: failures.append('identity:'+str(e))
    for p in required_att:
        try: need(att,p)
        except Exception as e: failures.append('attestation:'+str(e))
    if not failures:
        if ident['version'] != att['version']: failures.append('identity/attestation version mismatch')
        if ident['parent'] != att['parent']: failures.append('identity/attestation parent mismatch')
        if len(str(ident['parent']['archive_sha256'])) != 64: failures.append('parent archive_sha256 length')
        if len(str(ident['parent']['release_identity_sha256'])) != 64: failures.append('parent release_identity_sha256 length')
        semver=re.compile(r'^v?(\d+)\.(\d+)\.(\d+)$')
        pm=semver.match(str(ident['parent']['version'])); cm=semver.match(str(ident['version']))
        if not pm or not cm: failures.append('invalid semantic version format')
        elif tuple(map(int,cm.groups())) <= tuple(map(int,pm.groups())):
            failures.append(f'candidate version must advance sealed source parent: {ident["parent"]["version"]} -> {ident["version"]}')
    # Mirror the package_gate.ps1 installation-candidate contract.
    if ident.get('artifact_type') == 'INSTALLATION_CANDIDATE':
        if ident.get('production_ready') is not False: failures.append('candidate production_ready must be false')
        if ident.get('installation_purpose') != 'EXACT_WINDOWS_TARGET_PROOF': failures.append('candidate installation_purpose mismatch')
        if ident.get('broker_authority') != 'NONE': failures.append('candidate broker authority changed')
        cert=att.get('certification') or {}
        if att.get('artifact_type') != 'INSTALLATION_CANDIDATE' or att.get('installable') is not True: failures.append('attestation candidate boundary mismatch')
        if att.get('production_ready') is not False: failures.append('attestation production_ready must be false')
        if att.get('installation_purpose') != 'EXACT_WINDOWS_TARGET_PROOF': failures.append('attestation installation_purpose mismatch')
        if cert.get('current_level') != 'SOURCE_SEALED' or cert.get('SOURCE_SEALED') != 'PASS': failures.append('attestation SOURCE_SEALED mismatch')
        if cert.get('INSTALLABLE') != 'PENDING_INSTALLED_PROOF': failures.append('attestation INSTALLABLE mismatch')
        if cert.get('END_TO_END_ACCEPTED') != 'PENDING_ACCEPTANCE_GATE': failures.append('attestation END_TO_END_ACCEPTED mismatch')
    out={'ok':not failures,'failures':failures,'candidate_version':ident.get('version'),'sealed_parent_version':(ident.get('parent') or {}).get('version'),'broker_authority':ident.get('broker_authority')}
    print(json.dumps(out,indent=2))
    return 0 if not failures else 2
if __name__=='__main__': raise SystemExit(main())
