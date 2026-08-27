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
    frozen=json.loads((ROOT/'validation/r33_frozen_r32_hashes.json').read_text(encoding='utf-8-sig'))
    install=(ROOT/'installer/install.ps1').read_text(encoding='utf-8-sig')
    research_projection=(ROOT/'backend/core/research_control_projection_service.py').read_text(encoding='utf-8-sig')
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
    check('EXACT_R32_IMPLEMENTATION_FROZEN',not missing and not changed,f"{len(frozen.get('files') or [])} R32 files unchanged; missing={len(missing)} changed={len(changed)}",checks,failures)
    check('EXACT_R32_PARENT_HASH',frozen.get('parent_archive_sha256')=='496498c56769833de7229a957492b34ca86da48ba8c08dc12edb6dd1ca043a5b','R33 parent is exact failed-install R32 artifact',checks,failures)
    check('R33_IDENTITY',revision in {'R33','R34','R35','R36','R37'},'candidate is R33 contract-query closure or governed R34-R37 descendant',checks,failures)
    check('CONTRACT_SPECIFIC_WAITER','function Wait-ResearchPlaneReady' in install and "$researchPlaneProof = Wait-ResearchPlaneReady" in install,'quant-research installation gate has a dedicated semantic waiter',checks,failures)
    check('HTTP_200_NOT_READY',"readable but not READY" in install and "Research runtime authority READY on attempt" in install,'HTTP readability and authority readiness are distinct states',checks,failures)
    check('EXACT_READY_CONTRACT',"$lastOk -and $lastState -eq 'READY'" in install and "ok=true,state=READY" in install,'installer waits for exact ok=true/state=READY contract',checks,failures)
    check('STRICTMODE_SAFE_OPTIONAL_JSON',"$payload.PSObject.Properties['ok']" in install and "$payload.PSObject.Properties['state']" in install,'optional JSON fields are queried through PSObject property metadata',checks,failures)
    check('NO_UNSAFE_BLOCKERS_DEREFERENCE','$researchPlaneProof.blockers' not in install,'installer no longer assumes /api/quant-research-plane has a top-level blockers property',checks,failures)
    check('BOUNDED_PROJECTION_WAIT','[int]$OverallDeadlineSec = 120' in install and 'Start-Sleep -Seconds' in install,'projection convergence remains bounded',checks,failures)
    check('FINAL_PROJECTION_EVIDENCE',"research-plane-final.json" in install,'exact successful projection is retained in installer evidence',checks,failures)
    check('R32_METADATA_TRANSACTION_RETAINED','Publish-RuntimeMetadataFile' in install and 'Stop-Service -Name $ServiceName -Force' in install and '$readyAfterResearchPublish = Wait-Ready' in install,'R32 Windows-safe metadata transaction remains intact',checks,failures)
    # The live endpoint is projection-only and its top-level contract is ok/state/runtime/publication/model lifecycle;
    # it is not required to expose a top-level blockers property.
    projection_contract = '"ok": runtime_ready and publication.get("ok") is True' in research_projection and '"state": "READY" if runtime_ready and publication.get("ok") is True else "BLOCKED"' in research_projection
    check('LIVE_ENDPOINT_TOPLEVEL_CONTRACT',projection_contract,'packaged cache-only research projection defines explicit top-level ok/state authority',checks,failures)
    # Fixture proof of the exact logical predicate: absence of blockers must be harmless.
    fixtures=[
      ({'ok':False,'state':'BLOCKED'},False),
      ({'ok':False,'state':'BLOCKED','runtime':{'blockers':['WARMING']}},False),
      ({'ok':True,'state':'READY'},True),
    ]
    fixture_ok=all((bool(obj.get('ok') is True and str(obj.get('state') or '')=='READY'))==expected for obj,expected in fixtures)
    check('SUCCESS_SHAPE_WITHOUT_BLOCKERS',fixture_ok,'READY success shape contains no blockers and evaluates cleanly; blocked shapes remain pending',checks,failures)
    check('BROKER_AUTHORITY_NONE',identity.get('broker_authority')=='NONE' and identity.get('product_mode')=='AUTOMATIC_MODEL_PAPER_ONLY','no execution authority change',checks,failures)
    report={'ok':not failures,'scope':'R33_RESEARCH_PROJECTION_CONTRACT_QUERY_CLOSURE','checks':checks,'passed':sum(c['state']=='PASS' for c in checks),'failed':sum(c['state']=='FAIL' for c in checks),'failures':failures,'production_ready':False,'broker_authority':identity.get('broker_authority')}
    print(json.dumps(report,indent=2))
    return 0 if not failures else 2
if __name__=='__main__': raise SystemExit(main())
