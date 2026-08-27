# v114 typed local-state preservation orchestration.
#
# PowerShell owns process orchestration only. Path normalization, inventory
# enumeration, hashing, exclusions, collection semantics and comparisons are
# authoritative in local_state_manifest.py through explicit JSON/file contracts.

function Invoke-LocalStateManifestHelper {
  param(
    [Parameter(Mandatory=$true)][string]$PythonExe,
    [Parameter(Mandatory=$true)][string[]]$Arguments
  )
  if(!(Test-Path -LiteralPath $PythonExe -PathType Leaf)){ throw "Typed preservation Python is missing: $PythonExe" }
  $helper = Join-Path $PSScriptRoot 'local_state_manifest.py'
  if(!(Test-Path -LiteralPath $helper -PathType Leaf)){ throw "Typed preservation helper is missing: $helper" }
  $output = & $PythonExe $helper @Arguments 2>&1
  if($LASTEXITCODE -ne 0){ throw "Typed preservation helper failed: $([string]($output -join ' '))" }
  try { return ([string]($output -join "`n") | ConvertFrom-Json) }
  catch { throw "Typed preservation helper returned invalid JSON: $([string]($output -join ' '))" }
}

function Test-PreservedLocalStatePresence {
  param([Parameter(Mandatory=$true)][string]$PythonExe)
  $summary = Invoke-LocalStateManifestHelper -PythonExe $PythonExe -Arguments @(
    'presence','--install-dir',$InstallDir
  )
  if($summary.ok -ne $true){ throw 'Typed preservation presence check did not pass.' }
  return [bool]$summary.present
}

function New-PreservedLocalStateSnapshot {
  param(
    [Parameter(Mandatory=$true)][string]$PythonExe,
    [Parameter(Mandatory=$true)][string]$OutputPath
  )
  $summary = Invoke-LocalStateManifestHelper -PythonExe $PythonExe -Arguments @(
    'snapshot','--install-dir',$InstallDir,'--output',$OutputPath
  )
  if($summary.ok -ne $true -or !(Test-Path -LiteralPath $OutputPath -PathType Leaf)){
    throw 'Typed preservation snapshot did not produce its manifest.'
  }
  return $summary
}

function Assert-PreservedLocalState {
  param(
    [Parameter(Mandatory=$true)][string]$PythonExe,
    [Parameter(Mandatory=$true)][string]$BeforePath,
    [Parameter(Mandatory=$true)][string]$AfterPath,
    [Parameter(Mandatory=$true)][string]$ProofPath
  )
  $summary = Invoke-LocalStateManifestHelper -PythonExe $PythonExe -Arguments @(
    'verify','--install-dir',$InstallDir,'--before',$BeforePath,
    '--after-output',$AfterPath,'--proof-output',$ProofPath
  )
  if($summary.ok -ne $true -or !(Test-Path -LiteralPath $AfterPath -PathType Leaf) -or !(Test-Path -LiteralPath $ProofPath -PathType Leaf)){
    throw 'Typed preservation proof did not pass or produce complete evidence.'
  }
  return $summary
}
