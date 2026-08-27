param(
  [int]$Minutes = 20,
  [int]$SampleSeconds = 30,
  [switch]$IncludeRestart,
  [string]$BaseUrl = 'http://127.0.0.1:8086'
)

$ErrorActionPreference = 'Stop'
if ($Minutes -lt 15) { throw 'Level-4 proof requires at least 15 minutes.' }
if ($SampleSeconds -lt 15) { throw 'SampleSeconds must be at least 15.' }

$InstallDir = Split-Path -Parent $PSScriptRoot
$ReleaseIdentity = Get-Content -LiteralPath (Join-Path $InstallDir 'RELEASE_IDENTITY.json') -Raw | ConvertFrom-Json
$ExpectedVersion = [string]$ReleaseIdentity.version
$LogDir = Join-Path $InstallDir 'logs\validation'
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$EvidencePath = Join-Path $LogDir "level4-market-soak-$stamp.json"
$Started = Get-Date
$Deadline = $Started.AddMinutes($Minutes)
$samples = New-Object System.Collections.Generic.List[object]
$restartAttempted = $false
$restartPassed = $false
$successfulSamples = 0
$marketOpenSamples = 0
$rankingTraceObserved = $false
$riskReadyObserved = $false
$lifecycleReadyObserved = $false
$postCloseFlattenVerified = $false
$decisionSurfacePassed = $false
$modelLearningAuditPassed = $false
$scannerFingerprints = New-Object System.Collections.Generic.HashSet[string]

function Get-Api([string]$Path) {
  try {
    return Invoke-RestMethod -Uri ($BaseUrl.TrimEnd('/') + $Path) -Method Get -TimeoutSec 15
  } catch {
    return [pscustomobject]@{ ok = $false; error = $_.Exception.Message; path = $Path }
  }
}

function Is-ReadyCheck($Payload, [string]$Key) {
  foreach ($row in @($Payload.checks)) {
    if ([string]$row.key -eq $Key -and [string]$row.state -eq 'READY') { return $true }
  }
  return $false
}

function Get-PositionRows($Payload) {
  if ($null -ne $Payload.positions) { return @($Payload.positions) }
  if ($null -ne $Payload.rows) { return @($Payload.rows) }
  if ($Payload -is [System.Array]) { return @($Payload) }
  return @()
}

while ((Get-Date) -lt $Deadline) {
  $now = Get-Date
  if ($IncludeRestart -and -not $restartAttempted -and ($now - $Started).TotalMinutes -ge 5) {
    $restartAttempted = $true
    try {
      & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $InstallDir 'restart.ps1') | Out-Null
      Start-Sleep -Seconds 20
      $readyAfterRestart = Get-Api '/api/ready'
      $restartPassed = ([string]$readyAfterRestart.version -eq $ExpectedVersion -and $readyAfterRestart.ok -ne $false)
    } catch {
      $restartPassed = $false
    }
  }

  $ready = Get-Api '/api/ready'
  $readiness = Get-Api '/api/product-readiness'
  $scanner = Get-Api '/api/scanner/status'
  $today = Get-Api '/api/today-entries?mode=all'
  $risk = Get-Api '/api/risk-authority/status'
  $positions = Get-Api '/api/positions'
  $live = Get-Api '/api/live-market/status'
  $surfaceReconciliation = Get-Api '/api/decision-surface-reconciliation'
  $modelLearningAudit = Get-Api '/api/model-learning-audit'

  $sampleOk = ([string]$ready.version -eq $ExpectedVersion -and $ready.ok -ne $false -and $readiness.ok -ne $false)
  if ($sampleOk) { $successfulSamples++ }
  $marketOpen = ($readiness.market_open -eq $true -or $live.market_open -eq $true)
  if ($marketOpen) { $marketOpenSamples++ }

  $scannerJson = $scanner | ConvertTo-Json -Depth 12 -Compress
  [void]$scannerFingerprints.Add($scannerJson)
  $todayJson = $today | ConvertTo-Json -Depth 16 -Compress
  if ($todayJson -match '"ranking_trace_id"\s*:\s*"rank:') { $rankingTraceObserved = $true }
  if ((Is-ReadyCheck $readiness 'live_trade_monitor') -or $risk.ok -eq $true) { $riskReadyObserved = $true }
  if (Is-ReadyCheck $readiness 'intraday_lifecycle') { $lifecycleReadyObserved = $true }
  if ($surfaceReconciliation.passed -eq $true) { $decisionSurfacePassed = $true }
  if ($modelLearningAudit.passed -eq $true) { $modelLearningAuditPassed = $true }

  $istNow = [System.TimeZoneInfo]::ConvertTimeBySystemTimeZoneId((Get-Date), 'India Standard Time')
  if ($istNow.TimeOfDay -ge [TimeSpan]::FromHours(15.333333)) {
    $openIntraday = 0
    foreach ($row in (Get-PositionRows $positions)) {
      $mode = [string]$row.mode
      if (-not $mode) { $mode = [string]$row.trade_mode }
      if (-not $mode) { $mode = [string]$row.desk }
      $state = [string]$row.state
      if (-not $state) { $state = [string]$row.status }
      if (-not $state) { $state = [string]$row.position_state }
      if ($mode.ToLowerInvariant() -eq 'intraday' -and $state.ToUpperInvariant() -notin @('CLOSED','EXITED','FLATTENED','CANCELLED')) {
        $openIntraday++
      }
    }
    if ($openIntraday -eq 0) { $postCloseFlattenVerified = $true }
  }

  $samples.Add([pscustomobject]@{
    captured_at = (Get-Date).ToUniversalTime().ToString('o')
    service_ok = $sampleOk
    market_open = $marketOpen
    scanner_state = $scanner
    ranking_trace_observed = ($todayJson -match '"ranking_trace_id"\s*:\s*"rank:')
    risk_ready = ((Is-ReadyCheck $readiness 'live_trade_monitor') -or $risk.ok -eq $true)
    lifecycle_ready = (Is-ReadyCheck $readiness 'intraday_lifecycle')
    decision_surface_reconciliation = $surfaceReconciliation.state
    model_learning_audit = $modelLearningAudit.state
  })
  Start-Sleep -Seconds $SampleSeconds
}

