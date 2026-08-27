param(
  [string]$InstallDir = "$env:ProgramData\ProjectLaddu",
  [string]$BaseUrl = 'http://127.0.0.1:8086',
  [int]$WaitSeconds = 180
)
$ErrorActionPreference='Stop'
Set-StrictMode -Version 2.0
$PackageRoot=$PSScriptRoot
$release=Get-Content -LiteralPath (Join-Path $PackageRoot 'frontend\release-identity.json') -Raw | ConvertFrom-Json
$expectedVersion=[string]$release.version
$expectedBuild=[string]$release.build_marker
$uri=[Uri]$BaseUrl
if($uri.Host -ne '127.0.0.1' -or $uri.Port -ne 8086){throw 'Final acceptance is bound to http://127.0.0.1:8086 only.'}

$backendPointer=Join-Path $InstallDir 'runtime\backend_python.txt'
$python=''
if(Test-Path -LiteralPath $backendPointer -PathType Leaf){$python=(Get-Content -LiteralPath $backendPointer -Raw).Trim()}
if([string]::IsNullOrWhiteSpace($python) -or !(Test-Path -LiteralPath $python -PathType Leaf)){
  $fallback=Join-Path $env:ProgramFiles 'Python312\python.exe'
  if(Test-Path -LiteralPath $fallback -PathType Leaf){$python=$fallback}
}
if([string]::IsNullOrWhiteSpace($python) -or !(Test-Path -LiteralPath $python -PathType Leaf)){throw 'Installed backend Python is unavailable.'}

function Get-Json([string]$Path,[int]$Timeout=10){
  Invoke-RestMethod -Uri ($BaseUrl.TrimEnd('/')+$Path) -TimeoutSec $Timeout -Headers @{'Cache-Control'='no-store';Pragma='no-cache'}
}
function Get-ResearchIds($Payload){
  @($Payload.research | ForEach-Object {if(-not [string]::IsNullOrWhiteSpace([string]$_.research_candidate_id)){[string]$_.research_candidate_id}else{[string]$_.source_signal_id}} | Where-Object {-not [string]::IsNullOrWhiteSpace($_)} | Sort-Object -Unique)
}
function Wait-Ready([string]$DifferentBoot=''){
  $deadline=(Get-Date).AddSeconds([Math]::Max(30,$WaitSeconds))
  while((Get-Date) -lt $deadline){
    try{
      $ready=Get-Json '/api/ready' 5
      $boot=[string]$ready.process_boot_id
      if($ready.ok -eq $true -and [string]$ready.version -eq $expectedVersion -and ![string]::IsNullOrWhiteSpace($boot) -and ([string]::IsNullOrWhiteSpace($DifferentBoot) -or $boot -ne $DifferentBoot)){return $ready}
    }catch{}
    Start-Sleep -Seconds 2
  }
  throw 'Installed backend did not become ready with the expected build/process identity.'
}
function Invoke-Browser([string]$Output,[string]$RestartBeforeBoot=''){
  $runner=Join-Path $PackageRoot 'validation\installed_customer_vertical_acceptance_r3.py'
  $browserArgs=@('--base-url',$BaseUrl,'--output',$Output,'--install-dir',$InstallDir,'--expected-version',$expectedVersion,'--expected-build',$expectedBuild,'--wait-seconds',([string]$WaitSeconds),'--tracker',$trackerPath,'--require-market-open','--require-full-sweeps','--require-actionable','--require-settlement','--track-lifecycle')
  if(-not [string]::IsNullOrWhiteSpace($RestartBeforeBoot)){$browserArgs+=@('--verify-restart-before-boot-id',$RestartBeforeBoot)}
  & $python $runner @browserArgs | Out-Host
  return $LASTEXITCODE
}

$stamp=Get-Date -Format 'yyyyMMdd-HHmmss'
$outBase=Join-Path $InstallDir 'logs\validation\final-product-acceptance'
$outDir=Join-Path $outBase $stamp
New-Item -ItemType Directory -Path $outDir -Force|Out-Null
$bindingPath=Join-Path $outDir 'installed-package-binding.json'
$browserBeforePath=Join-Path $outDir 'browser-before-restart.json'
$browserAfterPath=Join-Path $outDir 'browser-after-restart.json'
$summaryPath=Join-Path $outDir 'FINAL_PRODUCT_ACCEPTANCE.json'
$trackerPath=Join-Path $outBase 'PL14_EXACT_VERTICAL_TRACKER.json'

