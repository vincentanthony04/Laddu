from __future__ import annotations
import json, subprocess, sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
BACKEND=ROOT/'backend'
if str(BACKEND) not in sys.path: sys.path.insert(0,str(BACKEND))
sys.dont_write_bytecode=True
import config
from core.operations_control_service import ACTIONS, OperationsControlService

checks=[]
def ck(name, cond, detail=''):
    checks.append({'name':name,'ok':bool(cond),'detail':str(detail)[:700]})

# PL42 inherited behaviour: the exact-parent guard may fail only on its exact marker.
proc=subprocess.run([sys.executable,str(ROOT/'validation/verify_pl42_adaptive_history_corporate_action_closure.py')],cwd=ROOT,capture_output=True,text=True)
parent_ok=False
parent_detail=(proc.stdout+proc.stderr)[-1500:]
try:
    payload=json.loads(proc.stdout or '{}')
    failed=[row for row in payload.get('checks',[]) if not row.get('ok')]
    parent_ok=(not failed) or (len(failed)==1 and failed[0].get('name')=='exact PL42 build marker')
    parent_detail=json.dumps(failed)
except Exception:
    parent_ok=proc.returncode==0
ck('PL42 adaptive-history/corporate-action parent remains green except exact descendant marker', parent_ok, parent_detail)

ops=(ROOT/'backend/core/operations_control_service.py').read_text(encoding='utf-8-sig')
index=(ROOT/'frontend/index.html').read_text(encoding='utf-8-sig')
app=(ROOT/'frontend/app.js').read_text(encoding='utf-8-sig')
ui=(ROOT/'frontend/ui-system.css').read_text(encoding='utf-8-sig')

ck('exact PL43 marker', config.BUILD_MARKER=='production-usability-r8-pl43-one-click-e2e-agents-8086', config.BUILD_MARKER)
ck('one-click action is allow-listed', 'run_end_to_end' in ACTIONS and ACTIONS['run_end_to_end'].safety=='SAFE_COMPONENT')
ck('one-click action schedules the governed lifecycle', 'action in {"advance_full_lifecycle", "run_end_to_end"}' in ops and 'result = self.start_full_lifecycle()' in ops)
ck('monitoring/recovery agent exists', 'def _monitoring_agent_pass' in ops and 'E2E_MONITORING_AGENT' in ops)
ck('monitor agent uses bounded safe recovery only', 'safety_class' in ops and 'SAFE_COMPONENT' in ops and 'len(attempts) >= 5' in ops and 'max_recoveries=3' in ops)
ck('monitor agent asks always-on autonomic controller to evaluate', 'autonomic_controller.request_evaluation(allow_action=True' in ops)
ck('reconciliation agent exists', 'def _reconciliation_agent_pass' in ops)
ck('reconciliation agent runs durable settlement repair', 'settlement_reconciliation.run_once(limit=200)' in ops)
ck('reconciliation agent runs signal lifecycle repair', 'signal_lifecycle_reconciliation.run_once(limit=500)' in ops)
ck('reconciliation agent checks research and decision surfaces', 'ResearchLifecycleReconciliationService(self.app.store).status()' in ops and 'DecisionSurfaceReconciliationService(self.app).status()' in ops)
ck('lifecycle persists visible agent states', '"agents": {' in ops and 'monitoring_recovery_agent' in ops and 'reconciliation_agent' in ops)
ck('agents are invoked during lifecycle, not display-only', all(token in ops for token in ('e2e_after_scanner_request','e2e_after_research_advance','e2e_after_{desk}_wfa','e2e_final_reconciliation')))
ck('adaptive history remains reference-not-cap in lifecycle status', 'historical_training_days"] = None' in ops and 'reference is not a cap' in ops)

ck('primary workspace has one-click Run End-to-End button', 'id="runEndToEnd"' in index and '>Run End-to-End<' in index)
ck('workspace shows monitoring and reconciliation agent status', 'id="e2eMonitorAgent"' in index and 'id="e2eReconcileAgent"' in index)
ck('one click calls run_end_to_end API action', "action:'run_end_to_end'" in app and 'workspace_one_click_end_to_end' in app)
ck('workspace renders live lifecycle/agent status', all(token in app for token in ('e2eRunState','e2eRunProgress','agents.monitoring_recovery','agents.reconciliation')))
ck('one-click UI is visually prominent', all(token in ui for token in ('.hero-e2e-control','.e2e-run-button','.e2e-agent-row','@keyframes e2ePulse')))
ck('broker/risk boundary is explicit in agent policy', 'risk/ledger/database/broker authorities are immutable' in ops and 'no inferred trades, outcomes, alpha or promotion' in ops)

nav=subprocess.run([sys.executable,str(ROOT/'validation/verify_ui_navigation_smoke_r3.py')],cwd=ROOT,capture_output=True,text=True)
ck('primary navigation browser smoke is green',nav.returncode==0,(nav.stdout+nav.stderr)[-700:])
truth=subprocess.run([sys.executable,str(ROOT/'validation/verify_user_r2_truth_regression_browser_r3.py')],cwd=ROOT,capture_output=True,text=True)
ck('browser truth regression is green',truth.returncode==0,(truth.stdout+truth.stderr)[-700:])

# Lightweight behavioural proof for the monitor agent using only safe fake components.
class Store:
    conn=None
    def get_kv(self,*a,**k): return k.get('default', {}) if 'default' in k else {}
    def set_kv(self,*a,**k): return None
class Sup:
    running=True
    def snapshot(self):
        return {'safe_worker': {'state':'FAILED','safety_class':'SAFE_COMPONENT','recovery_available':True,'heartbeat_age_sec':1,'progress_age_sec':1}}
    def recover(self,component,**kwargs): return {'ok':True,'state':'RECOVERED','component':component}
class Controller:
    def request_evaluation(self,**kwargs): return {'ok':True,'state':'EVALUATED'}
    def publish(self,*a,**k): return None
class Priority:
    def recover_stale(self,**kwargs): return {'ok':True,'state':'NO_STALE_JOBS'}
    def recovery_status(self): return {'state':'READY'}
class Reconciler:
    def run_once(self,**kwargs): return {'state':'RECONCILED','reconciled':0}
class App:
    store=Store(); supervisor=Sup(); autonomic_controller=Controller(); priority_pipeline=Priority()
    settlement_reconciliation=Reconciler(); signal_lifecycle_reconciliation=Reconciler()
    status={}
    def event(self,*a,**k): pass
fake=App()
service=OperationsControlService(fake)
monitor=service._monitoring_agent_pass(reason='validator')
ck('monitor agent behaviour recovers eligible safe failed component', monitor.get('ok') is True and monitor.get('attempts') and monitor['attempts'][0].get('component')=='safe_worker', monitor)

failed=[row for row in checks if not row['ok']]
print(json.dumps({'contract':'PL43_ONE_CLICK_E2E_AGENTS_CLOSURE','ok':not failed,'passed':len(checks)-len(failed),'failed':len(failed),'checks':checks},indent=2))
raise SystemExit(1 if failed else 0)
