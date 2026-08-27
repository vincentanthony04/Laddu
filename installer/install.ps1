param([string]$InstallDir = "$env:ProgramData\ProjectLaddu", [int]$Port = 8086, [int]$StartupDeadlineSec = 120)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0
$ServiceName = 'ProjectLaddu'
$PackageRoot = Split-Path -Parent $PSScriptRoot
$SelfPath = $MyInvocation.MyCommand.Path
$PayloadFolders = @('backend','frontend','installer','service','docs','validation','infra','tools')
$CoreOperatorFiles = @(
  'START.cmd','STOP.cmd','RESTART.cmd','STATUS.cmd',
  'settoken.ps1','uninstall.ps1','README_INSTALL.txt','RELEASE_IDENTITY.json','RELEASE_ATTESTATION.json',
  'requirements-runtime.txt','requirements-research.txt','RUN_QUANT_SUCCESS_AUDIT.cmd','train_ai_model.ps1'
)
$DiscoveredOperatorFiles = @(Get-ChildItem -LiteralPath $PackageRoot -File -ErrorAction Stop | Where-Object {
  $_.Name -match '^(?:RUN_|run_|VERIFY_|REHEARSE_)'
} | Sort-Object Name | Select-Object -ExpandProperty Name)
$OperatorFiles = @($CoreOperatorFiles + $DiscoveredOperatorFiles) | Sort-Object -Unique
$RuntimeStateFolders = @('data','secure','logs','runtime')
$RunId = Get-Date -Format 'yyyyMMdd_HHmmss'; $EvidenceRoot = 'C:\Temp\ProjectLaddu\installer'
$EvidenceDir = Join-Path $EvidenceRoot $RunId
$TransactionRoot = Join-Path $InstallDir 'runtime\installer-transactions'; $TransactionDir = Join-Path $TransactionRoot $RunId; $StageDir = Join-Path $TransactionDir 'stage'; $BackupDir = Join-Path $TransactionDir 'rollback-payload'; $PreflightHome = Join-Path $TransactionDir 'preflight-home'
$TranscriptPath = Join-Path $EvidenceDir 'install-transcript.log'
$FailurePath = Join-Path $EvidenceDir 'INSTALL_FAILURE_SUMMARY.json'
$ResultPath = Join-Path $EvidenceDir 'INSTALL_RESULT.json'
$PrecommitResultPath = Join-Path $EvidenceDir 'INSTALL_RESULT.precommit.json'
$TransactionJournalPath = Join-Path $EvidenceDir 'INSTALL_TRANSACTION.json'
$ManifestPath = Join-Path $InstallDir 'DEPLOY_MANIFEST.json'
$ResearchTaskNames = @('ProjectLaddu-First-Useful-Mode','ProjectLaddu-Premarket-Learning','ProjectLaddu-PostClose-Settlement','ProjectLaddu-NSE-Official-Data','ProjectLaddu-AI-Training','ProjectLaddu-Model-Governance','ProjectLaddu-Brand-Assets','ProjectLaddu-Weekend-Research')
$ResearchTaskBackupDir = Join-Path $TransactionDir 'research-tasks-before'; $ReferenceAuthorityBootstrapPath = Join-Path $EvidenceDir 'reference-authority-bootstrap.json'; function Test-Administrator {
  $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
  $principal = New-Object Security.Principal.WindowsPrincipal($identity)
  return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}
