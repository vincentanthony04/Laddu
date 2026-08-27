param([string]$InstallDir = "$env:ProgramData\ProjectLaddu")
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0
$envFile = Join-Path $InstallDir 'secure\data-plane.env.ps1'
if(!(Test-Path -LiteralPath $envFile -PathType Leaf)){ throw "Data-plane environment missing: $envFile" }
. $envFile
$python = [Environment]::GetEnvironmentVariable('PROJECT_LADDU_RESEARCH_PYTHON','Machine')
if(!$python){ $python = (Get-Content -LiteralPath (Join-Path $InstallDir 'runtime\research_python.txt') | Select-Object -First 1).Trim() }
if(!(Test-Path -LiteralPath $python -PathType Leaf)){ throw "Research Python missing: $python" }
$report = Join-Path $InstallDir ("logs\research\governance-{0}.json" -f (Get-Date -Format 'yyyyMMdd_HHmmss'))
New-Item -ItemType Directory -Path (Split-Path -Parent $report) -Force | Out-Null
& $python (Join-Path $InstallDir 'backend\tools\run_model_governance_cycle.py') --report $report
if($LASTEXITCODE -ne 0){ throw 'Model governance cycle failed.' }
