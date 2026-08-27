param(
  [string]$BaseUrl = 'http://127.0.0.1:8086',
  [int]$TimeoutSec = 8,
  [string]$ExpectedVersion = '',
  [switch]$FailOnBlocked
)
$ErrorActionPreference = 'Stop'

function Get-Json([string]$Path, [int]$Timeout = 0) {
  $effective = if($Timeout -gt 0){$Timeout}else{$TimeoutSec}
  Invoke-RestMethod -Uri ($BaseUrl.TrimEnd('/') + $Path) -TimeoutSec $effective
}
function Post-Json([string]$Path, [hashtable]$Body) {
  Invoke-RestMethod -Method Post -Uri ($BaseUrl.TrimEnd('/') + $Path) -ContentType 'application/json' -Body ($Body | ConvertTo-Json -Depth 8) -TimeoutSec $TimeoutSec
}
function Find-Check($Product, [string]$Key) {
  @($Product.checks | Where-Object { $_.key -eq $Key }) | Select-Object -First 1
}

$releaseIdentityPath = Join-Path $PSScriptRoot 'RELEASE_IDENTITY.json'
$releaseIdentity = $null
if(Test-Path -LiteralPath $releaseIdentityPath -PathType Leaf){
  try { $releaseIdentity = Get-Content -LiteralPath $releaseIdentityPath -Raw | ConvertFrom-Json } catch { throw "RELEASE_IDENTITY.json is unreadable: $($_.Exception.Message)" }
}
if([string]::IsNullOrWhiteSpace($ExpectedVersion)){
  $ExpectedVersion = if($null -ne $releaseIdentity){[string]$releaseIdentity.version}else{''}
}
if([string]::IsNullOrWhiteSpace($ExpectedVersion) -or $ExpectedVersion -notmatch '^v\d+\.\d+\.\d+$'){
  throw 'ExpectedVersion must come from the packaged RELEASE_IDENTITY.json or be supplied explicitly.'
}
if($null -ne $releaseIdentity -and [string]$releaseIdentity.version -ne $ExpectedVersion){
  throw "ExpectedVersion does not match packaged RELEASE_IDENTITY.json: expected=$ExpectedVersion packaged=$($releaseIdentity.version)"
}

$report = [ordered]@{
  checked_at = (Get-Date).ToString('o')
  base_url = $BaseUrl
  expected_version = $ExpectedVersion
  checks = @()
  product = $null
  evidence_boundaries = [ordered]@{
    software_runtime = 'tested by this verifier'
    installed_operational = 'passes only when all mandatory checks pass'
    trading_edge = 'not proven by installation or package availability'
  }
}
function Add-Check([string]$Name, [bool]$Ok, [string]$Detail, [object]$Data = $null, [bool]$Mandatory = $true) {
  $script:report.checks += [ordered]@{ name=$Name; ok=$Ok; mandatory=$Mandatory; detail=$Detail; data=$Data }
  $mark = if($Ok){'[PASS]'}elseif($Mandatory){'[FAIL]'}else{'[INFO]'}
  $colour = if($Ok){'Green'}elseif($Mandatory){'Red'}else{'Yellow'}
  Write-Host "$mark $Name - $Detail" -ForegroundColor $colour
}

try {
  $ready = Get-Json '/api/ready'
  Add-Check 'Process readiness and build identity' ($ready.ready -eq $true -and $ready.version -eq $report.expected_version) ("version=" + $ready.version) $ready
} catch { Add-Check 'Process readiness and build identity' $false $_.Exception.Message }

try {
  $frontend = Get-Json '/api/frontend-identity'
  $expectedOwner = 'standalone-' + $report.expected_version
  $frontendOk = $frontend.ok -eq $true -and $frontend.version -eq $report.expected_version -and $frontend.manifest_version -eq $report.expected_version -and $frontend.frontend_owner -eq $expectedOwner -and @($frontend.mismatches).Count -eq 0
  Add-Check 'Exact installed frontend identity' $frontendOk ("version=$($frontend.version); manifest=$($frontend.manifest_version); owner=$($frontend.frontend_owner); mismatches=$(@($frontend.mismatches) -join ',')") $frontend
} catch { Add-Check 'Exact installed frontend identity' $false $_.Exception.Message }