function Invoke-Elevation {
  if(Test-Administrator){ return }
  $arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$SelfPath`" -InstallDir `"$InstallDir`" -Port $Port -StartupDeadlineSec $StartupDeadlineSec"
  try {
    $child = Start-Process powershell.exe -Verb RunAs -ArgumentList $arguments -Wait -PassThru
    if($null -eq $child){ exit 1603 }
    $childExitCode = [int]$child.ExitCode
    exit $childExitCode
  } catch {
    Write-Host ("Elevation failed: " + $_.Exception.Message) -ForegroundColor Red
    exit 1603
  }
}
function Write-Step([string]$Name,[string]$Message){
  Write-Host ("[{0}] {1}: {2}" -f (Get-Date -Format 'HH:mm:ss'),$Name,$Message) -ForegroundColor Cyan
}
function Wait-ResearchPlaneReady {
  param(
    [int]$OverallDeadlineSec = 120,
    [int]$SleepSec = 3
  )
  # /api/quant-research-plane is a cache-only projection. HTTP 200 means the
  # projection is readable, not that its governed authority is READY.  Under
  # Set-StrictMode, optional JSON fields must never be dereferenced directly.
  $deadline = (Get-Date).AddSeconds($OverallDeadlineSec)
  $attempt = 0
  $lastState = 'UNREADABLE'
  $lastOk = $false
  $lastProjection = ''
  while((Get-Date) -lt $deadline){
    $attempt += 1
    try {
      $payload = Get-ApiJson '/api/quant-research-plane'
      $okProperty = $payload.PSObject.Properties['ok']
      $stateProperty = $payload.PSObject.Properties['state']
      $lastOk = ($null -ne $okProperty -and $okProperty.Value -eq $true)
      $lastState = if($null -ne $stateProperty){ [string]$stateProperty.Value } else { 'MISSING_STATE' }
      $lastProjection = ($payload | ConvertTo-Json -Depth 8 -Compress)
      if($lastProjection.Length -gt 1200){ $lastProjection = $lastProjection.Substring(0,1200) + '...' }
      if($lastOk -and $lastState -eq 'READY'){
        Write-Host ("[PROJECTION] Research runtime authority READY on attempt {0}" -f $attempt) -ForegroundColor DarkGreen
        return $payload
      }
      $remaining = [Math]::Max(0,[int]($deadline-(Get-Date)).TotalSeconds)
      Write-Host ("[PROJECTION] Research runtime authority readable but not READY attempt={0} state={1} ok={2} remaining={3}s" -f $attempt,$lastState,$lastOk,$remaining) -ForegroundColor DarkYellow
    } catch {
      $lastState = 'UNREADABLE'
      $lastOk = $false
      $lastProjection = $_.Exception.Message
      $remaining = [Math]::Max(0,[int]($deadline-(Get-Date)).TotalSeconds)
      Write-Host ("[PROJECTION] Research runtime authority unreadable attempt={0} remaining={1}s error={2}" -f $attempt,$remaining,$lastProjection) -ForegroundColor DarkYellow
    }
    if((Get-Date) -ge $deadline){ break }
    Start-Sleep -Seconds ([Math]::Max(1,$SleepSec))
  }
  throw ("Research runtime authority did not reach the exact contract ok=true,state=READY within {0}s. Last state={1}; ok={2}; projection={3}" -f $OverallDeadlineSec,$lastState,$lastOk,$lastProjection)
}
$InstallerModuleFiles = @('package_gate.ps1','authority_retention_gate.ps1','installed_target_discovery.ps1','clean_install_state.ps1','install_transaction_orchestration.ps1','install_http_orchestration.ps1','runtime_recovery.ps1','research_task_state.ps1','research_governance_migration.ps1','candle_catalog.ps1','prerequisites.ps1')
$RequiredInstallerCommands = @('Clear-PackageTransientPythonBytecode','Assert-PackageManifest','Assert-ReleaseLineage','Assert-PinnedPythonEnvironment','Test-PinnedPythonEnvironment','Resolve-PinnedPythonRuntime','Invoke-AuthorityRetentionEvidence','Get-PriorRuntimeVersion','Test-PreservedLocalStatePresence','New-PreservedLocalStateSnapshot','Assert-PreservedLocalState','Start-InstallTransaction','Set-InstallTransactionPhase','Complete-InstallTransaction','Stop-InstallTransaction','Initialize-InstallerWorkspace','Complete-InstallerWorkspaceCleanup','Wait-Ready','Get-ApiJson','Get-ApiJsonWithRetry','Get-ResearchRetentionProjectionProof','Get-HttpText','Restore-ParentRuntimeOwner','Backup-ResearchTasks','Quiesce-ResearchTasks','Restore-ResearchTasks','Invoke-LegacyResearchGovernanceMigration','Build-CandleFileCatalog','Ensure-ProjectLadduPrerequisites')
function Write-Json([string]$Path,[object]$Value){
  $parent = Split-Path -Parent $Path
  if($parent){ New-Item -ItemType Directory -Path $parent -Force | Out-Null }
  [IO.File]::WriteAllText($Path,($Value | ConvertTo-Json -Depth 12),(New-Object Text.UTF8Encoding($false)))
}
function Publish-RuntimeMetadataFile([string]$Source,[string]$Destination,[int]$Attempts = 8){
  if(!(Test-Path -LiteralPath $Source -PathType Leaf)){ throw "Runtime metadata source is missing: $Source" }
  $parent = Split-Path -Parent $Destination
  if($parent){ New-Item -ItemType Directory -Path $parent -Force | Out-Null }
  $expectedHash = (Get-FileHash -LiteralPath $Source -Algorithm SHA256).Hash.ToLowerInvariant()
  $lastError = $null
  for($attempt=1; $attempt -le $Attempts; $attempt++){
    $temp = Join-Path $parent ((Split-Path -Leaf $Destination) + '.publish.' + [Guid]::NewGuid().ToString('N') + '.tmp')
    try {
      Copy-Item -LiteralPath $Source -Destination $temp -Force -ErrorAction Stop
      if(Test-Path -LiteralPath $Destination -PathType Leaf){
        try { & attrib.exe -R $Destination 2>$null | Out-Null } catch {}
        try {
          Remove-Item -LiteralPath $Destination -Force -ErrorAction Stop
        } catch {
          # Runtime metadata is rebuildable, non-secret state. If an older file
          # carries a stale DACL, reclaim only this file and retry its removal.
          try { & takeown.exe /F $Destination /A 2>$null | Out-Null } catch {}
          try { & icacls.exe $Destination /grant:r '*S-1-5-18:(F)' '*S-1-5-32-544:(F)' 2>$null | Out-Null } catch {}
          Remove-Item -LiteralPath $Destination -Force -ErrorAction Stop
        }
      }
      Move-Item -LiteralPath $temp -Destination $Destination -Force -ErrorAction Stop
      $actualHash = (Get-FileHash -LiteralPath $Destination -Algorithm SHA256).Hash.ToLowerInvariant()
      if($actualHash -ne $expectedHash){ throw "Runtime metadata hash mismatch: expected=$expectedHash actual=$actualHash" }
      return [pscustomobject]@{ ok=$true; attempts=$attempt; sha256=$actualHash; destination=$Destination }
    } catch {
      $lastError = $_
      if(Test-Path -LiteralPath $temp -PathType Leaf){ Remove-Item -LiteralPath $temp -Force -ErrorAction SilentlyContinue }
      if($attempt -lt $Attempts){ Start-Sleep -Milliseconds ([Math]::Min(1600,200*$attempt)) }
    }
  }
  throw ("Runtime metadata publish failed after {0} attempts: {1}" -f $Attempts,$lastError.Exception.Message)
}
function Resolve-BasePython {
  foreach($candidate in @('C:\Program Files\Python312\python.exe',(Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe'))){
    if(Test-Path -LiteralPath $candidate -PathType Leaf){ return $candidate }
  }
  $py = Get-Command py.exe -ErrorAction SilentlyContinue
  if($py){
    $resolved = & $py.Source -3.12 -c "import sys; print(sys.executable)" 2>$null
    if($LASTEXITCODE -eq 0 -and $resolved -and (Test-Path -LiteralPath $resolved.Trim())){ return $resolved.Trim() }
  }
  $python = Get-Command python.exe -ErrorAction SilentlyContinue
  if($python){
    $version = & $python.Source -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>$null
    if($LASTEXITCODE -eq 0 -and [string]$version.Trim() -eq '3.12'){ return $python.Source }
  }
  throw 'Python 3.12 x64 is required and was not found.'
}
function Get-TreeHash([string]$Path){
  $root = (Get-Item -LiteralPath $Path).FullName.TrimEnd('\')
  $material = Get-ChildItem -LiteralPath $root -Recurse -File | Where-Object {
    $_.FullName -notmatch '[\\/](?:__pycache__|\.pytest_cache)[\\/]' -and $_.Extension -notin @('.pyc','.pyo')
  } | Sort-Object FullName | ForEach-Object {
    $relative = $_.FullName.Substring($root.Length + 1).Replace('\','/')
    $digest = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash
    "$relative`t$digest"
  }
  $bytes = [Text.Encoding]::UTF8.GetBytes(($material -join "`n"))
  return [BitConverter]::ToString([Security.Cryptography.SHA256]::Create().ComputeHash($bytes)).Replace('-','')
}
function Get-RuntimeOwnerState {
  $service = Get-CimInstance Win32_Service -Filter "Name='$ServiceName'" -ErrorAction SilentlyContinue
  $task = Get-ScheduledTask -TaskName $ServiceName -ErrorAction SilentlyContinue
  $taskInfo = if($task){ Get-ScheduledTaskInfo -TaskName $task.TaskName -TaskPath $task.TaskPath -ErrorAction SilentlyContinue } else { $null }
  return [pscustomobject]@{
    ServiceExists = $null -ne $service
    ServiceRunning = ($null -ne $service -and [string]$service.State -eq 'Running')
    ServicePath = if($service){ [string]$service.PathName } else { '' }
    ServiceStartMode = if($service){ [string]$service.StartMode } else { '' }
    TaskExists = $null -ne $task
    TaskRunning = ($null -ne $taskInfo -and [string]$taskInfo.State -eq 'Running')
    TaskName = if($task){ [string]$task.TaskName } else { '' }
    TaskPath = if($task){ [string]$task.TaskPath } else { '' }
  }
}
function Stop-RuntimeOwners {
  $task = Get-ScheduledTask -TaskName $ServiceName -ErrorAction SilentlyContinue
  if($task){ Stop-ScheduledTask -TaskName $task.TaskName -TaskPath $task.TaskPath -ErrorAction SilentlyContinue }
  $service = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
  if($service -and $service.Status -ne 'Stopped'){
    Stop-Service -Name $ServiceName -Force -ErrorAction SilentlyContinue
    try { $service.WaitForStatus('Stopped',[TimeSpan]::FromSeconds(20)) } catch {}
  }
  Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
    $_.ProcessId -ne $PID -and $_.CommandLine -and $_.CommandLine -like '*ProjectLaddu*backend*main.py*'
  } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
}
function Restore-Payload {
  Stop-RuntimeOwners
  foreach($folder in $PayloadFolders){
    $installed = Join-Path $InstallDir $folder
    $backup = Join-Path $BackupDir $folder
    if(Test-Path -LiteralPath $installed){ Remove-Item -LiteralPath $installed -Recurse -Force }
    if(Test-Path -LiteralPath $backup -PathType Container){ Move-Item -LiteralPath $backup -Destination $installed -Force }
  }
  foreach($file in $OperatorFiles){
    $installed = Join-Path $InstallDir $file
    $backup = Join-Path $BackupDir $file
    if(Test-Path -LiteralPath $installed -PathType Leaf){ Remove-Item -LiteralPath $installed -Force }
    if(Test-Path -LiteralPath $backup -PathType Leaf){ Move-Item -LiteralPath $backup -Destination $installed -Force }
  }
}
function Show-StartupEvidence {
  foreach($path in @((Join-Path $InstallDir 'logs\backend-startup-error.log'),(Join-Path $InstallDir 'logs\service.log'))){
    if(Test-Path -LiteralPath $path){ Write-Host "--- $path ---" -ForegroundColor Yellow; Get-Content -LiteralPath $path -Tail 120 -ErrorAction SilentlyContinue | Out-Host }
  }
}
Invoke-Elevation
$OwnerBefore = [pscustomobject]@{ ServiceExists=$false; ServiceRunning=$false; ServicePath=''; ServiceStartMode=''; TaskExists=$false; TaskRunning=$false; TaskName=''; TaskPath='' }
$PreviousInstalledVersion = ''
$PayloadChanged = $false
$ServiceCreated = $false
$ResearchTasksChanged = $false
$ResearchTasksQuiesced = $false
$ResearchTaskQuiescePath = Join-Path $EvidenceDir 'research-task-quiescence.json'
$ResearchPythonBefore = $null
$ResearchPointerPath = Join-Path $InstallDir 'runtime\research_python.txt'
$ResearchPointerExisted = $false
$ResearchPointerBefore = ''
$ResearchContinuityAfter = $null
$ResearchAfterPath = Join-Path $EvidenceDir 'research-continuity-after.json'
$ResearchManifestAfterPath = Join-Path $EvidenceDir 'research-preservation-manifest-after.json'
$AuthorityBeforeSchemaPath = Join-Path $EvidenceDir 'authority-retention-before-schema.json'
$AuthorityAfterSchemaPath = Join-Path $EvidenceDir 'authority-retention-after-schema.json'
$AuthorityBeforeSwapPath = Join-Path $EvidenceDir 'authority-retention-before-swap.json'
$AuthorityAfterActivationPath = Join-Path $EvidenceDir 'authority-retention-after-activation.json'
$AuthorityAfterResearchMigrationPath = Join-Path $EvidenceDir 'authority-retention-after-research-migration.json'
$ResearchGovernanceMigrationPath = Join-Path $EvidenceDir 'research-governance-migration.json'
$ParentMigrationLineagePath = Join-Path $EvidenceDir 'parent-migration-lineage.json'
$ParentRuntimeRestorePath = Join-Path $EvidenceDir 'parent-runtime-restore.json'
$ProtectedStateBeforePath = Join-Path $EvidenceDir 'preserved-local-state-before.json'
$ProtectedStateAfterPath = Join-Path $EvidenceDir 'preserved-local-state-after.json'
$ProtectedStateProofPath = Join-Path $EvidenceDir 'preserved-local-state-proof.json'
$ProtectedStateBefore = $null
$ProtectedStateProof = $null
$ParentVersionExpected = ''
$BackendPointerPath = Join-Path $InstallDir 'runtime\backend_python.txt'
$BackendPointerExisted = $false
$BackendPointerBefore = ''
$PortPointerPath = Join-Path $InstallDir 'runtime\port.txt'
$PortPointerExisted = $false
$PortPointerBefore = ''
$RuntimePointerChanged = $false
$RuntimeStopped = $false
$BackendRuntimeCreated = $false
$ResearchRuntimeCreated = $false
$backendRuntimeDir = $null
$researchRuntimeDir = $null
$TransactionStarted = $false
$TransactionCommitted = $false
$SchemaApplied = $false
try {
  New-Item -ItemType Directory -Path $EvidenceDir -Force -ErrorAction Stop | Out-Null
  Start-Transcript -Path $TranscriptPath -Force -ErrorAction Stop | Out-Null
  foreach($moduleName in $InstallerModuleFiles){
    $modulePath = Join-Path $PSScriptRoot $moduleName
    if(!(Test-Path -LiteralPath $modulePath -PathType Leaf)){ throw "Required installer module is missing: $moduleName" }
    . $modulePath
  }
  $missingCommands = @($RequiredInstallerCommands | Where-Object {
    $null -eq (Get-Command -Name $_ -CommandType Function -ErrorAction SilentlyContinue)
  })
  if($missingCommands.Count -gt 0){ throw "Installer module contract is incomplete: $($missingCommands -join ', ')" }
  $OwnerBefore = Get-RuntimeOwnerState
  $PreviousInstalledVersion = Get-PriorRuntimeVersion
  $ParentVersionExpected = $PreviousInstalledVersion
  $ResearchPythonBefore = [Environment]::GetEnvironmentVariable('PROJECT_LADDU_RESEARCH_PYTHON','Machine')
  $ResearchPointerExisted = Test-Path -LiteralPath $ResearchPointerPath -PathType Leaf
  $ResearchPointerBefore = if($ResearchPointerExisted){ Get-Content -LiteralPath $ResearchPointerPath -Raw -ErrorAction Stop } else { '' }
  $BackendPointerExisted = Test-Path -LiteralPath $BackendPointerPath -PathType Leaf
  $BackendPointerBefore = if($BackendPointerExisted){ Get-Content -LiteralPath $BackendPointerPath -Raw -ErrorAction Stop } else { '' }
  $PortPointerExisted = Test-Path -LiteralPath $PortPointerPath -PathType Leaf
  $PortPointerBefore = if($PortPointerExisted){ Get-Content -LiteralPath $PortPointerPath -Raw -ErrorAction Stop } else { '' }
  Write-Host ''
  Write-Host 'Project Laddu - single-authority install/update' -ForegroundColor Green
  Write-Host 'Clean application install/reinstall on any machine or prior Laddu version. Existing production data and secure state are preserved.' -ForegroundColor Yellow
  Write-Host "Evidence: $EvidenceDir"
  Write-Host ''
  Write-Step 'PACKAGE' 'Proving exact package inventory, checksums, installability and sealed source lineage before any target work'
  foreach($folder in $PayloadFolders){ if(!(Test-Path -LiteralPath (Join-Path $PackageRoot $folder) -PathType Container)){ throw "Package folder missing: $folder" } }
  foreach($file in @('backend\main.py','backend\application_runtime.py','frontend\index.html','frontend\release-identity.json','service\ProjectLadduService.cs','requirements-runtime.txt','requirements-research.txt','RELEASE_IDENTITY.json','RELEASE_ATTESTATION.json')){
    if(!(Test-Path -LiteralPath (Join-Path $PackageRoot $file) -PathType Leaf)){ throw "Package file missing: $file" }
  }
  $PackageTransientBytecodeProof = Clear-PackageTransientPythonBytecode
  Assert-PackageManifest
  $CandidateRelease = Assert-ReleaseLineage
  Write-Step 'TARGET' ("Prior runtime version captured for rollback evidence only: {0}" -f $(if([string]::IsNullOrWhiteSpace($ParentVersionExpected)){'UNKNOWN_OR_NONE'}else{$ParentVersionExpected}))
  Write-Step 'PREFLIGHT' 'Validating production environment before stopping the app'
  foreach($file in @('train_ai_model.ps1','run_model_governance_cycle.ps1','run_learning_cycle.ps1','run_first_useful_mode.ps1','run_nse_official_data_cycle.ps1','run_brand_asset_refresh.ps1','run_active_data_and_training_readiness.ps1','run_focused_market_diagnostics.ps1','RUN_FIRST_USEFUL_MODE.cmd','RUN_BRAND_ASSET_REFRESH.cmd','RUN_ACTIVE_DATA_AND_TRAINING_READINESS.cmd','RUN_FOCUSED_MARKET_DIAGNOSTICS.cmd')){
    if(!(Test-Path -LiteralPath (Join-Path $PackageRoot $file) -PathType Leaf)){ throw "Package file missing: $file" }
  }
  $packageFrontendHash = Get-TreeHash (Join-Path $PackageRoot 'frontend')
  Write-Step 'FRONTEND' "Package frontend tree hash=$packageFrontendHash"
  Get-ChildItem -LiteralPath $PackageRoot -Recurse -File -ErrorAction SilentlyContinue | ForEach-Object { Unblock-File -LiteralPath $_.FullName -ErrorAction SilentlyContinue }
  $PrerequisiteProof = Ensure-ProjectLadduPrerequisites -EvidenceDir $EvidenceDir
  $basePython = Resolve-BasePython
  Write-Step 'PREFLIGHT' 'Proving installer transaction state graph matches every real orchestration phase before target mutation'
  & $basePython (Join-Path $PackageRoot 'validation\validate_installer_phase_handoff.py') --transaction-parity-only
  if($LASTEXITCODE -ne 0){ throw 'Installer transaction phase/state-machine contract is inconsistent.' }
  Start-InstallTransaction
  Set-InstallTransactionPhase -Phase 'PACKAGE_PROOF' -Detail 'exact package inventory, checksums, installability and sealed source lineage passed'
  New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null
  foreach($folder in $RuntimeStateFolders){ New-Item -ItemType Directory -Path (Join-Path $InstallDir $folder) -Force | Out-Null }
  Initialize-InstallerWorkspace -TransactionDir $TransactionDir -EvidenceDir $EvidenceDir
  $requirements = Join-Path $PackageRoot 'requirements-runtime.txt'
  $releaseRuntimeTag = ([string]$CandidateRelease.version).TrimStart('v').Replace('.','_')
  $backendEnv = Resolve-PinnedPythonRuntime -BasePython $basePython -InstallDir $InstallDir -Family 'backend' -ReleaseTag $releaseRuntimeTag -RequirementsPath $requirements -Label 'RUNTIME'
  $backendRuntimeDir = [string]$backendEnv.RuntimeDir
  $venvPython = [string]$backendEnv.PythonExe
  if($backendEnv.Created){ $BackendRuntimeCreated = $true }
  $researchRequirements = Join-Path $PackageRoot 'requirements-research.txt'
  $researchEnv = Resolve-PinnedPythonRuntime -BasePython $basePython -InstallDir $InstallDir -Family 'research' -ReleaseTag $releaseRuntimeTag -RequirementsPath $researchRequirements -Label 'RESEARCH'
  $researchRuntimeDir = [string]$researchEnv.RuntimeDir
  $researchPython = [string]$researchEnv.PythonExe
  if($researchEnv.Created){ $ResearchRuntimeCreated = $true }
  & $researchPython -c "import numpy,pandas,scipy,sklearn,ta,duckdb,lightgbm,psycopg; print('research-runtime-ok')"
  if($LASTEXITCODE -ne 0){ throw 'Research runtime import checkpoint failed.' }
  Write-Step 'PREFLIGHT' 'Compiling and importing the staged backend in isolated test mode without writing bytecode into the sealed package tree'
  New-Item -ItemType Directory -Path $PreflightHome -Force | Out-Null
  $savedHome=$env:PROJECT_LADDU_HOME; $savedMode=$env:PROJECT_LADDU_DATA_PLANE_MODE; $savedBackend=$env:PROJECT_LADDU_BACKEND_DIR; $savedPycachePrefix=$env:PYTHONPYCACHEPREFIX; $savedDontWriteBytecode=$env:PYTHONDONTWRITEBYTECODE
  try {
    $env:PYTHONPYCACHEPREFIX=(Join-Path $PreflightHome 'pycache'); $env:PYTHONDONTWRITEBYTECODE='1'
    & $venvPython -m compileall -q (Join-Path $PackageRoot 'backend')
    if($LASTEXITCODE -ne 0){ throw 'Python compilation preflight failed.' }
    $env:PROJECT_LADDU_HOME=$PreflightHome; $env:PROJECT_LADDU_DATA_PLANE_MODE='test'; $env:PROJECT_LADDU_BACKEND_DIR=(Join-Path $PackageRoot 'backend')
    & $venvPython -c "import os,sys; sys.path.insert(0,os.environ['PROJECT_LADDU_BACKEND_DIR']); import main; print(main.APP_VERSION)"
    if($LASTEXITCODE -ne 0){ throw 'Backend import preflight failed.' }
  } finally { $env:PROJECT_LADDU_HOME=$savedHome; $env:PROJECT_LADDU_DATA_PLANE_MODE=$savedMode; $env:PROJECT_LADDU_BACKEND_DIR=$savedBackend; $env:PYTHONPYCACHEPREFIX=$savedPycachePrefix; $env:PYTHONDONTWRITEBYTECODE=$savedDontWriteBytecode }
  Set-InstallTransactionPhase -Phase 'ENVIRONMENT_PROOF' -Detail 'pinned isolated backend and research runtimes compiled and imported'
  $hasPriorDataPlaneAuthority = Test-Path -LiteralPath (Join-Path $InstallDir 'secure\data-plane.env.ps1') -PathType Leaf
  $hasPreservedLocalState = Test-PreservedLocalStatePresence -PythonExe $basePython
  Write-Step 'TARGET' ("Application-version-independent install: prior_data_plane={0} preserved_local_state={1}" -f $hasPriorDataPlaneAuthority,$hasPreservedLocalState)
  Write-Step 'DATA-PLANE' 'Preparing PostgreSQL and QuestDB infrastructure with zero schema or role mutation'
  $dataPlaneProvisioner = Join-Path $PackageRoot 'installer\data_plane.ps1'
  if(!(Test-Path -LiteralPath $dataPlaneProvisioner -PathType Leaf)){ throw 'Internal data-plane provisioner is missing.' }
  & $dataPlaneProvisioner -InstallDir $InstallDir -Mode Auto -PythonExe $venvPython -Phase Prepare -TransactionId $RunId
  if($LASTEXITCODE -ne 0){ throw 'Production data-plane non-mutating preparation failed.' }
  $prepareProof = Join-Path $InstallDir 'logs\data-plane-prepare.json'
  if(!(Test-Path -LiteralPath $prepareProof -PathType Leaf)){ throw 'Non-mutating data-plane preparation proof was not produced.' }
  Copy-Item -LiteralPath $prepareProof -Destination (Join-Path $EvidenceDir 'data-plane-prepare.json') -Force
  if($hasPriorDataPlaneAuthority){
    Write-Step 'LINEAGE' 'Proving the preserved canonical PostgreSQL migration ledger by immutable migration hash; application version is irrelevant'
    $ParentMigrationLineage = Assert-ParentMigrationLineage -PythonExe $venvPython -OutputPath $ParentMigrationLineagePath
    Set-InstallTransactionPhase -Phase 'DATA_AUTHORITY_PROOF' -Detail 'existing PostgreSQL migration lineage and QuestDB/Parquet authority admission passed'
    Write-Step 'RETENTION' 'Capturing direct PostgreSQL, QuestDB and Parquet authority evidence before runtime stop'
    $AuthorityBeforeSchema = Invoke-AuthorityRetentionEvidence -PythonExe $venvPython -OutputPath $AuthorityBeforeSchemaPath -Label 'BEFORE_SCHEMA'
  } else {
    Set-InstallTransactionPhase -Phase 'DATA_AUTHORITY_PROOF' -Detail 'clean-target data-plane infrastructure admission passed without schema mutation'
    Write-Step 'RETENTION' 'Recording clean-install authority sentinel before first schema creation'
    $AuthorityBeforeSchema = Invoke-AuthorityRetentionEvidence -PythonExe $venvPython -OutputPath $AuthorityBeforeSchemaPath -Label 'CLEAN_INSTALL' -CleanInstall
  }
  Set-InstallTransactionPhase -Phase 'RETENTION_SNAPSHOT' -Detail 'pre-schema direct authority high-water captured'
  Backup-ResearchTasks
  Write-Step 'QUIESCE' 'Disabling and stopping governed research tasks before durable-state preservation'
  $ResearchTasksQuiesced = $true
  $ResearchTaskQuiescence = Quiesce-ResearchTasks -OutputPath $ResearchTaskQuiescePath -DeadlineSec 45
  Write-Step 'QUIESCE' 'Governed research tasks are quiescent; no scheduled writer can retrigger during preservation proof'
  Write-Step 'STAGE' 'Preparing immutable runtime payload and service binary'
  New-Item -ItemType Directory -Path $StageDir -Force | Out-Null
  foreach($folder in $PayloadFolders){ Copy-Item -LiteralPath (Join-Path $PackageRoot $folder) -Destination (Join-Path $StageDir $folder) -Recurse -Force }
  foreach($file in $OperatorFiles){ $source=Join-Path $PackageRoot $file; if(Test-Path -LiteralPath $source -PathType Leaf){ Copy-Item -LiteralPath $source -Destination (Join-Path $StageDir $file) -Force } }
  $serviceExe = Join-Path $StageDir 'service\ProjectLadduService.exe'
  $csc = @("$env:WINDIR\Microsoft.NET\Framework64\v4.0.30319\csc.exe","$env:WINDIR\Microsoft.NET\Framework\v4.0.30319\csc.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
  if(!$csc){ throw 'Windows C# compiler is required to build the service owner.' }
  & $csc /target:exe /out:$serviceExe (Join-Path $StageDir 'service\ProjectLadduService.cs') /reference:System.ServiceProcess.dll | Out-Host
  if($LASTEXITCODE -ne 0 -or !(Test-Path -LiteralPath $serviceExe)){ throw 'Service wrapper compilation failed.' }
  Set-InstallTransactionPhase -Phase 'STAGED' -Detail 'immutable payload and service owner staged'
  Write-Step 'TRANSACTION' 'Stopping any existing Project Laddu runtime only after package, data-authority, retention and staging gates pass'
  Stop-RuntimeOwners
  $RuntimeStopped = $true
  Set-InstallTransactionPhase -Phase 'RUNTIME_QUIESCED' -Detail 'all prior Project Laddu runtime owners stopped'
  Write-Step 'PRESERVE' 'Hashing durable pre-existing local data and secure files after runtime quiescence; ephemeral runtime lock files and rebuildable compatibility projections are excluded'
  $ProtectedStateBefore = New-PreservedLocalStateSnapshot -PythonExe $basePython -OutputPath $ProtectedStateBeforePath
  if($hasPriorDataPlaneAuthority){
    Write-Step 'RETENTION' 'Capturing quiescent authority high-water immediately before forward schema work'
    $AuthorityBeforeSwap = Invoke-AuthorityRetentionEvidence -PythonExe $venvPython -OutputPath $AuthorityBeforeSwapPath -Label 'BEFORE_SWAP' -CompareBefore $AuthorityBeforeSchemaPath
  } else {
    $AuthorityBeforeSwap = $AuthorityBeforeSchema
  }
  Set-InstallTransactionPhase -Phase 'DURABLE_STATE_PROOF' -Detail 'typed local-state manifest and quiescent canonical authority high-water captured'
  Write-Step 'DATA-PLANE' 'Applying schema only after runtime stop; preserved canonical data planes require immutable historical migration hashes'
  if($hasPriorDataPlaneAuthority){
    & $dataPlaneProvisioner -InstallDir $InstallDir -Mode Auto -PythonExe $venvPython -Phase Apply -InPlaceUpgrade -TransactionId $RunId
  } else {
    & $dataPlaneProvisioner -InstallDir $InstallDir -Mode Auto -PythonExe $venvPython -Phase Apply -TransactionId $RunId
  }
  if($LASTEXITCODE -ne 0){ throw 'Production data-plane schema application failed.' }
  $SchemaApplied = $true
  $dataPlaneEnv = Join-Path $InstallDir 'secure\data-plane.env.ps1'
  if(!(Test-Path -LiteralPath $dataPlaneEnv -PathType Leaf)){ throw "Production environment was not created: $dataPlaneEnv" }
  $envText = Get-Content -LiteralPath $dataPlaneEnv -Raw
  foreach($required in @('PROJECT_LADDU_DATA_PLANE_MODE','PROJECT_LADDU_OPERATIONAL_DSN','PROJECT_LADDU_GOVERNANCE_DSN','PROJECT_LADDU_QUESTDB_HTTP_URL')){
    if($envText -notmatch [Regex]::Escape($required)){ throw "Production environment is incomplete: $required is missing." }
  }
  $dataPlaneProof = Join-Path $InstallDir 'logs\data-plane-provision.json'
  if(!(Test-Path -LiteralPath $dataPlaneProof -PathType Leaf)){ throw 'Production data-plane proof was not produced.' }
  Copy-Item -LiteralPath $dataPlaneProof -Destination (Join-Path $EvidenceDir 'data-plane-provision.json') -Force
  $retiredEvidence = Join-Path $InstallDir 'logs\retired-runtime-evidence.jsonl'
  if(Test-Path -LiteralPath $retiredEvidence -PathType Leaf){ Copy-Item -LiteralPath $retiredEvidence -Destination (Join-Path $EvidenceDir 'retired-runtime-evidence.jsonl') -Force }
  Set-InstallTransactionPhase -Phase 'SCHEMA_APPLIED' -Detail 'forward-only schema application completed under immutable migration identity'
  if($hasPriorDataPlaneAuthority){
    Write-Step 'RETENTION' 'Proving schema application preserved every quiescent retained authority before payload swap'
    $AuthorityAfterSchema = Invoke-AuthorityRetentionEvidence -PythonExe $venvPython -OutputPath $AuthorityAfterSchemaPath -Label 'AFTER_SCHEMA' -CompareBefore $AuthorityBeforeSwapPath
  } else {
    $AuthorityAfterSchema = Invoke-AuthorityRetentionEvidence -PythonExe $venvPython -OutputPath $AuthorityAfterSchemaPath -Label 'AFTER_SCHEMA'
  }
  Set-InstallTransactionPhase -Phase 'RETENTION_PROOF' -Detail 'post-schema canonical authority retention passed before payload activation'
  . $dataPlaneEnv
  Write-Step 'REFERENCE-AUTHORITY' 'Reconciling governed NSE/BSE cash/index catalogue before search or scanner can become ready'
  $referenceBootstrapTool = Join-Path $StageDir 'backend\tools\bootstrap_reference_authority.py'
  if(!(Test-Path -LiteralPath $referenceBootstrapTool -PathType Leaf)){ throw 'Reference-authority bootstrap tool is missing from staged payload.' }
  & $venvPython $referenceBootstrapTool --install-dir $InstallDir --report $ReferenceAuthorityBootstrapPath
  if($LASTEXITCODE -ne 0){ throw "Reference-authority bootstrap failed. Evidence: $ReferenceAuthorityBootstrapPath" }
  try { $referenceAuthority = Get-Content -LiteralPath $ReferenceAuthorityBootstrapPath -Raw | ConvertFrom-Json }
  catch { throw "Reference-authority bootstrap evidence is unreadable: $($_.Exception.Message)" }
  $referenceProof = $referenceAuthority.target.proof
  if($referenceAuthority.ok -ne $true -or $null -eq $referenceProof -or
     [int64]$referenceProof.active_total -le 0 -or [int64]$referenceProof.nse_equities -le 0 -or
     [int64]$referenceProof.bse_only_equities -le 0 -or [int64]$referenceProof.indices -le 0 -or
     [int64]$referenceProof.derivatives -ne 0 -or [int64]$referenceProof.out_of_policy_rows -ne 0){
    throw "Reference-authority readiness failed closed. Evidence: $ReferenceAuthorityBootstrapPath"
  }
  Write-Step 'REFERENCE-AUTHORITY' ("Ready active={0} nse={1} bse_only={2} indices={3}" -f $referenceProof.active_total,$referenceProof.nse_equities,$referenceProof.bse_only_equities,$referenceProof.indices)
  Write-Step 'RESEARCH-GOVERNANCE' 'Migrating retired SQLite research evidence into PostgreSQL outside runtime startup and sealing the completion checkpoint'
  $researchMigration = Invoke-LegacyResearchGovernanceMigration -PythonExe $venvPython -StageDir $StageDir -OutputPath $ResearchGovernanceMigrationPath -AuthorityAfterSchemaPath $AuthorityAfterSchemaPath -AuthorityAfterResearchMigrationPath $AuthorityAfterResearchMigrationPath
  Set-InstallTransactionPhase -Phase 'RESEARCH_GOVERNANCE_MIGRATED' -Detail 'legacy research evidence migrated/quarantined and immutable completion checkpoint verified before service startup'
  Write-Step 'TRANSACTION' 'Backing up and swapping runtime payload only'
  New-Item -ItemType Directory -Path $BackupDir -Force | Out-Null
  foreach($folder in $PayloadFolders){
    $installed=Join-Path $InstallDir $folder
    if(Test-Path -LiteralPath $installed -PathType Container){ Move-Item -LiteralPath $installed -Destination (Join-Path $BackupDir $folder) -Force }
    Move-Item -LiteralPath (Join-Path $StageDir $folder) -Destination $installed -Force
  }
  foreach($file in $OperatorFiles){
    $installed=Join-Path $InstallDir $file
    if(Test-Path -LiteralPath $installed -PathType Leaf){ Move-Item -LiteralPath $installed -Destination (Join-Path $BackupDir $file) -Force }
    $staged=Join-Path $StageDir $file
    if(Test-Path -LiteralPath $staged -PathType Leaf){ Move-Item -LiteralPath $staged -Destination $installed -Force }
  }
  $PayloadChanged = $true
  $installedFrontendHash = Get-TreeHash (Join-Path $InstallDir 'frontend')
  if($installedFrontendHash -ne $packageFrontendHash){ throw "Installed frontend differs from package: package=$packageFrontendHash installed=$installedFrontendHash" }
  Write-Step 'FRONTEND' "Installed frontend tree hash verified=$installedFrontendHash"
  Write-Step 'RUNTIME' 'Publishing release-isolated backend runtime pointer inside the payload transaction'
  Set-Content -LiteralPath $BackendPointerPath -Value $venvPython -Encoding ASCII
  Set-Content -LiteralPath $PortPointerPath -Value ([string]$Port) -Encoding ASCII
  $RuntimePointerChanged = $true
  $installedServiceExe = Join-Path $InstallDir 'service\ProjectLadduService.exe'
  $existingService = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
  if(!$existingService){
    Write-Step 'OWNER' 'Registering the one supported Windows service owner'
    New-Service -Name $ServiceName -BinaryPathName "`"$installedServiceExe`"" -DisplayName 'Project Laddu' -Description 'Project Laddu production runtime owner' -StartupType Automatic | Out-Null
    $ServiceCreated = $true
    & sc.exe config $ServiceName start= delayed-auto | Out-Null
  } else {
    & sc.exe config $ServiceName binPath= "`"$installedServiceExe`"" start= delayed-auto | Out-Null
  }
  $backendHash = Get-TreeHash (Join-Path $InstallDir 'backend')
  $versionMatch = Select-String -LiteralPath (Join-Path $InstallDir 'backend\config.py') -Pattern 'APP_VERSION\s*=\s*["'']([^"'']+)' | Select-Object -First 1
  $version = if($versionMatch){ [string]$versionMatch.Matches[0].Groups[1].Value } else { '' }
  Write-Json $ManifestPath ([ordered]@{ source_version=$version; parent_version=$CandidateRelease.parent.version; release_identity_hash=(Get-FileHash -LiteralPath (Join-Path $InstallDir 'RELEASE_IDENTITY.json') -Algorithm SHA256).Hash.ToLowerInvariant(); release_attestation_hash=(Get-FileHash -LiteralPath (Join-Path $InstallDir 'RELEASE_ATTESTATION.json') -Algorithm SHA256).Hash.ToLowerInvariant(); package_manifest_hash=(Get-FileHash -LiteralPath (Join-Path $InstallDir 'validation\package_manifest.sha256') -Algorithm SHA256).Hash.ToLowerInvariant(); backend_hash=$backendHash; frontend_hash=$installedFrontendHash; frontend_owner=('standalone-' + $version); frontend_identity_endpoint='/api/frontend-identity'; deployed_at=(Get-Date).ToString('o'); installer='argument-safe-module-closed-typed-transactional-install-v11'; transaction_journal=$TransactionJournalPath; install_mode='CLEAN_APPLICATION_REPLACE_PRESERVE_STATE'; installed_version_prerequisite='NONE'; runtime_owner='Windows service: ProjectLaddu'; data_plane_schema_ensured=$true; backend_runtime=$venvPython; backend_requirements_hash=([string]$backendEnv.RequirementsHash); research_runtime=$researchPython; research_requirements_hash=([string]$researchEnv.RequirementsHash); research_runtime_installed=$true; destructive_data_plane_change=$false })
  Set-InstallTransactionPhase -Phase 'PAYLOAD_ACTIVATED' -Detail 'payload hashes, runtime pointers, service owner and deploy manifest activated'
  Write-Step 'PRESERVE' 'Proving clean application replacement did not alter pre-existing local data or secure state'
  $ProtectedStateProof = Assert-PreservedLocalState -PythonExe $basePython -BeforePath $ProtectedStateBeforePath -AfterPath $ProtectedStateAfterPath -ProofPath $ProtectedStateProofPath
  Set-InstallTransactionPhase -Phase 'SECURE_DATA_PRESERVED' -Detail 'typed before/after preservation proof passed for all pre-existing data and secure files'
  $null = Build-CandleFileCatalog -InstallDir $InstallDir -BackendPython $venvPython -EvidenceDir $EvidenceDir
  Write-Step 'RESEARCH' 'Publishing the isolated research runtime authority before service startup'
  [Environment]::SetEnvironmentVariable('PROJECT_LADDU_RESEARCH_PYTHON',$researchPython,'Machine')
  $env:PROJECT_LADDU_RESEARCH_PYTHON = $researchPython
  Set-Content -LiteralPath $ResearchPointerPath -Value $researchPython -Encoding ASCII
  $ResearchTasksChanged = $true
  Write-Step 'START' 'Starting the installed service with a bounded readiness deadline'
  Start-Service -Name $ServiceName
  Set-InstallTransactionPhase -Phase 'SERVICE_STARTED' -Detail 'Windows service start command succeeded'
  $ready = Wait-Ready
  if($null -eq $ready){ Show-StartupEvidence; throw "Backend did not reach /api/ready within $StartupDeadlineSec seconds." }
  if($version -and $ready.version -and [string]$ready.version -ne $version){ throw "Stale runtime detected: running=$($ready.version) deployed=$version" }
  $RuntimeStopped = $false
  Set-InstallTransactionPhase -Phase 'BACKEND_READY' -Detail 'exact candidate backend reached bounded readiness'
  Write-Step 'FRONTEND' 'Attesting runtime, frontend manifest and browser entrypoint as one exact release'
  $frontendIdentity = Get-ApiJson '/api/frontend-identity'
  $expectedFrontendOwner = 'standalone-' + $version
  $frontendReleaseIdentityPath = Join-Path $InstallDir 'frontend\release-identity.json'
  if(!(Test-Path -LiteralPath $frontendReleaseIdentityPath -PathType Leaf)){ throw 'Installed frontend release-identity.json is missing.' }
  $expectedFrontendIdentity = Get-Content -LiteralPath $frontendReleaseIdentityPath -Raw | ConvertFrom-Json
  $expectedBuildMarker = [string]$expectedFrontendIdentity.build_marker
  if([string]::IsNullOrWhiteSpace($expectedBuildMarker)){ throw 'Installed frontend build marker is missing.' }
  if($frontendIdentity.ok -ne $true){ throw "Frontend identity rejected: $(@($frontendIdentity.mismatches) -join ',')" }
  if([string]$frontendIdentity.version -ne $version){ throw "Frontend runtime identity mismatch: frontend=$($frontendIdentity.version) deployed=$version" }
  if([string]$frontendIdentity.manifest_version -ne $version){ throw "Frontend manifest version mismatch: manifest=$($frontendIdentity.manifest_version) deployed=$version" }
  if([string]$frontendIdentity.frontend_owner -ne $expectedFrontendOwner){ throw "Frontend owner mismatch: owner=$($frontendIdentity.frontend_owner) expected=$expectedFrontendOwner" }
  if([string]$frontendIdentity.build_marker -ne $expectedBuildMarker){ throw "Frontend build marker mismatch: served=$($frontendIdentity.build_marker) expected=$expectedBuildMarker" }
  if(@($frontendIdentity.mismatches).Count -ne 0){ throw "Frontend asset mismatch: $(@($frontendIdentity.mismatches) -join ',')" }
  $entryHtml = Get-HttpText '/index.html'
  if($entryHtml -notmatch ('data-build-version="' + [Regex]::Escape($version) + '"')){ throw "Served index.html does not declare deployed version $version" }
  if($entryHtml -notmatch ('data-frontend-owner="' + [Regex]::Escape($expectedFrontendOwner) + '"')){ throw "Served index.html does not declare frontend owner $expectedFrontendOwner" }
  if($entryHtml -notmatch ('data-build-marker="' + [Regex]::Escape($expectedBuildMarker) + '"')){ throw "Served index.html does not declare exact build marker $expectedBuildMarker" }
  $assetVersion = [Regex]::Escape($version.TrimStart('v'))
  if($entryHtml -notmatch ('app\.css\?v=' + $assetVersion) -or $entryHtml -notmatch ('app\.js\?v=' + $assetVersion)){ throw "Served index.html asset versions do not match $version" }
  Write-Step 'FRONTEND' "Runtime/frontend served identity verified version=$version owner=$expectedFrontendOwner marker=$expectedBuildMarker"
  Write-Step 'BROWSER' 'Browser/customer acceptance is post-install evidence and is not an atomic installation commit gate'
  Set-InstallTransactionPhase -Phase 'FRONTEND_IDENTITY' -Detail 'backend readiness, frontend manifest, served index bytes, asset identity and exact build marker agree; browser acceptance remains post-install'
  Write-Step 'RETENTION' 'Proving direct authorities survived activation independently of application HTTP'
  $AuthorityAfterActivation = Invoke-AuthorityRetentionEvidence -PythonExe $venvPython -OutputPath $AuthorityAfterActivationPath -Label 'AFTER_ACTIVATION' -CompareBefore $AuthorityAfterResearchMigrationPath
  Write-Step 'RETENTION' 'Waiting for the running application retention projection to become readable after startup'
  $ResearchProjectionProof = Get-ResearchRetentionProjectionProof -OverallDeadlineSec 120 -AttemptTimeoutMs 30000
  $ResearchPreservationAfter = $ResearchProjectionProof.preservation
  $ResearchContinuityAfter = $ResearchProjectionProof.continuity
  Write-Json $ResearchManifestAfterPath $ResearchPreservationAfter
  Write-Json $ResearchAfterPath $ResearchContinuityAfter
  Write-Step 'RETENTION' "Application retention projection verified state=$($ResearchContinuityAfter.state) hash=$($ResearchContinuityAfter.content_hash)"
  $oldTask = Get-ScheduledTask -TaskName $ServiceName -ErrorAction SilentlyContinue
  if($oldTask){
    Write-Step 'CLEANUP' 'Removing the superseded scheduled-task runtime owner'
    Unregister-ScheduledTask -TaskName $oldTask.TaskName -TaskPath $oldTask.TaskPath -Confirm:$false -ErrorAction SilentlyContinue
  }
  $nsePlanDir = Join-Path $InstallDir 'data\config'
  $nsePlanPath = Join-Path $nsePlanDir 'nse_official_sources.json'
  $nsePlanDefault = Join-Path $InstallDir 'backend\resources\nse_official_sources.example.json'
  $nsePlanMerge = Join-Path $InstallDir 'backend\tools\merge_nse_official_source_plan.py'
  New-Item -ItemType Directory -Path $nsePlanDir -Force | Out-Null
  Write-Step 'NSE-DATA' 'Merging governed active NSE transports into retained source configuration'
  & $researchPython $nsePlanMerge --default-plan $nsePlanDefault --target-plan $nsePlanPath --output (Join-Path $EvidenceDir 'nse_source_plan.json')
  if($LASTEXITCODE -ne 0){ throw 'Governed NSE source-plan merge failed.' }
  Write-Step 'NSE-DATA' "Active NSE archive transports configured at $nsePlanPath; operator overrides and prior valid authority are preserved."
  Write-Step 'RESEARCH' 'Registering governed research lifecycle tasks and proving the isolated authority'
  & (Join-Path $InstallDir 'installer\register_research_tasks.ps1') -InstallDir $InstallDir -Port $Port
  if($LASTEXITCODE -ne 0){ throw 'Research lifecycle task registration failed.' }
  . $dataPlaneEnv
  # The verifier writes into installer evidence first.  The prior implementation
  # atomically replaced runtime\research_runtime.json while the live service was
  # reading it; Windows may deny that rename/delete share even to an elevated
  # installer.  Publication is now a separate bounded transaction performed with
  # the service quiesced, followed by an exact hash and live API proof.
  $researchManifestCandidate = Join-Path $EvidenceDir 'research_runtime.candidate.json'
  $researchManifest = Join-Path $InstallDir 'runtime\research_runtime.json'
  & $researchPython (Join-Path $InstallDir 'validation\verify_authoritative_quant_research_lifecycle.py') --install-dir $InstallDir --output $researchManifestCandidate --require-tasks
  if($LASTEXITCODE -ne 0){ throw 'Research runtime authority verification failed.' }
  $candidateResearch = Get-Content -LiteralPath $researchManifestCandidate -Raw | ConvertFrom-Json
  if($candidateResearch.ok -ne $true -or [string]$candidateResearch.state -ne 'READY'){ throw 'Research runtime candidate manifest did not prove READY.' }

  Write-Step 'RESEARCH' 'Publishing verified runtime metadata under a bounded Windows-safe quiesced swap'
  Stop-Service -Name $ServiceName -Force -ErrorAction Stop
  $serviceForMetadata = Get-Service -Name $ServiceName -ErrorAction Stop
  try { $serviceForMetadata.WaitForStatus('Stopped',[TimeSpan]::FromSeconds(20)) } catch {}
  $serviceForMetadata.Refresh()
  if($serviceForMetadata.Status -ne 'Stopped'){ throw 'Project Laddu service did not quiesce for runtime-metadata publication.' }
  $RuntimeStopped = $true
  $metadataPublish = Publish-RuntimeMetadataFile -Source $researchManifestCandidate -Destination $researchManifest -Attempts 8
  Write-Step 'RESEARCH' ("Runtime metadata published sha256={0} attempts={1}" -f $metadataPublish.sha256,$metadataPublish.attempts)
  Start-Service -Name $ServiceName -ErrorAction Stop
  $readyAfterResearchPublish = Wait-Ready
  if($null -eq $readyAfterResearchPublish){ Show-StartupEvidence; throw 'Backend did not return READY after research runtime metadata publication.' }
  if($version -and $readyAfterResearchPublish.version -and [string]$readyAfterResearchPublish.version -ne $version){ throw "Stale runtime after research metadata publish: running=$($readyAfterResearchPublish.version) deployed=$version" }
  $RuntimeStopped = $false
  $researchPlaneProof = Wait-ResearchPlaneReady -OverallDeadlineSec 120 -SleepSec 3
  Write-Json (Join-Path $EvidenceDir 'research-plane-final.json') $researchPlaneProof
  $finalFrontendIdentity = Get-ApiJson '/api/frontend-identity'
  if($finalFrontendIdentity.ok -ne $true -or [string]$finalFrontendIdentity.version -ne $version -or @($finalFrontendIdentity.mismatches).Count -ne 0){ throw 'Final frontend identity proof failed after research metadata publication restart.' }
  Copy-Item -LiteralPath $researchManifest -Destination (Join-Path $EvidenceDir 'research_runtime.json') -Force
  Set-InstallTransactionPhase -Phase 'OPERATIONAL_PROOF' -Detail 'direct authority retention, application projections, Windows-safe research metadata publication, final backend/frontend identity and research lifecycle proof passed'
  Write-Json $PrecommitResultPath ([ordered]@{ ok=$true; transaction_state='COMMIT'; transaction_journal=$TransactionJournalPath; version=$ready.version; backend_hash=$backendHash; frontend_hash=$installedFrontendHash; frontend_identity=$frontendIdentity; installed_at=(Get-Date).ToString('o'); install_dir=$InstallDir; evidence_dir=$EvidenceDir; data_plane_schema_ensured=$true; research_runtime=$researchPython; research_tasks=$ResearchTaskNames; authority_retention_before_schema=$AuthorityBeforeSchemaPath; authority_retention_after_schema=$AuthorityAfterSchemaPath; authority_retention_after_research_migration=$AuthorityAfterResearchMigrationPath; research_governance_migration=$ResearchGovernanceMigrationPath; authority_retention_before_swap=$AuthorityBeforeSwapPath; authority_retention_after_activation=$AuthorityAfterActivationPath; parent_migration_lineage=if($hasPriorDataPlaneAuthority){$ParentMigrationLineagePath}else{$null}; installed_version_prerequisite='NONE'; preserved_local_state_proof=$ProtectedStateProofPath; preserved_local_state=$ProtectedStateProof; research_continuity_after=$ResearchAfterPath; research_retention=$ResearchContinuityAfter; research_preservation_manifest_after=$ResearchManifestAfterPath; research_preservation=$ResearchPreservationAfter; destructive_data_plane_change=$false; runtime_owner='Windows service: ProjectLaddu'; browser_acceptance='POST_INSTALL_REQUIRED_NOT_INSTALL_COMMIT_GATE' })
  Move-Item -LiteralPath $PrecommitResultPath -Destination $ResultPath -Force
  Complete-InstallTransaction
  $workspaceCleanup = Complete-InstallerWorkspaceCleanup -TransactionDir $TransactionDir -EvidenceDir $EvidenceDir -State 'COMMITTED'; if($workspaceCleanup.Error){ throw ('Transaction workspace cleanup failed after commit: ' + $workspaceCleanup.Error) }
  Write-Host ''
  Write-Host "[OK] Project Laddu is ready at http://127.0.0.1:$Port" -ForegroundColor Green
  Write-Host "Evidence: $EvidenceDir" -ForegroundColor Green
  exit 0
}
catch {
  $failure = $_; $rollbackErrors = New-Object System.Collections.Generic.List[string]
  $TargetMutationObserved = $RuntimeStopped -or $PayloadChanged -or $RuntimePointerChanged -or $ResearchTasksChanged -or $ResearchTasksQuiesced -or $SchemaApplied
  Write-Host ''; Write-Host ("[FAILED] " + $failure.Exception.Message) -ForegroundColor Red
  foreach($candidateResult in @($PrecommitResultPath,$ResultPath)){
    if(Test-Path -LiteralPath $candidateResult -PathType Leaf){ Remove-Item -LiteralPath $candidateResult -Force -ErrorAction SilentlyContinue }
  }
  if($ResearchTasksQuiesced -or $ResearchTasksChanged){
    try {
      Restore-ResearchTasks
      $ResearchTasksQuiesced = $false
    } catch { $rollbackErrors.Add('research task-state rollback failed: ' + $_.Exception.Message) }
  }
  if($ResearchTasksChanged){
    try {
      [Environment]::SetEnvironmentVariable('PROJECT_LADDU_RESEARCH_PYTHON',$ResearchPythonBefore,'Machine')
      $env:PROJECT_LADDU_RESEARCH_PYTHON = $ResearchPythonBefore
    } catch { $rollbackErrors.Add('research lifecycle environment rollback failed: ' + $_.Exception.Message) }
    try {
      if($ResearchPointerExisted){ Set-Content -LiteralPath $ResearchPointerPath -Value $ResearchPointerBefore -Encoding ASCII }
      elseif(Test-Path -LiteralPath $ResearchPointerPath){ Remove-Item -LiteralPath $ResearchPointerPath -Force }
    } catch { $rollbackErrors.Add('research pointer rollback failed: ' + $_.Exception.Message) }
  }
  if($RuntimePointerChanged){
    try {
      if($BackendPointerExisted){ Set-Content -LiteralPath $BackendPointerPath -Value $BackendPointerBefore -Encoding ASCII }
      elseif(Test-Path -LiteralPath $BackendPointerPath){ Remove-Item -LiteralPath $BackendPointerPath -Force }
      if($PortPointerExisted){ Set-Content -LiteralPath $PortPointerPath -Value $PortPointerBefore -Encoding ASCII }
      elseif(Test-Path -LiteralPath $PortPointerPath){ Remove-Item -LiteralPath $PortPointerPath -Force }
      $RuntimePointerChanged = $false
    } catch { $rollbackErrors.Add('backend runtime pointer rollback failed: ' + $_.Exception.Message) }
  }
  if($PayloadChanged){
    try {
      Write-Step 'ROLLBACK' 'Restoring the prior runtime payload; forward-only data schemas and data remain preserved'
      Restore-Payload
      if($ServiceCreated){ & sc.exe delete $ServiceName | Out-Null }
      Restore-ParentRuntimeOwner -ExpectedVersion $ParentVersionExpected | Out-Null
    } catch { $rollbackErrors.Add('runtime payload rollback failed: ' + $_.Exception.Message) }
  }
  if($RuntimeStopped -and -not $PayloadChanged){
    try {
      Restore-ParentRuntimeOwner -ExpectedVersion $ParentVersionExpected | Out-Null
      $RuntimeStopped = $false
    } catch { $rollbackErrors.Add('prior runtime restart failed: ' + $_.Exception.Message) }
  }
  if($BackendRuntimeCreated -and -not $PayloadChanged){ try { Remove-Item -LiteralPath $backendRuntimeDir -Recurse -Force -ErrorAction SilentlyContinue } catch {} }
  if($ResearchRuntimeCreated -and -not $ResearchTasksChanged){ try { Remove-Item -LiteralPath $researchRuntimeDir -Recurse -Force -ErrorAction SilentlyContinue } catch {} }
  $rollbackState = if($rollbackErrors.Count){ 'PARTIAL' } elseif($TargetMutationObserved){ if($OwnerBefore.ServiceRunning -or $OwnerBefore.TaskRunning){'PRIOR_RUNTIME_RESTORED'}else{'CLEAN_TARGET_RESTORED'} } else { 'NOT_REQUIRED' }
  if($TransactionStarted -and -not $TransactionCommitted){
    try { Stop-InstallTransaction -Failure $failure.Exception.Message -RollbackState $rollbackState }
    catch { $rollbackErrors.Add('transaction failure journal update failed: ' + $_.Exception.Message); $rollbackState = 'PARTIAL' }
  }
  $rollbackError = if($rollbackErrors.Count){ $rollbackErrors -join '; ' } else { $null }
  $workspaceCleanup = Complete-InstallerWorkspaceCleanup -TransactionDir $TransactionDir -EvidenceDir $EvidenceDir -State $rollbackState; if($workspaceCleanup.Error){ if($null -eq $rollbackError){$rollbackError='transaction workspace cleanup failed: ' + $workspaceCleanup.Error}else{$rollbackError += '; transaction workspace cleanup failed: ' + $workspaceCleanup.Error} }
  Write-Json $FailurePath ([ordered]@{ ok=$false; failed_at=(Get-Date).ToString('o'); message=$failure.Exception.Message; location=$failure.ScriptStackTrace; failed_transaction=$TransactionJournalPath; rollback=$rollbackState; rollback_error=$rollbackError; install_dir=$InstallDir; package_root=$PackageRoot; data_plane_schema_ensured=$SchemaApplied; destructive_data_plane_change=$false; evidence_dir=$EvidenceDir; parent_runtime_restore=if(Test-Path -LiteralPath $ParentRuntimeRestorePath -PathType Leaf){$ParentRuntimeRestorePath}else{$null} })
  Write-Host "Evidence: $EvidenceDir" -ForegroundColor Yellow
  if($rollbackError){ Write-Host "[ROLLBACK PARTIAL] $rollbackError" -ForegroundColor Red }
  exit 1
}
finally { try { Stop-Transcript | Out-Null } catch {} }
