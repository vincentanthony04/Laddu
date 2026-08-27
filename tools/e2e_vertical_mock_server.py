from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs
import json, mimetypes, threading
from datetime import datetime, timezone, timedelta

ROOT = Path(__file__).resolve().parents[1] / 'frontend'
IST = timezone(timedelta(hours=5, minutes=30))
LOCK = threading.RLock()
STATE = {'phase': 'closed_partial', 'workspace_fail': False}
NOW = datetime(2026, 8, 19, 19, 7, tzinfo=IST)

def iso(dt): return dt.isoformat()
def ago(minutes=0, hours=0, days=0): return iso(NOW - timedelta(minutes=minutes, hours=hours, days=days))

def trust(state='TRUSTED', allowed=True, reason='data/read-model path current and no critical runtime blocker', stamp=None):
    return {
        'ok': True, 'version': 'trader-trust-state-1.0.0', 'evaluated_at': stamp or iso(NOW),
        'sequence_ns': 2000 if state == 'TRUSTED' else 1000,
        'state': state, 'decision_admission_allowed': allowed, 'market_open': False,
        'reason': reason, 'reasons': [reason], 'latency': {'customer_read_p95_ms': 580, 'customer_read_samples': 13},
    }

def coverage(delivery=(2560,4137), intraday=(2366,3364), complete=False):
    out = {}
    for desk, vals in [('delivery', delivery), ('intraday', intraday)]:
        processed,total=vals
        out[desk]={'processed':processed,'total':total,'pct':round(processed*100/total,3),'complete':complete or processed>=total,'state':'COMPLETE' if complete or processed>=total else 'CONTINUING_SWEEP','ranking_scope':'FULL_UNIVERSE' if complete or processed>=total else 'EVALUATED_SUBSET_ONLY'}
    return out

def candidate(symbol, mode, score, setup, stage='WATCH', complete=False, wait='Final admission evidence'):
    row={
        'symbol':symbol,'mode':mode,'rank_score':score,'evidence_score':score,'setup':setup,'candidate_stage':stage,
        'display_price': {'SHRIRAMFIN':1022.20,'IPCALAB':1903.70,'TECHM':1589.80,'SUNPHARMA':1990.50,'TFCILTD':131.41}.get(symbol,500.0),
        'next_action':wait,'generated_at':ago(minutes=7),'instrument_key':f'NSE_EQ|{symbol}',
        'research_only':True,'execution_price_authority':False if mode=='intraday' else True,
        'rank_readiness':'READY' if complete else 'PARTIAL',
        'rank_scoring_state':'NORMAL' if complete else 'DEGRADED',
        'rank_missing_inputs':[] if complete else ['sector_relative'],
        'freshness_state':'CURRENT' if complete else 'UNKNOWN',
    }
    return row

def final(symbol='RELIANCE', stage='FINAL', price=1488.30):
    return {
        'symbol':symbol,'mode':'delivery','setup':'Breakout retest','rank_score':88.2,'evidence_score':88.2,
        'display_price':price,'current_price':price,'entry':1484.00,'target':1532.00,'stop':1463.50,'reward_risk':2.34,
        'generated_at':ago(minutes=18),'holding_period':'5–10d','display_stage':stage,'status':stage,
        'final_signal_authority':'POSTGRESQL_CANONICAL_DECISION','decision_id':'decision-reliance-001','signal_id':'signal-reliance-001',
        'instrument_key':'NSE_EQ|RELIANCE','final_signal_join_state':'EXACT_CANONICAL','research_only':False,
        'rank_readiness':'READY','rank_scoring_state':'NORMAL','rank_missing_inputs':[],
    }

def market_rows():
    return [
        {'name':'NIFTY','display_name':'NIFTY 50','ltp':24154.90,'change_pct':-0.55},
        {'name':'SENSEX','display_name':'SENSEX','ltp':77235.46,'change_pct':-0.63},
        {'name':'BANK','display_name':'NIFTY BANK','ltp':57262.40,'change_pct':-0.41},
        {'name':'VIX','display_name':'INDIA VIX','ltp':11.32,'change_pct':-0.61},
        {'name':'BREADTH','advances':104,'declines':88},
    ]