try {
  $health = Get-Json '/api/health'
  $inst = $health.instruments
  $stats = $inst.universe_stats
  $instOk = $inst.loaded -eq $true -and $inst.cache_usable -eq $true -and [int]$inst.count -gt 0 -and $inst.universe_revision -eq 'nse-first-bse-fallback-ordinary-equity-v69.8.0' -and [int]$stats.nse_equities -gt 0 -and [int]$stats.bse_only_equities -gt 0 -and [int]$stats.indices -gt 0 -and [int]$stats.derivatives -eq 0 -and [int]$stats.out_of_policy_rows -eq 0
  Add-Check 'Health and focused instrument authority' ($health.service -eq 'running' -and $instOk) ("service=$($health.service); NSE=$($stats.nse_equities); BSE-only=$($stats.bse_only_equities); indices=$($stats.indices); derivatives=$($stats.derivatives); out-of-policy=$($stats.out_of_policy_rows); revision=$($inst.universe_revision)") $health
} catch { Add-Check 'Health and instrument authority' $false $_.Exception.Message }

try {
  $product = Get-Json '/api/product-readiness'
  $report.product = $product
  $dataPlane = Find-Check $product 'production_data_plane'
  $startup = Find-Check $product 'startup_phases'
  $identity = Find-Check $product 'instrument_identity'
  $searchGate = Find-Check $product 'search'
  $researchGate = Find-Check $product 'quant_research_plane'
  $acceptance = $product.installation_acceptance
  $ok = $product.product_state -eq 'OPERATIONAL' -and $acceptance.eligible -eq $true -and $acceptance.state -eq 'ACCEPTED' -and $dataPlane.state -eq 'READY' -and $startup.state -eq 'READY' -and $identity.state -eq 'READY' -and $searchGate.state -eq 'READY' -and $researchGate.state -eq 'READY'
  Add-Check 'Installed-product truth gate' $ok ("state=$($product.product_state); acceptance=$($acceptance.state); data-plane=$($dataPlane.state); startup=$($startup.state); identity=$($identity.state); search=$($searchGate.state); quant-research=$($researchGate.state); reasons=$(@($acceptance.reason_codes) -join ',')") $product
  foreach($blocker in @($product.blockers)) {
    Write-Host ("  - {0}: {1} | action: {2}" -f $blocker.code,$blocker.message,$blocker.action) -ForegroundColor Yellow
  }
  $usefulness = $product.customer_usefulness
  $useful = [string]$usefulness.state -eq 'READY'
  Add-Check 'Customer usefulness evidence' $useful ("state=$($usefulness.state); coverage=$($usefulness.verified_coverage); actionable=$($usefulness.verified_actionable); watchlist=$($usefulness.next_session_watchlist)") $usefulness $false
} catch { Add-Check 'Installed-product truth gate' $false $_.Exception.Message }

try {
  $settings = Get-Json '/api/operator-settings'
  $wallet = [double]$settings.settings.model_wallet
  $intradayCap = [double]$settings.settings.intraday_exposure_ceiling
  $settingsOk = $settings.ok -eq $true -and $settings.settings.editable -eq $true -and
    $wallet -ge 10000 -and $intradayCap -ge 0 -and $intradayCap -le $wallet -and
    [string]$settings.settings.broker_authority -eq 'NONE' -and
    [string]$settings.settings.applies_to -eq 'future_model_paper_admissions_only'
  Add-Check 'Governed Model Paper capital settings' $settingsOk ("wallet=$wallet; intraday_ceiling=$intradayCap; broker=$($settings.settings.broker_authority); applies_to=$($settings.settings.applies_to)") $settings
} catch { Add-Check 'Governed Model Paper capital settings' $false $_.Exception.Message }

try {
  $dataPlaneStatus = if($null -ne $health.production_data_plane){ $health.production_data_plane } else { $health.scanner.production_data_plane }
  $components = if($null -ne $dataPlaneStatus.planes){$dataPlaneStatus.planes}else{$dataPlaneStatus.components}
  $operational = if($null -ne $components.operational){$components.operational}else{$components.operational_postgres}
  $governance = if($null -ne $components.governance){$components.governance}else{$components.governance_postgres}
  $questdb = $components.questdb
  $dataPlaneOk = $dataPlaneStatus.mode -eq 'production' -and
    $dataPlaneStatus.production_ready -eq $true -and
    @($dataPlaneStatus.blockers).Count -eq 0 -and
    $operational.ok -eq $true -and
    $governance.ok -eq $true -and
    $questdb.ok -eq $true
  Add-Check 'Four-plane production authority' $dataPlaneOk ("mode=$($dataPlaneStatus.mode); operational=$($operational.ok); governance=$($governance.ok); questdb=$($questdb.ok); blockers=$(@($dataPlaneStatus.blockers) -join ',')") $dataPlaneStatus
} catch { Add-Check 'Four-plane production authority' $false $_.Exception.Message }

