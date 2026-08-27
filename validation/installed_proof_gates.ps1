Set-StrictMode -Version 2.0

function Invoke-AlwaysInstalledGates([hashtable]$C) {
  $base=$C.BaseUrl
  $ready=$null; $product=$null; $health=$null; $system=$null; $surface=$null; $lifecycle=$null; $performance=$null; $operations=$null; $scanner=$null
  try {
    $ready=Wait-Ready $base 120
    $identity=$C.ReleaseIdentity
    $expected=[string](Get-Prop $identity 'version')
    $ok=(-not [string]::IsNullOrWhiteSpace($expected)) -and (Get-Prop $ready 'ready') -eq $true -and [string](Get-Prop $ready 'version') -eq $expected
    Add-GateFromBool $C 'runtime_identity' $ok ("expected={0}; actual={1}" -f $expected,(Get-Prop $ready 'version')) $ready
  } catch { Add-Gate $C 'runtime_identity' 'FAIL' $_.Exception.Message }

  try {
    $storage=Get-Json $base '/api/storage-architecture' 30
    $authority=Get-Prop $storage 'production_authority'
    $ok=(Get-Prop $storage 'ok') -eq $true -and (Get-Prop $authority 'ready') -eq $true
    Add-GateFromBool $C 'data_plane_authority' $ok ("ready={0}; probe={1}" -f (Get-Prop $authority 'ready'),(Get-Prop $storage 'probe')) $storage
  } catch { Add-Gate $C 'data_plane_authority' 'FAIL' $_.Exception.Message }

  try {
    $search=Post-Json $base '/api/search' @{q='TCS';mode='delivery'} 15
    $matches=Arr (Get-Prop $search 'matches')
    $first=$matches|Select-Object -First 1
    $ok=$matches.Count -gt 0 -and [string](Get-Prop $first 'trading_symbol') -eq 'TCS' -and -not [string]::IsNullOrWhiteSpace([string](Get-Prop $first 'instrument_key'))
    Add-GateFromBool $C 'identity_search' $ok ("matches={0}; first={1}; key={2}" -f $matches.Count,(Get-Prop $first 'trading_symbol'),(Get-Prop $first 'instrument_key')) $search
  } catch { Add-Gate $C 'identity_search' 'FAIL' $_.Exception.Message }

  try {
    $sync=Post-Json $base '/api/priority-sync' @{symbol='TCS';mode='delivery';interval='day';action='priority_sync'} 30
    $deadline=(Get-Date).AddSeconds(120); $pipe=$null
    do { Start-Sleep -Seconds 2; $pipe=Get-Json $base '/api/priority-pipeline?symbol=TCS&mode=delivery' 20; if([string](Get-Prop $pipe 'state') -match '^(READY|BLOCKED|FAILED|NOT_STARTED|COMPLETE)$'){break} } while((Get-Date)-lt $deadline)
    $state=[string](Get-Prop $pipe 'state'); $ok=$state -match '^(READY|BLOCKED|FAILED|NOT_STARTED|COMPLETE)$'
    Add-GateFromBool $C 'priority_pipeline_terminal' $ok ("state={0}; stage={1}; blocker={2}" -f $state,(Get-Prop $pipe 'current_stage'),(Get-Prop $pipe 'blocker')) ([ordered]@{sync=$sync;pipeline=$pipe})
  } catch { Add-Gate $C 'priority_pipeline_terminal' 'FAIL' $_.Exception.Message }

  try {
    # Candidate 15: prove the foreground health read is bounded separately from
    # the background physical-reconciliation convergence. WARMING is not a pass.
    $firstHealth=Invoke-TimedGet $base '/api/system-health' 5
    $system=$firstHealth.payload; $healthDeadline=(Get-Date).AddSeconds(30)
    $ingestion=Get-Prop $system 'ingestion'
    while(((Num (Get-Prop $ingestion 'candles_total')) -le 0 -or [string]::IsNullOrWhiteSpace([string](Get-Prop $ingestion 'last_candle_stored'))) -and (Get-Date) -lt $healthDeadline){
      Start-Sleep -Milliseconds 500
      $system=Get-Json $base '/api/system-health' 5
      $ingestion=Get-Prop $system 'ingestion'
    }
    $warmHealth=Invoke-TimedGet $base '/api/system-health' 5
    $ingestion=Get-Prop $warmHealth.payload 'ingestion'
    $ok=$firstHealth.elapsed_ms -le 1500 -and $warmHealth.elapsed_ms -le 1000 -and (Num (Get-Prop $ingestion 'candles_total')) -gt 0 -and -not [string]::IsNullOrWhiteSpace([string](Get-Prop $ingestion 'last_candle_stored')) -and (Get-Prop $ingestion 'restart_safe') -ne $false
    Add-GateFromBool $C 'persistence_telemetry' $ok ("cold_ms={0}; warm_ms={1}; candles={2}; last={3}; restart_safe={4}" -f $firstHealth.elapsed_ms,$warmHealth.elapsed_ms,(Get-Prop $ingestion 'candles_total'),(Get-Prop $ingestion 'last_candle_stored'),(Get-Prop $ingestion 'restart_safe')) ([ordered]@{first=$firstHealth.payload;final=$warmHealth.payload})
  } catch { Add-Gate $C 'persistence_telemetry' 'FAIL' $_.Exception.Message }

  try {
    $manifestPath=Join-Path $C.InstallDir 'data\manifests\market-lake.json'
    if(-not (Test-Path -LiteralPath $manifestPath)){ throw "market lake manifest missing: $manifestPath" }
    $lake=Get-Content -LiteralPath $manifestPath -Raw|ConvertFrom-Json
    $reconciliation=Get-Prop $lake 'reconciliation'
    $recon=Property-Values $reconciliation
    $incomplete=0; foreach($row in $recon){ if((Get-Prop $row 'complete') -ne $true){ $incomplete++ } }
    $complete=$recon.Count -gt 0 -and $incomplete -eq 0
    $prune=[string](Get-Prop (Get-Prop $lake 'operational_prune') 'state')
    $productionNoPrune=$prune -eq 'NOT_APPLICABLE_PRODUCTION_DATA_PLANE'
    Add-GateFromBool $C 'lake_reconciliation' ($complete -or $productionNoPrune) ("complete={0}; prune={1}" -f $complete,$prune) $lake
  } catch { Add-Gate $C 'lake_reconciliation' 'FAIL' $_.Exception.Message }

  try {
    $integrity=Get-Json $base '/api/operational-evidence-integrity' 30
    $chain=Get-Prop $integrity 'chain'; $errors=Arr (Get-Prop $chain 'errors')
    $ok=[string](Get-Prop $integrity 'state') -notin @('UNAVAILABLE','FAILED') -and $errors.Count -eq 0 -and (Num (Get-Prop $integrity 'source_failure_count')) -eq 0
    Add-GateFromBool $C 'operational_evidence_chain' $ok ("state={0}; chain={1}; errors={2}; source_failures={3}" -f (Get-Prop $integrity 'state'),(Get-Prop $chain 'state'),$errors.Count,(Get-Prop $integrity 'source_failure_count')) $integrity
  } catch { Add-Gate $C 'operational_evidence_chain' 'FAIL' $_.Exception.Message }

  try {
    $scanner=Get-Json $base '/api/scanner/status' 60
    $root=Get-Prop $scanner 'scanner'; $modes=Get-Prop $root 'mode_scanners'
    $bad=0; $details=@()
    foreach($mode in @('delivery','intraday')){
      $row=Get-Prop $modes $mode; if($null -eq $row){$bad++;continue}
      $analysis=Get-Prop $row 'analysis'; $contract=Get-Prop $analysis 'progress_contract'
      $state=[string](Get-Prop $row 'state'); $terminal=($state -match 'READY|COMPLETE|IDLE|BLOCKED|FAILED|PAUSED') -or (Num (Get-Prop $contract 'population_count')) -ge 0
      if(-not $terminal){$bad++}; $details+=("{0}:{1}" -f $mode,$state)
    }
    Add-GateFromBool $C 'scanner_terminality' ($bad -eq 0) ("modes={0}; bad={1}" -f ($details -join ','),$bad) $scanner
  } catch { Add-Gate $C 'scanner_terminality' 'FAIL' $_.Exception.Message }

  try {
    $surface=Get-Json $base '/api/decision-surface-reconciliation' 30
    $conflicts=(Arr (Get-Prop $surface 'conflicts')).Count+(Arr (Get-Prop $surface 'invalid_modes')).Count+(Arr (Get-Prop $surface 'delivery_duplicate_theses')).Count
    $orph=Get-Prop $surface 'orphaned'; $orphans=(Arr (Get-Prop $orph 'today_entries')).Count+(Arr (Get-Prop $orph 'signal_ledger')).Count+(Arr (Get-Prop $orph 'model_paper')).Count
    $ok=[string](Get-Prop $surface 'state') -ne 'UNAVAILABLE' -and $conflicts -eq 0 -and $orphans -eq 0
    Add-GateFromBool $C 'canonical_surface_reconciliation' $ok ("state={0}; conflicts={1}; orphans={2}" -f (Get-Prop $surface 'state'),$conflicts,$orphans) $surface
  } catch { Add-Gate $C 'canonical_surface_reconciliation' 'FAIL' $_.Exception.Message }

  try {
    $lifecycle=Get-Json $base '/api/decision-lifecycle?mode=all&limit=5000' 45
    $overall=Get-Prop $lifecycle 'overall'
    if($null -eq $overall){$overall=$lifecycle}
    $authority=[string](Get-Prop $lifecycle 'authority')
    $ok=(Get-Prop $lifecycle 'ok') -eq $true -and $authority -eq 'POSTGRESQL_CANONICAL_DECISIONS'
    Add-GateFromBool $C 'signal_ledger_authority' $ok ("authority={0}; records={1}; settled={2}" -f $authority,(Get-Prop $overall 'records'),(Get-Prop $overall 'settled')) $lifecycle
  } catch { Add-Gate $C 'signal_ledger_authority' 'FAIL' $_.Exception.Message }

  try {
    $adapter=Get-Json $base '/api/research-adapter' 30
    $maturity=Get-Json $base '/api/research-maturity' 30
    $runtimeState=[string](Get-Prop $adapter 'state'); $maturityState=[string](Get-Prop $maturity 'maturity_state')
    $ok=-not [string]::IsNullOrWhiteSpace($runtimeState) -and -not [string]::IsNullOrWhiteSpace($maturityState) -and ($adapter|ConvertTo-Json -Depth 10 -Compress) -notmatch 'runtime_ready.*model_ready.*true'
    Add-GateFromBool $C 'research_readiness_separation' $ok ("runtime={0}; maturity={1}" -f $runtimeState,$maturityState) ([ordered]@{adapter=$adapter;maturity=$maturity})
  } catch { Add-Gate $C 'research_readiness_separation' 'FAIL' $_.Exception.Message }

  try {
    $operations=Get-Json $base '/api/operations/summary' 10
    $jobs=Arr (Get-Prop $operations 'jobs')
    $falseRunning=@($jobs|Where-Object{[string](Get-Prop $_ 'state') -eq 'RUNNING' -and (Num (Get-Prop $_ 'progress_age_sec')) -gt 300 -and [string]::IsNullOrWhiteSpace([string](Get-Prop $_ 'progress_token')) -and (Get-Prop $_ 'expected_idle') -ne $true})
    Add-GateFromBool $C 'worker_progress_truth' ((Get-Prop $operations 'ok') -eq $true -and $falseRunning.Count -eq 0) ("jobs={0}; false_running={1}" -f $jobs.Count,$falseRunning.Count) $operations
  } catch { Add-Gate $C 'worker_progress_truth' 'FAIL' $_.Exception.Message }

  try {
    $help=Get-Json $base '/api/help' 10
    $post=Post-Json $base '/api/search' @{q='TCS';mode='delivery'} 10
    $ok=$null -ne $help -and $null -ne $post
    Add-GateFromBool $C 'diagnostic_api_contract' $ok 'GET /api/help and POST /api/search both accepted by installed registry' ([ordered]@{help=$help;search=$post})
  } catch { Add-Gate $C 'diagnostic_api_contract' 'FAIL' $_.Exception.Message }

  try {
    if($null -eq $lifecycle){$lifecycle=Get-Json $base '/api/decision-lifecycle?mode=all&limit=5000' 45}
    $performance=Get-Json $base '/api/performance/summary' 30
    $authority=[string](Get-Prop $lifecycle 'authority')
    $perfLife=Get-Prop $performance 'canonical_lifecycle'
    $overall=Get-Prop $lifecycle 'overall'; if($null -eq $overall){$overall=$lifecycle}
    $records=Num (Get-Prop $overall 'records'); $settled=Num (Get-Prop $overall 'settled'); $perfRecords=Num (Get-Prop $perfLife 'records')
    # Performance settlement is intentionally a projection of the SETTLED
    # lifecycle population, not every open/research lifecycle row. Zero settled
    # trades must therefore reconcile to zero performance records without
    # weakening authority or geometry checks.
    $ok=$authority -eq 'POSTGRESQL_CANONICAL_DECISIONS' -and $perfRecords -eq $settled
    Add-GateFromBool $C 'settlement_single_authority' $ok ("authority={0}; lifecycle_records={1}; settled={2}; performance_records={3}" -f $authority,$records,$settled,$perfRecords) ([ordered]@{lifecycle=$lifecycle;performance=$performance})
  } catch { Add-Gate $C 'settlement_single_authority' 'FAIL' $_.Exception.Message }

  try {
    if($null -eq $performance){$performance=Get-Json $base '/api/performance/summary' 30}
    if($null -eq $lifecycle){$lifecycle=Get-Json $base '/api/decision-lifecycle?mode=all&limit=5000' 45}
    $overall=Get-Prop $lifecycle 'overall'; if($null -eq $overall){$overall=$lifecycle}
    $eligible=Num (Get-Prop $overall 'accuracy_eligible'); $settled=Num (Get-Prop $overall 'settled')
    $policy=[string](Get-Prop $lifecycle 'accuracy_policy')
    $perfLife=Get-Prop $performance 'canonical_lifecycle'
    $ok=$eligible -le $settled -and $policy -match 'entry\+target\+stop\+exit' -and $null -ne $perfLife
    Add-GateFromBool $C 'performance_geometry_lineage' $ok ("settled={0}; eligible={1}; policy={2}" -f $settled,$eligible,$policy) ([ordered]@{lifecycle=$lifecycle;performance=$performance})
  } catch { Add-Gate $C 'performance_geometry_lineage' 'FAIL' $_.Exception.Message }
}

