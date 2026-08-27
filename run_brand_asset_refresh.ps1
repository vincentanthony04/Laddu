param(
  [string]$InstallDir = "$env:ProgramData\ProjectLaddu",
  [int]$Limit = 600
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0
$pointer = Join-Path $InstallDir 'runtime\research_python.txt'
if(!(Test-Path -LiteralPath $pointer -PathType Leaf)){ throw "Research runtime pointer missing: $pointer" }
$python = (Get-Content -LiteralPath $pointer -Raw).Trim()
if(!(Test-Path -LiteralPath $python -PathType Leaf)){ throw "Research runtime missing: $python" }
$dataDir = Join-Path $InstallDir 'data'
$tool = Join-Path $InstallDir 'backend\tools\refresh_instrument_brand_assets.py'
$plan = Join-Path $InstallDir 'backend\resources\instrument_brand_sources.json'
& $python $tool --data-dir $dataDir --plan $plan --limit $Limit
if($LASTEXITCODE -ne 0){ throw "Instrument brand refresh failed with exit code $LASTEXITCODE" }