try {
  $architecture = Get-Json '/api/architecture'
  $runtimeAuthority = $architecture.runtime_authority
  $runtimeOk = $architecture.compatibility_runtime_dependency -eq $false -and $runtimeAuthority.storage_engine -eq 'in_process_memory'
  Add-Check 'Canonical runtime authority' $runtimeOk ("engine=$($runtimeAuthority.storage_engine); owner=$($runtimeAuthority.owner_class); compatibility_dependency=$($architecture.compatibility_runtime_dependency)") $architecture

  $snapshots = @($architecture.authority.snapshots)
  $deliverySnapshot = @($snapshots | Where-Object { [string]$_.desk -eq 'DELIVERY' }) | Select-Object -First 1
  $intradaySnapshot = @($snapshots | Where-Object { [string]$_.desk -eq 'INTRADAY' }) | Select-Object -First 1
  $deliveryCount = if($null -ne $deliverySnapshot){[int]$deliverySnapshot.population_count}else{0}
  $intradayCount = if($null -ne $intradaySnapshot){[int]$intradaySnapshot.population_count}else{0}
  $canonicalCount = [int]$architecture.universe_authority.canonical_stocks
  $intradayEligibility = $architecture.universe_authority.eligibility
  $fullUniverseScreening =
    [string]$intradayEligibility.intraday_starting_filter -eq 'ALL_CANONICAL_INTRADAY_SERIES_IDENTITY_VERIFIED' -and
    $intradayEligibility.intraday_liquidity_is_ranking_evidence_not_universe_filter -eq $true
  $snapshotOk = $null -ne $deliverySnapshot -and $null -ne $intradaySnapshot -and
    $deliveryCount -gt 0 -and $deliveryCount -le 1500 -and
    $canonicalCount -gt 0 -and $intradayCount -gt 0 -and $intradayCount -le $canonicalCount -and
    $fullUniverseScreening -and
    -not [string]::IsNullOrWhiteSpace([string]$deliverySnapshot.content_hash) -and
    -not [string]::IsNullOrWhiteSpace([string]$intradaySnapshot.content_hash)
  Add-Check 'Canonical desk universe snapshots' $snapshotOk ("Canonical=$canonicalCount; Delivery=$deliveryCount; Intraday-screening=$intradayCount; full-universe-screening=$fullUniverseScreening; authority=$($architecture.authority.authority)") $architecture.universe_authority
} catch {
  Add-Check 'Canonical runtime authority' $false $_.Exception.Message
  Add-Check 'Canonical desk universe snapshots' $false $_.Exception.Message
}

try {
  $runtimeErrors = @($health.scanner.api_errors)
  if($runtimeErrors.Count -eq 0){ $runtimeErrors = @($health.api_errors) }
  $canonicalErrors = @($runtimeErrors | Where-Object {
    [string]$_.error -match 'canonical_decisions_side_check|Canonical decision write failed|idle-in-transaction timeout|server closed the connection unexpectedly'
  })
  Add-Check 'Canonical persistence error gate' ($canonicalErrors.Count -eq 0) ("recent canonical/transaction errors=" + $canonicalErrors.Count) $canonicalErrors
} catch { Add-Check 'Canonical persistence error gate' $false $_.Exception.Message }

$searchTimes = @()
foreach($symbol in @('TCS','RELIANCE','NIFTY 50')) {
  try {
    # Warm the local in-memory search path once. The measured request remains
    # cache-only and must resolve the exact focused identity.
    $null = Post-Json '/api/search' @{ q=$symbol; mode='delivery' }
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    $search = Post-Json '/api/search' @{ q=$symbol; mode='delivery' }
    $sw.Stop()
    $first = @($search.matches)[0]
    $resolved = $null -ne $first -and -not [string]::IsNullOrWhiteSpace([string]$first.instrument_key)
    $exactSymbol = if($symbol -eq 'NIFTY 50'){
      [string]$first.trading_symbol -in @('NIFTY 50','NIFTY50','NIFTY') -and [string]$first.instrument_key -match '^NSE_(INDEX|IDX)\|'
    } else {
      [string]$first.trading_symbol -eq $symbol
    }
    $latencyOk = $sw.ElapsedMilliseconds -lt 1000
    $searchTimes += $sw.ElapsedMilliseconds
    $slo = if($sw.ElapsedMilliseconds -lt 250){'target'}else{'within hard ceiling'}
    Add-Check ("Identity/search " + $symbol) ($resolved -and $exactSymbol -and $latencyOk) ("$($first.trading_symbol) -> $($first.instrument_key); $($sw.ElapsedMilliseconds)ms ($slo)") $search
  } catch { Add-Check ("Identity/search " + $symbol) $false $_.Exception.Message }
}