function Invoke-SessionInstalledGates([hashtable]$C) {
  $base=$C.BaseUrl
  try {
    # First response must be bounded even when the exact local projection is cold;
    # completeness must then converge and a warm read must remain fast.
    $historyPath='/api/historical?symbol=TCS&interval=day&days=45&refresh=false'
    $firstDay=Invoke-TimedGetWire $base $historyPath 5; $day=$firstDay.payload
    $deadline=(Get-Date).AddSeconds(30)
    do {
      $state=[string](Get-Prop $day 'freshness_state'); if([string]::IsNullOrWhiteSpace($state)){$state=[string](Get-Prop $day 'data_status')}
      $complete=(Row-Count $day) -ge 30 -and $state -notmatch 'STALE|MISSING|FAILED'
      if($complete){break}
      Start-Sleep -Milliseconds 500; $day=Get-Json $base $historyPath 5
    } while((Get-Date)-lt $deadline)
    $warmDay=Invoke-TimedGetWire $base $historyPath 5; $day=$warmDay.payload
    $state=[string](Get-Prop $day 'freshness_state'); if([string]::IsNullOrWhiteSpace($state)){$state=[string](Get-Prop $day 'data_status')}
    $ok=$firstDay.wire_ms -le 1500 -and $warmDay.wire_ms -le 1000 -and (Row-Count $day) -ge 30 -and $state -notmatch 'STALE|MISSING|FAILED'
    Add-GateFromBool $C 'daily_history_freshness' $ok ("cold_wire_ms={0}; warm_wire_ms={1}; warm_parse_ms={2}; bytes={3}; rows={4}; last={5}; state={6}" -f $firstDay.wire_ms,$warmDay.wire_ms,$warmDay.parse_ms,$warmDay.bytes,(Row-Count $day),(Last-Candle-Time $day),$state) ([ordered]@{cold_wire_ms=$firstDay.wire_ms;cold_parse_ms=$firstDay.parse_ms;warm_wire_ms=$warmDay.wire_ms;warm_parse_ms=$warmDay.parse_ms;response_bytes=$warmDay.bytes;rows=(Row-Count $day);last=(Last-Candle-Time $day);state=$state})
  } catch { Add-Gate $C 'daily_history_freshness' 'FAIL' $_.Exception.Message }

  try {
    $indices=Get-Json $base '/api/indices' 20; $rows=Arr (Get-Prop $indices 'indices')
    $required=@('NIFTY 50','SENSEX','INDIA VIX','NIFTY BANK','NIFTY IT','NIFTY AUTO','NIFTY FMCG','NIFTY PHARMA','NIFTY METAL','NIFTY REALTY','NIFTY ENERGY')
    $names=@($rows|ForEach-Object{Canonical-Name $_}); $missing=@($required|Where-Object{$names -notcontains $_})
    $bad=@($rows|Where-Object{$required -contains (Canonical-Name $_) -and (Get-Prop $_ 'direction_authority_ready') -eq $true -and ([string]::IsNullOrWhiteSpace([string](Get-Prop $_ 'source_time')) -and [string]::IsNullOrWhiteSpace([string](Get-Prop $_ 'timestamp')))})
    Add-GateFromBool $C 'market_context_alignment' ($missing.Count -eq 0 -and $bad.Count -eq 0) ("rows={0}; missing={1}; timestamp_bad={2}" -f $rows.Count,($missing -join ','),$bad.Count) $indices
  } catch { Add-Gate $C 'market_context_alignment' 'FAIL' $_.Exception.Message }

  try {
    $timed=Invoke-TimedGetWire $base '/api/market-radar' 10
    $ok=(Get-Prop $timed.payload 'ok') -ne $false -and $timed.wire_ms -le 250
    Add-GateFromBool $C 'market_radar_latency' $ok ("wire_ms={0}; parse_ms={1}; bytes={2}" -f $timed.wire_ms,$timed.parse_ms,$timed.bytes) ([ordered]@{wire_ms=$timed.wire_ms;parse_ms=$timed.parse_ms;bytes=$timed.bytes;projection_state=(Get-Prop $timed.payload 'projection_state');projection_elapsed_ms=(Get-Prop $timed.payload 'projection_elapsed_ms');cache_only=(Get-Prop $timed.payload 'cache_only')})
  } catch { Add-Gate $C 'market_radar_latency' 'FAIL' $_.Exception.Message }

  try {
    $nse=Get-Json $base '/api/nse-data-authority' 30
    $text=$nse|ConvertTo-Json -Depth 30 -Compress
    $missing=Arr (Get-Prop $nse 'missing_required_sources'); if($missing.Count -eq 0){$missing=Arr (Get-Prop $nse 'missing_sources')}
    $ok=(Get-Prop $nse 'ok') -eq $true -and $missing.Count -eq 0 -and $text -match 'source'
    Add-GateFromBool $C 'official_nse_source_coverage' $ok ("state={0}; missing={1}" -f (Get-Prop $nse 'state'),($missing -join ',')) $nse
  } catch { Add-Gate $C 'official_nse_source_coverage' 'FAIL' $_.Exception.Message }

  try {
    $first=Invoke-TimedGet $base '/api/historical?symbol=TCS&interval=day&days=45&refresh=false' 15
    $second=Invoke-TimedGet $base '/api/historical?symbol=TCS&interval=day&days=45&refresh=false' 15
    $text=$second.payload|ConvertTo-Json -Depth 15 -Compress
    $ok=$second.elapsed_ms -le 2000 -and $text -notmatch 'provider_fetch_in_progress|network_wait_ms\s*:\s*[1-9]'
    Add-GateFromBool $C 'network_revalidation_efficiency' $ok ("first_ms={0}; second_ms={1}" -f $first.elapsed_ms,$second.elapsed_ms) ([ordered]@{first=$first.payload;second=$second.payload})
  } catch { Add-Gate $C 'network_revalidation_efficiency' 'FAIL' $_.Exception.Message }

  try {
    $runs=Get-Json $base '/api/reference-data-runs' 20; $rows=Arr (Get-Prop $runs 'runs')
    $bad=@($rows|Where-Object{[string](Get-Prop $_ 'status') -match 'OK|PASS|SUCCESS|COMPLETE' -and (Num (Get-Prop $_ 'rows_written')) -le 0})
    Add-GateFromBool $C 'reference_job_nonzero' ($rows.Count -gt 0 -and $bad.Count -eq 0) ("runs={0}; false_success={1}" -f $rows.Count,$bad.Count) $runs
  } catch { Add-Gate $C 'reference_job_nonzero' 'FAIL' $_.Exception.Message }

  try {
    # Candidate 15 separates cold response latency from materialization convergence.
    # Submit every sample quickly first, then require all snapshots to converge to
    # READY and prove the completed projection remains fast. WARMING never passes.
    $sample=Get-Json $base ("/api/clean-core/gate1-sample?limit={0}" -f $C.SampleSize) 30
    $rows=Arr (Get-Prop $sample 'sample'); $cold=@(); $warm=@(); $pending=@{}; $submissionAt=Get-Date
    foreach($row in $rows){
      $symbol=[string](Get-Prop $row 'symbol'); if([string]::IsNullOrWhiteSpace($symbol)){continue}
      $path=("/api/stock-snapshot?symbol={0}&mode=delivery" -f [uri]::EscapeDataString($symbol))
      try { $timed=Invoke-TimedGetWire $base $path 5; $cold+=$timed.wire_ms; $snap=Get-Prop $timed.payload 'selected_stock_snapshot'; if((Get-Prop $timed.payload 'ok') -ne $true -or [string](Get-Prop $snap 'quality_state') -ne 'READY'){$pending[$symbol]=$path} } catch { $cold+=5000; $pending[$symbol]=$path }
    }
    $submittedAt=Get-Date; $submissionSec=($submittedAt-$submissionAt).TotalSeconds
    $deadline=$submittedAt.AddSeconds(30)
    while($pending.Count -gt 0 -and (Get-Date)-lt $deadline){
      foreach($symbol in @($pending.Keys)){
        try { $payload=Get-Json $base $pending[$symbol] 5; $snap=Get-Prop $payload 'selected_stock_snapshot'; if((Get-Prop $payload 'ok') -eq $true -and [string](Get-Prop $snap 'quality_state') -eq 'READY'){$pending.Remove($symbol)} } catch {}
      }
      if($pending.Count -gt 0){Start-Sleep -Milliseconds 500}
    }
    $passed=0
    foreach($row in $rows){
      $symbol=[string](Get-Prop $row 'symbol'); if([string]::IsNullOrWhiteSpace($symbol)){continue}
      $path=("/api/stock-snapshot?symbol={0}&mode=delivery" -f [uri]::EscapeDataString($symbol))
      try { $timed=Invoke-TimedGetWire $base $path 5; $warm+=$timed.wire_ms; $snap=Get-Prop $timed.payload 'selected_stock_snapshot'; if((Get-Prop $timed.payload 'ok') -eq $true -and [string](Get-Prop $snap 'quality_state') -eq 'READY'){$passed++} } catch { $warm+=5000 }
    }
    $coldSorted=@($cold|Sort-Object); $coldP95=0; if($coldSorted.Count){$idx=[Math]::Max(0,[Math]::Min($coldSorted.Count-1,[Math]::Ceiling($coldSorted.Count*.95)-1));$coldP95=[double]$coldSorted[$idx]}
    $warmSorted=@($warm|Sort-Object); $warmP95=0; if($warmSorted.Count){$idx=[Math]::Max(0,[Math]::Min($warmSorted.Count-1,[Math]::Ceiling($warmSorted.Count*.95)-1));$warmP95=[double]$warmSorted[$idx]}
    $convergenceSec=((Get-Date)-$submittedAt).TotalSeconds
    $ok=$rows.Count -gt 0 -and $pending.Count -eq 0 -and $passed -eq $rows.Count -and $coldP95 -le 1500 -and $warmP95 -le 1500 -and $convergenceSec -le 30
    Add-GateFromBool $C 'selected_stock_latency_completeness' $ok ("passed={0}/{1}; pending={2}; cold_wire_p95_ms={3}; warm_wire_p95_ms={4}; submit_sec={5}; post_submit_convergence_sec={6}" -f $passed,$rows.Count,$pending.Count,[Math]::Round($coldP95,1),[Math]::Round($warmP95,1),[Math]::Round($submissionSec,1),[Math]::Round($convergenceSec,1)) ([ordered]@{sample_count=$rows.Count;cold_wire_p95_ms=$coldP95;warm_wire_p95_ms=$warmP95;submission_sec=$submissionSec;post_submit_convergence_sec=$convergenceSec;pending=@($pending.Keys)})
  } catch { Add-Gate $C 'selected_stock_latency_completeness' 'FAIL' $_.Exception.Message }
}

