from __future__ import annotations
import hashlib,json,subprocess
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
def sha(p): return hashlib.sha256(p.read_bytes()).hexdigest()
def check(n,o,d,c,f): c.append({'gate':n,'state':'PASS' if o else 'FAIL','detail':d}); f.extend([] if o else [n+':'+d])
def main():
 c=[];f=[]; ident=json.loads((ROOT/'RELEASE_IDENTITY.json').read_text(encoding='utf-8-sig')); rev=str(ident.get('candidate_revision') or '').upper(); js=(ROOT/'frontend/app.js').read_text(); css=(ROOT/'frontend/app.css').read_text(); html=(ROOT/'frontend/index.html').read_text()
 check('R34_OR_DESCENDANT',rev in {'R34','R35','R36','R37'},'R34 customer closure or bounded R35/R36/R37 descendant',c,f)
 check('PRIMARY_SR_RETAINED',"addPriceLine(baseSupport, 'S · SUPPORT', p.green, 1, 0)" in js or "addPriceLine(baseSupport, 'SUPPORT', p.green, 1, 0)" in js,'one thin solid primary support remains',c,f)
 check('PRIMARY_RESISTANCE_RETAINED',"addPriceLine(baseResistance, 'R · RESISTANCE', p.red, 1, 0)" in js or "addPriceLine(baseResistance, 'RESISTANCE', p.red, 1, 0)" in js,'one thin solid primary resistance remains',c,f)
 check('MAJOR_PAIR_RETAINED',('MAJOR S' in js and 'MAJOR R' in js),'major pair remains customer-visible when structural evidence exists',c,f)
 check('CAMARILLA_HIDDEN','data-overlay="camarilla"' not in html,'Camarilla remains hidden from customer chart',c,f)
 check('PROGRESSIVE_DECISION_PROOF','proof-downstream' in js and 'customerNextRequirement' in js,'progressive decision proof retained',c,f)
 check('ALL_10_TIMEFRAMES',all(x in js for x in ["['1m', '1minute']","['3m', '3minute']","['5m', '5minute']","['15m', '15minute']","['30m', '30minute']","['1H', '60minute']","['4H', '240minute']","['1D', 'day']","['1W', 'week']","['1M', 'month']"]),'all ten timeframe controls retained',c,f)
 node=subprocess.run(['node','--check',str(ROOT/'frontend/app.js')],capture_output=True,text=True); check('JS_SYNTAX',node.returncode==0,'frontend parses cleanly',c,f)
 r={'ok':not f,'scope':'R34_CUSTOMER_UI_BASE_OR_DESCENDANT','checks':c,'passed':sum(x['state']=='PASS' for x in c),'failed':sum(x['state']=='FAIL' for x in c),'failures':f,'production_ready':False,'broker_authority':'NONE'}; print(json.dumps(r,indent=2)); return 0 if not f else 2
if __name__=='__main__': raise SystemExit(main())