try {
  $healthForBse = Get-Json '/api/health'
  $bseSamples = @($healthForBse.instruments.universe_stats.bse_only_sample)
  $sample = $bseSamples | Select-Object -First 1
  if($null -eq $sample){
    Add-Check 'BSE-only equity inclusion' $false 'No BSE-only sample was published by the focused catalogue' $healthForBse.instruments
  } else {
    $bseSearch = Post-Json '/api/search' @{ q=$sample.trading_symbol; mode='delivery' }
    $match = @($bseSearch.matches | Where-Object { $_.instrument_key -eq $sample.instrument_key }) | Select-Object -First 1
    Add-Check 'BSE-only equity inclusion' ($null -ne $match) ("$($sample.trading_symbol) -> $($sample.instrument_key)") $bseSearch
  }
} catch { Add-Check 'BSE-only equity inclusion' $false $_.Exception.Message }

try {
  $coverageProof = Get-Json '/api/data-coverage?symbol=TCS' 20
  $catalog = $coverageProof.candle_catalog
  $catalogOk = $coverageProof.ok -eq $true -and $catalog.state -eq 'READY' -and $catalog.usable -eq $true -and [int]$catalog.unreadable_files -eq 0
  Add-Check 'Atomic candle catalogue authority' $catalogOk ("state=$($catalog.state); usable=$($catalog.usable); generation=$($catalog.serving_generation); rebuild=$($catalog.rebuild_state); series=$($catalog.series); files=$($catalog.files); unreadable=$($catalog.unreadable_files)") $coverageProof
} catch { Add-Check 'Atomic candle catalogue authority' $false $_.Exception.Message }

try {
  $selected = $null
  $selectedOk = $false
  for($attempt=0; $attempt -lt 4; $attempt++){
    $selected = Get-Json '/api/stock-intelligence?symbol=TCS&mode=delivery&refresh=false' 60
    $selectedQuote = $selected.selected_quote
    $selectedIdentity = $selected.identity_contract
    $quoteKey=[string]$selectedQuote.instrument_key
    $identityKey=[string]$selectedIdentity.instrument_key
    $quoteIdentityOk=([string]::IsNullOrWhiteSpace($quoteKey) -or $quoteKey -eq $identityKey)
    $selectedOk = $selected.ok -eq $true -and $selectedIdentity.ok -eq $true -and
      [double]$selectedQuote.ltp -gt 0 -and -not [string]::IsNullOrWhiteSpace($identityKey) -and $quoteIdentityOk
    if($selectedOk){ break }
    Start-Sleep -Seconds 4
  }
  $history = Get-Json '/api/historical?symbol=TCS&interval=30minute&refresh=false' 60
  $historyOk = $history.ok -eq $true -and [int]$history.count -gt 0 -and @($history.candles).Count -gt 0
  $freshness = [string]$selected.selected_quote.freshness_state
  $freshnessOk = $freshness -in @('live','current_at_close','closed_market')
  Add-Check 'Selected-stock report and chart truth' ($selectedOk -and $historyOk -and $freshnessOk) ("TCS price=$($selected.selected_quote.ltp); freshness=$freshness; chart_rows=$($history.count); intelligence_status=$($selected.selected_stock_truth.data_status)") ([ordered]@{ intelligence=$selected; history=[ordered]@{ok=$history.ok; count=$history.count; data_status=$history.data_status; freshness_state=$history.freshness_state} })
} catch { Add-Check 'Selected-stock report and chart truth' $false $_.Exception.Message }

try {
  $contextResults = @()
  foreach($contextSymbol in @('NIFTY','PHARMA')) {
    # Queue a real provider-valid cold-cache hydration first, then require
    # actual completed chart rows. Identity-only or perpetual pending states
    # are not accepted because the browser cannot render them.
    try { $null = Get-Json ("/api/historical?symbol=" + $contextSymbol + "&interval=day&refresh=true") 60 } catch {}
    $contextHistory = $null
    for($contextAttempt=0; $contextAttempt -lt 15; $contextAttempt++) {
      $contextHistory = Get-Json ("/api/historical?symbol=" + $contextSymbol + "&interval=day&refresh=false") 60
      $rowCount = [int]($contextHistory.count)
      if($contextHistory.ok -eq $true -and $rowCount -gt 0 -and @($contextHistory.candles).Count -gt 0){ break }
      Start-Sleep -Seconds 4
    }
    $contextKey = [string]$contextHistory.instrument.instrument_key
    if([string]::IsNullOrWhiteSpace($contextKey)){ $contextKey = [string]$contextHistory.instrument_key }
    $statusText = [string]($contextHistory.data_status)
    $errorText = [string]($contextHistory.error_type) + ' ' + [string]($contextHistory.message)
    $resolved = $contextKey -match '^(NSE|BSE)_(INDEX|IDX)\|'
    $notIdentityFailure = $errorText -notmatch 'identity|unresolv|not found'
    $renderable = $contextHistory.ok -eq $true -and [int]$contextHistory.count -gt 0 -and @($contextHistory.candles).Count -gt 0
    $contextResults += [ordered]@{
      symbol=$contextSymbol; ok=($resolved -and $notIdentityFailure -and $renderable)
      instrument_key=$contextKey; count=[int]$contextHistory.count
      data_status=$statusText; source=[string]$contextHistory.source
      last_candle=[string]$contextHistory.last_candle.timestamp; error=$errorText.Trim()
    }
  }
  $contextOk = @($contextResults | Where-Object { -not $_.ok }).Count -eq 0
  Add-Check 'Canonical NIFTY/sector chart rendering sequence' $contextOk (($contextResults | ForEach-Object { "$($_.symbol)=$($_.instrument_key) rows=$($_.count) [$($_.data_status)] error=$($_.error)" }) -join '; ') $contextResults
} catch { Add-Check 'Canonical NIFTY/sector chart rendering sequence' $false $_.Exception.Message }

