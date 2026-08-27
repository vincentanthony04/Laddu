from __future__ import annotations
import json,re,sys
from pathlib import Path
from datetime import datetime,timezone,timedelta
from playwright.sync_api import sync_playwright

ROOT=Path(__file__).resolve().parents[1]; BACK=ROOT/'backend'; FRONT=ROOT/'frontend'
sys.path.insert(0,str(BACK))
import core.runtime_primitives as runtime_primitives
from routes_get_system import r_trader_workspace, r_trader_live_state

OUT=Path('/mnt/data/LADDU_E2E_BACKEND_FRONTEND_WIRING_PROOF.json')
SHOTS=Path('/mnt/data/LADDU_E2E_BACKEND_FRONTEND_WIRING_SCREENSHOTS'); SHOTS.mkdir(exist_ok=True)
IST=timezone(timedelta(hours=5,minutes=30)); NOW=datetime.now(IST)
checks=[]
def ck(name,ok,detail=None): checks.append({'name':name,'ok':bool(ok),'detail':detail}); print('FAIL',name,detail) if not ok else None

def ts(minutes=0): return (NOW-timedelta(minutes=minutes)).isoformat()

class Trust:
    def snapshot(self): return {'ok':True,'version':'trader-trust-state-1.0.0','evaluated_at':datetime.now(IST).isoformat(timespec='seconds'),'sequence_us':int(datetime.now(timezone.utc).timestamp()*1_000_000),'state':'TRUSTED','decision_admission_allowed':True,'market_open':False,'reason':'data/read-model path current and no critical runtime blocker','latency':{'customer_read_p95_ms':180,'customer_read_samples':20}}
class Hist:
    def snapshot(self): return {'state':'READY'}
class Quotes:
    def __init__(self,app): self.app=app
    def snapshot(self,requested,market_open=False,max_age_sec=8.0):
        if requested is None: return {}
        out={}
        for sym in requested:
            px=self.app.prices.get(sym)
            if px is not None: out[sym]={'symbol':sym,'ltp':px,'identity_verified':True,'freshness_state':'live' if market_open else 'closed_market','provider_timestamp':datetime.now(IST).isoformat(timespec='seconds')}
        return out
class LM:
    def __init__(self,app): self.quotes=Quotes(app)

class FakeApp:
    def __init__(self):
        self.phase='closed_partial'; self.trust_state_service=Trust(); self.historical_pit_sweep=Hist(); self.live_market=LM(self); self.market_data=None; self.status={'service':'running'}; self._market_radar_http_snapshot={}; self._market_radar_snapshot={}; self._coverage_quote_cache={}; self.prices={}
    def _coverage(self): return {'delivery':(2560,4137),'intraday':(2366,3364)} if self.phase=='closed_partial' else {'delivery':(4137,4137),'intraday':(3364,3364)}
    def scanner_status(self):
        modes={}
        for desk,(done,total) in self._coverage().items(): modes[desk]={'processed':done,'total':total,'state':'CONTINUING_SWEEP' if done<total else 'RUNNING'}
        return {'service':'running','scanner':{'mode_scanners':modes},'instruments':{'universe_count':4137}}
    def heatmap_snapshot(self): return []
    def dashboard_cards_data(self,mode):
        final=[]; selected=[]
        if self.phase=='closed_partial':
            selected=[{'symbol':'SHRIRAMFIN','mode':'delivery','setup':'Accumulation zone','rank_score':75,'rank_readiness':'PARTIAL','rank_scoring_state':'DEGRADED','rank_missing_inputs':['sector_relative'],'freshness_state':'UNKNOWN','generated_at':ts(20),'status':'WATCH','research_only':True,'instrument_key':'NSE_EQ|SHRIRAMFIN','captured_price':1022.2}]
            self.prices={'SHRIRAMFIN':1022.2}
        elif self.phase in {'ready','active','crossed'}:
            stage='OPEN' if self.phase in {'active','crossed'} else 'FINAL'; px=1492.6 if self.phase!='crossed' else 1535.0; self.prices={'RELIANCE':px}
            row={'symbol':'RELIANCE','mode':'delivery','side':'LONG','setup':'Breakout retest','rank_score':88.2,'evidence_score':88.2,'rank_readiness':'READY','rank_scoring_state':'NORMAL','rank_missing_inputs':[],'freshness_state':'CURRENT','generated_at':ts(15),'holding_period':'5–10d','display_stage':stage,'status':stage,'canonical_state':stage,'entry':1484.0,'target':1532.0,'stop':1463.5,'original_stop':1463.5,'original_target':1532.0,'reward_risk':2.34,'final_signal_authority':'POSTGRESQL_CANONICAL_DECISION','decision_id':'decision-reliance-001','signal_id':'signal-reliance-001','instrument_key':'NSE_EQ|RELIANCE','research_only':False}
            if stage=='OPEN': row.update({'opened_at':ts(8),'position_id':'paper-pos-001'})
            final=[row]
        return {'projection_state':'READY','time':datetime.now(IST).isoformat(timespec='seconds'),'final_signals':final,'active_positions':[],'discovery':{'near_qualified':[]},'watch_queue':selected,'decision_list':[],'selected_memory':selected}

