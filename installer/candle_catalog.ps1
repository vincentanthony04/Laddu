function Build-CandleFileCatalog {
  param(
    [Parameter(Mandatory=$true)][string]$InstallDir,
    [Parameter(Mandatory=$true)][string]$BackendPython,
    [Parameter(Mandatory=$true)][string]$EvidenceDir
  )
  $proofPath = Join-Path $EvidenceDir 'candle-file-catalog-v2.json'
  $builder = Join-Path $InstallDir 'backend\tools\build_candle_file_catalog.py'
  if(!(Test-Path -LiteralPath $builder -PathType Leaf)){ throw "Candle catalogue builder missing: $builder" }
  Write-Host '[CANDLE-CATALOG] Building rebuildable timestamp-indexed cold catalogue; persisted candle bytes are not mutated.' -ForegroundColor Cyan
  & $BackendPython $builder --data-dir (Join-Path $InstallDir 'data') --force > $proofPath
  if($LASTEXITCODE -ne 0){ throw "Candle catalogue rebuild failed. Evidence: $proofPath" }
  $proof = Get-Content -LiteralPath $proofPath -Raw | ConvertFrom-Json
  if($proof.ok -ne $true -or [string]$proof.state -ne 'READY'){ throw "Candle catalogue did not reach READY. Evidence: $proofPath" }
  Write-Host "[CANDLE-CATALOG] READY; files=$($proof.catalog.files); series=$($proof.catalog.series)" -ForegroundColor Green
  return $proof
}