$Completed = Get-Date
$durationSeconds = [int]($Completed - $Started).TotalSeconds
$serviceContinuity = ($successfulSamples -ge [Math]::Max(1, $samples.Count - 2))
$scannerProgress = ($scannerFingerprints.Count -gt 1)
$checks = [ordered]@{
  service_continuity = $serviceContinuity
  scanner_progress_observed = $scannerProgress
  canonical_ranking_trace_observed = $rankingTraceObserved
  restart_recovery_passed = ($IncludeRestart -and $restartAttempted -and $restartPassed)
  risk_monitor_ready = $riskReadyObserved
  intraday_lifecycle_ready = $lifecycleReadyObserved
  post_close_intraday_flatten_verified = $postCloseFlattenVerified
  decision_surface_reconciliation_passed = $decisionSurfacePassed
  model_learning_audit_passed = $modelLearningAuditPassed
}
$rawSamples = $samples | ConvertTo-Json -Depth 20 -Compress
$sha = [System.Security.Cryptography.SHA256]::Create()
$bytes = [System.Text.Encoding]::UTF8.GetBytes($rawSamples)
$digest = ([BitConverter]::ToString($sha.ComputeHash($bytes))).Replace('-', '').ToLowerInvariant()
$evidence = [ordered]@{
  build = $ExpectedVersion
  started_at = $Started.ToUniversalTime().ToString('o')
  completed_at = $Completed.ToUniversalTime().ToString('o')
  duration_seconds = $durationSeconds
  sample_count = $samples.Count
  market_open_samples = $marketOpenSamples
  samples_digest = $digest
  checks = $checks
  samples = $samples
  note = 'A PASS requires a market-hours run, one restart recovery and a post-15:20 IST zero-open-Intraday verification. A partial run remains evidence but cannot promote maturity.'
}
$evidence | ConvertTo-Json -Depth 24 | Set-Content -Path $EvidencePath -Encoding UTF8

$submission = [ordered]@{
  build = $evidence.build
  started_at = $evidence.started_at
  completed_at = $evidence.completed_at
  duration_seconds = $durationSeconds
  sample_count = $samples.Count
  market_open_samples = $marketOpenSamples
  samples_digest = $digest
  evidence_path = $EvidencePath
  checks = $checks
}
try {
  $response = Invoke-RestMethod -Uri ($BaseUrl.TrimEnd('/') + '/api/validation/market-soak-proof') -Method Post -ContentType 'application/json' -Body ($submission | ConvertTo-Json -Depth 8) -TimeoutSec 20
} catch {
  $response = [pscustomobject]@{ ok = $false; state = 'SUBMIT_FAILED'; error = $_.Exception.Message }
}

Write-Host "Evidence: $EvidencePath"
$response | ConvertTo-Json -Depth 8
if ($response.passed -ne $true) { exit 2 }
