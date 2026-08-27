from __future__ import annotations
import json, re
from pathlib import Path
from playwright.sync_api import sync_playwright
ROOT=Path(__file__).resolve().parents[1]; FRONT=ROOT/'frontend';
import sys
sys.path.insert(0,str(ROOT/'backend'))
import config
FIX=ROOT/'validation/fixtures/r2_installed_truth_regression_20260819.json'; OUT=Path('/mnt/data/LADDU_USER_R2_TRUTH_REGRESSION_BROWSER_R3.json')
fixture=json.loads(FIX.read_text()); rt=fixture['runtime']
checks=[]
def check(name,ok,detail=None): checks.append({'name':name,'ok':bool(ok),'detail':detail}); print('FAIL',name,detail) if not ok else None
live_trust=dict(rt['trust']); live_trust.update({'evaluated_at':rt['time'],'sequence_us':2000})
stale_trust={'ok':True,'state':'DO_NOT_TRUST','decision_admission_allowed':False,'reason':'index_levels no_progress','reasons':['index_levels no_progress'],'market_open':False,'evaluated_at':'2026-08-19T19:00:00+05:30','sequence_us':1000}
cov={}
for desk,row in rt['coverage'].items():
    p=row['completed']; t=row['total']; cov[desk]={'processed':p,'total':t,'pct':round(p*100/t,3),'complete':False,'ranking_scope':'EVALUATED_SUBSET_ONLY','state':row['stage']}
workspace={'ok':True,'contract_version':'trader-workspace-1.5.0-live-truth-and-ranking-scope','server_time':rt['time'],'as_of':rt['time'],'market_open':False,'market_state':'CLOSED','trust':stale_trust,'coverage':cov,'final_signals':[],'candidates':[{'symbol':'SHRIRAMFIN','mode':'delivery','rank_score':75,'evidence_score':75,'setup':'Position/Delivery Accumulation Zone','display_price':1022.20,'generated_at':'2026-08-19T18:55:00+05:30','rank_readiness':'PARTIAL','rank_scoring_state':'DEGRADED','freshness_state':'UNKNOWN','rank_missing_inputs':['sector_relative'],'candidate_stage':'VALIDATING','next_action':'Live price confirmation'}],'indices':[{'name':'NIFTY','display_name':'NIFTY 50','ltp':24154.9,'change_pct':-.55},{'name':'VIX','display_name':'INDIA VIX','ltp':11.32,'change_pct':-.61},{'name':'BREADTH','advances':104,'declines':88}],'market_movers':{'advances':104,'declines':88},'counts':{'final_signals':0,'active':0,'preparing':0,'candidates':1,'universe':4137},'mode_status':{}}
live={'ok':True,'contract_version':'trader-live-state-1.0.0','server_time':rt['time'],'market_open':False,'market_state':'CLOSED','trust':live_trust}
index=(FRONT/'index.html').read_text(); css=(FRONT/'app.css').read_text()+'\n'+(FRONT/'ui-system.css').read_text(); js=(FRONT/'app.js').read_text()
index=re.sub(r'<link[^>]+href="/app\.css[^>]*>','',index); index=re.sub(r'<link[^>]+href="/ui-system\.css[^>]*>','',index); index=index.replace('</head>',f'<style>{css}</style></head>'); index=re.sub(r'<script src="/app\.js[^>]*></script>','__APP__',index)
mock=json.dumps({'workspace':workspace,'live':live})
stub=f'''<script>window.__m={mock};window.fetch=async function(i){{const p=String(i).split('?')[0];let d;if(p==='/api/trader-workspace')d=window.__m.workspace;else if(p==='/api/trader-live-state')d=window.__m.live;else if(p==='/api/frontend-identity')d={{ok:true,version:'v131.0.0',manifest_version:'v131.0.0',frontend_owner:'standalone-v131.0.0',build_marker:{json.dumps(config.BUILD_MARKER)},mismatches:[]}};else if(p==='/api/performance')d={{ok:true,canonical_lifecycle:{{records:[]}}}};else d={{ok:true,rows:[]}};return new Response(JSON.stringify(d),{{status:200,headers:{{'Content-Type':'application/json'}}}})}};</script><script>{js}</script>'''
html=index.replace('__APP__',stub)
with sync_playwright() as pw:
    b=pw.chromium.launch(headless=True,executable_path='/usr/bin/chromium',args=['--no-sandbox']); p=b.new_page(viewport={'width':1366,'height':768}); p.set_content(html,wait_until='load'); p.wait_for_timeout(1800)
    details=p.locator('#watchNextPanel details');
    if details.count(): details.evaluate('(e)=>e.open=true')
    trust=p.locator('#trustState').inner_text().strip(); reason=p.locator('#trustReason').inner_text(); meta=p.locator('#watchNextMeta').inner_text(); row=p.locator('#watchNextRows tr').first
    check('actual installed evidence newer TRUSTED wins over stale browser projection',trust=='TRUSTED' and 'no_progress' not in reason,{'trust':trust,'reason':reason,'runtime':live_trust})
    check('actual incomplete coverage is explicitly provisional','PROVISIONAL' in meta.upper() and '61.9' in meta and '70.3' in meta,meta)
    check('provisional candidate has no false global ordinal',row.locator('td').first.inner_text().strip()=='•',row.inner_text())
    check('partial unknown evidence cannot display 75 as complete score',row.locator('td:nth-child(5)').inner_text().strip()=='—',row.inner_text())
    check('market closed candidate cannot say LIVE VALIDATION','LIVE VALIDATION' not in row.inner_text().upper(),row.inner_text())
    check('market closed truth explicit','MARKET CLOSED' in p.locator('#workspaceLiveState').inner_text().upper(),p.locator('#workspaceLiveState').inner_text())
    b.close()
passed=sum(x['ok'] for x in checks); failed=len(checks)-passed; res={'ok':failed==0,'contract':'USER_R2_INSTALLED_TRUTH_REGRESSION_BROWSER_R3','source_fixture':str(FIX),'passed':passed,'failed':failed,'checks':checks}; OUT.write_text(json.dumps(res,indent=2)+'\n'); print(json.dumps({k:v for k,v in res.items() if k!='checks'},indent=2)); raise SystemExit(0 if res['ok'] else 1)
