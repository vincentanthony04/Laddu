from __future__ import annotations
import hashlib, json, os, subprocess, sys
from datetime import datetime
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
EXPECTED_PARENT_SHA='22748384d5b4c204d8ab6ff7f67c48ab650a8796c095d9c432326a85603144d0'
FROZEN=json.loads((ROOT/'validation/r47_frozen_r46_hashes.json').read_text(encoding='utf-8'))
FUNCTIONAL_ALLOWED={
 'backend/core/intraday_session_structure_authority.py',
 'backend/market_layers.py','backend/core/intraday_session_policy.py','backend/core/nse_official_evidence_service.py',
 'backend/core/decision_engine_service.py','backend/engines.py','backend/models.py','backend/core/trade_geometry_authority.py',
 'backend/core/structural_trade_map_service.py','backend/runtime_discovery.py','backend/core/evidence_engine_service.py',
 'backend/core/vectorized_evidence_screening_service.py','backend/core/research_plane_contract.py',
 'backend/core/model_paper_lifecycle_authority.py','backend/core/model_portfolio_service.py','backend/core/outcome_accuracy_taxonomy.py',
 'backend/core/market_radar_service.py','backend/routes_get_system.py',
 'frontend/app.js','frontend/index.html','frontend/ui-system.css',
}
METADATA_ALLOWED={
 'RELEASE_IDENTITY.json','RELEASE_ATTESTATION.json','frontend/release-identity.json',
 'validation/package_allowlist.json','validation/package_manifest.sha256','validation/validate_deployable_candidate.py',
 'validation/r47_frozen_r46_hashes.json','validation/verify_r47_intraday_price_action_session_structure.py',
 'docs/R47_INTRADAY_PRICE_ACTION_SESSION_STRUCTURE.md',
}
ALLOWED=FUNCTIONAL_ALLOWED|METADATA_ALLOWED

def sha(path:Path)->str:return hashlib.sha256(path.read_bytes()).hexdigest()