app=FakeApp(); phases={}
for phase,open_market in [('closed_partial',False),('ready',True),('active',True),('crossed',True),('settled',False)]:
    app.phase=phase; runtime_primitives.is_india_market_open=(lambda value=open_market: value)
    # routes_get_system imports is_india_market_open inside call, so monkeypatching module is enough.
    if phase=='settled': app.prices={}
    phases[phase]={'workspace':r_trader_workspace(app,{},'', 'all'),'live':r_trader_live_state(app,{},'', 'all')}

# Backend contract checks from real route code.
cp=phases['closed_partial']['workspace']; rd=phases['ready']['workspace']; ac=phases['active']['workspace']; cr=phases['crossed']['workspace']
ck('real workspace route exposes incomplete delivery scope',cp['coverage']['delivery']['ranking_scope']=='EVALUATED_SUBSET_ONLY' and cp['coverage']['delivery']['complete'] is False,cp['coverage']['delivery'])
ck('real workspace route preserves canonical decision identity',len(rd['final_signals'])==1 and rd['final_signals'][0]['decision_id']=='decision-reliance-001',rd['final_signals'])
rr=rd['final_signals'][0]
ck('real workspace route preserves frozen trade geometry',rr.get('entry')==1484.0 and rr.get('target')==1532.0 and rr.get('stop')==1463.5,rr)
ck('real workspace quote overlay updates only current price',rr.get('current_price')==1492.6 and rr.get('entry')==1484.0,rr)
ck('real workspace active lifecycle projects OPEN',ac['final_signals'][0].get('display_stage')=='OPEN',ac['final_signals'][0])
ck('real workspace target cross becomes reconciliation required',cr['final_signals'][0].get('display_stage')=='RECONCILIATION_REQUIRED' and cr['final_signals'][0].get('price_cross_reconciliation')=='TARGET',cr['final_signals'][0])

# Frontend receives exact real route payloads.
performance={'closed_partial':{'ok':True,'canonical_lifecycle':{'records':[]}},'ready':{'ok':True,'canonical_lifecycle':{'records':[]}},'active':{'ok':True,'canonical_lifecycle':{'records':[]}},'crossed':{'ok':True,'canonical_lifecycle':{'records':[]}},'settled':{'ok':True,'canonical_lifecycle':{'records':[{'symbol':'RELIANCE','mode':'delivery','accuracy_eligible':True,'performance_eligible':True,'exit_reason':'TARGET_HIT','signal_outcome':'SUCCESS','economic_outcome':'SUCCESS','net_pnl':6120.0,'realized_r':2.31,'holding_minutes':7200,'closed_at':ts(1),'instrument_key':'NSE_EQ|RELIANCE','after_state':'CONTINUED'}]}}}
for k in phases: phases[k]['performance']=performance[k]

