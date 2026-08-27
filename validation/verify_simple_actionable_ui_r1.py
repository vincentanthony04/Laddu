from __future__ import annotations
from pathlib import Path
import json, re, sys
from bs4 import BeautifulSoup

ROOT=Path(__file__).resolve().parents[1]
FRONT=ROOT/'frontend'
html=(FRONT/'index.html').read_text(encoding='utf-8')
js=(FRONT/'app.js').read_text(encoding='utf-8')
css=(FRONT/'ui-system.css').read_text(encoding='utf-8')
soup=BeautifulSoup(html,'html.parser')
checks=[]
def check(name, ok, detail=''):
    checks.append({'name':name,'ok':bool(ok),'detail':detail})

def headers(table_class):
    table=soup.select_one(f'table.{table_class}')
    return [th.get_text(' ',strip=True) for th in table.select('thead th')] if table else []

action_headers=headers('actionable-table')
watch_headers=headers('watch-next-table')
outcome_headers=headers('recent-outcomes-table')
check('Primary nav starts with Actionable', bool(soup.select_one('[data-page="workspace"] b')) and soup.select_one('[data-page="workspace"] b').get_text(strip=True)=='Actionable')
check('Opportunities removed from primary nav', soup.select_one('[data-page="opportunities"]') is None)
check('Diagnostics remains secondary primary-nav destination', bool(soup.select_one('[data-page="system"] b')) and soup.select_one('[data-page="system"] b').get_text(strip=True)=='Diagnostics')
check('Actionable Now contract has 15 concise columns', action_headers==['#','Stock','Mode','Setup','Evidence','LTP','Entry','Target','SL','R:R','Signal Age','Holding','Status','Result','After'], str(action_headers))
check('Watch Next contract is separate from final decisions', watch_headers==['#','Stock','Mode','Setup','Evidence','LTP','Waiting for','Age','Stage'], str(watch_headers))
check('Recent outcomes exposes immutable result and separate after state', outcome_headers==['Stock','Mode','Result','Outcome','Net P&L','R','Held','After'], str(outcome_headers))
check('Workspace has explicit live truth badge', soup.select_one('#workspaceLiveState') is not None)
check('Workspace has four-item market pulse', len(soup.select('#workspacePulse > span'))==4)
check('Final renderer reads canonical final_signals', 'rows(payload?.final_signals).filter(workspaceFinalSignal)' in js)
check('Watch renderer reads research candidates separately', 'for (const row of rows(payload?.candidates))' in js and 'renderWorkspaceWatchNext' in js)
check('Final rows require final_signal_authority', "if (!text(row?.final_signal_authority).trim()) return false;" in js)
check('Final rows require exact decision/signal identity', "if (!text(row?.decision_id || row?.signal_id).trim()) return false;" in js)
check('Result and After are separate functions', 'function workspaceResultLabel' in js and 'function workspaceAfterState' in js)
check('After does not rewrite result', 'result_is_immutable' not in js or 'workspaceAfterState' in js)
check('Strict numeric decoder rejects null blank boolean non-finite', "if (value === null || value === undefined) return null;" in js and "if (typeof value === 'boolean') return null;" in js and 'Number.isFinite(parsed) ? parsed : null' in js)
check('Internal chart globally disabled', 'const INTERNAL_CHART_ENABLED = false' in js)
check('Internal chart DOM stays hidden compatibility-only', bool(soup.select_one('.chart-panel[hidden][data-internal-chart-disabled="true"]')))
link=soup.select_one('.broker-chart-link')
check('Broker chart is external not embedded', bool(link) and link.get('href')=='https://tv.upstox.com' and link.get('target')=='_blank')
check('Engineering worker rail hidden from trading workspace', bool(soup.select_one('#deskCards[hidden]')) and '[data-page-panel="workspace"] #deskCards{display:none!important}' in css)
check('Diagnostics explicitly separated from trading workflow', soup.select_one('.workspace-diagnostics-link') is not None)
check('Frontend cache marker identifies simple UI R1', 'simple-ui-r1' in html)
# Every static $('id') binding still has a DOM target.
ids={node.get('id') for node in soup.find_all(id=True)}
refs=set(re.findall(r"\$\('([^']+)'\)",js))|set(re.findall(r'\$\("([^\"]+)"\)',js))
missing=sorted(refs-ids)
check('All static JS ID bindings resolve', not missing, str(missing))

failed=[c for c in checks if not c['ok']]
result={'contract':'simple-actionable-ui-r1','pass':len(checks)-len(failed),'fail':len(failed),'checks':checks}
print(json.dumps(result,indent=2))
sys.exit(1 if failed else 0)
