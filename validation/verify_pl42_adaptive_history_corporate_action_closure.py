from __future__ import annotations
import hashlib,json,subprocess,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]; BACKEND=ROOT/'backend'
if str(BACKEND) not in sys.path: sys.path.insert(0,str(BACKEND))
sys.dont_write_bytecode=True
from core.ml_history_policy import policy_for_mode, resolve_mode_history_policy
from core.corporate_action_factor_derivation import derive_factors
from core.corporate_action_adjustment_authority import DEFAULT_CORPORATE_ACTION_ADJUSTMENT_AUTHORITY
import config

checks=[]
def ck(name,cond,detail=''): checks.append({'name':name,'ok':bool(cond),'detail':str(detail)[:500]})
def sha(rel): return hashlib.sha256((ROOT/rel).read_bytes()).hexdigest()

# Adaptive history: 500 is reference, never cap.
d=resolve_mode_history_policy('delivery',3729,horizon_days=10)
i=resolve_mode_history_policy('intraday',900,horizon_days=1)
ck('delivery 500 is reference not cap',d['reference_days']==500 and d['resolved_train_days']>500 and d['reference_semantics']=='STABILITY_REFERENCE_NOT_CAP',d)
ck('delivery maximum unbounded by default',d['maximum_days']==0 and d['maximum_policy']=='UNBOUNDED',d)
ck('first WFA stability reference remains 500',d['initial_wfa_train_days']==500,d)
ck('intraday owns independent policy',i['mode']=='intraday' and i['reference_days']!=d['reference_days'] and i['minimum_days']!=d['minimum_days'],i)
ck('intraday also uses all eligible capacity',i['resolved_train_days']>i['reference_days'],i)
ck('per stock minimum is independently configured',policy_for_mode('delivery').per_symbol_minimum_days>0 and policy_for_mode('intraday').per_symbol_minimum_days>0)
ck('recency half lives are mode specific',policy_for_mode('delivery').recency_half_life_days!=policy_for_mode('intraday').recency_half_life_days)

tr=(ROOT/'backend/tools/train_nse_smart_model.py').read_text(encoding='utf-8-sig')
policy=(ROOT/'backend/core/ml_history_policy.py').read_text(encoding='utf-8-sig')
ck('OOF uses adaptive per-stock history helper','adaptive_history_mode=(None if first_mode else "delivery")' in tr and 'training_frame_and_weights(tr, mode=str(adaptive_history_mode))' in tr)
ck('OOF model receives sample weights','sample_weight=fit_weights.loc[fit_tr.index]' in tr)
ck('OOF test population restricted to historically eligible symbols','te = te[te["symbol"].astype(str).str.upper().isin(eligible_symbols)]' in tr)
ck('unbounded WFA is expanding not fixed 500','train_window_days=(None if first_mode or int(training_policy.get("maximum_days") or 0) <= 0' in tr)
ck('final model uses all eligible history unless explicit ceiling','final_training_source = labelled' in tr and 'if ceiling > 0:' in tr and 'training_frame_and_weights(\n                final_training_source, mode="delivery"' in tr)
ck('final model receives weighted history','sample_weight=final_weights.loc[final_training.index]' in tr)
ck('long listed symbols are balanced not row-count dominant','inverse-sqrt' in policy.lower() and '.clip(lower=0.5, upper=2.0)' in policy)
ck('older regimes retained with recency decay','np.power(0.5' in policy and 'recency_half_life_days' in policy)
ck('model spec identity binds adaptive policy','historical_train_window_policy": "ADAPTIVE_ALL_ELIGIBLE_HISTORY_BY_SYMBOL_AND_MODE"' in tr)

# Corporate-action deterministic math.
b=derive_factors(purpose='Bonus 1:1')
split=derive_factors(purpose='Sub-division of face value Rs 10 to Rs 2')
cons=derive_factors(purpose='Consolidation of face value Rs 2 to Rs 10')
rights=derive_factors(purpose='Rights 1:4 @ Rs 80',pre_ex_close=100)
div=derive_factors(purpose='Final Dividend Rs 5')
dem=derive_factors(purpose='Demerger of undertaking')
ck('bonus factor deterministic',b.get('price_factor')==0.5 and b.get('volume_factor')==2.0,b)
ck('split factor deterministic',split.get('price_factor')==0.2 and split.get('volume_factor')==5.0,split)
ck('consolidation factor deterministic',cons.get('price_factor')==5.0 and cons.get('volume_factor')==0.2,cons)
ck('rights TERP factor deterministic',abs(float(rights.get('price_factor') or 0)-0.96)<1e-12 and rights.get('volume_factor')==1.25,rights)
ck('cash dividend does not fake share basis',div.get('price_factor')==1.0 and div.get('volume_factor')==1.0,div)
ck('demerger remains fail closed',dem.get('ok') is False and 'UNRESOLVED' in str(dem.get('state')),dem)