try {
  $scannerProof = $null
  $intradayProgress = $null
  $deliveryProgress = $null
  for($attempt=0; $attempt -lt 8; $attempt++){
    $scannerProof = Get-Json '/api/scanner/status' 15
    # scanner-status-v2 wraps the immutable runtime snapshot under `scanner`.
    # Accept the direct shape only for compatibility with fixture/test servers.
    $scannerRoot = if($null -ne $scannerProof.scanner){$scannerProof.scanner}else{$scannerProof}
    $modes = $scannerRoot.mode_scanners
    $intradayProgress = if($null -ne $modes.intraday.progress_contract){$modes.intraday.progress_contract}else{$modes.intraday.analysis.progress_contract}
    $deliveryProgress = if($null -ne $modes.delivery.progress_contract){$modes.delivery.progress_contract}else{$modes.delivery.analysis.progress_contract}
    $universe = if($null -ne $scannerRoot.universe_authority.snapshots){
      $scannerRoot.universe_authority.snapshots
    } else {
      $scannerRoot.startup_phases.operational.universe.snapshots
    }
    $expectedIntraday = [int]$universe.intraday.population_count
    $expectedDelivery = [int]$universe.delivery.population_count
    $progressReady = $intradayProgress.version -eq 'scanner-progress-contract-3.0.0' -and
      $deliveryProgress.version -eq 'scanner-progress-contract-3.0.0' -and
      $expectedIntraday -gt 0 -and $expectedDelivery -gt 0 -and
      [int]$intradayProgress.population_count -eq $expectedIntraday -and
      [int]$deliveryProgress.population_count -eq $expectedDelivery
    if($progressReady){ break }
    Start-Sleep -Seconds 5
  }
  $iTotal = [int]$intradayProgress.population_count
  $dTotal = [int]$deliveryProgress.population_count
  $iCurrent = if($null -eq $intradayProgress.current_sweep_scanned){0}else{[int]$intradayProgress.current_sweep_scanned}
  $dCurrent = [int]$deliveryProgress.current_sweep_scanned
  $iLast = [int]$intradayProgress.last_completed_sweep_count
  $dLast = [int]$deliveryProgress.last_completed_sweep_count
  $invariants = $iCurrent -ge 0 -and $iCurrent -le $iTotal -and
    $dCurrent -ge 0 -and $dCurrent -le $dTotal -and
    $iLast -ge 0 -and $iLast -le $iTotal -and
    $dLast -ge 0 -and $dLast -le $dTotal -and
    [double]($deliveryProgress.current_sweep_pct) -ge 0 -and [double]($deliveryProgress.current_sweep_pct) -le 100
  $noLegacyLeak = -not ([string]$intradayProgress.display_detail -match '2382|1963') -and
    -not ([string]$deliveryProgress.display_detail -match '1927|2240')
  Add-Check 'Stable scanner progress authority' ($progressReady -and $invariants -and $noLegacyLeak) ("Intraday=$($intradayProgress.state) $($intradayProgress.display_value), last=$iLast/$iTotal; Delivery=$($deliveryProgress.state) $($deliveryProgress.display_value), last=$dLast/$dTotal") $scannerProof
} catch { Add-Check 'Stable scanner progress authority' $false $_.Exception.Message }

try {
  $bars = Get-Json '/api/canonical-bars'
  $configured = @($bars.health.configured_intervals)
  $required = @('1m','3m','5m','15m','30m','60m','240m')
  $actual = @{}
  foreach($row in @($bars.health.intervals)){
    if($row -and $row.interval){ $actual[[string]$row.interval] = [int]$row.rows }
  }
  $missingConfigured = @($required | Where-Object { $_ -notin $configured })
  $oneMinuteRows = if($actual.ContainsKey('1m')){ [int]$actual['1m'] } else { 0 }
  $missingPopulated = @()
  if($oneMinuteRows -gt 0){
    $missingPopulated = @($required | Where-Object { !$actual.ContainsKey($_) -or [int]$actual[$_] -le 0 })
  }
  $missing = @($missingConfigured + $missingPopulated | Select-Object -Unique)
  Add-Check 'Canonical intraday runtime bar rows' ($bars.ok -eq $true -and $missing.Count -eq 0) ("configured=$($configured -join ','); populated=$(@($actual.Keys) -join ','); missing=$($missing -join ',')") $bars
  $script:report['canonical_bars'] = $bars
} catch { Add-Check 'Canonical intraday runtime bar schema' $false $_.Exception.Message }


