# Fail-closed restoration proof for the exact pre-install runtime, independent of source lineage.
function Restore-ParentRuntimeOwner([string]$ExpectedVersion) {
  $wasRunning = ($OwnerBefore.ServiceRunning -or $OwnerBefore.TaskRunning)
  if(!$wasRunning){
    $proof = [ordered]@{ ok=$true; state='NOT_REQUIRED'; expected_version=$ExpectedVersion; restored_at=(Get-Date).ToString('o') }
    Write-Json $ParentRuntimeRestorePath $proof
    return $proof
  }
  if($OwnerBefore.ServiceExists -and $OwnerBefore.ServiceRunning){ Start-Service -Name $ServiceName -ErrorAction SilentlyContinue }
  if($OwnerBefore.TaskExists -and $OwnerBefore.TaskRunning){ Start-ScheduledTask -TaskName $OwnerBefore.TaskName -TaskPath $OwnerBefore.TaskPath -ErrorAction SilentlyContinue }
  $ready = Wait-Ready
  if($null -eq $ready){ throw 'Prior runtime owner did not recover readiness after failed clean application replacement.' }
  $actualVersion = [string]$ready.version
  if(-not [string]::IsNullOrWhiteSpace($ExpectedVersion) -and $actualVersion -ne $ExpectedVersion){
    throw "Prior runtime readiness returned unexpected version '$actualVersion'; expected '$ExpectedVersion'."
  }
  $proof = [ordered]@{ ok=$true; state='PRIOR_RUNTIME_RESTORED'; expected_version=$ExpectedVersion; actual_version=$actualVersion; restored_at=(Get-Date).ToString('o') }
  Write-Json $ParentRuntimeRestorePath $proof
  Write-Host "[OK] Prior runtime restored and ready: $actualVersion" -ForegroundColor Green
  return $proof
}
