from __future__ import annotations
import hashlib,json,subprocess
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
def sha(p): return hashlib.sha256(p.read_bytes()).hexdigest()
def main():
  checks=[]; failures=[]
  def ck(name,ok,detail):
    checks.append({'name':name,'ok':bool(ok),'detail':detail})
    if not ok: failures.append(name+':'+detail)
  ident=json.loads((ROOT/'RELEASE_IDENTITY.json').read_text(encoding='utf-8-sig'))
  freeze=json.loads((ROOT/'validation/pl20_frozen_pl17_trading_hashes.json').read_text())['files']
  changed=[rel for rel,d in freeze.items() if not (ROOT/rel).is_file() or sha(ROOT/rel)!=d]
  ck('PL21_IDENTITY','PL21_EVIDENCE_ORCHESTRATION_CLOSURE' in str(ident.get('acceptance_state') or ''),'PL21 acceptance state declared')
  ck('TRADING_CORE_FROZEN',not changed,'protected PL17 trading core unchanged: '+','.join(changed))
  pub=(ROOT/'backend/core/ai_training_publication_service.py').read_text()
  ck('CAPITAL_WFA_PERSISTED','for validation_payload in (validation, capital_validation)' in pub,'both research and capital validation persisted')
  pit=(ROOT/'backend/core/historical_pit_sweep_service.py').read_text()
  ck('PIT_WFA_VISIBLE','"capital_validation": payload.get("capital_validation")' in pit,'PIT supervisor retains capital validation')

  ck('OFF_MARKET_STARVATION_LEASE','_off_market_waiter_since' in pit and '_off_market_lease_until' in pit and 'yields >= 3' in pit and 'now + 300.0' in pit,'repeated off-market waiter pressure grants bounded convergence lease while recovery/manual/interactive still preempt')
  ck('PUBLICATION_OUTBOX_REPLAY','_replay_publication_outbox' in pit and 'AITrainingPublicationService(self.app.store).publish(bundle)' in pit and 'publication_receipts' in pit,'runtime owns durable outbox replay through authoritative in-process publication boundary')
  eps=(ROOT/'backend/core/evidence_pipeline_status_service.py').read_text()
  ck('CURRENT_VS_RETAINED_TRAINING_TRUTH','retained_training_artifact_exists' in eps and 'current_cycle_completed = bool(pit.get("last_success_at"))' in eps,'diagnostic separates retained artifact from current PIT completion')
  chall=(ROOT/'backend/core/nse_calibrated_challenger_service.py').read_text()
  status=chall[chall.index('    def status('):chall.index('    def latest_model(')]
  ck('CHALLENGER_STATUS_BOUNDED','self._dataset(' not in status and 'quant_training_evidence_status' in status,'GET status uses bounded aggregate, no heavy dataset build')
  replay=(ROOT/'backend/core/selection_walk_forward_replay_service.py').read_text()
  ck('WFA_NONOPAQUE','EVIDENCE_NOT_READY' in replay and 'CANONICAL_SELECTOR_EVIDENCE_NOT_MIGRATED' in replay,'evidence-not-ready returned as structured state')
  registry=(ROOT/'backend/routes_get_registry.py').read_text()
  ck('PIPELINE_STATUS_ROUTE','/api/evidence-pipeline/status' in registry,'consolidated evidence status route shipped')
  import re
  config=(ROOT/'backend/config.py').read_text()
  frontend=json.loads((ROOT/'frontend/release-identity.json').read_text(encoding='utf-8-sig'))
  index=(ROOT/'frontend/index.html').read_text(encoding='utf-8-sig')
  m=re.search(r'^BUILD_MARKER\s*=\s*[\"\']([^\"\']+)',config,re.M)
  backend_marker=m.group(1) if m else ''
  frontend_marker=str(frontend.get('build_marker') or '')
  ck('EXACT_BUILD_MARKER_SINGLE_AUTHORITY', backend_marker==frontend_marker and backend_marker=='production-usability-r8-pl21-evidence-orchestration-8086' and f'data-build-marker=\"{backend_marker}\"' in index, f'backend={backend_marker} frontend={frontend_marker}')
  manual=(ROOT/'run_historical_pit_enrichment.ps1').read_text()
  ck('STALE_R35_MARKER_REMOVED',"ExpectedBuildMarker = ''" in manual and "product-recovery-r35-decision-dashboard-pit-enrichment" not in manual,'manual compatibility runner no longer silently skips descendants')
  node=subprocess.run(['node','--check',str(ROOT/'frontend/app.js')],capture_output=True,text=True)
  ck('JS_SYNTAX',node.returncode==0,'frontend JS syntax')
  result={'ok':not failures,'passed':sum(1 for x in checks if x['ok']),'failed':sum(1 for x in checks if not x['ok']),'checks':checks,'failures':failures,'broker_authority':'NONE'}
  print(json.dumps(result,indent=2)); return 0 if not failures else 2
if __name__=='__main__': raise SystemExit(main())
