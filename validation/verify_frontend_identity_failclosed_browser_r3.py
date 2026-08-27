from __future__ import annotations
import json,re,sys
from pathlib import Path
from datetime import datetime,timezone,timedelta
from playwright.sync_api import sync_playwright
ROOT=Path(__file__).resolve().parents[1]; FRONT=ROOT/'frontend'
html=(FRONT/'index.html').read_text(); css=(FRONT/'app.css').read_text()+'\n'+(FRONT/'ui-system.css').read_text(); js=(FRONT/'app.js').read_text()
html=re.sub(r'<link[^>]+href="/app\.css[^>]*>','',html); html=re.sub(r'<link[^>]+href="/ui-system\.css[^>]*>','',html); html=html.replace('</head>',f'<style>{css}</style></head>'); html=re.sub(r'<script src="/app\.js[^>]*></script>','__APP__',html)
now=datetime(2026,8,19,11,0,tzinfo=timezone(timedelta(hours=5,minutes=30))).isoformat()
trust={'state':'TRUSTED','decision_admission_allowed':True,'evaluated_at':now,'sequence_us':20,'reason':'runtime trusted'}
final={'symbol':'RELIANCE','mode':'delivery','setup':'Breakout retest','rank_score':88,'evidence_score':88,'display_price':1490,'entry':1484,'target':1532,'stop':1463.5,'generated_at':'2026-08-19T10:50:00+05:30','holding_period':'5-10d','display_stage':'FINAL','status':'FINAL','final_signal_authority':'POSTGRESQL_CANONICAL_DECISION','decision_id':'d1','signal_id':'s1','instrument_key':'NSE_EQ|RELIANCE','research_only':False,'rank_readiness':'READY','rank_scoring_state':'NORMAL','rank_missing_inputs':[]}
workspace={'ok':True,'contract_version':'trader-workspace-1.5.0-live-truth-and-ranking-scope','market_state':'LIVE','market_open':True,'as_of':now,'route_elapsed_ms':20,'trust':trust,'indices':[],'market_movers':{},'coverage':{'delivery':{'processed':4137,'total':4137,'pct':100,'complete':True,'ranking_scope':'FULL_UNIVERSE'},'intraday':{'processed':3364,'total':3364,'pct':100,'complete':True,'ranking_scope':'FULL_UNIVERSE'}},'final_signals':[final],'candidates':[],'counts':{'final_signals':1,'active':0,'candidates':0,'universe':4137}}
live={'ok':True,'contract_version':'trader-live-state-1.0.0','market_open':True,'market_state':'LIVE','trust':trust}
identity={'ok':True,'version':'v131.0.0','manifest_version':'v131.0.0','frontend_owner':'standalone-v131.0.0','build_marker':'WRONG-BUILD','mismatches':[]}
stub=f'''<script>window.fetch=async function(input){{const p=String(input).split('?')[0];let d=p==='/api/trader-workspace'?{json.dumps(workspace)}:p==='/api/trader-live-state'?{json.dumps(live)}:p==='/api/frontend-identity'?{json.dumps(identity)}:p==='/api/performance'?{{ok:true,state:'READY',canonical_lifecycle:{{records:[]}}}}:{{ok:true}};return new Response(JSON.stringify(d),{{status:200,headers:{{'Content-Type':'application/json'}}}})}};</script><script>{js}</script>'''
page_html=html.replace('__APP__',stub)
checks=[]
def ck(name,ok,detail=''): checks.append({'name':name,'ok':bool(ok),'detail':detail})
with sync_playwright() as pw:
    b=pw.chromium.launch(headless=True,executable_path='/usr/bin/chromium',args=['--no-sandbox']); p=b.new_page(viewport={'width':1366,'height':768}); p.set_content(page_html,wait_until='load'); p.wait_for_timeout(1800)
    ck('visible identity failure','IDENTITY FAIL' in p.locator('#versionPill').inner_text(),p.locator('#versionPill').inner_text())
    ck('trust fail closed despite trusted backend',p.locator('#trustState').inner_text().strip()=='DO NOT TRUST',p.locator('#trustState').inner_text())
    ck('live authority suppressed','NOT ACTIONABLE' in p.locator('#workspaceLiveState').inner_text(),p.locator('#workspaceLiveState').inner_text())
    ck('canonical row not presented actionable while frontend identity invalid',p.locator('#topEntriesRows tr.actionable-row').count()==0,p.locator('#topEntriesRows').inner_text())
    b.close()
failed=[x for x in checks if not x['ok']]; out={'ok':not failed,'contract':'FRONTEND_IDENTITY_FAILCLOSED_BROWSER_R3','passed':len(checks)-len(failed),'failed':len(failed),'checks':checks}; Path('/mnt/data/LADDU_FRONTEND_IDENTITY_FAILCLOSED_BROWSER_R3.json').write_text(json.dumps(out,indent=2)+'\n'); print(json.dumps(out,indent=2)); sys.exit(1 if failed else 0)