try {
  $bindingMtf = Get-Json '/api/binding-mtf-contract'
  $requiredRoster = @('30m','1H','4H','1D','1W','1M')
  $rosterOk = (@($bindingMtf.required_timeframes) -join ',') -eq ($requiredRoster -join ',')
  $proofOk = $bindingMtf.ok -eq $true -and $bindingMtf.state -eq 'READY' -and $rosterOk -and $bindingMtf.completed_periods_only -eq $true -and $bindingMtf.checks.weekly_identity_stable -eq $true -and $bindingMtf.checks.breakout_completed_close -eq $true -and $bindingMtf.checks.retest_completed_close -eq $true
  Add-Check 'Binding Delivery MTF/master-candle contract' $proofOk ("frames=$(@($bindingMtf.required_timeframes) -join ','); state=$($bindingMtf.weekly_master_candle.state); stable_identity=$($bindingMtf.checks.weekly_identity_stable); completed_breakout=$($bindingMtf.checks.breakout_completed_close); completed_retest=$($bindingMtf.checks.retest_completed_close)") $bindingMtf
  $script:report['binding_mtf_contract'] = $bindingMtf
} catch { Add-Check 'Binding Delivery MTF/master-candle contract' $false $_.Exception.Message }

try {
  $live = Get-Json '/api/live-market/status'
  $gateway = $live.live_market_gateway
  $desired = [int]$gateway.subscriptions.desired_total
  $applied = [int]$gateway.subscriptions.applied_total
  $closedContradiction = $gateway.connected -eq $true -and ([string]$gateway.last_error -match 'closed')
  if($report.product.market_open){
    $liveOk = $gateway.connected -eq $true -and -not $closedContradiction -and $gateway.stale -ne $true -and $desired -gt 0 -and $applied -eq $desired -and $null -ne $gateway.last_message_at
  } else {
    $liveOk = -not $closedContradiction -and (($desired -eq 0) -or ($applied -eq $desired) -or ($gateway.connected -ne $true))
  }
  Add-Check 'Canonical live feed/subscription consistency' $liveOk ("state=$($gateway.operational_state); connected=$($gateway.connected); desired=$desired; applied=$applied; last=$($gateway.last_message_at); error=$($gateway.last_error)") $gateway
} catch { Add-Check 'Canonical live feed/subscription consistency' $false $_.Exception.Message }

try {
  $fundamentals = $health.fundamentals
  $fundamentalOk = $fundamentals.loaded -eq $true -and [int]$fundamentals.count -gt 0
  Add-Check 'Point-in-time fundamentals availability' $fundamentalOk ("loaded=$($fundamentals.loaded); rows=$($fundamentals.count); source=$($fundamentals.source)") $fundamentals $false
} catch { Add-Check 'Point-in-time fundamentals availability' $false $_.Exception.Message $null $false }

try {
  $research = Get-Json '/api/research-libraries' 30
  $adapterOk = $research.research_adapter.ok -eq $true
  $runtimeOk = -not [string]::IsNullOrWhiteSpace([string]$research.research_python)
  Add-Check 'Isolated research runtime' ($adapterOk -and $runtimeOk) ("python=$($research.research_python); adapter=$($research.research_adapter.reason)") $research $false
} catch { Add-Check 'Isolated research runtime' $false $_.Exception.Message $null $false }

try {
  $methods = Get-Json '/api/active-research-methods' 30
  $authorityOk = $methods.dependency_authority -eq 'research_venv_registry'
  Add-Check 'Research dependency authority' $authorityOk ("authority=$($methods.dependency_authority); python=$($methods.research_python)") $methods $false
} catch { Add-Check 'Research dependency authority' $false $_.Exception.Message $null $false }