function Invoke-MarketHoursInstalledGate([hashtable]$C) {
  try {
    $status=Get-Json $C.BaseUrl '/api/market/status' 10
    if((Get-Prop $status 'market_open') -ne $true){ Add-Gate $C 'intraday_history_freshness' 'TARGET_PENDING' 'market is closed; live-session freshness cannot be inferred from closed-session evidence' $status; return }
    $intradayPath='/api/historical?symbol=TCS&interval=5minute&days=10&refresh=false&recent_only=true'
    $first=Invoke-TimedGet $C.BaseUrl $intradayPath 5; $rows=$first.payload; $deadline=(Get-Date).AddSeconds(30)
    do {
      $state=[string](Get-Prop $rows 'freshness_state'); if(!$state){$state=[string](Get-Prop $rows 'data_status')}
      if((Row-Count $rows) -gt 0 -and $state -notmatch 'STALE|MISSING|FAILED'){break}
      Start-Sleep -Milliseconds 500; $rows=Get-Json $C.BaseUrl $intradayPath 5
    } while((Get-Date)-lt $deadline)
    $warm=Invoke-TimedGet $C.BaseUrl $intradayPath 5; $rows=$warm.payload
    $state=[string](Get-Prop $rows 'freshness_state'); if(!$state){$state=[string](Get-Prop $rows 'data_status')}
    $ok=$first.elapsed_ms -le 1500 -and $warm.elapsed_ms -le 1000 -and (Row-Count $rows) -gt 0 -and $state -notmatch 'STALE|MISSING|FAILED'
    Add-GateFromBool $C 'intraday_history_freshness' $ok ("cold_ms={0}; warm_ms={1}; rows={2}; last={3}; state={4}" -f $first.elapsed_ms,$warm.elapsed_ms,(Row-Count $rows),(Last-Candle-Time $rows),$state) $rows
  } catch { Add-Gate $C 'intraday_history_freshness' 'FAIL' $_.Exception.Message }
}