index=(FRONT/'index.html').read_text(); css=(FRONT/'app.css').read_text()+'\n'+(FRONT/'ui-system.css').read_text(); js=(FRONT/'app.js').read_text()
index=re.sub(r'<link[^>]+href="/app\.css[^>]*>','',index); index=re.sub(r'<link[^>]+href="/ui-system\.css[^>]*>','',index); index=index.replace('</head>',f'<style>{css}</style></head>'); index=re.sub(r'<script src="/app\.js[^>]*></script>','__APP__',index)
mocks=json.dumps(phases,default=str)
def html(initial='closed_partial'):
    stub=f'''<script>window.__phase={json.dumps(initial)};window.__fail=false;window.__m={mocks};window.fetch=async function(input){{const p=String(input).split('?')[0],ph=window.__phase; if(p==='/api/trader-workspace'&&window.__fail)return new Response(JSON.stringify({{ok:false,error:'outage'}}),{{status:503,headers:{{'Content-Type':'application/json'}}}}); let d=p==='/api/trader-workspace'?window.__m[ph].workspace:p==='/api/trader-live-state'?window.__m[ph].live:p==='/api/performance'?window.__m[ph].performance:p==='/api/frontend-identity'?{{ok:true,version:'v131.0.0',manifest_version:'v131.0.0',frontend_owner:'standalone-v131.0.0',build_marker:'terminal-actionable-ui-r3-e2e-acceptance',mismatches:[]}}:{{ok:true,state:'READY',rows:[],items:[]}};return new Response(JSON.stringify(d),{{status:200,headers:{{'Content-Type':'application/json'}}}});}};</script><script>{js}</script>'''
    return index.replace('__APP__',stub)

with sync_playwright() as pw:
    b=pw.chromium.launch(headless=True,executable_path='/usr/bin/chromium',args=['--no-sandbox']); p=b.new_page(viewport={'width':1366,'height':768}); errors=[]; p.on('pageerror',lambda e: errors.append(str(e)))
    p.set_content(html(),wait_until='load'); p.wait_for_timeout(1500)
    ck('frontend renders real backend partial coverage as provisional','PROVISIONAL' in p.locator('#watchNextMeta').inner_text().upper(),p.locator('#watchNextMeta').inner_text())
    ck('frontend suppresses partial evidence from real backend','—'==p.locator('#watchNextRows tr:has-text("SHRIRAMFIN") td:nth-child(5)').inner_text().strip(),p.locator('#watchNextRows').inner_text())
    p.evaluate("window.__phase='ready'"); p.wait_for_timeout(3300)
    rt=p.locator('#topEntriesRows').inner_text(); ck('frontend renders canonical READY from real backend route','RELIANCE' in rt and 'READY' in rt and '₹1,484.00' in rt and '₹1,532.00' in rt,rt); p.screenshot(path=str(SHOTS/'01_ready.png'))
    p.evaluate("window.__phase='active'"); p.wait_for_timeout(3300); at=p.locator('#topEntriesRows').inner_text(); ck('frontend renders canonical ACTIVE from real backend route','ACTIVE' in at,at)
    p.evaluate("window.__phase='crossed'"); p.wait_for_timeout(3300); ct=p.locator('#topEntriesRows').inner_text(); ck('frontend cannot show clean ACTIVE after target cross','RECONCILE' in ct and 'ACTIVE' not in ct,ct); p.screenshot(path=str(SHOTS/'02_target_cross_reconcile.png'))
    p.evaluate('window.__fail=true'); p.wait_for_timeout(4300); ck('real-route browser outage fails retained rows closed',p.locator('#trustState').inner_text().strip()=='DEGRADED' and 'NOT ACTIONABLE' in p.locator('#workspaceLiveState').inner_text(),p.locator('#globalNotice').inner_text())
    # True frontend restart against settled authority.
    p.set_content(html('settled'),wait_until='load'); p.wait_for_timeout(1400); ot=p.locator('#workspaceOutcomeRows').inner_text(); ck('settlement restart renders immutable Result Outcome After',all(x in ot for x in ('RELIANCE','TARGET HIT','SUCCESS','CONTINUED')),ot); p.screenshot(path=str(SHOTS/'03_settled_restart.png'))
    ck('integrated browser has no JavaScript page errors',not errors,errors); b.close()

passed=sum(c['ok'] for c in checks); failed=len(checks)-passed
payload={'ok':failed==0,'contract':'E2E_BACKEND_FRONTEND_WIRING_R3','passed':passed,'failed':failed,'checks':checks,'screenshots':str(SHOTS)}
OUT.write_text(json.dumps(payload,indent=2,default=str)+'\n'); print(json.dumps({k:v for k,v in payload.items() if k!='checks'},indent=2)); raise SystemExit(0 if payload['ok'] else 1)