def workspace_for_phase(phase):
    closed = phase.startswith('closed') or phase in {'settled'}
    market_state='CLOSED' if closed else 'LIVE'
    final_rows=[]
    candidates=[]
    cov=coverage()
    if phase == 'closed_partial':
        candidates=[
            candidate('SHRIRAMFIN','delivery',75,'Position/Delivery Accumulation Zone',stage='SELECTED',complete=False,wait='Last selected snapshot retained for continuity; not actionable'),
            candidate('IPCALAB','intraday',100,'ORB + VWAP Confirmation · Breakout / Retest',stage='VALIDATING',complete=False,wait='Live price confirmation'),
            candidate('TECHM','delivery',80.9,'VCP / Contraction · Breakout / Retest · Institutional Participation',complete=False),
        ]
        old_trust=trust('DO_NOT_TRUST',False,'index_levels no_progress',stamp=ago(minutes=8))
    elif phase == 'live_ready':
        cov=coverage((4137,4137),(3364,3364),True)
        final_rows=[final(stage='FINAL')]
        candidates=[candidate('IPCALAB','intraday',82.0,'ORB + VWAP Confirmation',stage='QUALIFIED',complete=True,wait='3m acceptance above trigger')]
        old_trust=trust('TRUSTED',True)
    elif phase == 'active':
        cov=coverage((4137,4137),(3364,3364),True)
        row=final(stage='OPEN',price=1492.60); row.update({'position_id':'paper-pos-001','opened_at':ago(minutes=9),'position_age_seconds':540})
        final_rows=[row]
        old_trust=trust('TRUSTED',True)
    elif phase == 'settled':
        cov=coverage((4137,4137),(3364,3364),True)
        old_trust=trust('TRUSTED',True)
    else:
        old_trust=trust('TRUSTED',True)
    return {
        'ok':True,'contract_version':'trader-workspace-1.5.0-live-truth-and-ranking-scope','server_time':iso(NOW),
        'market_state':market_state,'market_open':not closed,'as_of':iso(NOW),'route_elapsed_ms':112.5,
        'projection_state':'READY','trust':old_trust,'indices':market_rows(),'market_movers':{'advances':104,'declines':88,'top_gainers':[],'top_losers':[]},
        'coverage':cov,'mode_status':{},'final_signals':final_rows,'active':[],'preparing':[],'candidates':candidates,
        'counts':{'final_signals':len(final_rows),'active':1 if phase=='active' else 0,'preparing':0,'candidates':len(candidates),'universe':4137},
        'health':{'service':'running'},
    }

def live_state_for_phase(phase):
    closed = phase.startswith('closed') or phase == 'settled'
    return {
        'ok':True,'contract_version':'trader-live-state-1.0.0','server_time':iso(NOW),
        'market_open':not closed,'market_state':'CLOSED' if closed else 'LIVE',
        'trust':trust('TRUSTED',True,'data/read-model path current and no critical runtime blocker',stamp=iso(NOW)),
    }

def performance_for_phase(phase):
    records=[]
    if phase=='settled':
        records=[{
            'symbol':'RELIANCE','mode':'delivery','accuracy_eligible':True,'performance_eligible':True,
            'exit_reason':'TARGET_HIT','signal_outcome':'SUCCESS','economic_outcome':'SUCCESS',
            'net_pnl':6120.00,'gross_pnl':6410.00,'costs':290.00,'realized_r':2.31,'holding_minutes':7200,
            'closed_at':ago(minutes=2),'instrument_key':'NSE_EQ|RELIANCE','entry':1484.00,'exit':1532.00,'quantity':100,
            'after_state':'CONTINUED'
        }]
    return {'ok':True,'state':'READY','canonical_lifecycle':{'records':records,'overall':{'settled':len(records)}}}

class H(BaseHTTPRequestHandler):
    def _json(self,obj,status=200):
        data=json.dumps(obj).encode('utf-8'); self.send_response(status); self.send_header('Content-Type','application/json'); self.send_header('Cache-Control','no-store'); self.send_header('Content-Length',str(len(data))); self.end_headers(); self.wfile.write(data)
    def do_GET(self):
        parsed=urlparse(self.path); path=parsed.path; qs=parse_qs(parsed.query)
        with LOCK: phase=STATE['phase']; fail=STATE['workspace_fail']
        if path=='/__test__/state': return self._json(dict(STATE))
        if path=='/api/trader-workspace':
            if fail: return self._json({'ok':False,'error':'simulated workspace outage'},503)
            return self._json(workspace_for_phase(phase))
        if path=='/api/trader-live-state': return self._json(live_state_for_phase(phase))
        if path=='/api/performance': return self._json(performance_for_phase(phase))
        if path=='/api/frontend-identity': return self._json({'version':'v131.0.0','build_marker':'e2e-acceptance-r3'})
        if path.startswith('/api/'): return self._json({'ok':True,'state':'READY','rows':[],'items':[]})
        rel='index.html' if path in ('/','') else path.lstrip('/')
        target=(ROOT/rel).resolve()
        if not str(target).startswith(str(ROOT.resolve())) or not target.exists(): self.send_response(404); self.end_headers(); return
        data=target.read_bytes(); c=mimetypes.guess_type(str(target))[0] or 'application/octet-stream'
        self.send_response(200); self.send_header('Content-Type',c); self.send_header('Cache-Control','no-store'); self.send_header('Content-Length',str(len(data))); self.end_headers(); self.wfile.write(data)
    def do_POST(self):
        parsed=urlparse(self.path); path=parsed.path; qs=parse_qs(parsed.query)
        if path=='/__test__/phase':
            phase=(qs.get('value') or [''])[0]
            with LOCK: STATE['phase']=phase
            return self._json({'ok':True,'phase':phase})
        if path=='/__test__/workspace-fail':
            value=(qs.get('value') or ['0'])[0]
            with LOCK: STATE['workspace_fail']=value in ('1','true','yes')
            return self._json({'ok':True,'workspace_fail':STATE['workspace_fail']})
        return self._json({'ok':True})
    def log_message(self,*args): pass

if __name__=='__main__': ThreadingHTTPServer(('127.0.0.1',8898),H).serve_forever()
