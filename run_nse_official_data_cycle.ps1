param(
  [string]$InstallDir = "$env:ProgramData\ProjectLaddu",
  [string]$TradeDate = "",
  [switch]$InboxOnly
)
$ErrorActionPreference = 'Stop'
$pointer = Join-Path $InstallDir 'runtime\research_python.txt'
if(!(Test-Path $pointer -PathType Leaf)){ throw "Research runtime pointer missing: $pointer" }
$python = (Get-Content -LiteralPath $pointer -Raw).Trim()
if(!(Test-Path $python -PathType Leaf)){ throw "Research runtime missing: $python" }
$dataDir = Join-Path $InstallDir 'data'
$plan = Join-Path $dataDir 'config\nse_official_sources.json'
$defaultPlan = Join-Path $InstallDir 'backend\resources\nse_official_sources.example.json'
$mergeTool = Join-Path $InstallDir 'backend\tools\merge_nse_official_source_plan.py'
& $python $mergeTool --default-plan $defaultPlan --target-plan $plan
if($LASTEXITCODE -ne 0){ throw "NSE official source plan merge failed with exit code $LASTEXITCODE" }
$args = @((Join-Path $InstallDir 'backend\tools\run_nse_official_data_cycle.py'),'--data-dir',$dataDir,'--plan',$plan)
if($TradeDate){ $args += @('--trade-date',$TradeDate) }
if($InboxOnly){ $args += '--inbox-only' }
& $python @args
if($LASTEXITCODE -ne 0){ throw "NSE official data cycle failed with exit code $LASTEXITCODE" }
