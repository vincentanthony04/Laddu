from __future__ import annotations
import json, re
from pathlib import Path
from datetime import datetime, timezone, timedelta
from playwright.sync_api import sync_playwright

ROOT=Path(__file__).resolve().parents[1]
FRONT=ROOT/'frontend'
OUT=Path('/mnt/data/LADDU_E2E_CUSTOMER_VERTICAL_BROWSER_PROOF.json')
SHOTS=Path('/mnt/data/LADDU_E2E_VERTICAL_SCREENSHOTS'); SHOTS.mkdir(exist_ok=True)
checks=[]
def check(name, ok, detail=''):
    checks.append({'name':name,'ok':bool(ok),'detail':detail})
    if not ok: print('FAIL',name,detail)

IST=timezone(timedelta(hours=5,minutes=30)); NOW=datetime(2026,8,19,19,7,tzinfo=IST)
def iso(dt): return dt.isoformat()
def ago(minutes=0,hours=0,days=0): return iso(NOW-timedelta(minutes=minutes,hours=hours,days=days))
def trust(state='TRUSTED',allowed=True,reason='data/read-model path current and no critical runtime blocker',stamp=None,sequence_us=None):
    seq = sequence_us if sequence_us is not None else (2000 if state=='TRUSTED' else 1000)
    return {'ok':True,'version':'trader-trust-state-1.0.0','evaluated_at':stamp or iso(NOW),'sequence_ns':seq*1000,'sequence_us':seq,'state':state,'decision_admission_allowed':allowed,'market_open':False,'reason':reason,'reasons':[reason],'latency':{'customer_read_p95_ms':580,'customer_read_samples':13}}
def coverage(delivery=(2560,4137),intraday=(2366,3364),complete=False):
    out={}
    for desk,(processed,total) in [('delivery',delivery),('intraday',intraday)]:
        done=complete or processed>=total
        out[desk]={'processed':processed,'total':total,'pct':round(processed*100/total,3),'complete':done,'state':'COMPLETE' if done else 'CONTINUING_SWEEP','ranking_scope':'FULL_UNIVERSE' if done else 'EVALUATED_SUBSET_ONLY'}
    return out
def candidate(symbol,mode,score,setup,stage='WATCH',complete=False,wait='Final admission evidence'):
    return {'symbol':symbol,'mode':mode,'rank_score':score,'evidence_score':score,'setup':setup,'candidate_stage':stage,'display_price':{'SHRIRAMFIN':1022.20,'IPCALAB':1903.70,'TECHM':1589.80}.get(symbol,500.0),'next_action':wait,'generated_at':ago(minutes=7),'instrument_key':f'NSE_EQ|{symbol}','research_only':True,'execution_price_authority':False if mode=='intraday' else True,'rank_readiness':'READY' if complete else 'PARTIAL','rank_scoring_state':'NORMAL' if complete else 'DEGRADED','rank_missing_inputs':[] if complete else ['sector_relative'],'freshness_state':'CURRENT' if complete else 'UNKNOWN'}
def final(stage='FINAL',price=1488.30):
    return {'symbol':'RELIANCE','mode':'delivery','setup':'Breakout retest','rank_score':88.2,'evidence_score':88.2,'display_price':price,'current_price':price,'entry':1484.00,'target':1532.00,'stop':1463.50,'reward_risk':2.34,'generated_at':ago(minutes=18),'holding_period':'5–10d','display_stage':stage,'status':stage,'final_signal_authority':'POSTGRESQL_CANONICAL_DECISION','decision_id':'decision-reliance-001','signal_id':'signal-reliance-001','instrument_key':'NSE_EQ|RELIANCE','final_signal_join_state':'EXACT_CANONICAL','research_only':False,'rank_readiness':'READY','rank_scoring_state':'NORMAL','rank_missing_inputs':[]}
def market_rows():
    return [{'name':'NIFTY','display_name':'NIFTY 50','ltp':24154.90,'change_pct':-0.55},{'name':'SENSEX','display_name':'SENSEX','ltp':77235.46,'change_pct':-0.63},{'name':'BANK','display_name':'NIFTY BANK','ltp':57262.40,'change_pct':-0.41},{'name':'VIX','display_name':'INDIA VIX','ltp':11.32,'change_pct':-0.61},{'name':'BREADTH','advances':104,'declines':88}]