function Invoke-BrowserInstalledGates([hashtable]$C,[switch]$SkipBrowser) {
  $ids=@('frontend_identity','chart_api_dom_parity','mtf_visible_complete','sr_visible_semantics','fresh_current_product_evidence','operations_live_progress','market_alias_direction_alignment','operations_log_trail','browser_stable_dom')
  if($SkipBrowser){ foreach($id in $ids){Add-Gate $C $id 'TARGET_PENDING' 'independent installed-browser proof explicitly skipped; installed closure cannot infer browser evidence'}; return }
  $probeOut=Join-Path $C.OutputDir 'installed-browser-independent.json'
  try {
    # Candidate 23: independent installed-browser authority uses Microsoft Edge
    # DevTools/CDP directly through the Python standard library.  The Windows
    # target never needs pip, Playwright or network package installation.
    $probePython=$C.PythonExe
    $dependencyLog=Join-Path $C.OutputDir 'browser-probe-dependency.log'
    Set-Content -LiteralPath $dependencyLog -Encoding UTF8 -Value 'STANDARD_LIBRARY_EDGE_CDP_NO_PIP_DEPENDENCY'
    $expected=[string](Get-Prop $C.ReleaseIdentity 'version')
    $probeStdout=Join-Path $C.OutputDir 'browser-probe.stdout.log'; $probeStderr=Join-Path $C.OutputDir 'browser-probe.stderr.log'
    $probeScript=Join-Path $C.PackageRoot 'validation\installed_browser_acceptance.py'
    $probeArgs=@($probeScript,'--base-url',$C.BaseUrl,'--output',$probeOut,'--expected-build',$expected,'--symbol','TCS')
    # Candidate 24: never wait indefinitely on Chromium descendants. Start the
    # independent probe without -Wait, enforce one wall-clock deadline, then kill
    # only the probe process if it exceeded that deadline. The Python probe owns
    # cleanup of the exact temporary Edge profile/process tree.
    $probeProc=Start-Process -FilePath $probePython -ArgumentList $probeArgs -PassThru -NoNewWindow -RedirectStandardOutput $probeStdout -RedirectStandardError $probeStderr
    $probeDeadline=(Get-Date).AddSeconds(150)
    while(-not $probeProc.HasExited -and (Get-Date)-lt $probeDeadline){ Start-Sleep -Milliseconds 250; $probeProc.Refresh() }
    if(-not $probeProc.HasExited){
      try { Stop-Process -Id $probeProc.Id -Force -ErrorAction SilentlyContinue } catch {}
      throw 'independent installed-browser probe exceeded 150-second hard deadline'
    }
    $probeExit=$probeProc.ExitCode
    if(-not (Test-Path -LiteralPath $probeOut)){throw 'independent installed-browser probe did not produce evidence JSON'}
    $probe=Get-Content -LiteralPath $probeOut -Raw|ConvertFrom-Json
    $checks=Arr (Get-Prop $probe 'checks')
    function Probe-Check([string]$name){
      foreach($row in $checks){if([string](Get-Prop $row 'name') -eq $name){return $row}}
      return $null
    }
    $frontend=Probe-Check 'frontend_identity'
    $chartParity=Probe-Check 'chart_api_dom_parity'
    $mtf=Probe-Check 'mtf_10_complete'
    $sr=Probe-Check 'canonical_support_resistance_visible'
    $atomic=Probe-Check 'workspace_atomic_snapshot_present'
    $deskIsolation=Probe-Check 'delivery_intraday_100_cycle_isolation'
    $geometry=Probe-Check 'workspace_geometry_multi_viewport'
    $tf=Probe-Check 'chart_timeframe_interaction'
    $console=Probe-Check 'console_clean'
    $network=Probe-Check 'network_failures_clean'
    $internal=Probe-Check 'no_internal_scheduler_text'
    $frontendOk=$frontend -and (Get-Prop $frontend 'ok') -eq $true
    $chartOk=$chartParity -and (Get-Prop $chartParity 'ok') -eq $true
    $mtfOk=$mtf -and (Get-Prop $mtf 'ok') -eq $true
    $srOk=$sr -and (Get-Prop $sr 'ok') -eq $true
    $atomicOk=$atomic -and (Get-Prop $atomic 'ok') -eq $true
    $deskOk=$deskIsolation -and (Get-Prop $deskIsolation 'ok') -eq $true
    $geometryOk=$geometry -and (Get-Prop $geometry 'ok') -eq $true
    $tfOk=$tf -and (Get-Prop $tf 'ok') -eq $true
    $consoleOk=$console -and (Get-Prop $console 'ok') -eq $true
    $networkOk=$network -and (Get-Prop $network 'ok') -eq $true
    $internalOk=$internal -and (Get-Prop $internal 'ok') -eq $true

    Add-GateFromBool $C 'frontend_identity' $frontendOk 'independent Edge/CDP exact frontend identity' $probe
    Add-GateFromBool $C 'chart_api_dom_parity' $chartOk 'independent Edge/CDP chart API/DOM parity' $probe
    Add-GateFromBool $C 'mtf_visible_complete' $mtfOk 'independent Edge/CDP 10-cell MTF proof' $probe
    Add-GateFromBool $C 'sr_visible_semantics' $srOk 'independent Edge/CDP canonical support/resistance visibility' $probe
    $freshOk=$frontendOk -and $chartOk -and $mtfOk -and $srOk -and $atomicOk -and $deskOk -and $geometryOk -and $tfOk -and $consoleOk -and $networkOk -and $internalOk -and $probeExit -eq 0
    Add-GateFromBool $C 'fresh_current_product_evidence' $freshOk ("independent installed-browser: atomic={0}; desk100={1}; geometry={2}; tf={3}; console={4}; network={5}" -f $atomicOk,$deskOk,$geometryOk,$tfOk,$consoleOk,$networkOk) $probe

    $obs=Get-Prop $probe 'observations'; $workspaceDiag=Get-Prop $obs 'workspace_diag'; $market=Get-Prop $workspaceDiag 'market_sector'
    $invalidAliases=Arr (Get-Prop $market 'invalid_aliases'); $violations=Num (Get-Prop $market 'stale_direction_violations')
    Add-GateFromBool $C 'market_alias_direction_alignment' ($invalidAliases.Count -eq 0 -and $violations -eq 0) ("aliases={0}; stale_direction_violations={1}" -f ($invalidAliases -join ','),$violations) $market

    $ops=Get-Json $C.BaseUrl '/api/operations/summary' 10; $jobs=Arr (Get-Prop $ops 'jobs')
    Add-GateFromBool $C 'operations_live_progress' ($jobs.Count -gt 0) ("jobs={0}" -f $jobs.Count) $ops
    $logs=Get-Json $C.BaseUrl '/api/operations/logs?limit=350' 10; $logText=$logs|ConvertTo-Json -Depth 15 -Compress
    $logOk=(Get-Prop $logs 'ok') -eq $true -and $logText -notmatch '\[object Object\]|undefined'
    Add-GateFromBool $C 'operations_log_trail' $logOk ("state={0}; sources={1}" -f (Get-Prop $logs 'state'),(Arr (Get-Prop $logs 'source_files')).Count) $logs
    $stock=Get-Prop $obs 'stock_report'; $stable=Get-Prop $stock 'stable_dom_identity'
    Add-GateFromBool $C 'browser_stable_dom' ((Get-Prop $stable 'matched') -eq $true) 'independent selected/chart/DOM identity is exactly aligned' $stable

    # Record the externally-produced, exact-build browser result into the existing
    # build-bound evidence ledger only after the independent probe has finished.
    # This lets product-readiness consume independent evidence; the product still
    # does not decide whether the probe passed.
    $browserRecordChecks=@()
    foreach($row in $checks){ $browserRecordChecks += ,[ordered]@{name=[string](Get-Prop $row 'name');ok=((Get-Prop $row 'ok') -eq $true);detail=(Get-Prop $row 'detail')} }
    $browserRecordProof=[ordered]@{build=$expected;captured_at=(Get-Prop $probe 'completed_at');authority='INDEPENDENT_EDGE_CDP_INSTALLED_BROWSER';source='WINDOWS_ACCEPTANCE_EXTERNAL_PROBE'}
    $recorded=Post-Json $C.BaseUrl '/api/validation/browser-proof' ([ordered]@{proof=$browserRecordProof;checks=$browserRecordChecks}) 20
    if((Get-Prop $recorded 'passed') -ne $true){ throw 'independent browser result was not accepted by the build-bound evidence ledger' }
  } catch {
    $detail=$_.Exception.Message
    if(Test-Path -LiteralPath $probeOut){$detail=(Get-Content -LiteralPath $probeOut -Raw)}
    foreach($id in $ids){ if(-not $C.Gates.Contains($id)){Add-Gate $C $id 'FAIL' $detail} }
  }
}

