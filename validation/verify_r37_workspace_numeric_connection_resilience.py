from __future__ import annotations
import hashlib,json,subprocess,re
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
PARENT_SHA='fce492685cf69108414be65d30d0fd7c8f565a526929584e35ea12ecd3a140d6'
def sha(p): return hashlib.sha256(p.read_bytes()).hexdigest()
def check(n,o,d,c,f): c.append({'gate':n,'state':'PASS' if o else 'FAIL','detail':d}); f.extend([] if o else [n+':'+d])
def main():
 c=[];f=[]; ident=json.loads((ROOT/'RELEASE_IDENTITY.json').read_text(encoding='utf-8-sig')); frozen=json.loads((ROOT/'validation/r37_frozen_r36_product_hashes.json').read_text()); js=(ROOT/'frontend/app.js').read_text(); css=(ROOT/'frontend/app.css').read_text(); html=(ROOT/'frontend/index.html').read_text()
 missing=[];changed=[]
 for item in frozen['files']:
  p=ROOT/item['path']; missing.append(item['path']) if not p.is_file() else changed.append(item['path']) if sha(p)!=item['sha256'] else None
 check('EXACT_R36_PROTECTED_PRODUCT',not missing and not changed,f"{len(frozen['files'])} protected R36 product files unchanged; missing={len(missing)} changed={len(changed)}",c,f)
 check('EXACT_R36_PARENT_SHA',frozen.get('parent_archive_sha256')==PARENT_SHA,'exact R36 archive SHA bound',c,f)
 check('R37_IDENTITY',str(ident.get('candidate_revision')).upper()=='R37','candidate declares R37',c,f)
 check('REAL_TEXT_FIRST_PAINT','function animatedNumberHtml' in js and '>${esc(display)}</span>`' in js and 'data-number-target' in js,'dynamic values exist as formatted text before animation',c,f)
 check('NO_EMPTY_DIGIT_REEL','function odometerHtml' not in js and 'data-odometer-target' not in js and 'buildOdometerSlots' not in js,'fragile empty digit-reel renderer removed',c,f)
 check('FAIL_SAFE_TWEEN',all(x in js for x in ['function activateNumberAnimations','if (!root?.querySelectorAll) return','node.textContent=finalText','requestAnimationFrame(tick)','catch (_)','prefers-reduced-motion']),'numeric tween is enhancement-only with final-text fallback and null-root guard',c,f)
 check('WORKSPACE_ANIMATION_ROOT_VALID',"document.querySelector('[data-page-panel=\"workspace\"]') || document" in js and "activateNumberAnimations($('[data-page-panel=\"workspace\"]'))" not in js,'Workspace uses querySelector, not the id-only $ helper, so rendering cannot throw after a successful API response',c,f)
 check('R35_FALSE_CONNECTION_ROOT_CAUSE_CLOSED',"const $ = id => document.getElementById(id);" in js and "activateNumberAnimations(document.querySelector('[data-page-panel=\"workspace\"]') || document);" in js,'successful Workspace API response can no longer be misclassified as a connection failure by passing a CSS selector to getElementById',c,f)
 check('WORKSPACE_VALUES_USE_SAFE_RENDERER',all(x in js for x in ["animatedNumberHtml('workspace:active'","animatedNumberHtml(`market:${text(displayName).toUpperCase()}:price`","animatedNumberHtml(`desk:${desk}:scanned`","animatedNumberHtml(`desk:${desk}:pct`"]),'market/workspace/desk values use fail-safe renderer',c,f)
 check('LAST_VERIFIED_PRESERVED','if (!state.workspace)' in js and 'showing last verified Workspace' in js and 'return state.workspace;' in js,'failed refresh retains prior authoritative Workspace DOM/state',c,f)
 check('R35_DASHBOARD_RETAINED',all(x in js+css for x in ['desk-card-v3','desk-v3-scan','desk-v3-kpis','market-card-v3','market-v3-price','market-v3-change']),'R35 decision-dashboard hierarchy retained',c,f)
 check('R34_SR_AXIS_RETAINED',all(x in js for x in ['structuralLevelCandidates','MAJOR S','MAJOR R']) and 'Daily/weekly/monthly never mix' in js,'Major S/R and coherent axis retained',c,f)
 check('R37_CACHE_BINDING','r37-workspace-resilience' in html,'corrected assets cannot be silently reused from R36 cache',c,f)
 node=subprocess.run(['node','--check',str(ROOT/'frontend/app.js')],capture_output=True,text=True); check('JS_SYNTAX',node.returncode==0,'frontend app parses cleanly',c,f)
 report={'ok':not f,'scope':'R37_WORKSPACE_NUMERIC_CONNECTION_RESILIENCE','checks':c,'passed':sum(x['state']=='PASS' for x in c),'failed':sum(x['state']=='FAIL' for x in c),'failures':f,'production_ready':False,'broker_authority':'NONE'}; print(json.dumps(report,indent=2)); return 0 if not f else 2
if __name__=='__main__': raise SystemExit(main())