try {
  $quantPlane = Get-Json '/api/quant-research-plane' 30
  $runtimeReady = $quantPlane.runtime.state -eq 'READY' -and $quantPlane.runtime.ok -eq $true
  $publicationReady = $quantPlane.publication_authority.ok -eq $true
  $policyReady = $quantPlane.training_data_policy -eq 'PARQUET_DUCKDB_ONLY' -and $quantPlane.production_weight_policy -eq 'ZERO_UNTIL_FORWARD_PAPER_PROMOTION' -and $quantPlane.broker_authority -eq 'NONE'
  Add-Check 'Authoritative Quant/AI lifecycle plane' ($quantPlane.ok -eq $true -and $runtimeReady -and $publicationReady -and $policyReady) ("state=$($quantPlane.state); runtime=$($quantPlane.runtime.state); publication=$($quantPlane.publication_authority.state); training=$($quantPlane.training_data_policy); production=$($quantPlane.production_weight_policy)") $quantPlane $false
} catch { Add-Check 'Authoritative Quant/AI lifecycle plane' $false $_.Exception.Message $null $false }

$researchManifestPath = Join-Path $env:ProgramData 'ProjectLaddu\runtime\research_runtime.json'
if(Test-Path $researchManifestPath -PathType Leaf){
  try {
    $researchManifest = Get-Content $researchManifestPath -Raw | ConvertFrom-Json
    $tasks = @('ProjectLaddu-AI-Training','ProjectLaddu-Model-Governance','ProjectLaddu-Weekend-Research')
    $missingTasks = @()
    foreach($taskName in $tasks){
      $task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
      if($null -eq $task -or $task.State -eq 'Disabled'){ $missingTasks += $taskName }
    }
    $manifestOk = $researchManifest.state -eq 'READY' -and $researchManifest.task_proof_required -eq $true -and @($researchManifest.blockers).Count -eq 0
    Add-Check 'Quant/AI runtime manifest and scheduled lifecycle' ($manifestOk -and $missingTasks.Count -eq 0) ("state=$($researchManifest.state); task-proof=$($researchManifest.task_proof_required); missing=$($missingTasks -join ',')") $researchManifest $false
  } catch { Add-Check 'Quant/AI runtime manifest and scheduled lifecycle' $false $_.Exception.Message $null $false }
} else { Add-Check 'Quant/AI runtime manifest and scheduled lifecycle' $false "manifest missing: $researchManifestPath" $null $false }

foreach($desk in @('intraday','delivery')) {
  try {
    $lifecycle = $quantPlane.model_lifecycle.$desk
    $weight = [double]($lifecycle.production_weight)
    $stateText = [string]($lifecycle.state)
    $safeWeight = $weight -ge 0 -and $weight -le 1 -and (($lifecycle.production_influence -eq $true) -or $weight -eq 0)
    $lifecycleOk = -not [string]::IsNullOrWhiteSpace($stateText) -and $safeWeight
    Add-Check ("Model lifecycle authority " + $desk) $lifecycleOk ("state=$stateText; training=$($lifecycle.training_state); folds=$($lifecycle.walk_forward_folds); production_weight=$weight; influence=$($lifecycle.production_influence)") $lifecycle
  } catch { Add-Check ("Model lifecycle authority " + $desk) $false $_.Exception.Message }
}

try {
  $maturity = Get-Json '/api/market-cycle-maturity' 30
  $level = [int]$maturity.maturity_level
  $maturityOk = $maturity.ok -eq $true -and $level -ge 0 -and $level -le 4 -and $maturity.decision_boundary.maturity_status_production_influence -eq $false
  Add-Check 'Market-cycle and sector-rotation maturity authority' $maturityOk ("level=$level/4; state=$($maturity.maturity_state); sector=$($maturity.sector_rotation.level)/4; model_influence=$($maturity.decision_boundary.ml_production_influence)") $maturity
} catch { Add-Check 'Market-cycle and sector-rotation maturity authority' $false $_.Exception.Message }

try {
  $productMaturity = Get-Json '/api/product-maturity' 30
  $maturityLevel = [int]$productMaturity.maturity_level
  $maturityContractOk = $productMaturity.build -eq $report.expected_version -and $maturityLevel -ge 0 -and $maturityLevel -le 5
  Add-Check 'Product maturity and Level-4 evidence authority' $maturityContractOk ("level=$maturityLevel/5; state=$($productMaturity.maturity_state); level4=$($productMaturity.level4_ready); missing=$(@($productMaturity.missing_level4_gates) -join ',')") $productMaturity $false
} catch { Add-Check 'Product maturity and Level-4 evidence authority' $false $_.Exception.Message $null $false }

try {
  $surface = Get-Json '/api/decision-surface-reconciliation' 30
  $surfaceConflicts = @($surface.conflicts).Count + @($surface.invalid_modes).Count + @($surface.delivery_duplicate_theses).Count
  $surfaceOrphans = @($surface.orphaned.today_entries).Count + @($surface.orphaned.signal_ledger).Count + @($surface.orphaned.model_paper).Count
  $surfaceContractOk = [string]$surface.state -ne 'UNAVAILABLE' -and $surfaceConflicts -eq 0 -and $surfaceOrphans -eq 0
  Add-Check 'Canonical decision-surface reconciliation contract' $surfaceContractOk ("state=$($surface.state); canonical=$($surface.counts.canonical); today=$($surface.counts.today_entries); ledger=$($surface.counts.signal_ledger); model-paper=$($surface.counts.model_paper); conflicts=$surfaceConflicts; orphans=$surfaceOrphans") $surface
} catch { Add-Check 'Canonical decision-surface reconciliation contract' $false $_.Exception.Message }

