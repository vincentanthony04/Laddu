param(
  [string]$InstallDir = "$env:ProgramData\ProjectLaddu",
  [int]$Port = 8086,
  [ValidateSet('premarket','market','settlement','weekend')][string]$Cycle = 'weekend'
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0
$envFile = Join-Path $InstallDir 'secure\data-plane.env.ps1'
if(!(Test-Path -LiteralPath $envFile -PathType Leaf)){ throw "Data-plane environment missing: $envFile" }
. $envFile
$python = Join-Path $InstallDir 'runtime\python\Scripts\python.exe'
if(!(Test-Path -LiteralPath $python -PathType Leaf)){ throw "Runtime Python missing: $python" }
& $python (Join-Path $InstallDir 'backend\tools\run_operational_learning_cycle.py') --cycle $Cycle --data-dir (Join-Path $InstallDir 'data') --api-url "http://127.0.0.1:$Port"
if($LASTEXITCODE -ne 0){ throw "Operational learning cycle failed: $Cycle" }