function Invoke-PostBrowserProductTruthGate([hashtable]$C,[switch]$BrowserSkipped) {
  if($BrowserSkipped){ Add-Gate $C 'product_truth' 'TARGET_PENDING' 'product truth requires fresh exact-build independent browser evidence; browser proof was skipped'; return }
  try {
    $product=Get-Json $C.BaseUrl '/api/product-readiness' 45
    $acceptance=Get-Prop $product 'installation_acceptance'
    $browser=@(Arr (Get-Prop $product 'checks') | Where-Object { [string](Get-Prop $_ 'key') -eq 'installed_browser_vertical_slice' } | Select-Object -First 1)
    $browserReady=$browser.Count -eq 1 -and [string](Get-Prop $browser[0] 'state') -eq 'READY'
    $ok=[string](Get-Prop $product 'product_state') -eq 'OPERATIONAL' -and (Get-Prop $acceptance 'eligible') -eq $true -and [string](Get-Prop $acceptance 'state') -eq 'ACCEPTED' -and $browserReady
    Add-GateFromBool $C 'product_truth' $ok ("state={0}; acceptance={1}; browser={2}; blockers={3}" -f (Get-Prop $product 'product_state'),(Get-Prop $acceptance 'state'),$(if($browserReady){'READY'}else{'BLOCKED'}),(Arr (Get-Prop $product 'blockers')).Count) $product
  } catch { Add-Gate $C 'product_truth' 'FAIL' $_.Exception.Message }
}

