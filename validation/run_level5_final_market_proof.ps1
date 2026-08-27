param(
  [string]$InstallDir = "$env:ProgramData\ProjectLaddu",
  [string]$BaseUrl = 'http://127.0.0.1:8086',
  [string]$Symbol = 'TCS',
  [int]$MinTrainingDates = 504
)
$ErrorActionPreference='Stop'
Set-StrictMode -Version 2.0
$PackageRoot=(Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$Identity=Get-Content -LiteralPath (Join-Path $PackageRoot 'RELEASE_IDENTITY.json') -Raw | ConvertFrom-Json
$ExpectedBuild=[string]$Identity.version
$stamp=Get-Date -Format 'yyyyMMdd_HHmmss'
$outDir=Join-Path 'C:\Temp\ProjectLaddu' ("level5-final-market-proof\"+$stamp)
New-Item -ItemType Directory -Force -Path $outDir | Out-Null
$steps=New-Object System.Collections.ArrayList
$hardBlockers=New-Object System.Collections.ArrayList
function Add-Step([string]$name,[string]$state,[string]$detail,[string]$evidence='',[bool]$productionGate=$true){
  [void]$steps.Add([ordered]@{name=$name;state=$state;detail=$detail;evidence=$evidence;production_gate=$productionGate})
  if($productionGate -and $state -ne 'PASS'){ [void]$hardBlockers.Add($name) }
}
function Write-Json($obj,[string]$path){ [IO.File]::WriteAllText($path,($obj|ConvertTo-Json -Depth 60),(New-Object Text.UTF8Encoding($false))) }
function Resolve-Python {
  $p=(Get-Command python.exe -ErrorAction SilentlyContinue).Source
  if([string]::IsNullOrWhiteSpace($p)){ $p=(Get-Command python -ErrorAction SilentlyContinue).Source }
  if([string]::IsNullOrWhiteSpace($p)){ throw 'Python is unavailable.' }
  return $p
}
$python=Resolve-Python

# 1. The package must prove every deterministic Level-5 safety contract before target evidence is trusted.
$guardNames=@('verify_customer_vertical_slice.py','verify_runtime_lifecycle_authority_closure.py','verify_intelligence_evaluation_guard.py','verify_data_utilization_guard.py','verify_level5_evidence_closure.py')
foreach($guard in $guardNames){
  $log=Join-Path $outDir ($guard+'.txt')
  & $python (Join-Path $PackageRoot ('validation\'+$guard)) *> $log
  if($LASTEXITCODE -eq 0){ Add-Step ('PACKAGE_'+$guard.Replace('.py','').ToUpper()) 'PASS' 'Deterministic packaged guard passed.' $log $true }
  else{ Add-Step ('PACKAGE_'+$guard.Replace('.py','').ToUpper()) 'FAIL' 'Deterministic packaged guard failed.' $log $true }
}

# 2. Bind the target to this exact R21 package. No blank/weak expected-build acceptance is allowed.
$binding=Join-Path $outDir 'installed-package-binding.json'
& $python (Join-Path $PackageRoot 'validation\verify_installed_package_binding.py') --package-root $PackageRoot --install-dir $InstallDir --output $binding *> (Join-Path $outDir 'installed-package-binding-console.txt')
if($LASTEXITCODE -eq 0){ Add-Step 'EXACT_INSTALLED_PACKAGE_BINDING' 'PASS' "Installed payload is byte-bound to $ExpectedBuild package identity." $binding $true }
else{ Add-Step 'EXACT_INSTALLED_PACKAGE_BINDING' 'FAIL' 'Installed payload does not match this exact package. Install this package before interpreting any later evidence.' $binding $true }

# 3. Runtime and independent customer browser path during the real session.
try{
  $health=Invoke-RestMethod -UseBasicParsing -Uri ($BaseUrl.TrimEnd('/')+'/api/health') -TimeoutSec 15
  $healthPath=Join-Path $outDir 'api-health.json'; Write-Json $health $healthPath
  Add-Step 'API_HEALTH' 'PASS' 'Installed runtime health endpoint responded.' $healthPath $true
}catch{ Add-Step 'API_HEALTH' 'FAIL' $_.Exception.Message '' $true }
try{
  $ready=Invoke-RestMethod -UseBasicParsing -Uri ($BaseUrl.TrimEnd('/')+'/api/ready') -TimeoutSec 20
  $readyPath=Join-Path $outDir 'api-ready.json'; Write-Json $ready $readyPath
  if($ready.ready -eq $true){ Add-Step 'API_READY' 'PASS' 'Installed runtime reports ready.' $readyPath $true }
  else{ Add-Step 'API_READY' 'FAIL' 'Installed runtime did not report ready.' $readyPath $true }
}catch{ Add-Step 'API_READY' 'FAIL' $_.Exception.Message '' $true }
# Scanner operability is a separate production gate. API health or a rendered shell
# can never substitute for real market-hours quote -> deep mathematics progression.
$scannerProof=Join-Path $outDir 'live-scanner-operability.json'
& $python (Join-Path $PackageRoot 'validation\verify_live_scanner_operability.py') --base-url $BaseUrl --output $scannerProof --wait-seconds 180 --poll-seconds 5 --require-market-open *> (Join-Path $outDir 'live-scanner-operability-console.txt')
if($LASTEXITCODE -eq 0){ Add-Step 'LIVE_MARKET_SCANNER_OPERABILITY' 'PASS' 'Both Intraday and Delivery proved quote-ready deep mathematical analysis during the live session; zero promotions, if any, are explicitly explained.' $scannerProof $true }
else{ Add-Step 'LIVE_MARKET_SCANNER_OPERABILITY' 'FAIL' 'Scanner is not operational enough for production: blank/warming/no-data output, missing quotes, or no deep mathematical analysis is a hard failure.' $scannerProof $true }

$browser=Join-Path $outDir 'installed-browser-acceptance.json'
& $python (Join-Path $PackageRoot 'validation\installed_browser_acceptance.py') --base-url $BaseUrl --output $browser --symbol $Symbol --expected-build $ExpectedBuild *> (Join-Path $outDir 'installed-browser-console.txt')
if($LASTEXITCODE -eq 0){ Add-Step 'INDEPENDENT_EDGE_CUSTOMER_FLOW' 'PASS' 'Exact-build Edge/CDP customer flow passed.' $browser $true }
else{ Add-Step 'INDEPENDENT_EDGE_CUSTOMER_FLOW' 'FAIL' 'Exact-build Edge/CDP customer flow failed.' $browser $true }

# 4. Resolve installed production data-plane credentials and research runtime only after package binding.
$envFile=Join-Path $InstallDir 'secure\data-plane.env.ps1'
$researchPython=$null
if(Test-Path -LiteralPath $envFile -PathType Leaf){
  try{
    . $envFile
    $researchPython=[Environment]::GetEnvironmentVariable('PROJECT_LADDU_RESEARCH_PYTHON','Machine')
    if([string]::IsNullOrWhiteSpace($researchPython)){
      $ptr=Join-Path $InstallDir 'runtime\research_python.txt'
      if(Test-Path $ptr){ $researchPython=(Get-Content -LiteralPath $ptr -Raw).Trim() }
    }
    if($researchPython -and (Test-Path -LiteralPath $researchPython -PathType Leaf)){ Add-Step 'RESEARCH_RUNTIME' 'PASS' 'Installed research runtime and data-plane environment resolved.' $envFile $true }
    else{ Add-Step 'RESEARCH_RUNTIME' 'FAIL' 'Installed research Python could not be resolved.' $envFile $true }
  }catch{ Add-Step 'RESEARCH_RUNTIME' 'FAIL' $_.Exception.Message $envFile $true }
}else{ Add-Step 'RESEARCH_RUNTIME' 'FAIL' "Data-plane environment missing: $envFile" '' $true }

# 5. Reconcile only explicit content-addressed official corporate-action factors. Never infer or fabricate coverage.
$corpPath=Join-Path $outDir 'corporate-action-reconciliation.json'
if($researchPython -and (Test-Path $researchPython)){
  & $researchPython (Join-Path $InstallDir 'backend\tools\reconcile_corporate_action_authority.py') --output $corpPath *> (Join-Path $outDir 'corporate-action-console.txt')
  if($LASTEXITCODE -eq 0 -and (Test-Path $corpPath)){
    try{
      $corp=Get-Content -LiteralPath $corpPath -Raw | ConvertFrom-Json
      if($corp.full_history_coverage_complete -eq $true){ Add-Step 'CORPORATE_ACTION_PIT_AUTHORITY' 'PASS' 'Verified full-history corporate-action coverage is complete.' $corpPath $true }
      else{ Add-Step 'CORPORATE_ACTION_PIT_AUTHORITY' 'BLOCKED' ('Fail-closed: '+(($corp.blockers|ForEach-Object{[string]$_}) -join '; ')) $corpPath $true }
    }catch{ Add-Step 'CORPORATE_ACTION_PIT_AUTHORITY' 'FAIL' $_.Exception.Message $corpPath $true }
  }else{ Add-Step 'CORPORATE_ACTION_PIT_AUTHORITY' 'FAIL' 'Corporate-action authority reconciliation failed to run.' $corpPath $true }
}else{ Add-Step 'CORPORATE_ACTION_PIT_AUTHORITY' 'BLOCKED' 'Research runtime unavailable.' '' $true }

# 6. Re-project the production research catalogue so WFA sees the current PIT authorities.
$catalogLog=Join-Path $outDir 'research-catalog-refresh.txt'
if($researchPython -and (Test-Path $researchPython)){
  & $researchPython (Join-Path $InstallDir 'backend\tools\refresh_research_catalog.py') --data-dir (Join-Path $InstallDir 'data') *> $catalogLog
  if($LASTEXITCODE -eq 0){ Add-Step 'RESEARCH_CATALOG_REFRESH' 'PASS' 'PIT research catalogue refreshed from retained authorities.' $catalogLog $true }
  else{ Add-Step 'RESEARCH_CATALOG_REFRESH' 'FAIL' 'PIT research catalogue refresh failed.' $catalogLog $true }
}else{ Add-Step 'RESEARCH_CATALOG_REFRESH' 'BLOCKED' 'Research runtime unavailable.' '' $true }

# 7. Prove the independently-governed Intraday mathematical baseline on the retained selector evidence.
# This is intentionally a capital-profile gate. If the existing prospective selector evidence cannot
# prove capital/concurrency/MTM/no-leverage semantics, the build remains BLOCKED rather than silently
# downgrading to a research-only replay.
$intradayWfa=Join-Path $outDir 'intraday-selector-capital-wfa.json'
try{
  $uri=$BaseUrl.TrimEnd('/')+'/api/selection-walk-forward-replay?desk=intraday&horizon=30m&top_fraction=0.20&min_train_days=252&test_days=63&max_folds=20&embargo_days=1&min_samples=300&profile=capital'
  $iw=Invoke-RestMethod -UseBasicParsing -Uri $uri -TimeoutSec 180
  Write-Json $iw $intradayWfa
  $heuristicApproved=$false
  try{ $heuristicApproved=($iw.arms.heuristic.validation.approved -eq $true) }catch{}
  $samePopulation=$false
  try{ $samePopulation=($iw.same_candidate_population_across_arms -eq $true) }catch{}
  if($iw.ok -eq $true -and $heuristicApproved -and $samePopulation){
    Add-Step 'INTRADAY_HISTORICAL_CAPITAL_WFA' 'PASS' 'Intraday deterministic mathematical baseline passed the governed capital-profile replay on the same immutable selector populations.' $intradayWfa $true
  }else{
    Add-Step 'INTRADAY_HISTORICAL_CAPITAL_WFA' 'BLOCKED' 'Intraday capital-profile WFA is not approved on the immutable selector evidence; research-only evidence is not accepted as production proof.' $intradayWfa $true
  }
}catch{
  [ordered]@{ok=$false;error=$_.Exception.Message;uri=$uri}|ConvertTo-Json -Depth 8|Set-Content -LiteralPath $intradayWfa -Encoding UTF8
  Add-Step 'INTRADAY_HISTORICAL_CAPITAL_WFA' 'BLOCKED' 'Intraday capital-profile WFA endpoint failed or timed out; production proof remains blocked.' $intradayWfa $true
}

# 8. Run the real historical capital-profile Delivery WFA. R21 uses all eligible chronological OOF folds beyond the deep minimum.
$trainingLog=Join-Path $outDir 'historical-capital-wfa.txt'
if($researchPython -and (Test-Path $researchPython)){
  & $researchPython (Join-Path $InstallDir 'backend\tools\train_nse_smart_model.py') --data-dir (Join-Path $InstallDir 'data') --api-url $BaseUrl --horizon 10 --min-dates ([Math]::Max(504,$MinTrainingDates)) *> $trainingLog
  if($LASTEXITCODE -eq 0){
    $latest=Join-Path $InstallDir 'data\manifests\latest-training-run.json'
    if(Test-Path $latest){ Copy-Item -LiteralPath $latest -Destination (Join-Path $outDir 'latest-training-run.json') -Force }
    Add-Step 'DELIVERY_HISTORICAL_CAPITAL_WFA' 'PASS' 'Governed historical Delivery training/WFA completed on retained PIT history.' $trainingLog $true
  }else{ Add-Step 'DELIVERY_HISTORICAL_CAPITAL_WFA' 'BLOCKED' 'Governed historical Delivery capital WFA did not pass; see exact blocker evidence.' $trainingLog $true }
}else{ Add-Step 'DELIVERY_HISTORICAL_CAPITAL_WFA' 'BLOCKED' 'Research runtime unavailable.' '' $true }

# 9. Reconcile model governance after the WFA publication.
$govLog=Join-Path $outDir 'model-governance-cycle.txt'
if(Test-Path -LiteralPath (Join-Path $InstallDir 'run_model_governance_cycle.ps1')){
  & PowerShell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $InstallDir 'run_model_governance_cycle.ps1') -InstallDir $InstallDir *> $govLog
  if($LASTEXITCODE -eq 0){ Add-Step 'MODEL_GOVERNANCE_RECONCILIATION' 'PASS' 'Model governance cycle completed.' $govLog $true }
  else{ Add-Step 'MODEL_GOVERNANCE_RECONCILIATION' 'FAIL' 'Model governance cycle failed.' $govLog $true }
}else{ Add-Step 'MODEL_GOVERNANCE_RECONCILIATION' 'FAIL' 'Installed model-governance entrypoint is missing.' '' $true }

# 10. Capture live scanner/research/data-utilization/maturity read models after the run.
$apiTargets=[ordered]@{
  scanner='/api/scanner/status'; ml_qualification='/api/ml-population-qualification';
  forward_maturity='/api/level5-forward-maturity'; quant_edge='/api/quant-edge-status';
  product_maturity='/api/product-maturity'; nse_data='/api/nse-data-authority'
}
$apiSnapshots=[ordered]@{}
foreach($name in $apiTargets.Keys){
  try{
    $v=Invoke-RestMethod -UseBasicParsing -Uri ($BaseUrl.TrimEnd('/')+$apiTargets[$name]) -TimeoutSec 30
    $apiSnapshots[$name]=$v
    Write-Json $v (Join-Path $outDir ($name+'.json'))
  }catch{ $apiSnapshots[$name]=[ordered]@{ok=$false;error=$_.Exception.Message} }
}
$apiSummaryPath=Join-Path $outDir 'live-authority-snapshots.json'; Write-Json $apiSnapshots $apiSummaryPath
Add-Step 'LIVE_AUTHORITY_SNAPSHOTS' 'PASS' 'Captured scanner, ML qualification, forward maturity, research and NSE data authorities.' $apiSummaryPath $false

# Forward Level-5 is a real elapsed-evidence gate. Historical replay is never substituted.
$forward=$apiSnapshots['forward_maturity']
$forwardReady=$false
try{ $forwardReady=($forward.level5_ready -eq $true) }catch{}
if($forwardReady){ Add-Step 'REAL_FORWARD_LEVEL5_MATURITY' 'PASS' 'Both desks satisfy genuine forward Level-5 evidence.' (Join-Path $outDir 'forward_maturity.json') $true }
else{ Add-Step 'REAL_FORWARD_LEVEL5_MATURITY' 'ACCUMULATING' 'Real forward evidence has not yet satisfied every Level-5 gate. Historical replay was not substituted.' (Join-Path $outDir 'forward_maturity.json') $true }

# 11. Run existing installed supporting proof without duplicating Edge/restart/fault injection in market hours.
$support=Join-Path $outDir 'installed-supporting-proof.txt'
if(Test-Path -LiteralPath (Join-Path $PackageRoot 'VERIFY_INSTALLED_PRODUCT.ps1')){
  & PowerShell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PackageRoot 'VERIFY_INSTALLED_PRODUCT.ps1') -InstallDir $InstallDir -BaseUrl $BaseUrl -SkipBrowser -SkipRestart -SkipFaultInjection *> $support
  if($LASTEXITCODE -eq 0){ Add-Step 'INSTALLED_SUPPORTING_GATES' 'PASS' 'Existing installed-product supporting proof passed.' $support $true }
  else{ Add-Step 'INSTALLED_SUPPORTING_GATES' 'FAIL' 'Existing installed-product supporting proof failed.' $support $true }
}

$productionReady=($hardBlockers.Count -eq 0)
$summary=[ordered]@{
  product='Project Laddu'; version=$ExpectedBuild; candidate_revision=[string]$Identity.candidate_revision;
  authority='LEVEL5_FINAL_MARKET_PROOF'; captured_at=(Get-Date).ToString('o');
  engineering_and_installed_gates_passed=(@($steps|Where-Object{$_.production_gate -and $_.state -notin @('PASS','ACCUMULATING')}).Count -eq 0);
  production_ready=$productionReady; level5_forward_ready=$forwardReady;
  hard_blockers=@($hardBlockers|Select-Object -Unique); steps=$steps;
  historical_replay_counts_as_forward_time=$false; broker_authority='NONE';
  policy='Production-ready is true only when exact installed binding, live customer path, PIT/corporate-action authority, real historical capital WFA, governance, installed supporting gates and genuine forward Level-5 maturity all pass. No elapsed-time gate is fabricated.'
}
$summaryPath=Join-Path $outDir 'LEVEL5-FINAL-MARKET-PROOF.json'; Write-Json $summary $summaryPath
$sha=(Get-FileHash $summaryPath -Algorithm SHA256).Hash.ToLowerInvariant(); Set-Content ($summaryPath+'.sha256') ($sha+'  '+[IO.Path]::GetFileName($summaryPath)) -Encoding ASCII
$zip=$outDir+'.zip'; Compress-Archive -Path (Join-Path $outDir '*') -DestinationPath $zip -Force
Write-Host ''
Write-Host ('Level-5 evidence: '+$outDir) -ForegroundColor Cyan
Write-Host ('Bundle:          '+$zip) -ForegroundColor Cyan
Write-Host ('Production ready: '+$productionReady) -ForegroundColor $(if($productionReady){'Green'}else{'Yellow'})
if($hardBlockers.Count){ Write-Host ('Blockers: '+((@($hardBlockers|Select-Object -Unique)) -join ', ')) -ForegroundColor Yellow }
if($productionReady){ exit 0 } else { exit 2 }
