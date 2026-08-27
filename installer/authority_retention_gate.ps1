# Clean Core R4 direct authority-retention gate.
# The application HTTP process is deliberately not an input to preservation.
function Invoke-AuthorityRetentionEvidence {
  param(
    [Parameter(Mandatory=$true)][string]$PythonExe,
    [Parameter(Mandatory=$true)][string]$OutputPath,
    [Parameter(Mandatory=$true)][string]$Label,
    [string]$CompareBefore = '',
    [switch]$CleanInstall
  )
  $tool = Join-Path $PackageRoot 'validation\capture_authority_retention_evidence.py'
  if(!(Test-Path -LiteralPath $tool -PathType Leaf)){ throw 'Direct authority-retention evidence tool is missing.' }
  $arguments = @($tool,'--install-dir',$InstallDir,'--output',$OutputPath,'--label',$Label)
  if($CleanInstall){ $arguments += '--clean-install' }
  else {
    $envFile = Join-Path $InstallDir 'secure\data-plane.env.ps1'
    if(!(Test-Path -LiteralPath $envFile -PathType Leaf)){ throw "Retained production data-plane environment is missing: $envFile" }
    $arguments += @('--env-file',$envFile)
  }
  if($CompareBefore){ $arguments += @('--compare-before',$CompareBefore) }
  & $PythonExe @arguments | Out-Host
  if($LASTEXITCODE -ne 0){ throw "Direct authority-retention evidence failed: label=$Label output=$OutputPath" }
  if(!(Test-Path -LiteralPath $OutputPath -PathType Leaf)){ throw "Authority-retention evidence was not produced: $OutputPath" }
  $result = Get-Content -LiteralPath $OutputPath -Raw | ConvertFrom-Json
  if($result.ok -ne $true){ throw "Authority-retention evidence rejected: label=$Label" }
  if($CompareBefore -and $result.comparison -and $result.comparison.ok -ne $true){
    throw "Authority retention regression: $(@($result.comparison.regressions | ConvertTo-Json -Compress) -join ',')"
  }
  return $result
}
function Test-PinnedPythonEnvironment {
  param(
    [Parameter(Mandatory=$true)][string]$PythonExe,
    [Parameter(Mandatory=$true)][string]$RequirementsPath
  )
  if(!(Test-Path -LiteralPath $PythonExe -PathType Leaf)){ return $false }
  $tool = Join-Path $PackageRoot 'validation\verify_pinned_environment.py'
  & $PythonExe $tool --requirements $RequirementsPath | Out-Host
  if($LASTEXITCODE -ne 0){ return $false }
  & $PythonExe -m pip check | Out-Host
  return ($LASTEXITCODE -eq 0)
}
function Assert-PinnedPythonEnvironment {
  param(
    [Parameter(Mandatory=$true)][string]$PythonExe,
    [Parameter(Mandatory=$true)][string]$RequirementsPath
  )
  if(!(Test-PinnedPythonEnvironment -PythonExe $PythonExe -RequirementsPath $RequirementsPath)){
    throw "Pinned Python environment does not match $RequirementsPath"
  }
}
function Resolve-PinnedPythonRuntime {
  param(
    [Parameter(Mandatory=$true)][string]$BasePython,
    [Parameter(Mandatory=$true)][string]$InstallDir,
    [Parameter(Mandatory=$true)][ValidateSet('backend','research')][string]$Family,
    [Parameter(Mandatory=$true)][string]$ReleaseTag,
    [Parameter(Mandatory=$true)][string]$RequirementsPath,
    [Parameter(Mandatory=$true)][string]$Label
  )
  $runtimeRoot = Join-Path $InstallDir 'runtime'
  New-Item -ItemType Directory -Path $runtimeRoot -Force | Out-Null
  $requirementsHash = (Get-FileHash -LiteralPath $RequirementsPath -Algorithm SHA256).Hash.ToLowerInvariant()
  $hashTag = $requirementsHash.Substring(0,12)
  $preferredDir = Join-Path $runtimeRoot ($Family + '-python-' + $ReleaseTag + '-' + $hashTag)
  $baseVersion = (& $BasePython -c "import sys; print(str(sys.version_info.major)+'.'+str(sys.version_info.minor))" | Select-Object -Last 1)
  if($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace([string]$baseVersion)){ throw "$Label base Python version could not be resolved." }
  $baseVersion = ([string]$baseVersion).Trim()

  # A Python environment is dependency infrastructure, not release source.  Reuse
  # across micro-releases is permitted only when both Python major.minor and the
  # exact pinned dependency set independently verify.  Never mutate an older venv.
  $candidates = @()
  if(Test-Path -LiteralPath $preferredDir -PathType Container){ $candidates += Get-Item -LiteralPath $preferredDir }
  $candidates += @(Get-ChildItem -LiteralPath $runtimeRoot -Directory -ErrorAction SilentlyContinue | Where-Object {
    $_.Name -like ($Family + '-python-*-' + $hashTag) -and $_.FullName -ne $preferredDir
  } | Sort-Object LastWriteTime -Descending)
  foreach($candidate in $candidates){
    $candidatePython = Join-Path $candidate.FullName 'Scripts\python.exe'
    if(!(Test-Path -LiteralPath $candidatePython -PathType Leaf)){ continue }
    $candidateVersion = (& $candidatePython -c "import sys; print(str(sys.version_info.major)+'.'+str(sys.version_info.minor))" 2>$null | Select-Object -Last 1)
    if($LASTEXITCODE -ne 0 -or ([string]$candidateVersion).Trim() -ne $baseVersion){
      Write-Step $Label ("Ignoring incompatible cached environment {0}; Python={1} required={2}" -f $candidate.FullName,([string]$candidateVersion).Trim(),$baseVersion)
      continue
    }
    if(Test-PinnedPythonEnvironment -PythonExe $candidatePython -RequirementsPath $RequirementsPath){
      Write-Step $Label ("Reusing exact verified dependency environment: {0}" -f $candidate.FullName)
      return [pscustomobject]@{ PythonExe=$candidatePython; RuntimeDir=$candidate.FullName; Created=$false; Reused=$true; RequirementsHash=$requirementsHash }
    }
    Write-Step $Label ("Cached environment is partial/inconsistent and will not be mutated: {0}" -f $candidate.FullName)
  }

  # If the preferred release path is poisoned by a prior interrupted attempt,
  # create a new recovery candidate rather than modifying/deleting a potentially
  # active environment. This makes retries safe and finite.
  $targetDir = $preferredDir
  if(Test-Path -LiteralPath (Join-Path $targetDir 'Scripts\python.exe') -PathType Leaf){
    $targetDir = $preferredDir + '-recovery-' + (Get-Date -Format 'yyyyMMddHHmmss')
  }
  Write-Step $Label ("Creating exact isolated dependency environment: {0}" -f $targetDir)
  & $BasePython -m venv $targetDir
  if($LASTEXITCODE -ne 0){ throw "$Label isolated Python environment creation failed." }
  $targetPython = Join-Path $targetDir 'Scripts\python.exe'
  & $targetPython -m pip install --disable-pip-version-check -r $RequirementsPath
  if($LASTEXITCODE -ne 0){ throw "$Label dependency installation failed." }
  Assert-PinnedPythonEnvironment -PythonExe $targetPython -RequirementsPath $RequirementsPath
  return [pscustomobject]@{ PythonExe=$targetPython; RuntimeDir=$targetDir; Created=$true; Reused=$false; RequirementsHash=$requirementsHash }
}
function Assert-ParentMigrationLineage {
  param(
    [Parameter(Mandatory=$true)][string]$PythonExe,
    [Parameter(Mandatory=$true)][string]$OutputPath
  )
  $tool = Join-Path $PackageRoot 'validation\verify_parent_migration_lineage.py'
  $envFile = Join-Path $InstallDir 'secure\data-plane.env.ps1'
  if(!(Test-Path -LiteralPath $tool -PathType Leaf)){ throw 'Parent migration-lineage verifier is missing.' }
  if(!(Test-Path -LiteralPath $envFile -PathType Leaf)){ throw "Retained production data-plane environment is missing: $envFile" }
  & $PythonExe $tool --install-dir $InstallDir --env-file $envFile --output $OutputPath | Out-Host
  $lineageExitCode = $LASTEXITCODE
  $proof = $null
  if(Test-Path -LiteralPath $OutputPath -PathType Leaf){
    try { $proof = Get-Content -LiteralPath $OutputPath -Raw | ConvertFrom-Json } catch {}
  }
  if($lineageExitCode -ne 0){
    $failureClass = if($null -ne $proof -and $proof.failure_class){ [string]$proof.failure_class } else { 'LINEAGE_VERIFIER_FAILED' }
    $failureDetail = if($null -ne $proof -and $proof.failures){ @($proof.failures) -join '; ' } else { "exit=$lineageExitCode" }
    throw "Parent migration-lineage verification failed [$failureClass]: $failureDetail. Upgrade is blocked before runtime stop."
  }
  if($null -eq $proof -or $proof.ok -ne $true){ throw 'Authoritative parent migration lineage proof was rejected.' }
  return $proof
}
