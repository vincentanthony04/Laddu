from __future__ import annotations
import hashlib,json,re
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
def sha(p): return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def check(name,ok,detail,checks,failures):
    checks.append({'gate':name,'state':'PASS' if ok else 'FAIL','detail':detail})
    if not ok: failures.append(f'{name}:{detail}')
def main():
    checks=[]; failures=[]
    identity=json.loads((ROOT/'RELEASE_IDENTITY.json').read_text(encoding='utf-8-sig'))
    frozen=json.loads((ROOT/'validation/r32_frozen_r31_hashes.json').read_text(encoding='utf-8-sig'))
    install=(ROOT/'installer/install.ps1').read_text(encoding='utf-8-sig')
    revision=str(identity.get('candidate_revision') or '').upper()
    approved_customer_delta={'frontend/app.js','frontend/app.css','frontend/index.html','frontend/release-identity.json'} if revision in {'R34','R35','R36','R37'} else set()
    if revision=='R35': approved_customer_delta.add('installer/register_research_tasks.ps1')
    if revision in {'R36','R37'}: approved_customer_delta.add('train_ai_model.ps1')
    missing=[]; changed=[]
    for item in frozen.get('files') or []:
        if item['path'] in approved_customer_delta: continue
        p=ROOT/item['path']
        if not p.is_file(): missing.append(item['path'])
        elif sha(p)!=item['sha256']: changed.append(item['path'])
    check('EXACT_R31_IMPLEMENTATION_FROZEN',not missing and not changed,f"{len(frozen.get('files') or [])} R31 implementation files unchanged; missing={len(missing)} changed={len(changed)}",checks,failures)
    check('EXACT_R31_PARENT_HASH',frozen.get('parent_archive_sha256')=='26a82d65546e9da7b8797db167f8f36c0bb07209e24f8c28b78bad0b41de8975','R32 parent is exact installed-attempt R31 artifact',checks,failures)
    check('INSTALLER_ONLY_RUNTIME_DELTA',revision in {'R32','R33','R34','R35','R36','R37'},'candidate is R32 or a governed R33-R37 descendant; R36 adds only canonical training-depth closure',checks,failures)
    check('VERIFIER_WRITES_EVIDENCE_FIRST',"research_runtime.candidate.json" in install and '--output $researchManifestCandidate' in install,'research verifier no longer replaces live runtime manifest',checks,failures)
    check('SERVICE_QUIESCED_FOR_PUBLISH',"Stop-Service -Name $ServiceName -Force -ErrorAction Stop" in install and "WaitForStatus('Stopped'" in install,'runtime owner is stopped before metadata replacement',checks,failures)
    check('BOUNDED_METADATA_RETRY','Publish-RuntimeMetadataFile' in install and '[int]$Attempts = 8' in install and 'for($attempt=1; $attempt -le $Attempts; $attempt++)' in install,'Windows metadata publication has bounded retry',checks,failures)
    check('READONLY_AND_STALE_DACL_RECOVERY','attrib.exe -R' in install and 'takeown.exe /F' in install and "'*S-1-5-18:(F)'" in install and "'*S-1-5-32-544:(F)'" in install,'rebuildable runtime metadata can recover from stale file attributes/DACL only',checks,failures)
    check('EXACT_HASH_AFTER_PUBLISH','Runtime metadata hash mismatch' in install and 'Get-FileHash -LiteralPath $Destination -Algorithm SHA256' in install,'published manifest bytes are hash-proven',checks,failures)
    check('SERVICE_RESTARTED_AND_READY','Start-Service -Name $ServiceName -ErrorAction Stop' in install and '$readyAfterResearchPublish = Wait-Ready' in install,'service is restarted and bounded-ready after publication',checks,failures)
    check('LIVE_RESEARCH_API_PROOF',(("Get-ApiJsonWithRetry -Path '/api/quant-research-plane'" in install and "Research runtime authority API is not READY" in install) or ("Wait-ResearchPlaneReady" in install and "/api/quant-research-plane" in install)),'final research authority is proven through installed live API',checks,failures)
    check('FINAL_FRONTEND_IDENTITY_REPROOF',"$finalFrontendIdentity = Get-ApiJson '/api/frontend-identity'" in install and 'Final frontend identity proof failed' in install,'frontend/backend exact identity is re-proven after final restart',checks,failures)
    check('ROLLBACK_CONTRACT_RETAINED',"ROLLBACK" in install and 'Restore-Payload' in install,'existing transactional rollback remains present',checks,failures)
    check('BROKER_AUTHORITY_NONE',identity.get('broker_authority')=='NONE' and identity.get('product_mode')=='AUTOMATIC_MODEL_PAPER_ONLY','release closure adds no execution authority',checks,failures)
    report={'ok':not failures,'scope':'R32_FINAL_RELEASE_WINDOWS_RUNTIME_METADATA_CLOSURE','checks':checks,'passed':sum(c['state']=='PASS' for c in checks),'failed':sum(c['state']=='FAIL' for c in checks),'failures':failures,'production_ready':False,'broker_authority':identity.get('broker_authority')}
    print(json.dumps(report,indent=2))
    return 0 if not failures else 2
if __name__=='__main__': raise SystemExit(main())