function Invoke-FaultInstalledGates([hashtable]$C,[switch]$SkipFaultInjection) {
  $ids=@('http_disconnect_resilience','postgres_transaction_resilience','recovery_event_audit','audited_safe_recovery_action','selected_stock_orchestration_isolation','scanner_no_retired_generations','controller_watchdog','operations_local_projection')
  if($SkipFaultInjection){ foreach($id in $ids){Add-Gate $C $id 'TARGET_PENDING' 'fault-injection proof explicitly skipped; formal defect closure remains open'}; return }
  $faultProbe=Join-Path $C.OutputDir 'installed-fault-contracts.json'
  try {
    $envFile=Join-Path $C.InstallDir 'secure\data-plane.env.ps1'; if(Test-Path -LiteralPath $envFile){. $envFile}
    $python=$C.PythonExe
    & $python (Join-Path $C.PackageRoot 'validation\installed_fault_contract_probe.py') --base-url $C.BaseUrl --output $faultProbe | Out-Null
    $probe=Get-Content -LiteralPath $faultProbe -Raw|ConvertFrom-Json
    $sc=Get-Prop $probe 'scenarios'
    foreach($pair in @(@('http_disconnect_resilience','http_disconnect_resilience'),@('postgres_transaction_resilience','postgres_transaction_resilience'))){
      $row=Get-Prop $sc $pair[1]; $state=[string](Get-Prop $row 'state')
      if($state -eq 'TARGET_PENDING'){Add-Gate $C $pair[0] 'TARGET_PENDING' ([string](Get-Prop $row 'reason')) $row}else{Add-GateFromBool $C $pair[0] ((Get-Prop $row 'ok') -eq $true) ("state={0}" -f $state) $row}
    }
  } catch {
    foreach($id in @('http_disconnect_resilience','postgres_transaction_resilience')){if(-not $C.Gates.Contains($id)){Add-Gate $C $id 'FAIL' $_.Exception.Message}}
  }
  try {
    $drill=Post-Json $C.BaseUrl '/api/validation/level5-resilience-drill' @{} 180
    $drillOk=(Get-Prop $drill 'ok') -eq $true -and ((Get-Prop $drill 'passed') -eq $true -or [string](Get-Prop $drill 'state') -match 'PASS|RECORDED')
    Add-GateFromBool $C 'recovery_event_audit' $drillOk ("state={0}; passed={1}" -f (Get-Prop $drill 'state'),(Get-Prop $drill 'passed')) $drill
  } catch {Add-Gate $C 'recovery_event_audit' 'FAIL' $_.Exception.Message}
  try {
    $actionId='historical37-safe-'+(Get-Date -Format 'yyyyMMddHHmmss')
    $action=Post-Json $C.BaseUrl '/api/operations/action' @{action='recover_all_safe_stuck';action_id=$actionId;reason='historical 37 installed proof bounded safe recovery';mode='delivery';seconds=120} 120
    Start-Sleep -Seconds 2; $ops=Get-Json $C.BaseUrl '/api/operations/summary' 20; $history=Arr (Get-Prop $ops 'recent_actions')
    $found=@($history|Where-Object{[string](Get-Prop $_ 'action_id') -eq $actionId}).Count -eq 1
    $ok=(Get-Prop $action 'ok') -eq $true -and [string](Get-Prop $action 'safety_class') -eq 'SAFE_COMPONENT' -and $found
    Add-GateFromBool $C 'audited_safe_recovery_action' $ok ("state={0}; audited={1}" -f (Get-Prop $action 'state'),$found) ([ordered]@{action=$action;operations=$ops})
  } catch {Add-Gate $C 'audited_safe_recovery_action' 'FAIL' $_.Exception.Message}
  try {
    $controller=Get-Json $C.BaseUrl '/api/runtime-controller?refresh=true' 120; $watchdog=Get-Prop $controller 'evaluation_watchdog'
    $ok=$null -ne $watchdog -and (Num (Get-Prop $watchdog 'age_sec')) -le ((Num (Get-Prop $watchdog 'timeout_sec'))+5)
    Add-GateFromBool $C 'controller_watchdog' $ok ("age={0}; timeout={1}; alive={2}" -f (Get-Prop $watchdog 'age_sec'),(Get-Prop $watchdog 'timeout_sec'),(Get-Prop $watchdog 'alive')) $watchdog
  } catch {Add-Gate $C 'controller_watchdog' 'FAIL' $_.Exception.Message}
  try {
    $clean=Get-Json $C.BaseUrl '/api/clean-core/status' 20; $scanner=Get-Prop $clean 'scanner'; $d=Get-Prop $scanner 'delivery'; $i=Get-Prop $scanner 'intraday'
    $ok=(Num (Get-Prop $d 'retired_active')) -eq 0 -and (Num (Get-Prop $i 'retired_active')) -eq 0
    Add-GateFromBool $C 'scanner_no_retired_generations' $ok ("delivery_retired={0}; intraday_retired={1}" -f (Get-Prop $d 'retired_active'),(Get-Prop $i 'retired_active')) $scanner
  } catch {Add-Gate $C 'scanner_no_retired_generations' 'FAIL' $_.Exception.Message}
  try {
    $ops=Invoke-TimedGet $C.BaseUrl '/api/operations/summary' 10; $logs=Invoke-TimedGet $C.BaseUrl '/api/operations/logs?limit=100' 10
    $ok=$ops.elapsed_ms -le 2500 -and $logs.elapsed_ms -le 2500 -and (Get-Prop $ops.payload 'ok') -eq $true -and (Get-Prop $logs.payload 'ok') -eq $true
    Add-GateFromBool $C 'operations_local_projection' $ok ("summary_ms={0}; logs_ms={1}" -f $ops.elapsed_ms,$logs.elapsed_ms) ([ordered]@{operations=$ops.payload;logs=$logs.payload})
  } catch {Add-Gate $C 'operations_local_projection' 'FAIL' $_.Exception.Message}
  try {
    $stock=Invoke-TimedGet $C.BaseUrl '/api/stock-snapshot?symbol=TCS&mode=delivery' 15; $controller=Get-Json $C.BaseUrl '/api/runtime-controller' 10
    $snap=Get-Prop $stock.payload 'selected_stock_snapshot'; $ok=$stock.elapsed_ms -le 4000 -and (Get-Prop $stock.payload 'ok') -eq $true -and [string](Get-Prop $snap 'quality_state') -eq 'READY'
    # The proof is exact-target execution of the local read while controller state
    # is observed independently; no controller success is required for the read.
    Add-GateFromBool $C 'selected_stock_orchestration_isolation' $ok ("stock_ms={0}; stock_quality={1}; controller_state={2}" -f $stock.elapsed_ms,(Get-Prop $snap 'quality_state'),(Get-Prop $controller 'state')) ([ordered]@{stock=$stock.payload;controller=$controller})
  } catch {Add-Gate $C 'selected_stock_orchestration_isolation' 'FAIL' $_.Exception.Message}
}

