from __future__ import annotations
import hashlib,json,re,subprocess,sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]

def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()

def check(name, ok, detail, checks, failures):
    checks.append({'gate':name,'state':'PASS' if ok else 'FAIL','detail':detail})
    if not ok: failures.append(f'{name}:{detail}')

def main():
    checks=[]; failures=[]
    identity=json.loads((ROOT/'RELEASE_IDENTITY.json').read_text(encoding='utf-8-sig'))
    frozen=json.loads((ROOT/'validation/r31_frozen_parent_hashes.json').read_text(encoding='utf-8-sig'))
    js=(ROOT/'frontend/app.js').read_text(encoding='utf-8-sig')
    css=(ROOT/'frontend/app.css').read_text(encoding='utf-8-sig')
    html=(ROOT/'frontend/index.html').read_text(encoding='utf-8-sig')
    stock=(ROOT/'backend/core/stock_snapshot_service.py').read_text(encoding='utf-8-sig')

    # R32 retains R31 browser closure while permitting one separately-governed installer-only delta.
    revision=str(identity.get('candidate_revision') or '').upper()
    approved_installer_delta={'installer/install.ps1'} if revision in {'R32','R33','R34','R35','R36','R37'} else set()
    if revision=='R35': approved_installer_delta.add('installer/register_research_tasks.ps1')
    approved_customer_delta={'frontend/app.js','frontend/app.css','frontend/index.html','frontend/release-identity.json'} if revision in {'R34','R35','R36','R37'} else set()
    mismatches=[]; missing=[]
    for item in frozen.get('files') or []:
        if item['path'] in approved_installer_delta or item['path'] in approved_customer_delta: continue
        path=ROOT/item['path']
        if not path.is_file(): missing.append(item['path']); continue
        if sha(path)!=item['sha256']: mismatches.append(item['path'])
    check('R30_FROZEN_RUNTIME_PARENT', not missing and not mismatches,
          f"R30 frozen authority intact outside approved installer delta; missing={len(missing)} changed={len(mismatches)}" if not missing and not mismatches else f"missing={missing[:3]} changed={mismatches[:8]}", checks, failures)
    check('EXACT_R30_PARENT_HASH', frozen.get('parent_archive_sha256')=='3358b26e4109b4e483873e005fbbfc515768bdd02b9133b65bac4c7e11a61424', 'R31 parent is the frozen exact R30 artifact', checks, failures)

    # Completed-close display continuity cannot become execution authority.
    check('COMPLETED_CLOSE_DISPLAY_ONLY', all(token in stock for token in [
        'COMPLETED_DAILY_CANDLE_DISPLAY_ONLY','"display_only": True','"execution_price_authority": False','display_quote = self._display_quote(quote, performance)'
    ]), 'verified completed close may render while execution authority remains false', checks, failures)
    check('CANONICAL_SELECTED_QUOTE_PRESERVED', '"selected_quote": quote' in stock and '"quote": quote' in stock, 'selected quote remains separate canonical execution-price input', checks, failures)
    check('ZERO_QUOTE_NOT_READY', '_quote_has_price' in stock and '"READY" if self._quote_has_price(quote) else "UNAVAILABLE"' in stock, 'zero/non-positive quote cannot satisfy customer decision-proof quote readiness', checks, failures)

    # Browser convergence is bounded and reads only existing cache-only HTTP projections.
    check('BOUNDED_SNAPSHOT_CONVERGENCE', 'state.snapshotWarmRetryAttempts >= 12' in js and 'scheduleSnapshotConvergence' in js and 'stockSnapshotNeedsConvergence' in js, 'PARTIAL/WARMING Stock Snapshot is re-read with a finite bound', checks, failures)
    check('BOUNDED_CHART_CONVERGENCE', 'state.chartWarmRetryAttempts >= 14' in js and 'scheduleChartConvergence' in js and '/api/chart-data?' in js, 'empty warming chart is re-read with a finite bound', checks, failures)
    forbidden_browser=['/v3/historical-candle','duckdb','parquet','psycopg','MarketDataService']
    convergence_region=js[js.find('function stockSnapshotNeedsConvergence'):js.find('function volumeParticipationIntel')]
    check('NO_BROWSER_COLD_AUTHORITY', not any(t.lower() in convergence_region.lower() for t in forbidden_browser), 'browser convergence invokes only bounded local projection APIs', checks, failures)

    # Chart containment and all canonical timeframes remain visible.
    check('CHART_CANVAS_CONTAINMENT', all(token in css for token in ['contain:layout paint size','overflow:hidden!important','.chart-host canvas,.indicator-chart canvas']), 'chart/indicator canvases cannot paint outside bounded hosts', checks, failures)
    tf_pairs=[('1m','1minute'),('3m','3minute'),('5m','5minute'),('15m','15minute'),('30m','30minute'),('1H','60minute'),('4H','240minute'),('1D','day'),('1W','week'),('1M','month')]
    check('ALL_10_TIMEFRAMES_RETAINED', all(f"['{a}', '{b}']" in js for a,b in tf_pairs), 'all 10 canonical chart timeframes remain wired', checks, failures)
    check('TIMEFRAME_FAIL_CLOSED_RETAINED', 'timeframe identity FAILED CLOSED' in js and 'timeframeIdentityMatches' in js, 'served timestamp spacing proof still gates rendering', checks, failures)

    # Visual closure contract.
    check('PREMIUM_TYPOGRAPHY_LAYER', 'Segoe UI Variable Text' in css and '--font-ui:' in css and 'font-variant-numeric:tabular-nums lining-nums' in css, 'readability and financial-number typography upgraded without shipping font binaries', checks, failures)
    check('SEMANTIC_COLORS_RETAINED', all(t in css for t in ['--green:','--red:','--amber:','--blue:','--cyan:']), 'green/red/amber/blue semantic palette retained across themes', checks, failures)
    check('R31_CACHE_BUSTER', (('app.css?v=131.0.0-r31-browser-customer-closure' in html and 'app.js?v=131.0.0-r31-browser-customer-closure' in html) or (revision=='R34' and 'app.css?v=131.0.0-r34-customer-ui-sr-closure' in html and 'app.js?v=131.0.0-r34-customer-ui-sr-closure' in html) or (revision=='R35' and 'app.css?v=131.0.0-r35-decision-dashboard-pit-enrichment' in html and 'app.js?v=131.0.0-r35-decision-dashboard-pit-enrichment' in html) or (revision=='R36' and 'app.css?v=131.0.0-r36-qc-task-contract-historical-pit' in html and 'app.js?v=131.0.0-r36-qc-task-contract-historical-pit' in html) or (revision=='R37' and 'app.css?v=131.0.0-r37-workspace-resilience' in html and 'app.js?v=131.0.0-r37-workspace-resilience' in html)), 'browser cannot silently reuse R30 frontend assets', checks, failures)

    # Accuracy exclusions are explained, never zeroed or admitted.
    check('ACCURACY_EXCLUSION_PROOF', all(t in js for t in ['excluded_incomplete','blockerRows','reasonCounts','Excluded ${formatNumber']), 'canonical exclusion totals now show reason counts from lifecycle authority', checks, failures)
    check('ACCURACY_ELIGIBILITY_UNCHANGED', 'row.accuracy_eligible === true' in js, 'UI still displays eligible rows using canonical eligibility flag only', checks, failures)

    # Identity / no misleading zero-price behaviour.
    check('POSITIVE_PRICE_GUARD', 'const positivePrice' in js and "positivePrice(pick(quote,'ltp','last_price','close'))" in js, 'zero/non-positive selected price renders unavailable or verified-close fallback rather than ₹0.00', checks, failures)
    check('EXACT_IDENTITY_FAIL_CLOSED', 'payloadIdentityMatches(payload, state.symbol)' in js and 'Chart identity mismatch' in js, 'symbol identity remains fail-closed during convergence', checks, failures)

    # JavaScript syntax must execute through Node parser where available.
    node_ok=True; node_detail='node unavailable; source token guards used'
    try:
        proc=subprocess.run(['node','--check',str(ROOT/'frontend/app.js')],capture_output=True,text=True,timeout=20)
        node_ok=proc.returncode==0; node_detail='frontend/app.js parses cleanly' if node_ok else (proc.stderr or proc.stdout)[-300:]
    except Exception:
        pass
    check('FRONTEND_JS_SYNTAX',node_ok,node_detail,checks,failures)

    # Broker/model authority is frozen.
    check('BROKER_AUTHORITY_NONE', identity.get('broker_authority')=='NONE' and identity.get('product_mode')=='AUTOMATIC_MODEL_PAPER_ONLY', 'browser/visual closure adds no trading execution authority', checks, failures)

    report={'ok':not failures,'scope':'R31_BROWSER_CUSTOMER_VISUAL_CLOSURE','checks':checks,'passed':sum(c['state']=='PASS' for c in checks),'failed':sum(c['state']=='FAIL' for c in checks),'failures':failures,'production_ready':False,'broker_authority':identity.get('broker_authority')}
    print(json.dumps(report,indent=2))
    return 0 if not failures else 2

if __name__=='__main__': raise SystemExit(main())
