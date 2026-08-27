param(
  [string]$InstallDir = "$env:ProgramData\ProjectLaddu",
  [int]$Port = 8086,
  [ValidateSet('MID_SESSION','POST_CLOSE')][string]$Phase = 'MID_SESSION'
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0
$dir = Join-Path $InstallDir 'data\manifests\focused_diagnostics'
New-Item -ItemType Directory -Path $dir -Force | Out-Null
$stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
$path = Join-Path $dir ("{0}-{1}.json" -f $Phase.ToLower(),$stamp)
$base = "http://127.0.0.1:$Port"
$endpoints = [ordered]@{
  ready='/api/ready'
  scanner='/api/scanner/status'
  coverage='/api/data-coverage'
  nse_data='/api/nse-data-authority'
  first_mode='/api/first-useful-mode?cohort_size=96'
  ml='/api/ml-population-qualification'
  forward_clock='/api/forward-evidence-clock'
  performance='/api/performance/summary'
}
$payload = [ordered]@{ version='focused-market-diagnostics-1.0.0'; build='v99.0.0'; phase=$Phase; captured_at=(Get-Date).ToString('o'); endpoints=[ordered]@{} }
foreach($name in $endpoints.Keys){
  try { $payload.endpoints[$name] = Invoke-RestMethod -UseBasicParsing -Method Get -Uri ($base+$endpoints[$name]) -TimeoutSec 25 }
  catch { $payload.endpoints[$name] = [ordered]@{ ok=$false; error=$_.Exception.Message } }
}
$payload | ConvertTo-Json -Depth 25 | Set-Content -LiteralPath $path -Encoding UTF8
Write-Host "Focused diagnostics: $path"
exit 0
