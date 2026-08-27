param(
  [string]$InstallDir = "$env:ProgramData\ProjectLaddu",
  [string]$BaseUrl = 'http://127.0.0.1:8086',
  [int]$SampleSize = 40,
  [switch]$SkipBrowser,
  [switch]$SkipRestart,
  [switch]$SkipFaultInjection,
  [switch]$FailOnOpen
)
$ErrorActionPreference='Stop'
Set-StrictMode -Version 2.0

$PackageRoot=$PSScriptRoot
. (Join-Path $PackageRoot 'validation\installed_proof_common.ps1')
. (Join-Path $PackageRoot 'validation\installed_proof_gates.ps1')

$planPath=Join-Path $PackageRoot 'validation\historical_37_installed_proof_plan.json'
if(!(Test-Path -LiteralPath $planPath -PathType Leaf)){throw "Installed historical-defect proof plan missing: $planPath"}
$plan=Get-Content -LiteralPath $planPath -Raw|ConvertFrom-Json
$release=Read-ReleaseIdentity $PackageRoot
if($null -eq $release){throw 'RELEASE_IDENTITY.json is required for exact-build installed proof.'}

$backendPointer=Join-Path $InstallDir 'runtime\backend_python.txt'
$python=''
if(Test-Path -LiteralPath $backendPointer -PathType Leaf){$python=(Get-Content -LiteralPath $backendPointer -Raw).Trim()}
if([string]::IsNullOrWhiteSpace($python) -or !(Test-Path -LiteralPath $python -PathType Leaf)){
  $fallback=Join-Path $env:ProgramFiles 'Python312\python.exe'
  if(Test-Path -LiteralPath $fallback -PathType Leaf){$python=$fallback}
}
if([string]::IsNullOrWhiteSpace($python) -or !(Test-Path -LiteralPath $python -PathType Leaf)){throw 'Installed backend Python is unavailable for exact-build target fault probes.'}

$stamp=Get-Date -Format 'yyyyMMdd-HHmmss'
$outRoot=Join-Path $InstallDir 'logs\validation\installed-product'
$outDir=Join-Path $outRoot $stamp
New-Item -ItemType Directory -Path $outDir -Force|Out-Null
$context=@{
  BaseUrl=$BaseUrl; InstallDir=$InstallDir; PackageRoot=$PackageRoot; OutputDir=$outDir;
  ReleaseIdentity=$release; PythonExe=$python; SampleSize=$SampleSize; Gates=[ordered]@{}
}

Write-Host 'Project Laddu - exact-build installed product proof'
Write-Host ("Exact release: {0} | plan={1}" -f (Get-Prop $release 'version'),(Get-Prop $plan 'authority_version'))
Write-Host 'Source PASS never implies installed CLOSED. Market-hours/restart/fault/browser proof remains explicit.'

Invoke-AlwaysInstalledGates $context
Invoke-SessionInstalledGates $context
Invoke-MarketHoursInstalledGate $context
Invoke-BrowserInstalledGates $context -SkipBrowser:$SkipBrowser
# Product truth is deliberately evaluated only after independent browser evidence
# has either been recorded or explicitly marked pending/failed.
Invoke-PostBrowserProductTruthGate $context -BrowserSkipped:$SkipBrowser
Invoke-FaultInstalledGates $context -SkipFaultInjection:$SkipFaultInjection
Invoke-RestartInstalledGates $context -SkipRestart:$SkipRestart

# Missing gate output is a proof-runner defect and fails closed.
foreach($property in $plan.gate_catalog.PSObject.Properties){
  $gateId=[string]$property.Name
  if(-not $context.Gates.Contains($gateId)){Add-Gate $context $gateId 'FAIL' 'installed proof runner emitted no result for required gate'}
}