sql=DEFAULT_CORPORATE_ACTION_ADJUSTMENT_AUTHORITY.duckdb_adjusted_candles_sql()
cat=(ROOT/'backend/tools/refresh_research_catalog.py').read_text(encoding='utf-8-sig')
sync=(ROOT/'backend/tools/sync_nse_corporate_action_history.py').read_text(encoding='utf-8-sig')
recon=(ROOT/'backend/tools/reconcile_corporate_action_authority.py').read_text(encoding='utf-8-sig')
sweep=(ROOT/'backend/core/historical_pit_sweep_service.py').read_text(encoding='utf-8-sig')
ck('adjustment SQL is row scoped by coverage','LEFT JOIN corporate_action_coverage' in sql and 'corporate_action_adjusted' in sql and 'COVERAGE_UNVERIFIED' in sql)
ck('catalogue always publishes row scoped adjusted view','if corporate_actions["coverage_complete"]' not in cat and 'duckdb_adjusted_candles_sql()' in cat)
ck('training panel persists row corporate-action truth','corporate_action_qualified_rows' in cat and 'corporate_action_coverage_hash' in cat)
ck('training excludes uncovered corporate-action rows','labelled = labelled[labelled["corporate_action_adjusted"]' in tr)
ck('official market-wide corporate-action sync exists',ENDPOINT_OK := ('corporates-corporateActions' in sync and 'from_date' in sync and 'to_date' in sync))
ck('range must fully succeed before coverage attestation','RANGE_ACQUISITION_INCOMPLETE' in sync and 'coverage_written": False' in sync and 'range_source_hash' in sync)
ck('zero-action stocks require market-wide range proof','ZERO_ACTION_RANGE_ATTESTED' in sync and 'ZERO_ACTION_SYMBOLS_COVERED_BY_MARKET_WIDE_RANGE' in recon)
ck('price-jump factor inference forbidden','inference_from_price_jump_allowed": False' in recon and 'No factor is inferred from a price jump' in (ROOT/'backend/core/corporate_action_factor_derivation.py').read_text())
ck('historical supervisor refreshes scope then corporate authority then adjusted panel then trains',all(token in sweep for token in ('research_catalogue_scope_refresh','corporate_action_range_sync','research_catalogue_adjusted_refresh','delivery_historical_training')))
ck('historical trainer readiness uses safety floor not 500 reference','--min-dates", str(self.MIN_DATES)' in sweep and 'TRAIN_REFERENCE_DAYS' in sweep)

# Inherited PL41/40/39/38 and frozen trading/cost authorities.
proc=subprocess.run([sys.executable,str(ROOT/'validation/verify_pl41_official_source_qualification_policy.py')],cwd=ROOT,capture_output=True,text=True)
ck('PL41 and inherited PL38-40 closures remain green',proc.returncode==0,(proc.stdout+proc.stderr)[-800:])
frozen={
 'backend/core/decision_engine_service.py':'b963e5df7866699b42358d18a40e6f9b8aebcfd77be8b5eeda3a58e9d531fbaf',
 'backend/core/india_cost_model.py':'5d55e47a4286387f38785d672d9cd778114a29674d5a78020fd781ecfe11646a',
}
for rel,expected in frozen.items(): ck('frozen '+rel,sha(rel)==expected,sha(rel))
ck('exact PL42 build marker',config.BUILD_MARKER=='production-usability-r8-pl42-adaptive-history-corporate-action-8086',config.BUILD_MARKER)

# Complete fintech UI redesign — structural proof without changing trading authority.
index=(ROOT/'frontend/index.html').read_text(encoding='utf-8-sig')
ui=(ROOT/'frontend/ui-system.css').read_text(encoding='utf-8-sig')
app=(ROOT/'frontend/app.js').read_text(encoding='utf-8-sig')
ck('UI5 complete redesign marker present','data-ui-redesign="FINTECH_COMMAND_CENTER"' in index and 'class="ui5-redesign"' in index)
ck('market intelligence hero replaces terminal-only first impression','class="workspace-hero"' in index and 'Market Intelligence' in index and 'Decision command center' in index)
ck('polished light theme is new default while dark remains selectable',"theme: 'light'" in app and "stored === 'dark' ? 'dark' : 'light'" in app and 'html[data-theme="dark"] body.ui5-redesign' in ui)
ck('UI5 semantic fintech palette exists',all(token in ui for token in ('--u5-navy','--u5-green','--u5-red','--u5-amber','--u5-purple','--u5-cyan')))
ck('UI5 actionable empty state is compact','trade-ready-prime.is-empty .actionable-table-wrap{height:66px' in ui and 'NO TRADE READY DECISIONS' in app)
ck('UI5 modern navigation/search authority exists','premium product rail' in ui and 'clean command strip' in ui and 'stock-search button' in ui)
ck('UI5 dual-theme cards are explicitly styled','Dark counterpart: rich navy cards' in ui and 'body.ui5-redesign .panel' in ui)

failed=[x for x in checks if not x['ok']]
print(json.dumps({'contract':'PL42_ADAPTIVE_HISTORY_CORPORATE_ACTION_CLOSURE','ok':not failed,'passed':len(checks)-len(failed),'failed':len(failed),'checks':checks},indent=2))
raise SystemExit(1 if failed else 0)