def workspace(phase):
    closed=phase in ('closed_partial','settled'); finals=[]; candidates=[]; cov=coverage(); old=trust('TRUSTED',True)
    if phase=='closed_partial':
        old=trust('DO_NOT_TRUST',False,'index_levels no_progress',stamp=iso(NOW),sequence_us=1000)
        bad_final=final('FINAL'); bad_final.update({'symbol':'FUTUREBAD','instrument_key':'NSE_EQ|FUTUREBAD','decision_id':'decision-future-bad','signal_id':'signal-future-bad','generated_at':iso(datetime.now(IST)+timedelta(minutes=10))})
        finals=[bad_final]
        bad_watch=candidate('FUTUREWATCH','delivery',99,'Future timestamp candidate','WATCH',True,'Final admission evidence'); bad_watch['generated_at']=iso(datetime.now(IST)+timedelta(minutes=10))
        candidates=[candidate('SHRIRAMFIN','delivery',75,'Position/Delivery Accumulation Zone','SELECTED',False,'Last selected snapshot retained for continuity; not actionable'),candidate('IPCALAB','intraday',100,'ORB + VWAP Confirmation · Breakout / Retest','VALIDATING',True,'Live price confirmation'),candidate('TECHM','delivery',80.9,'VCP / Contraction · Breakout / Retest · Institutional Participation','WATCH',False),bad_watch]
    elif phase=='live_ready':
        cov=coverage((4137,4137),(3364,3364),True); finals=[final('FINAL')]; candidates=[candidate('IPCALAB','intraday',82,'ORB + VWAP Confirmation','QUALIFIED',True,'3m acceptance above trigger')]
    elif phase=='active':
        cov=coverage((4137,4137),(3364,3364),True); row=final('OPEN',1492.60); row.update({'position_id':'paper-pos-001','opened_at':ago(minutes=9),'position_age_seconds':540}); finals=[row]
    elif phase=='settled': cov=coverage((4137,4137),(3364,3364),True)
    return {'ok':True,'contract_version':'trader-workspace-1.5.0-live-truth-and-ranking-scope','server_time':iso(NOW),'market_state':'CLOSED' if closed else 'LIVE','market_open':not closed,'as_of':iso(NOW),'route_elapsed_ms':112.5,'projection_state':'READY','trust':old,'indices':market_rows(),'market_movers':{'advances':104,'declines':88,'top_gainers':[],'top_losers':[]},'coverage':cov,'mode_status':{},'final_signals':finals,'active':[],'preparing':[],'candidates':candidates,'counts':{'final_signals':len(finals),'active':1 if phase=='active' else 0,'preparing':0,'candidates':len(candidates),'universe':4137},'health':{'service':'running'}}
def live_state(phase):
    closed=phase in ('closed_partial','settled'); return {'ok':True,'contract_version':'trader-live-state-1.0.0','server_time':iso(NOW),'market_open':not closed,'market_state':'CLOSED' if closed else 'LIVE','trust':trust('TRUSTED',True,stamp=iso(NOW),sequence_us=2000)}
def perf(phase):
    rows=[]
    if phase=='settled': rows=[{'symbol':'RELIANCE','mode':'delivery','accuracy_eligible':True,'performance_eligible':True,'exit_reason':'TARGET_HIT','signal_outcome':'SUCCESS','economic_outcome':'SUCCESS','net_pnl':6120.0,'realized_r':2.31,'holding_minutes':7200,'closed_at':ago(minutes=2),'instrument_key':'NSE_EQ|RELIANCE','after_state':'CONTINUED'}]
    return {'ok':True,'state':'READY','canonical_lifecycle':{'records':rows,'overall':{'settled':len(rows)}}}

index=(FRONT/'index.html').read_text(); css=(FRONT/'app.css').read_text()+'\n'+(FRONT/'ui-system.css').read_text(); js=(FRONT/'app.js').read_text()
index=re.sub(r'<link[^>]+href="/app\.css[^>]*>','',index); index=re.sub(r'<link[^>]+href="/ui-system\.css[^>]*>','',index); index=index.replace('</head>',f'<style>{css}</style></head>'); index=re.sub(r'<script src="/app\.js[^>]*></script>','__APP__',index)
PHASES={p:{'workspace':workspace(p),'live':live_state(p),'performance':perf(p)} for p in ('closed_partial','live_ready','active','settled')}