Write-Host ("Project Laddu final product acceptance · {0} · {1} · 8086" -f $expectedVersion,$expectedBuild)
& $python (Join-Path $PackageRoot 'validation\verify_installed_package_binding.py') --package-root $PackageRoot --install-dir $InstallDir --output $bindingPath | Out-Host
$bindingRc=$LASTEXITCODE
if($bindingRc -ne 0){throw 'Installed package binding failed.'}

$readyBefore=Wait-Ready
$frontendBefore=Get-Json '/api/frontend-identity' 8
$portfolioBefore=Get-Json '/api/model-portfolio?mode=all&detail=core' 12
$research_ids_before=@(Get-ResearchIds $portfolioBefore)
$browserBeforeRc=Invoke-Browser $browserBeforePath
$browserBefore=if(Test-Path -LiteralPath $browserBeforePath){Get-Content -LiteralPath $browserBeforePath -Raw|ConvertFrom-Json}else{$null}
$trackerStage=if($browserBefore -and $browserBefore.tracker -and $browserBefore.tracker.state){[string]$browserBefore.tracker.state.stage}else{'WAITING_FOR_ACTIONABLE'}
$browserBeforeClean=($browserBefore -and [int]$browserBefore.failed -eq 0 -and $browserBeforeRc -in @(0,3))
$restartEligible=($browserBeforeClean -and $trackerStage -in @('AFTER_OBSERVED','RESTART_VERIFIED'))

$beforeBoot=[string]$readyBefore.process_boot_id
$readyAfter=$readyBefore
$portfolioAfter=$portfolioBefore
$research_ids_after=$research_ids_before
$missingResearch=@()
$researchPersistenceProven=$false
$browserAfterRc=3
$browserAfter=$null
$bootChanged=$false
if($restartEligible){
  $identity=[Security.Principal.WindowsIdentity]::GetCurrent()
  $principal=New-Object Security.Principal.WindowsPrincipal($identity)
  if(!$principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)){throw 'Acceptance requires elevation to prove service restart persistence.'}
  Restart-Service -Name 'ProjectLaddu' -Force -ErrorAction Stop
  $readyAfter=Wait-Ready $beforeBoot
  $portfolioAfter=Get-Json '/api/model-portfolio?mode=all&detail=core' 12
  $research_ids_after=@(Get-ResearchIds $portfolioAfter)
  $missingResearch=@($research_ids_before | Where-Object {$research_ids_after -notcontains $_})
  $researchPersistenceProven=($research_ids_before.Count -gt 0 -and $missingResearch.Count -eq 0)
  $browserAfterRc=Invoke-Browser $browserAfterPath $beforeBoot
  $browserAfter=if(Test-Path -LiteralPath $browserAfterPath){Get-Content -LiteralPath $browserAfterPath -Raw|ConvertFrom-Json}else{$null}
  $bootChanged=([string]$readyAfter.process_boot_id -ne $beforeBoot)
}

$identityOk=($frontendBefore.ok -eq $true -and [string]$frontendBefore.version -eq $expectedVersion -and [string]$frontendBefore.build_marker -eq $expectedBuild -and @($frontendBefore.mismatches).Count -eq 0)
$ok=($bindingRc -eq 0 -and $identityOk -and $browserBeforeClean -and $restartEligible -and $browserAfterRc -eq 0 -and $browserAfter.ok -eq $true -and $bootChanged -and $researchPersistenceProven)
$state=if($ok){
  'FINAL_PRODUCT_ACCEPTANCE_PASSED'
}elseif($trackerStage -eq 'WAITING_FOR_ACTIONABLE'){
  'PENDING_NATURAL_ACTIONABLE_AND_RUNTIME_GATES'
}elseif($trackerStage -in @('ACTIONABLE_OBSERVED','MODEL_OPEN_OBSERVED')){
  'PENDING_SAME_DECISION_SETTLEMENT'
}elseif($trackerStage -eq 'SETTLED_OBSERVED'){
  'PENDING_SAME_DECISION_AFTER_EVIDENCE'
}elseif($trackerStage -in @('AFTER_OBSERVED','RESTART_VERIFIED')){
  'FAILED_RESTART_OR_POST_RESTART_ACCEPTANCE'
}else{
  'FAILED'
}

