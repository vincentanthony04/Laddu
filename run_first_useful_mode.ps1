param(
  [string]$InstallDir = "$env:ProgramData\ProjectLaddu",
  [int]$Port = 8086,
  [int]$MaxMinutes = 240,
  [int]$IntervalSeconds = 45,
  [int]$CohortSize = 96,
  [int]$BatchSize = 12,
  [switch]$SkipTraining
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0

$base = "http://127.0.0.1:$Port"
$dataDir = Join-Path $InstallDir 'data'
$logDir = Join-Path $InstallDir 'logs\first-mode'
$manifestDir = Join-Path $dataDir 'manifests\first_useful_mode'
New-Item -ItemType Directory -Path $logDir,$manifestDir -Force | Out-Null
$stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
$log = Join-Path $logDir "first-mode-$stamp.log"
$reportPath = Join-Path $manifestDir "first-mode-$stamp.json"
$latestPath = Join-Path $manifestDir 'latest.json'

function Invoke-JsonGet([string]$Path,[int]$Timeout=15){
  return Invoke-RestMethod -UseBasicParsing -Method Get -Uri ($base + $Path) -TimeoutSec $Timeout
}
function Invoke-JsonPost([string]$Path,[hashtable]$Body,[int]$Timeout=20){
  $json = $Body | ConvertTo-Json -Depth 8 -Compress
  return Invoke-RestMethod -UseBasicParsing -Method Post -Uri ($base + $Path) -ContentType 'application/json' -Body $json -TimeoutSec $Timeout
}
function Write-RunLine([string]$Text){
  $line = "[{0}] {1}" -f (Get-Date -Format 'HH:mm:ss'),$Text
  $line | Tee-Object -FilePath $log -Append | Write-Host
}

$started = Get-Date
$deadline = $started.AddMinutes([Math]::Max(5,$MaxMinutes))
$ready = $null
for($i=0; $i -lt 30; $i++){
  try {
    $ready = Invoke-JsonGet '/api/ready' 5
    if($ready.ready -eq $true){ break }
  } catch {}
  Start-Sleep -Seconds 10
}
if($null -eq $ready -or $ready.ready -ne $true){ throw 'Project Laddu runtime did not become ready.' }

$lastStatus = $null
$activations = @()
$nextActivation = Get-Date
while((Get-Date) -lt $deadline){
  # Submit one bounded batch initially and then at most once every five
  # minutes. The prior loop POSTed on every status poll, causing duplicate
  # queue pressure and expensive dashboard/coverage work.
  if((Get-Date) -ge $nextActivation){
    try {
      $activation = Invoke-JsonPost '/api/first-useful-mode/run' @{ cohort_size=$CohortSize; batch_size=$BatchSize } 30
      $activations += [ordered]@{
        time = (Get-Date).ToString('o')
        state = $activation.state
        batch = @($activation.batch)
        scheduled_daily = $activation.scheduled_daily
        scheduled_m30 = $activation.scheduled_m30
      }
      $nextActivation = (Get-Date).AddMinutes(5)
    } catch {
      Write-RunLine ("activation unavailable: " + $_.Exception.Message)
      $nextActivation = (Get-Date).AddMinutes(1)
    }
  }
  try {
    $lastStatus = Invoke-JsonGet ("/api/first-useful-mode?cohort_size={0}" -f $CohortSize) 20
    $history = $lastStatus.history
    $today = $lastStatus.today
    Write-RunLine ("{0} | history {1}/{2} daily, {3}/{4} 30m | research {5} | final {6} | watch {7}" -f $lastStatus.state,$history.daily_ready,$lastStatus.cohort_size,$history.m30_ready,$lastStatus.cohort_size,$today.research_rows,$today.final,$today.watchlist)
    if($lastStatus.useful -eq $true){ break }
  } catch {
    Write-RunLine ("status unavailable: " + $_.Exception.Message)
  }
  Start-Sleep -Seconds ([Math]::Max(15,$IntervalSeconds))
}

$learning = $null
try {
  & (Join-Path $InstallDir 'run_learning_cycle.ps1') -InstallDir $InstallDir -Port $Port -Cycle premarket
  $learning = [ordered]@{ ok=($LASTEXITCODE -eq 0); exit_code=$LASTEXITCODE }
} catch {
  $learning = [ordered]@{ ok=$false; error=$_.Exception.Message }
}

$training = [ordered]@{ attempted=$false; ok=$false; state='SKIPPED' }
if(-not $SkipTraining -and $null -ne $lastStatus -and [int]$lastStatus.history.daily_ready -ge 10){
  $training.attempted = $true
  try {
    & (Join-Path $InstallDir 'train_ai_model.ps1') -InstallDir $InstallDir -Port $Port -FirstMode
    $training.ok = ($LASTEXITCODE -eq 0)
    $training.state = if($training.ok){ 'SHADOW_TRAINING_COMPLETED' } else { 'SHADOW_TRAINING_BLOCKED' }
    $training.exit_code = $LASTEXITCODE
  } catch {
    $training.ok = $false
    $training.state = 'SHADOW_TRAINING_BLOCKED'
    $training.error = $_.Exception.Message
  }
}

if($null -eq $lastStatus){
  try { $lastStatus = Invoke-JsonGet ("/api/first-useful-mode?cohort_size={0}" -f $CohortSize) 20 } catch { $lastStatus = [ordered]@{ ok=$false; state='STATUS_UNAVAILABLE'; error=$_.Exception.Message } }
}
$report = [ordered]@{
  version = 'first-useful-mode-runner-1.0.0'
  started_at = $started.ToString('o')
  completed_at = (Get-Date).ToString('o')
  install_dir = $InstallDir
  port = $Port
  cohort_size = $CohortSize
  batch_size = $BatchSize
  status = $lastStatus
  activations = $activations
  learning = $learning
  training = $training
  production_influence = 0.0
  broker_authority = 'NONE'
  success = ($null -ne $lastStatus -and $lastStatus.useful -eq $true)
}
$json = $report | ConvertTo-Json -Depth 12
Set-Content -LiteralPath $reportPath -Value $json -Encoding UTF8
Set-Content -LiteralPath $latestPath -Value $json -Encoding UTF8
Write-RunLine ("report " + $reportPath)
if($report.success){ exit 0 }
exit 2