def harness(initial='closed_partial'):
    mocks=json.dumps(PHASES)
    stub=f'''<script>window.__ladduPhase={json.dumps(initial)};window.__workspaceFail=false;window.__mocks={mocks};window.fetch=async function(input,options){{const raw=String(input);const path=raw.split('?')[0];const phase=window.__ladduPhase;let data;if(path==='/api/trader-workspace'){{if(window.__workspaceFail)return new Response(JSON.stringify({{ok:false,error:'simulated workspace outage'}}),{{status:503,headers:{{'Content-Type':'application/json'}}}});data=window.__mocks[phase].workspace;}}else if(path==='/api/trader-live-state')data=window.__mocks[phase].live;else if(path==='/api/performance')data=window.__mocks[phase].performance;else if(path==='/api/frontend-identity')data={{ok:true,version:'v131.0.0',manifest_version:'v131.0.0',frontend_owner:'standalone-v131.0.0',build_marker:'terminal-actionable-ui-r3-e2e-acceptance',mismatches:[]}};else data={{ok:true,state:'READY',rows:[],items:[]}};return new Response(JSON.stringify(data),{{status:200,headers:{{'Content-Type':'application/json'}}}});}};</script><script>{js}</script>'''
    return index.replace('__APP__',stub)

with sync_playwright() as pw:
    browser=pw.chromium.launch(headless=True, executable_path='/usr/bin/chromium', args=['--no-sandbox'])
    page=browser.new_page(viewport={'width':1366,'height':768})
    page.set_content(harness(),wait_until='load'); page.wait_for_timeout(1600)
    check('stale workspace trust cannot override newer trust authority',page.locator('#trustState').inner_text().strip()=='TRUSTED',page.locator('#trustReason').inner_text())
    check('market closed explicit','MARKET CLOSED' in page.locator('#workspaceLiveState').inner_text())
    check('future-dated canonical decision is fail-closed','FUTUREBAD' not in page.locator('#topEntriesRows').inner_text(),page.locator('#topEntriesRows').inner_text())
    meta=page.locator('#watchNextMeta').inner_text(); meta_upper=meta.upper(); check('partial ranking explicitly provisional','PROVISIONAL' in meta_upper and 'DELIVERY 61.9%' in meta_upper and 'INTRADAY 70.3%' in meta_upper,meta)
    check('partial candidates have no false global ordinal rank',page.locator('#watchNextRows tr').first.locator('td').first.inner_text().strip()=='•')
    partial_scores=[]
    for symbol in ('SHRIRAMFIN','TECHM'):
        row=page.locator(f'#watchNextRows tr:has-text("{symbol}")')
        partial_scores.append(row.locator('td:nth-child(5)').inner_text().strip())
    check('incomplete evidence score suppressed',all(x=='—' for x in partial_scores),str(partial_scores))
    partial_stages=[]
    for symbol in ('SHRIRAMFIN','TECHM'):
        partial_stages.append(page.locator(f'#watchNextRows tr:has-text("{symbol}") .simple-state').inner_text())
    check('incomplete evidence visibly labelled',all('EVIDENCE INCOMPLETE' in x for x in partial_stages),str(partial_stages))
    ipc=page.locator('#watchNextRows tr:has-text("IPCALAB")')
    check('market-closed complete candidate never says LIVE VALIDATION','NEXT SESSION' in ipc.locator('.simple-state').inner_text() and 'LIVE VALIDATION' not in ipc.locator('.simple-state').inner_text(),ipc.locator('.simple-state').inner_text())
    fw=page.locator('#watchNextRows tr:has-text("FUTUREWATCH")')
    check('future-dated research evidence loses score and authority',fw.locator('td:nth-child(5)').inner_text().strip()=='—' and 'EVIDENCE INCOMPLETE' in fw.locator('.simple-state').inner_text(),fw.inner_text())
    check('empty outcomes collapses',page.locator('#recentOutcomesPanel').is_hidden())
    box=page.locator('#actionablePanel').bounding_box(); check('empty actionable collapses',bool(box and box['height']<150),str(box))
    check('diagnostics secondary',page.locator('#progressQuick').inner_text().strip()=='Diagnostics')
    dims=page.evaluate('({sw:document.documentElement.scrollWidth,iw:innerWidth,bw:document.body.getBoundingClientRect().width})'); check('1366 viewport no document overflow',dims['sw']<=dims['iw']+2 and dims['bw']>=dims['iw']-2,str(dims)); page.screenshot(path=str(SHOTS/'01_closed_partial_1366x768.png'),full_page=False)

    page.evaluate("window.__ladduPhase='live_ready'"); page.wait_for_timeout(3400)
    check('full sweep rank semantics','FULL SWEEP' in page.locator('#watchNextMeta').inner_text(),page.locator('#watchNextMeta').inner_text())
    check('canonical Final appears Actionable','RELIANCE' in page.locator('#topEntriesRows').inner_text() and 'READY' in page.locator('#topEntriesRows').inner_text())
    check('live system trust current',page.locator('#trustState').inner_text().strip()=='TRUSTED' and 'LIVE' in page.locator('#workspaceLiveState').inner_text()); page.screenshot(path=str(SHOTS/'02_live_ready_1366x768.png'),full_page=False)

    page.evaluate("window.__ladduPhase='active'"); page.wait_for_timeout(3400); check('same canonical decision transitions ACTIVE','RELIANCE' in page.locator('#topEntriesRows').inner_text() and 'ACTIVE' in page.locator('#topEntriesRows').inner_text())
    page.evaluate('window.__workspaceFail=true'); page.wait_for_timeout(4200)
    check('workspace outage marks retained rows stale/non-actionable',page.locator('#trustState').inner_text().strip()=='DEGRADED' and 'STALE' in page.locator('#globalNotice').inner_text(),page.locator('#globalNotice').inner_text())
    check('workspace outage cannot retain LIVE action authority','NOT ACTIONABLE' in page.locator('#workspaceLiveState').inner_text())
    page.evaluate('window.__workspaceFail=false'); page.wait_for_timeout(3400); check('workspace recovery restores current trust',page.locator('#trustState').inner_text().strip()=='TRUSTED' and 'ACTIVE' in page.locator('#topEntriesRows').inner_text())

    # True reload/restart simulation: new DOM/runtime reads the same persisted settled authority.
    page.set_content(harness('settled'),wait_until='load'); page.wait_for_timeout(2200)
    check('settled signal leaves Actionable',page.locator('#topEntriesRows .stock-link').count()==0)
    check('outcomes visible',not page.locator('#recentOutcomesPanel').is_hidden())
    ot=page.locator('#workspaceOutcomeRows').inner_text(); check('result TARGET HIT immutable','TARGET HIT' in ot); check('outcome SUCCESS','SUCCESS' in ot); check('After CONTINUED separate','CONTINUED' in ot); page.screenshot(path=str(SHOTS/'03_settled_1366x768.png'),full_page=False)
    page.set_content(harness('settled'),wait_until='load'); page.wait_for_timeout(2200); ot2=page.locator('#workspaceOutcomeRows').inner_text(); check('restart preserves settled truth',all(x in ot2 for x in ('RELIANCE','TARGET HIT','SUCCESS','CONTINUED')),ot2)

    for width,height in [(1600,900),(1920,1080)]:
        p=browser.new_page(viewport={'width':width,'height':height}); p.set_content(harness('live_ready'),wait_until='load'); p.wait_for_timeout(800); d=p.evaluate('({sw:document.documentElement.scrollWidth,iw:innerWidth,bw:document.body.getBoundingClientRect().width})'); check(f'{width}x{height} full viewport/no overflow',d['sw']<=d['iw']+2 and d['bw']>=d['iw']-2,str(d)); p.screenshot(path=str(SHOTS/f'live_ready_{width}x{height}.png'),full_page=False); p.close()
    browser.close()

passed=sum(c['ok'] for c in checks); failed=len(checks)-passed; result={'ok':failed==0,'contract':'E2E_CUSTOMER_VERTICAL_BROWSER_R3','passed':passed,'failed':failed,'checks':checks,'screenshots':str(SHOTS)}; OUT.write_text(json.dumps(result,indent=2)+'\n'); print(json.dumps({'ok':result['ok'],'passed':passed,'failed':failed},indent=2)); raise SystemExit(0 if result['ok'] else 1)