try {
  $learningAudit = Get-Json '/api/model-learning-audit' 30
  $learningErrors = @($learningAudit.errors).Count + @($learningAudit.observation_collisions).Count
  $learningContractOk = [string]$learningAudit.state -ne 'UNAVAILABLE' -and $learningErrors -eq 0
  Add-Check 'Model-learning observation and authority audit' $learningContractOk ("state=$($learningAudit.state); observations=$($learningAudit.model_observations); complete=$($learningAudit.contract_complete_observations); settled-links=$($learningAudit.settled_linked_candidates); errors=$learningErrors") $learningAudit
} catch { Add-Check 'Model-learning observation and authority audit' $false $_.Exception.Message }

try {
  $integrity = Get-Json '/api/operational-evidence-integrity' 30
  $chainErrors = @($integrity.chain.errors).Count
  $integrityContractOk = $integrity.build -eq $report.expected_version -and [string]$integrity.state -notin @('UNAVAILABLE','FAILED') -and $chainErrors -eq 0 -and [int]$integrity.source_failure_count -eq 0
  Add-Check 'Operational evidence hash-chain contract' $integrityContractOk ("state=$($integrity.state); chain=$($integrity.chain.state); entries=$($integrity.chain.entry_count); pending=$(@($integrity.missing_gates).Count); errors=$chainErrors") $integrity
} catch { Add-Check 'Operational evidence hash-chain contract' $false $_.Exception.Message }

try {
  $storage = Get-Json '/api/storage-architecture' 15
  $storageOk = $storage.ok -eq $true -and
    $storage.production_authority.ready -eq $true -and
    $storage.production_authority.canonical_bar_runtime.production_authority -eq $true -and
    $storage.policy.compatibility_projection -match 'never production authority'
  Add-Check 'Storage ownership endpoint' $storageOk ("ready=$($storage.production_authority.ready); probe=$($storage.probe); compatibility=$($storage.policy.compatibility_projection)") $storage
} catch { Add-Check 'Storage ownership endpoint' $false $_.Exception.Message }

$installDir = Join-Path $env:ProgramData 'ProjectLaddu'
$lakeManifest = Join-Path $installDir 'data\manifests\market-lake.json'
if(Test-Path -LiteralPath $lakeManifest){
  try {
    $lake = Get-Content -LiteralPath $lakeManifest -Raw | ConvertFrom-Json
    $recon = @($lake.reconciliation.PSObject.Properties | ForEach-Object { $_.Value })
    $reconciled = $recon.Count -gt 0 -and @($recon | Where-Object { $_.complete -ne $true }).Count -eq 0
    $pruneState = [string]$lake.operational_prune.state
    # Production four-plane authority never prunes canonical operational data.
    # transient_cleanup is optional maintenance metadata, not a prerequisite for
    # recognizing the explicit production no-prune state.
    $productionNoPrune = $pruneState -eq 'NOT_APPLICABLE_PRODUCTION_DATA_PLANE'
    $lakeOk = ($reconciled -and $pruneState -notin @('BLOCKED_BY_RECONCILIATION','DEFERRED','')) -or $productionNoPrune
    Add-Check 'Lake reconciliation before operational prune' $lakeOk ("reconciled=$reconciled; prune=$pruneState; production-no-prune=$productionNoPrune") $lake
  } catch { Add-Check 'Lake reconciliation before operational prune' $false $_.Exception.Message }
} else {
  Add-Check 'Lake reconciliation before operational prune' $false "manifest missing: $lakeManifest"
}

$mandatoryFailures = @($report.checks | Where-Object { $_.mandatory -and -not $_.ok })
$report['all_checks_passed'] = $mandatoryFailures.Count -eq 0
$report['mandatory_failure_count'] = $mandatoryFailures.Count
$report['trading_edge_validated'] = $false
$outDir = Join-Path $env:ProgramData 'ProjectLaddu\logs\validation'
New-Item -ItemType Directory -Force -Path $outDir | Out-Null
$outFile = Join-Path $outDir ('operational-proof-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '.json')
$report | ConvertTo-Json -Depth 30 | Set-Content -Encoding UTF8 $outFile
Write-Host "Report: $outFile"

if($FailOnBlocked -and $mandatoryFailures.Count -gt 0) { exit 2 }
exit 0