$defects=@()
foreach($row in (Arr $plan.rows)){
  $required=Arr $row.required_gates
  $passed=@(); $pending=@(); $failed=@(); $evidence=@()
  foreach($gateId in $required){
    $gate=$context.Gates[[string]$gateId]
    $status=[string]$gate.status
    if($status -eq 'PASS'){$passed += [string]$gateId}elseif($status -eq 'TARGET_PENDING'){$pending += [string]$gateId}else{$failed += [string]$gateId}
    $evidence += ,[ordered]@{gate_id=$gateId;status=$status;detail=$gate.detail}
  }
  $state=if($failed.Count -gt 0){'FAILED_TARGET_PROOF'}elseif($pending.Count -gt 0){'TARGET_PENDING'}else{'CLOSED_ELIGIBLE'}
  $defects += ,[ordered]@{
    defect_id=$row.id; title=$row.title; source_pass=$true; required_target_gates=$required;
    passed_target_gates=$passed; pending_target_gates=$pending; failed_target_gates=$failed;
    formal_status_candidate=$state; evidence=$evidence
  }
}

$closedEligible=0; $targetPending=0; $failedDefects=0
foreach($defect in $defects){
  $state=[string]$defect.formal_status_candidate
  if($state -eq 'CLOSED_ELIGIBLE'){$closedEligible++}elseif($state -eq 'TARGET_PENDING'){$targetPending++}else{$failedDefects++}
}
$gateRows=Arr $context.Gates.Values
$gatePass=0; $gatePending=0; $gateFail=0
foreach($gateRow in $gateRows){
  $status=[string]$gateRow.status
  if($status -eq 'PASS'){$gatePass++}elseif($status -eq 'TARGET_PENDING'){$gatePending++}else{$gateFail++}
}
$report=[ordered]@{
  ok=($closedEligible -eq 37); state=$(if($closedEligible -eq 37){'ALL_37_INSTALLED_PROVEN'}elseif($failedDefects -gt 0){'BLOCKED'}else{'TARGET_PENDING'});
  authority='Historical37InstalledClosureProof'; authority_version='1.0.1-ps51-shape-safe'; exact_build=(Get-Prop $release 'version');
  release_line=(Get-Prop $release 'release_line'); captured_at=(Get-Date).ToString('o'); base_url=$BaseUrl;
  plan_content_sha256=(Get-Prop $plan 'content_sha256'); source_reconciliation_policy='source PASS never implies installed CLOSED';
  counts=[ordered]@{tracked=37;closed_eligible=$closedEligible;target_pending=$targetPending;failed_target_proof=$failedDefects;gates=$gateRows.Count;gate_pass=$gatePass;gate_pending=$gatePending;gate_fail=$gateFail};
  gates=$gateRows; defects=$defects;
  claim_boundary='This report establishes installed defect evidence only for gates actually observed on this exact build. It does not prove trading edge, Level 4, Level 5 or live broker execution.';
  broker_authority='NONE'; product_mode='AUTOMATIC_MODEL_PAPER_ONLY'
}
$outPath=Join-Path $outDir 'historical-37-installed-proof.json'
$report|ConvertTo-Json -Depth 80|Set-Content -LiteralPath $outPath -Encoding UTF8
$sha=(Get-FileHash -LiteralPath $outPath -Algorithm SHA256).Hash.ToLowerInvariant()
Set-Content -LiteralPath ($outPath+'.sha256') -Value ("{0}  {1}" -f $sha,[IO.Path]::GetFileName($outPath)) -Encoding ASCII
$resultValidator=Join-Path $PackageRoot 'validation\validate_historical_37_installed_proof_result.py'
& $python $resultValidator --proof $outPath | Out-Host
if($LASTEXITCODE -ne 0){throw 'Historical 37 installed proof result failed independent plan/result validation.'}
Write-Host ("Historical 37 proof: {0}" -f $report.state)
Write-Host ("CLOSED_ELIGIBLE={0}/37 TARGET_PENDING={1} FAILED={2}" -f $closedEligible,$targetPending,$failedDefects)
Write-Host ("Evidence: {0}" -f $outPath)
if($FailOnOpen -and $closedEligible -ne 37){exit 2}
exit 0