def main()->int:
 failures=[]; checks=[]
 def check(name,ok,detail):
  checks.append({'gate':name,'state':'PASS' if ok else 'FAIL','detail':detail})
  if not ok: failures.append(f'{name}:{detail}')
 check('EXACT_R46_PARENT_ARCHIVE',FROZEN.get('parent_archive_sha256')==EXPECTED_PARENT_SHA,'R47 bound to exact R46 archive')
 missing=[];unexpected=[];changed=[]
 for rel,digest in dict(FROZEN.get('hashes') or {}).items():
  p=ROOT/rel
  if not p.is_file(): missing.append(rel); continue
  if sha(p)!=digest:
   (changed if rel in ALLOWED else unexpected).append(rel)
 check('R46_NON_REGRESSION',not missing and not unexpected,f'missing={len(missing)} unexpected_changed={unexpected}')
 check('DECLARED_FUNCTIONAL_BOUNDARY',set(changed).issubset(ALLOWED),f'changed={sorted(changed)}')
 research_protected=['backend/tools/refresh_research_catalog.py','backend/tools/train_nse_smart_model.py','backend/core/historical_pit_sweep_service.py','backend/core/selection_walk_forward_replay_service.py']
 bad=[rel for rel in research_protected if rel in FROZEN['hashes'] and sha(ROOT/rel)!=FROZEN['hashes'][rel]]
 check('R46_RESEARCH_WFA_FROZEN',not bad,f'changed={bad}')
 # runtime behavior imports
 sys.path.insert(0,str(ROOT/'backend'))
 from market_layers import support_resistance_levels, orb_context
 from core.intraday_session_policy import IntradaySessionPolicy
 from core.intraday_session_structure_authority import IntradaySessionStructureAuthority
 from core.trade_geometry_authority import TradeGeometryAuthority
 from core.outcome_accuracy_taxonomy import OutcomeAccuracyTaxonomy

 candles=[
  {'timestamp':'2026-08-18T09:15:00+05:30','open':100,'high':102,'low':99,'close':101,'volume':1000},
  {'timestamp':'2026-08-18T09:20:00+05:30','open':101.5,'high':103,'low':101,'close':102.5,'volume':1400},
  {'timestamp':'2026-08-18T09:25:00+05:30','open':102.2,'high':103.1,'low':101.8,'close':102.4,'volume':1200},
 ]
 at=lambda hhmmss:datetime.fromisoformat(f'2026-08-18T{hhmmss}+05:30')
 before=orb_context(candles,at('09:19:59')); ready=orb_context(candles,at('09:20:00'))
 check('ORB5_NOT_EARLY',before.get('phase')=='building_5m' and before.get('ok') is False,f'before={before.get("phase")}')
 check('ORB5_READY_EXACT_0920',ready.get('phase')=='orb5_ready' and ready.get('orb_high')==102.0 and ready.get('orb_low')==99.0 and ready.get('opening_range_minutes')==5,f'phase={ready.get("phase")} high={ready.get("orb_high")} low={ready.get("orb_low")}')
 policy=IntradaySessionPolicy()
 states={t:policy.at(at(t)) for t in ['09:19:00','09:20:00','14:15:00','14:30:00','15:00:00']}
 check('SESSION_0915_0920_OBSERVE_ONLY',states['09:19:00']['phase']=='ORB5_OBSERVE_ONLY' and not states['09:19:00']['new_entry_allowed'],str(states['09:19:00']))
 check('SESSION_ENTRY_FROM_0920',states['09:20:00']['phase']=='ENTRY_ALLOWED' and states['09:20:00']['new_entry_allowed'],str(states['09:20:00']))
 check('SESSION_1415_A_PLUS_ONLY',states['14:15:00']['phase']=='A_PLUS_ONLY' and states['14:15:00']['a_plus_only'] and states['14:15:00']['new_entry_allowed'],str(states['14:15:00']))
 check('SESSION_1430_NO_NEW_ENTRY',states['14:30:00']['phase']=='NO_NEW_INTRADAY' and not states['14:30:00']['new_entry_allowed'],str(states['14:30:00']))
 check('SESSION_1500_MANDATORY_FLAT',states['15:00:00']['phase']=='MANDATORY_FLAT' and states['15:00:00']['mandatory_flat'],str(states['15:00:00']))
 old_tokens=['14:45','15:05','15:12','15:15','mature_15m','building_15m']
 backend_text='\n'.join(p.read_text(encoding='utf-8-sig',errors='ignore') for p in (ROOT/'backend').rglob('*.py') if '__pycache__' not in p.parts)
 present=[token for token in old_tokens if token in backend_text]
 check('OLD_INTRADAY_TIME_POLICY_REMOVED',not present,f'old_tokens={present}')
 # native resistance cannot flip from location alone
 base=[]
 for i in range(38):
  base.append({'timestamp':f'2026-08-18T{9+(15+i)//60:02d}:{(15+i)%60:02d}:00+05:30','open':98.2,'high':99.0,'low':97.8,'close':98.5,'volume':1000})
 one=base+[{'timestamp':'2026-08-18T10:00:00+05:30','open':99.0,'high':102.3,'low':98.8,'close':102.0,'volume':1800}]
 two=one+[{'timestamp':'2026-08-18T10:01:00+05:30','open':102.0,'high':102.8,'low':101.7,'close':102.4,'volume':1700}]
 sr1=support_resistance_levels(one,prev_day_ohlc=(100,96,98)); sr2=support_resistance_levels(two,prev_day_ohlc=(100,96,98))
 def pdh(report):
  return next((r for r in list(report.get('ranked_levels') or []) if 'previous_day_high' in list(r.get('sources') or [])),None)
 r1,r2=pdh(sr1),pdh(sr2)
 check('ROLE_FLIP_FAIL_CLOSED_WITHOUT_ACCEPTANCE',bool(r1) and r1.get('native_kind')=='resistance' and r1.get('kind')=='resistance' and 'UNCONFIRMED' in str(r1.get('role_state')) and not r1.get('actionable'),str(r1))
 check('ROLE_FLIP_AFTER_ACCEPTANCE',bool(r2) and r2.get('native_kind')=='resistance' and r2.get('kind')=='support' and str(r2.get('role_state')).startswith('RESISTANCE_TO_SUPPORT_') and r2.get('validated'),str(r2))
 # session price-action convergence; official evidence may change confidence, not price geometry
 orb={'orb_high':102,'orb_low':99,'confirmed':True,'session_relative_volume':1.5,'participation_decision_usable':True,'previous_day_high':104,'previous_day_low':98}
 kwargs=dict(candles=candles,current_price=102.4,atr=1,ema20=101.95,ema50=101.5,vwap=101.98,orb=orb,historical_level_report={'ok':True,'support':[],'resistance':[]},market_structure={'bias':'long'},session_policy={'phase':'ENTRY_ALLOWED','new_entry_allowed':True})
 plain=IntradaySessionStructureAuthority.project(**kwargs)
 official={'state':'READY','decision_features':{'delivery_pct_surprise':1.2,'delivered_quantity_surprise':1.4,'nse_turnover_z20':1.1,'nse_trades_z20':.9},'risk_blocks':[]}
 rich=IntradaySessionStructureAuthority.project(**kwargs,official_nse_evidence=official)
 sources=set((rich.get('operating_support') or {}).get('sources') or [])
 check('SESSION_STRUCTURE_CONFLUENCE',rich.get('state')=='ACTIONABLE' and rich.get('long',{}).get('promotion_ready') and {'orb5_high_role_flip','session_vwap','ema20'}.issubset(sources),f'state={rich.get("state")} support={rich.get("operating_support")}')
 check('NSE_CONFIRMS_NEVER_MANUFACTURES_LEVEL',plain.get('support')==rich.get('support') and plain.get('resistance')==rich.get('resistance') and plain.get('long',{}).get('entry_trigger')==rich.get('long',{}).get('entry_trigger') and rich.get('long',{}).get('score',0)>plain.get('long',{}).get('score',0),f'plain={plain.get("long")} rich={rich.get("long")}')
 blocked=IntradaySessionStructureAuthority.project(**kwargs,official_nse_evidence={'state':'READY','risk_blocks':['SURVEILLANCE_ACTIVE']})
 check('NSE_RISK_BLOCKS_PROMOTION',not blocked.get('long',{}).get('promotion_ready') and 'SURVEILLANCE_ACTIVE' in blocked.get('blockers',[]),f'blockers={blocked.get("blockers")}')
 chased=IntradaySessionStructureAuthority.project(**{**kwargs,'current_price':104.5})
 check('NO_LATE_CHASE',chased.get('long',{}).get('extended') and not chased.get('long',{}).get('promotion_ready'),str(chased.get('long')))
 # structural invalidation and structure-first target
 level={'ok':True,'resistance':[{'price':104.1,'importance_score':80,'validated':True,'touches':3,'freshness':'CURRENT','timeframe':'SESSION','source_level':'session_resistance'}],'support':[]}
 geo=TradeGeometryAuthority.project(mode='intraday',side='LONG',entry=102.4,atr=1,nearest_support=101.5,nearest_resistance=104.1,current_price=102.4,level_report=level)
 check('STRUCTURAL_STOP_OUTSIDE_SUPPORT',geo.get('stop_source')=='structural_support_invalidation' and float(geo.get('stop'))<101.5 and not geo.get('structural_risk_budget_block'),f'stop={geo.get("stop")} source={geo.get("stop_source")}')
 check('STRUCTURE_FIRST_TARGET',str(geo.get('target_source')).startswith('first_structural_obstacle') and float(geo.get('target_1'))<104.1 and geo.get('promotion_allowed'),f't1={geo.get("target_1")} source={geo.get("target_source")} allowed={geo.get("promotion_allowed")}')
 wide=TradeGeometryAuthority.project(mode='intraday',side='LONG',entry=102.4,atr=1,nearest_support=99,nearest_resistance=104.1,current_price=102.4,level_report=level)
 check('WIDE_STRUCTURAL_STOP_REJECTS',wide.get('structural_risk_budget_block') and not wide.get('promotion_allowed') and 'STRUCTURAL_STOP_OUTSIDE_RISK_BUDGET' in str(wide.get('structural_risk_reason')),str(wide.get('structural_risk_reason')))
 tax=OutcomeAccuracyTaxonomy()
 check('CUTOFF_SIGNAL_FAILURE_ECONOMIC_SEPARATE',tax.signal_from_exit_reason('TIME_EXIT_1500_TARGET_NOT_HIT_BY_CUTOFF')=='FAILURE' and tax.economic_from_pnl(100)=='WIN','cutoff signal FAILURE remains separate from positive economic P&L WIN')
 check('UNTRIGGERED_SIGNAL_FAILURE',tax.signal_from_exit_reason('ENTRY_NOT_TRIGGERED_BY_DEADLINE')=='FAILURE','untriggered final signal is FAILURE')
 # source/static contracts
 nse=(ROOT/'backend/core/nse_official_evidence_service.py').read_text(encoding='utf-8')
 check('NSE_OFFICIAL_FEATURES_USED',all(token in nse for token in ['delivery_pct_surprise','delivered_quantity_surprise','nse_turnover_z20','nse_trades_z20','nse_impact_cost','nse_surveillance_flag']),'delivery/turnover/trades/impact/surveillance are projected')
 radar=(ROOT/'backend/core/market_radar_service.py').read_text(encoding='utf-8'); routes=(ROOT/'backend/routes_get_system.py').read_text(encoding='utf-8')
 check('MARKET_BREADTH_MOVERS_AUTHORITY',all(token in radar for token in ['"advances"','"declines"','"unchanged"','"change_unknown"']) and 'market_movers' in routes,'workspace gets bounded in-memory Market Radar breadth/movers')
 app=(ROOT/'frontend/app.js').read_text(encoding='utf-8'); html=(ROOT/'frontend/index.html').read_text(encoding='utf-8'); css=(ROOT/'frontend/ui-system.css').read_text(encoding='utf-8')
 ui_tokens=['NIFTY 50','SENSEX','PSU BANK','PVT BANK','OIL&GAS','FMCG','PHARMA','METAL','REALTY','ENERGY','HEALTH','CONSUMER','MEDIA','MIDCAP','SMALLCAP','Market breadth & movers']
 check('DENSE_MARKET_MAP_UI',all(token in app for token in ui_tokens) and 'market-map-sectors' in css,'benchmark + major-sector two-row market map and expandable movers shipped')
 check('RESEARCH_STAGE_CLARITY','function researchStageLabel' in app and 'LIVE VALIDATION' in app and "geometryComplete ? 'FINAL' : 'LIVE VALIDATION'" in app,'research states are display-clear and incomplete geometry cannot display FINAL')
 check('COLLAPSIBLE_SECTIONS','function initCollapsibleSections' in app and 'localStorage.setItem' in app and 'expandSystemSections' in app and 'collapseSystemSections' in app and 'id="expandSystemSections"' in html and 'id="collapseSystemSections"' in html and 'collapsible-section' in css,'major sections collapse with persistent state; system expand/collapse all')
 # preserve chart library asset exactly
 chart='frontend/assets/lightweight-charts.js'
 check('CHART_LIBRARY_FROZEN',chart in FROZEN['hashes'] and sha(ROOT/chart)==FROZEN['hashes'][chart],'chart library unchanged')
 identity=json.loads((ROOT/'RELEASE_IDENTITY.json').read_text(encoding='utf-8-sig'))
 check('BROKER_NONE_ML_FAIL_CLOSED',identity.get('broker_authority')=='NONE' and identity.get('production_ready') is False,'broker NONE; candidate not production ready')
 # compile/import
 py=list((ROOT/'backend').rglob('*.py'))+list((ROOT/'validation').rglob('*.py'))
 compile_fail=[]
 for source in py:
  if '__pycache__' in source.parts: continue
  try: compile(source.read_text(encoding='utf-8-sig'),str(source),'exec')
  except Exception as exc: compile_fail.append(f'{source.relative_to(ROOT)}:{type(exc).__name__}:{exc}')
 check('PYTHON_COMPILE',not compile_fail,f'files={len(py)} failures={compile_fail[:3]}')
 cp=subprocess.run(['node','--check','frontend/app.js'],cwd=ROOT,text=True,capture_output=True)
 check('FRONTEND_JS_SYNTAX',cp.returncode==0,f'rc={cp.returncode} stderr={cp.stderr[-300:]}')
 code="import os,sys;sys.path.insert(0,os.path.join(os.getcwd(),'backend'));import main;print(main.APP_VERSION)"
 env={**os.environ,'PROJECT_LADDU_DATA_PLANE_MODE':'test','PROJECT_LADDU_HOME':'/tmp/project-laddu-r47-import-home','PYTHONDONTWRITEBYTECODE':'1'}
 cp=subprocess.run([sys.executable,'-c',code],cwd=ROOT,text=True,capture_output=True,env=env)
 check('INSTALLER_EQUIVALENT_BACKEND_IMPORT',cp.returncode==0 and 'v131.0.0' in cp.stdout,f'rc={cp.returncode} stderr={cp.stderr[-300:]}')
 report={'ok':not failures,'scope':'R47_INTRADAY_PRICE_ACTION_SESSION_STRUCTURE_UI_CLARITY','checks':checks,'failures':failures}
 print(json.dumps(report,indent=2))
 return 0 if not failures else 2
if __name__=='__main__':raise SystemExit(main())