$browserProofPublication=$null
if($ok){
  $proofChecks=@($browserAfter.checks | ForEach-Object {[ordered]@{name=[string]$_.name;ok=($_.ok -eq $true)}})
  $proofBody=[ordered]@{
    proof=[ordered]@{build=$expectedVersion;build_marker=$expectedBuild;captured_at=(Get-Date).ToUniversalTime().ToString('o');authority='EXACT_INSTALLED_PL13_END_TO_END';evidence_sha256=(Get-FileHash -LiteralPath $browserAfterPath -Algorithm SHA256).Hash.ToLowerInvariant()};
    checks=$proofChecks
  }|ConvertTo-Json -Depth 20
  $browserProofPublication=Invoke-RestMethod -Method Post -Uri ($BaseUrl.TrimEnd('/')+'/api/validation/browser-proof') -ContentType 'application/json' -Body $proofBody -TimeoutSec 20
  if($browserProofPublication.ok -ne $true){$ok=$false;$state='FAILED_BROWSER_PROOF_RECONCILIATION'}
}
$summary=[ordered]@{
  ok=$ok; state=$state; captured_at=(Get-Date).ToString('o'); base_url=$BaseUrl; expected_version=$expectedVersion; expected_build=$expectedBuild;
  installed_package_binding=if(Test-Path -LiteralPath $bindingPath){Get-Content -LiteralPath $bindingPath -Raw|ConvertFrom-Json}else{$null};
  frontend_identity=$frontendBefore; ready_before=$readyBefore; ready_after=$readyAfter; process_boot_changed=$bootChanged;
  research_ids_before=$research_ids_before; research_ids_after=$research_ids_after; missing_research_ids=$missingResearch; research_persistence_proven=$researchPersistenceProven;
  tracker_path=$trackerPath; tracker_stage=$trackerStage; restart_eligible=$restartEligible;
  browser_before=$browserBefore; browser_after=$browserAfter; browser_proof_publication=$browserProofPublication;
  protected_boundaries=[ordered]@{broker_authority='NONE';release_eligible=$ok;port=8086;research_performance_in_final=$false;thresholds_weakened=$false;synthetic_signal_used=$false};
  claim_boundary=if($ok){'Exact installed PL13 passed mandatory market-open trust, full sweeps, natural Actionable, same-decision Model Paper, settlement, Result/After, restart persistence, browser semantics and readiness-evidence reconciliation.'}else{'NOT ACCEPTED / NOT RELEASE. PL13 remains pending until every mandatory same-decision product gate passes naturally.'}
}
$summary|ConvertTo-Json -Depth 100|Set-Content -LiteralPath $summaryPath -Encoding UTF8
$summaryHash=(Get-FileHash -LiteralPath $summaryPath -Algorithm SHA256).Hash.ToLowerInvariant()
Set-Content -LiteralPath ($summaryPath+'.sha256') -Value ("{0}  {1}" -f $summaryHash,[IO.Path]::GetFileName($summaryPath)) -Encoding ASCII
$zip=Join-Path $outBase ("ProjectLaddu-FinalProductAcceptance-{0}.zip" -f $stamp)
Compress-Archive -Path (Join-Path $outDir '*') -DestinationPath $zip -Force
$zipHash=(Get-FileHash -LiteralPath $zip -Algorithm SHA256).Hash.ToLowerInvariant()
Set-Content -LiteralPath ($zip+'.sha256') -Value ("{0}  {1}" -f $zipHash,[IO.Path]::GetFileName($zip)) -Encoding ASCII
Write-Host ("State: {0}" -f $state)
Write-Host ("Evidence: {0}" -f $zip)
Write-Host ("SHA-256: {0}" -f $zipHash)
if(!$ok){exit 3}
exit 0
