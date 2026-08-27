import re,json
from pathlib import Path
from playwright.sync_api import sync_playwright
ROOT=Path(__file__).resolve().parents[1]; F=ROOT/'frontend'
import sys
sys.path.insert(0,str(ROOT/'backend'))
import config
html=(F/'index.html').read_text(); css=(F/'app.css').read_text()+'\n'+(F/'ui-system.css').read_text(); js=(F/'app.js').read_text()
html=re.sub(r'<link[^>]+href="/app\.css[^>]*>','',html); html=re.sub(r'<link[^>]+href="/ui-system\.css[^>]*>','',html); html=html.replace('</head>',f'<style>{css}</style></head>'); html=re.sub(r'<script src="/app\.js[^>]*></script>','__APP__',html)
workspace={'ok':True,'contract_version':'trader-workspace-1.5.0-live-truth-and-ranking-scope','server_time':'2026-08-19T19:07:00+05:30','market_state':'CLOSED','market_open':False,'as_of':'2026-08-19T19:07:00+05:30','route_elapsed_ms':100,'projection_state':'READY','trust':{'ok':True,'state':'TRUSTED','decision_admission_allowed':True,'evaluated_at':'2026-08-19T19:07:00+05:30','sequence_us':2000,'reason':'current'},'indices':[],'market_movers':{},'coverage':{'delivery':{'processed':4137,'total':4137,'pct':100,'complete':True,'ranking_scope':'FULL_UNIVERSE'},'intraday':{'processed':3364,'total':3364,'pct':100,'complete':True,'ranking_scope':'FULL_UNIVERSE'}},'mode_status':{},'final_signals':[],'active':[],'preparing':[],'candidates':[],'counts':{'final_signals':0,'active':0,'preparing':0,'candidates':0,'universe':4137},'health':{'service':'running'}}
live={'ok':True,'market_open':False,'market_state':'CLOSED','trust':workspace['trust']}
# endpoint-specific safe shapes
responses={
'/api/trader-workspace':workspace,'/api/trader-live-state':live,'/api/frontend-identity':{'ok':True,'version':'v131.0.0','manifest_version':'v131.0.0','frontend_owner':'standalone-v131.0.0','build_marker':config.BUILD_MARKER,'mismatches':[]},
'/api/performance':{'ok':True,'state':'READY','canonical_lifecycle':{'records':[],'overall':{'settled':0}},'accuracy':{},'performance':{},'periods':[]},
'/api/model-portfolio':{'ok':True,'state':'READY','positions':[],'open_positions':[],'closed_positions':[],'items':[]},
'/api/research-lifecycle-reconciliation':{'ok':True,'state':'READY','by_desk':{}},
'/api/trader-research':{'ok':True,'state':'READY','delivery':{},'intraday':{}},
'/api/research-libraries':{'ok':True,'state':'READY','libraries':[]},
'/api/operations/summary':{'ok':True,'state':'READY','jobs':[],'counts':{}},
'/api/operations/jobs':{'ok':True,'state':'READY','jobs':[]},
'/api/operations/events':{'ok':True,'state':'READY','events':[]},
'/api/operations/logs':{'ok':True,'state':'READY','lines':[]},
'/api/operations/maturity-blockers':{'ok':True,'state':'READY','blockers':[]},
}
mock=json.dumps(responses)
stub=f'''<script>window.__responses={mock};window.fetch=async function(input){{const p=String(input).split('?')[0];const d=window.__responses[p]||{{ok:true,state:'READY',rows:[],items:[],records:[]}};return new Response(JSON.stringify(d),{{status:200,headers:{{'Content-Type':'application/json'}}}});}};</script><script>{js}</script>'''
html=html.replace('__APP__',stub)
with sync_playwright() as pw:
    b=pw.chromium.launch(headless=True,executable_path='/usr/bin/chromium',args=['--no-sandbox'])
    p=b.new_page(viewport={'width':1366,'height':768})
    errs=[]; console_errors=[]; checks=[]
    p.on('pageerror',lambda e: errs.append(str(e)))
    p.on('console',lambda m: console_errors.append(m.text) if m.type=='error' else None)
    p.set_content(html,wait_until='load'); p.wait_for_timeout(1200)
    for page_name in ['workspace','report','model-paper','accuracy','research','system']:
        if page_name != 'workspace':
            p.locator(f'[data-page="{page_name}"]').click(); p.wait_for_timeout(450)
        panel=p.locator(f'[data-page-panel="{page_name}"]')
        checks.append({'name':f'{page_name} primary page activates','ok':'active' in (panel.get_attribute('class') or '').split(),'detail':panel.get_attribute('class')})
    p.locator('[data-page="workspace"]').click(); p.wait_for_timeout(350)
    checks.append({'name':'all primary navigation renders without page exceptions','ok':not errs,'detail':errs})
    checks.append({'name':'all primary navigation renders without console errors','ok':not console_errors,'detail':console_errors})
    dims=p.evaluate('({sw:document.documentElement.scrollWidth,iw:innerWidth})')
    checks.append({'name':'navigation traversal keeps document inside viewport','ok':dims['sw']<=dims['iw']+2,'detail':dims})
    b.close()
passed=sum(x['ok'] for x in checks); failed=len(checks)-passed
out={'ok':failed==0,'contract':'UI_NAVIGATION_SMOKE_R3','passed':passed,'failed':failed,'checks':checks}
Path('/mnt/data/LADDU_UI_NAVIGATION_SMOKE_R3.json').write_text(json.dumps(out,indent=2)+'\n')
print(json.dumps({k:v for k,v in out.items() if k!='checks'},indent=2))
raise SystemExit(0 if out['ok'] else 1)