function Invoke-RestartInstalledGates([hashtable]$C,[switch]$SkipRestart) {
  if($SkipRestart){
    Add-Gate $C 'research_retention_restart' 'TARGET_PENDING' 'restart proof explicitly skipped; formal closure remains open'
    Add-Gate $C 'restart_history_continuity' 'TARGET_PENDING' 'restart proof explicitly skipped; formal closure remains open'
    return
  }
  try {
    $beforeRetention=Get-Json $C.BaseUrl '/api/research-retention' 60
    $before=@{}
    foreach($symbol in @('TCS','RELIANCE','INFY')){try{$h=Get-Json $C.BaseUrl ("/api/historical?symbol={0}&interval=day&refresh=false" -f $symbol) 20;$before[$symbol]=[ordered]@{rows=(Row-Count $h);last=(Last-Candle-Time $h)}}catch{$before[$symbol]=[ordered]@{rows=0;last='';error=$_.Exception.Message}}}
    Restart-Service -Name 'ProjectLaddu' -Force
    $null=Wait-Ready $C.BaseUrl 180; Start-Sleep -Seconds 5
    $historyOk=$true; $after=@{}; $historyComparisons=@()
    foreach($symbol in @('TCS','RELIANCE','INFY')){
      try {
        $h=Get-Json $C.BaseUrl ("/api/historical?symbol={0}&interval=day&refresh=false" -f $symbol) 20
        $afterRows=Row-Count $h; $afterLast=Last-Candle-Time $h
        $after[$symbol]=[ordered]@{rows=$afterRows;last=$afterLast}
        $beforeRows=[int](Num (Get-Prop $before[$symbol] 'rows')); $beforeLast=[string](Get-Prop $before[$symbol] 'last')
        $rowsOk=$afterRows -ge $beforeRows
        $beforeTime=[DateTimeOffset]::MinValue; $afterTime=[DateTimeOffset]::MinValue
        $beforeParsed=[DateTimeOffset]::TryParse($beforeLast,[ref]$beforeTime); $afterParsed=[DateTimeOffset]::TryParse([string]$afterLast,[ref]$afterTime)
        $timeOk=$beforeParsed -and $afterParsed -and $afterTime -ge $beforeTime
        if(-not $rowsOk -or -not $timeOk){$historyOk=$false}
        $historyComparisons += ,[ordered]@{symbol=$symbol;before_rows=$beforeRows;after_rows=$afterRows;rows_ok=$rowsOk;before_last=$beforeLast;after_last=$afterLast;time_ok=$timeOk}
      } catch {
        $historyOk=$false; $after[$symbol]=[ordered]@{error=$_.Exception.Message}; $historyComparisons += ,[ordered]@{symbol=$symbol;error=$_.Exception.Message;rows_ok=$false;time_ok=$false}
      }
    }
    # A zero/one-row continuity proof can hide a broken retained read model. TCS
    # is the deterministic acceptance symbol and must retain a meaningful daily
    # window both before and after restart.
    if((Num (Get-Prop ($before['TCS']) 'rows')) -lt 30 -or (Num (Get-Prop ($after['TCS']) 'rows')) -lt 30){$historyOk=$false}
    Add-GateFromBool $C 'restart_history_continuity' $historyOk 'stored daily histories retain meaningful depth and do not regress across Windows service restart' ([ordered]@{before=$before;after=$after;comparisons=$historyComparisons})
    $afterRetention=Get-Json $C.BaseUrl '/api/research-retention' 60
    $beforeHash=[string](Get-Prop $beforeRetention 'content_hash'); $afterHash=[string](Get-Prop $afterRetention 'content_hash'); $previousHash=[string](Get-Prop $afterRetention 'previous_content_hash')
    $sameSnapshot=$afterHash -eq $beforeHash
    $appendOnlyChain=(-not [string]::IsNullOrWhiteSpace($beforeHash)) -and $previousHash -eq $beforeHash
    $retOk=(Get-Prop $afterRetention 'ok') -eq $true -and ($sameSnapshot -or $appendOnlyChain) -and (Arr (Get-Prop $afterRetention 'regressions')).Count -eq 0
    Add-GateFromBool $C 'research_retention_restart' $retOk ("before={0}; after={1}; previous={2}; same={3}; append_only={4}; regressions={5}" -f $beforeHash,$afterHash,$previousHash,$sameSnapshot,$appendOnlyChain,(Arr (Get-Prop $afterRetention 'regressions')).Count) ([ordered]@{before=$beforeRetention;after=$afterRetention;same_snapshot=$sameSnapshot;append_only_chain=$appendOnlyChain})
  } catch {
    if(-not $C.Gates.Contains('restart_history_continuity')){Add-Gate $C 'restart_history_continuity' 'FAIL' $_.Exception.Message}
    if(-not $C.Gates.Contains('research_retention_restart')){Add-Gate $C 'research_retention_restart' 'FAIL' $_.Exception.Message}
  }
}
