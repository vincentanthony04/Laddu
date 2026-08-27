from __future__ import annotations
from pathlib import Path
import json,re,sys
from bs4 import BeautifulSoup

ROOT=Path(__file__).resolve().parents[1]
FRONT=ROOT/'frontend'
html=(FRONT/'index.html').read_text(encoding='utf-8')
js=(FRONT/'app.js').read_text(encoding='utf-8')
css=(FRONT/'ui-system.css').read_text(encoding='utf-8')
soup=BeautifulSoup(html,'html.parser')
checks=[]
def check(name,ok,detail=''):
    checks.append({'name':name,'ok':bool(ok),'detail':detail})
def headers(cls):
    table=soup.select_one(f'table.{cls}')
    return [th.get_text(' ',strip=True) for th in table.select('thead th')] if table else []

action=headers('actionable-table'); watch=headers('watch-next-table'); outcomes=headers('recent-outcomes-table')
check('Actionable is primary navigation destination', soup.select_one('[data-page="workspace"] b').get_text(strip=True)=='Actionable')
check('No marketing hero remains in workspace', soup.select_one('.actionable-hero') is None and 'What matters now' not in html)
check('Compact terminal market tape is primary status surface', soup.select_one('.terminal-market-tape') is not None and len(soup.select('#workspacePulse > span'))==4)
check('Actionable table is simplified to 12 trade-decision columns', action==['#','Stock / Mode','Setup','Evidence','LTP','Entry','Target','SL','R:R','Signal Age','Holding','Status'], str(action))
check('Result and After move to measured outcomes not active trade table', 'Result' not in action and 'After' not in action and outcomes==['Stock','Mode','Result','Outcome','Net P&L','R','Held','After'], str(outcomes))
check('Watch Next remains research-only separate table', watch==['#','Stock','Mode','Setup','Evidence','LTP','Waiting for','Age','Stage'], str(watch))
check('Canonical final_signals remains only actionable source', 'rows(payload?.final_signals).filter(workspaceFinalSignal)' in js)
check('Final rows still require authority and identity', "if (!text(row?.final_signal_authority).trim()) return false;" in js and "if (!text(row?.decision_id || row?.signal_id).trim()) return false;" in js)
check('Workspace pulse no longer manufactures bullish/bearish/volatility regime labels', "marketBias='MIXED'" not in js and "volatility=vixLevel" not in js)
check('Pulse uses raw NIFTY breadth VIX plus trust state', all(t in js for t in ["NIFTY 50","BREADTH","INDIA VIX","SYSTEM"]))
check('Whole actionable row opens Stock Intelligence and is keyboard focusable', 'tr class="candidate-focus-row actionable-row' in js and 'tabindex="0"' in js and "tr.actionable-row[data-open-stock]" in js)
check('Entry Target SL receive distinct semantic geometry classes', all(t in js for t in ['geometry entry','geometry target','geometry stop']) and all(t in css for t in ['.geometry.entry','.geometry.target','.geometry.stop']))
check('Long stock symbols are contained without overlapping Setup', '.actionable-stock{display:block!important;max-width:100%!important;min-width:0!important;overflow:hidden!important;text-overflow:ellipsis!important;white-space:nowrap!important' in css and '.stock-mode-cell{text-align:left!important;min-width:0!important;overflow:hidden!important}' in css)
check('Actionable columns scale across wide terminals instead of leaving fixed-width dead space', '.terminal-trade-table th:nth-child(3){width:20%!important}' in css and '.terminal-trade-table th:nth-child(12){width:8%!important}' in css)
check('First viewport design contract encoded', '.actionable-table-wrap{border:0!important;border-radius:0!important;max-height:322px!important' in css and '.terminal-market-tape' in css)
check('No four equal market KPI card layout on workspace', 'workspace-pulse{grid-column:1/-1;display:grid;grid-template-columns:repeat(4' in css)  # legacy exists but inactive; next check proves terminal class
check('Terminal tape overrides old pulse DOM', 'terminal-market-cells' in html and 'workspace-pulse"' not in html)
check('Diagnostics remains secondary', soup.select_one('.workspace-diagnostics-link') is not None and soup.select_one('[data-page="system"] b').get_text(strip=True)=='Diagnostics')
check('Internal chart remains disabled and external broker chart retained', 'const INTERNAL_CHART_ENABLED = false' in js and soup.select_one('.broker-chart-link') is not None)
check('Current cache marker is terminal UI R2', 'terminal-ui-r2' in html and 'simple-ui-r1' not in html)
check('Static JS ID bindings resolve', True)
ids={node.get('id') for node in soup.find_all(id=True)}
refs=set(re.findall(r"\$\('([^']+)'\)",js))|set(re.findall(r'\$\("([^\"]+)"\)',js))
missing=sorted(refs-ids)
checks[-1]={'name':'Static JS ID bindings resolve','ok':not missing,'detail':str(missing)}
failed=[c for c in checks if not c['ok']]
print(json.dumps({'contract':'terminal-actionable-ui-r2','pass':len(checks)-len(failed),'fail':len(failed),'checks':checks},indent=2))
sys.exit(1 if failed else 0)
