param(
  [string]$InstallDir = "$env:ProgramData\ProjectLaddu",
  [int]$Port = 8086,
  [switch]$FirstMode
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0
$envFile = Join-Path $InstallDir 'secure\data-plane.env.ps1'
if(!(Test-Path -LiteralPath $envFile -PathType Leaf)){ throw "Data-plane environment missing: $envFile" }
. $envFile
$python = [Environment]::GetEnvironmentVariable('PROJECT_LADDU_RESEARCH_PYTHON','Machine')
if(!$python){ $python = (Get-Content -LiteralPath (Join-Path $InstallDir 'runtime\research_python.txt') | Select-Object -First 1).Trim() }
if(!(Test-Path -LiteralPath $python -PathType Leaf)){ throw "Research Python missing: $python" }
$dataDir = Join-Path $InstallDir 'data'
$logDir = Join-Path $InstallDir 'logs\research'
New-Item -ItemType Directory -Path $logDir -Force | Out-Null
$log = Join-Path $logDir ("training-{0}.log" -f (Get-Date -Format 'yyyyMMdd_HHmmss'))
Start-Transcript -Path $log -Force | Out-Null
try {
  & $python (Join-Path $InstallDir 'backend\tools\refresh_research_catalog.py') --data-dir $dataDir
  if($LASTEXITCODE -ne 0){ throw 'Research catalogue refresh failed.' }
  $args = @((Join-Path $InstallDir 'backend\tools\train_nse_smart_model.py'), '--data-dir', $dataDir, '--api-url', "http://127.0.0.1:$Port")
  if($FirstMode){
    $args += '--first-mode'
  } else {
    # R36: preserve the authoritative AI-training task identity while requiring
    # materially deeper governed historical PIT coverage before normal training
    # can qualify. Historical reconstruction remains research/shadow evidence;
    # it never counts as elapsed forward Model-Paper time.
    $args += @('--min-dates','504')
  }
  & $python @args
  if($LASTEXITCODE -ne 0){ throw 'Governed model training failed.' }
} finally { try { Stop-Transcript | Out-Null } catch {} }
