# PowerShell orchestration adapter for the typed durable transaction authority.
function Invoke-InstallTransactionHelper {
  param(
    [Parameter(Mandatory=$true)][string]$PythonExe,
    [Parameter(Mandatory=$true)][string[]]$Arguments,
    [switch]$AllowFaultInjection
  )
  $helper = Join-Path $PSScriptRoot 'install_transaction.py'
  if(!(Test-Path -LiteralPath $helper -PathType Leaf)){ throw "Installer transaction helper is missing: $helper" }
  $output = & $PythonExe $helper @Arguments 2>&1
  $code = $LASTEXITCODE
  if($AllowFaultInjection -and $code -eq 86){
    throw "Injected installer failure after $env:PROJECT_LADDU_INSTALL_FAIL_AFTER"
  }
  if($code -ne 0){ throw "Installer transaction state failure: $([string]($output -join ' '))" }
  try { return ([string]($output -join "`n") | ConvertFrom-Json) }
  catch { throw "Installer transaction helper returned invalid JSON: $([string]($output -join ' '))" }
}
function Start-InstallTransaction {
  $transactionArgs = @(
    'begin','--journal',$TransactionJournalPath,'--transaction-id',$RunId,
    '--release-version',[string]$CandidateRelease.version,
    '--release-identity-sha256',(Get-FileHash -LiteralPath (Join-Path $PackageRoot 'RELEASE_IDENTITY.json') -Algorithm SHA256).Hash,
    '--package-root',$PackageRoot,'--install-dir',$InstallDir
  )
  if(-not [string]::IsNullOrWhiteSpace([string]$ParentVersionExpected)){
    $transactionArgs += @('--previous-version',[string]$ParentVersionExpected)
  }
  if($OwnerBefore.ServiceRunning -or $OwnerBefore.TaskRunning){ $transactionArgs += '--prior-runtime-running' }
  Invoke-InstallTransactionHelper -PythonExe $basePython -Arguments $transactionArgs | Out-Null
  $script:TransactionStarted = $true
}
function Set-InstallTransactionPhase {
  param([Parameter(Mandatory=$true)][string]$Phase,[string]$Detail='')
  $transactionArgs = @('advance','--journal',$TransactionJournalPath,'--phase',$Phase)
  if(-not [string]::IsNullOrWhiteSpace($Detail)){ $transactionArgs += @('--detail',$Detail) }
  $fault = [string]$env:PROJECT_LADDU_INSTALL_FAIL_AFTER
  if(-not [string]::IsNullOrWhiteSpace($fault)){ $transactionArgs += @('--fault-after',$fault) }
  Invoke-InstallTransactionHelper -PythonExe $basePython -Arguments $transactionArgs -AllowFaultInjection | Out-Null
}
function Complete-InstallTransaction {
  Invoke-InstallTransactionHelper -PythonExe $basePython -Arguments @(
    'commit','--journal',$TransactionJournalPath,'--detail','installed operational proof complete'
  ) | Out-Null
  $script:TransactionCommitted = $true
}
function Stop-InstallTransaction {
  param([Parameter(Mandatory=$true)][string]$Failure,[Parameter(Mandatory=$true)][string]$RollbackState)
  # Native argv serialization in Windows PowerShell 5.1 is not safe for arbitrary
  # exception text containing embedded quotes/newlines. Encode the diagnostic
  # payload so the durable failure journal cannot itself fail during rollback.
  $failureBytes = [Text.Encoding]::UTF8.GetBytes($Failure)
  $failureB64 = [Convert]::ToBase64String($failureBytes)
  Invoke-InstallTransactionHelper -PythonExe $basePython -Arguments @(
    'fail','--journal',$TransactionJournalPath,'--failure-b64',$failureB64,'--rollback-state',$RollbackState
  ) | Out-Null
}

function Initialize-InstallerWorkspace {
  param([Parameter(Mandatory=$true)][string]$TransactionDir,[Parameter(Mandatory=$true)][string]$EvidenceDir)
  New-Item -ItemType Directory -Path $TransactionDir -Force | Out-Null
  Write-Json (Join-Path $EvidenceDir 'transaction-workspace.json') ([ordered]@{ path=$TransactionDir; policy='PROGRAMDATA_RUNTIME_ONLY'; temp_policy='EVIDENCE_AND_LOGS_ONLY'; created_at=(Get-Date).ToString('o') })
}
function Complete-InstallerWorkspaceCleanup {
  param([Parameter(Mandatory=$true)][string]$TransactionDir,[Parameter(Mandatory=$true)][string]$EvidenceDir,[Parameter(Mandatory=$true)][string]$State)
  $retained=$false; $cleanupError=$null
  if(Test-Path -LiteralPath $TransactionDir -PathType Container){
    if($State -eq 'PARTIAL'){ $retained=$true }
    else { try { Remove-Item -LiteralPath $TransactionDir -Recurse -Force -ErrorAction Stop } catch { $retained=$true; $cleanupError=$_.Exception.Message } }
  }
  Write-Json (Join-Path $EvidenceDir 'transaction-workspace-cleanup.json') ([ordered]@{ path=$TransactionDir; retained_for_recovery=$retained; state=$State; completed_at=(Get-Date).ToString('o'); temp_policy='EVIDENCE_AND_LOGS_ONLY' })
  return [pscustomobject]@{ Retained=$retained; Error=$cleanupError }
}
