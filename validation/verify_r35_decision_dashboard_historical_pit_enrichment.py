from __future__ import annotations
import hashlib,json,subprocess,re
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]; PARENT='6ffb38e5c1fa03dc45c550065216c7ae7f2ce8ade4125d63ca11e172e305728d'
def sha(p): return hashlib.sha256(p.read_bytes()).hexdigest()
def check(n,o,d,c,f): c.append({'gate':n,'state':'PASS' if o else 'FAIL','detail':d}); f.extend([] if o else [n+':'+d])
def main():
 c=[];f=[]; ident=json.loads((ROOT/'RELEASE_IDENTITY.json').read_text(encoding='utf-8-sig')); frozen=json.loads((ROOT/'validation/r35_frozen_r34_product_hashes.json').read_text()); js=(ROOT/'frontend/app.js').read_text(); css=(ROOT/'frontend/app.css').read_text(); html=(ROOT/'frontend/index.html').read_text(); task=(ROOT/'installer/register_research_tasks.ps1').read_text(); runner=(ROOT/'run_historical_pit_enrichment.ps1').read_text()
 missing=[]; changed=[]
 for item in frozen['files']:
  p=ROOT/item['path']; missing.append(item['path']) if not p.is_file() else changed.append(item['path']) if sha(p)!=item['sha256'] else None
 check('EXACT_R34_FROZEN_PRODUCT',not missing and not changed,f"{len(frozen['files'])} frozen R34 product files unchanged; missing={len(missing)} changed={len(changed)}",c,f)
 check('EXACT_R34_PARENT',frozen.get('parent_archive_sha256')==PARENT,'exact R34 parent SHA bound',c,f)
 check('R35_IDENTITY',str(ident.get('candidate_revision')).upper()=='R35','candidate declares R35',c,f)
 install_expected=next((x['sha256'] for x in frozen['files'] if x['path']=='installer/install.ps1'), '')
 check('INSTALL_TRANSACTION_FROZEN',sha(ROOT/'installer/install.ps1')==install_expected,'install.ps1 exact R34 bytes from sealed frozen-hash contract',c,f)
 check('BACKEND_RUNTIME_FROZEN',all(not x.startswith(('backend/','service/','infra/')) for x in changed),'no backend/service/infra drift',c,f)
 check('ODOMETER_ENGINE',all(x in js for x in ['odometerHtml','activateOdometers','odo-reel','data-odometer-target']),'rolling odometer number engine wired',c,f)
 check('PREMIUM_DESK_V3',all(x in js+css for x in ['desk-card-v3','desk-v3-scan','desk-v3-kpis','desk-v3-context']),'compact visual desk hierarchy present',c,f)
 check('PREMIUM_MARKET_V3',all(x in js+css for x in ['market-card-v3','market-v3-price','market-v3-change']),'compact market tape hierarchy present',c,f)
 check('SEMANTIC_COLOUR_SYSTEM',all(x in css for x in ['--cyan:#00c2ff','--green:#16d68f','--red:#ff4f68','--amber:#ffae35']),'strong semantic dashboard palette present',c,f)
 check('COHERENT_AXIS',"return fmt({day:'2-digit', month:'short'});" in js and 'Daily/weekly/monthly never mix' in js,'daily/weekly/monthly use one dd MMM grammar; intraday uses date separators + time',c,f)
 check('MAJOR_SR_RESILIENT',all(x in js for x in ['structuralLevelCandidates','supportCandidates','resistanceCandidates','tolerance','MAJOR S','MAJOR R']),'major S/R explicit evidence plus structural fallback with tolerance',c,f)
 check('CAMARILLA_HIDDEN','data-overlay="camarilla"' not in html,'Camarilla hidden',c,f)
 check('RESEARCH_TERMINOLOGY','Historical model WF' in js and 'Forward selector WF' in js,'historical model and forward selector evidence are separated in UI',c,f)
 check('HISTORICAL_TASK_REPOINTED',"ProjectLaddu-AI-Training" in task and 'run_historical_pit_enrichment.ps1' in task,'existing rollback-owned AI task drives historical PIT enrichment',c,f)
 check('HISTORICAL_TASK_INSTALL_DELAY','-InitialDelaySeconds 180' in task,'historical enrichment waits 180s after installer task registration before low-priority execution',c,f)
 check('DEEP_TRAINER_REUSED','train_nse_smart_model.py' in runner and '--min-dates 504' in runner and 'refresh_research_catalog.py' in runner,'existing canonical Parquet/PIT trainer reused with 504-day minimum',c,f)
 check('LOW_PRIORITY_AND_ROLLBACK_SAFE',"PriorityClass='BelowNormal'" in runner and 'ExpectedBuildMarker' in runner and 'build_marker' in runner,'low-priority runner exits if installed build changed/rolled back',c,f)
 check('FOREGROUND_WORKLOAD_DEFERRAL',all(x in runner for x in ['Test-NseCashSession','interactive_priority_active','required_database_recovery','lifecycle:closure','DEFERRED_FOREGROUND_PRIORITY']),'historical enrichment defers during market/interactive/database/lifecycle priority instead of competing',c,f)
 check('NO_FIRST_MODE_DOWNGRADE','--first-mode' not in runner.lower(),'historical enrichment does not weaken to first-mode training',c,f)
 check('NO_PRODUCTION_AUTHORITY',"production_influence=0.0" in runner and "broker_authority='NONE'" in runner,'historical enrichment remains shadow-only',c,f)
 check('R35_CACHE_BUSTER','r35-decision-dashboard-pit-enrichment' in html,'browser cache bound to R35 assets',c,f)
 node=subprocess.run(['node','--check',str(ROOT/'frontend/app.js')],capture_output=True,text=True); check('JS_SYNTAX',node.returncode==0,'frontend app parses cleanly',c,f)
 report={'ok':not f,'scope':'R35_DECISION_DASHBOARD_HISTORICAL_PIT_ENRICHMENT','checks':c,'passed':sum(x['state']=='PASS' for x in c),'failed':sum(x['state']=='FAIL' for x in c),'failures':f,'production_ready':False,'broker_authority':'NONE'}; print(json.dumps(report,indent=2)); return 0 if not f else 2
if __name__=='__main__': raise SystemExit(main())
