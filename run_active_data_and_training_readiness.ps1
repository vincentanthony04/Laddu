param(
  [string]$InstallDir = "$env:ProgramData\ProjectLaddu",
  [int]$Port = 8086,
  [int]$CohortSize = 96,
  [int]$BatchSize = 12,
  [int]$FirstModeMinutes = 90,
  [switch]$SkipNse,
  [switch]$SkipBrandAssets,
  [switch]$SkipTraining
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0

$dataDir = Join-Path $InstallDir 'data'
$manifestDir = Join-Path $dataDir 'manifests\training_readiness'
$logDir = Join-Path $InstallDir 'logs\research'
New-Item -ItemType Directory -Path $manifestDir,$logDir -Force | Out-Null
$stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
$reportPath = Join-Path $manifestDir "training-readiness-$stamp.json"
$latestPath = Join-Path $manifestDir 'latest.json'
$steps = New-Object System.Collections.Generic.List[object]

function Invoke-Step([string]$Name,[scriptblock]$Action,[bool]$Required=$true){
  $started = Get-Date
  try {
    & $Action
    $code = if($null -eq $LASTEXITCODE){ 0 } else { [int]$LASTEXITCODE }
    if($code -ne 0){ throw "$Name returned exit code $code" }
    $steps.Add([ordered]@{ name=$Name; state='PASS'; required=$Required; started_at=$started.ToString('o'); completed_at=(Get-Date).ToString('o') })
    return $true
  } catch {
    $steps.Add([ordered]@{ name=$Name; state='BLOCKED'; required=$Required; started_at=$started.ToString('o'); completed_at=(Get-Date).ToString('o'); error=$_.Exception.Message })
    if($Required){ Write-Warning ("{0}: {1}" -f $Name,$_.Exception.Message) }
    return $false
  }
}

$ready = Invoke-Step 'Runtime readiness' {
  $response = Invoke-RestMethod -UseBasicParsing -Method Get -Uri "http://127.0.0.1:$Port/api/ready" -TimeoutSec 10
  if($response.ready -ne $true){ throw 'Runtime is not ready.' }
}

if(-not $SkipNse){
  Invoke-Step 'Active NSE official data acquisition' { & (Join-Path $InstallDir 'run_nse_official_data_cycle.ps1') -InstallDir $InstallDir } $true | Out-Null
}
if(-not $SkipBrandAssets){
  Invoke-Step 'Issuer logo catalogue refresh (non-blocking)' { & (Join-Path $InstallDir 'run_brand_asset_refresh.ps1') -InstallDir $InstallDir } $false | Out-Null
}
if($ready){
  Invoke-Step 'Bounded first useful mode and exact-gap history' {
    & (Join-Path $InstallDir 'run_first_useful_mode.ps1') -InstallDir $InstallDir -Port $Port -MaxMinutes $FirstModeMinutes -IntervalSeconds 30 -CohortSize $CohortSize -BatchSize $BatchSize -SkipTraining
  } $true | Out-Null
  Invoke-Step 'Research and Model Paper settlement' { & (Join-Path $InstallDir 'run_learning_cycle.ps1') -InstallDir $InstallDir -Port $Port -Cycle settlement } $true | Out-Null
  if(-not $SkipTraining){
    Invoke-Step 'Governed shadow model training' { & (Join-Path $InstallDir 'train_ai_model.ps1') -InstallDir $InstallDir -Port $Port -FirstMode } $true | Out-Null
    Invoke-Step 'Model governance reconciliation' { & (Join-Path $InstallDir 'run_model_governance_cycle.ps1') -InstallDir $InstallDir } $true | Out-Null
  }
}

$status = $null
try {
  $status = [ordered]@{
    nse_data_authority = Invoke-RestMethod -UseBasicParsing -Method Get -Uri "http://127.0.0.1:$Port/api/nse-data-authority" -TimeoutSec 20
    first_useful_mode = Invoke-RestMethod -UseBasicParsing -Method Get -Uri "http://127.0.0.1:$Port/api/first-useful-mode?cohort_size=$CohortSize" -TimeoutSec 20
    scanner = Invoke-RestMethod -UseBasicParsing -Method Get -Uri "http://127.0.0.1:$Port/api/scanner/status" -TimeoutSec 20
    ml_qualification = Invoke-RestMethod -UseBasicParsing -Method Get -Uri "http://127.0.0.1:$Port/api/ml-population-qualification" -TimeoutSec 20
    forward_clock = Invoke-RestMethod -UseBasicParsing -Method Get -Uri "http://127.0.0.1:$Port/api/forward-evidence-clock" -TimeoutSec 20
  }
} catch {
  $status = [ordered]@{ state='STATUS_PARTIAL'; error=$_.Exception.Message }
}

$requiredFailures = @($steps | Where-Object { $_.required -eq $true -and $_.state -ne 'PASS' })
$report = [ordered]@{
  version = 'active-data-training-readiness-1.0.0'
  build = 'v99.0.0'
  started_at = ($steps | Select-Object -First 1).started_at
  completed_at = (Get-Date).ToString('o')
  install_dir = $InstallDir
  steps = $steps
  status = $status
  production_model_influence = 0.0
  broker_authority = 'NONE'
  state = if($requiredFailures.Count -eq 0){ 'READINESS_CYCLE_COMPLETE' } else { 'REQUIRED_STEP_BLOCKED' }
}
$json = $report | ConvertTo-Json -Depth 20
Set-Content -LiteralPath $reportPath -Value $json -Encoding UTF8
Set-Content -LiteralPath $latestPath -Value $json -Encoding UTF8
Write-Host "Training readiness evidence: $reportPath"
if($requiredFailures.Count -gt 0){ exit 2 }
exit 0
