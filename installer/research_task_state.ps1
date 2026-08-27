# Transactional preservation/restoration and quiescence of governed Project Laddu research tasks.
$ResearchTaskStatePath = Join-Path $ResearchTaskBackupDir 'research-task-runtime-state.json'

function Backup-ResearchTasks {
  New-Item -ItemType Directory -Path $ResearchTaskBackupDir -Force | Out-Null
  $states = @()
  foreach($name in $ResearchTaskNames){
    $task = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    if($task){
      Export-ScheduledTask -TaskName $name | Set-Content -LiteralPath (Join-Path $ResearchTaskBackupDir ($name + '.xml')) -Encoding Unicode
      $states += [ordered]@{
        name = [string]$name
        existed = $true
        enabled = [bool]$task.Settings.Enabled
        running = ([string]$task.State -eq 'Running')
      }
    } else {
      $states += [ordered]@{ name=[string]$name; existed=$false; enabled=$false; running=$false }
    }
  }
  $states | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $ResearchTaskStatePath -Encoding UTF8
}

function Quiesce-ResearchTasks {
  param(
    [Parameter(Mandatory=$true)][string]$OutputPath,
    [int]$DeadlineSec = 45
  )
  $rows = @()
  foreach($name in $ResearchTaskNames){
    $taskStarted = Get-Date
    $task = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    if(!$task){
      $rows += [ordered]@{ name=$name; present=$false; disabled=$false; stopped=$true; final_state='MISSING'; elapsed_ms=0 }
      continue
    }
    # Disable first so a trigger cannot restart the writer while install preservation is in progress.
    Disable-ScheduledTask -TaskName $task.TaskName -TaskPath $task.TaskPath -ErrorAction Stop | Out-Null
    Stop-ScheduledTask -TaskName $task.TaskName -TaskPath $task.TaskPath -ErrorAction SilentlyContinue
    $stopped = $false
    $state = 'UNKNOWN'
    $attempts = 0
    # IMPORTANT: deadline is PER TASK. A shared deadline across the whole task list can expire
    # before later tasks are inspected, producing an empty/unknown state and a false install failure.
    while(((Get-Date) - $taskStarted).TotalSeconds -lt $DeadlineSec){
      $attempts++
      $current = Get-ScheduledTask -TaskName $task.TaskName -TaskPath $task.TaskPath -ErrorAction SilentlyContinue
      if($null -eq $current){ $state='MISSING'; $stopped=$true; break }
      $state = [string]$current.State
      if([string]::IsNullOrWhiteSpace($state)){ $state='UNKNOWN' }
      if($state -eq 'Disabled' -or $state -eq 'Ready'){ $stopped=$true; break }
      if($state -ne 'Running' -and $state -ne 'Queued' -and $state -ne 'UNKNOWN'){ $stopped=$true; break }
      Start-Sleep -Milliseconds 250
    }
    $elapsedMs = [int][Math]::Round(((Get-Date) - $taskStarted).TotalMilliseconds)
    if(!$stopped){ throw "Research task failed to quiesce before preservation: $name state=$state elapsed_ms=$elapsedMs attempts=$attempts deadline_sec=$DeadlineSec" }
    $rows += [ordered]@{ name=$name; present=$true; disabled=$true; stopped=$true; final_state=$state; elapsed_ms=$elapsedMs; attempts=$attempts }
  }
  $proof = [ordered]@{
    ok=$true
    contract='research-task-quiescence-1.0.1'
    captured_at=(Get-Date).ToString('o')
    deadline_sec=$DeadlineSec
    deadline_scope='PER_TASK'
    tasks=$rows
  }
  $proof | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $OutputPath -Encoding UTF8
  return [pscustomobject]$proof
}

function Restore-ResearchTasks {
  foreach($name in $ResearchTaskNames){ Unregister-ScheduledTask -TaskName $name -Confirm:$false -ErrorAction SilentlyContinue }
  foreach($name in $ResearchTaskNames){
    $path = Join-Path $ResearchTaskBackupDir ($name + '.xml')
    if(Test-Path -LiteralPath $path -PathType Leaf){ Register-ScheduledTask -TaskName $name -Xml (Get-Content -LiteralPath $path -Raw) -Force | Out-Null }
  }
  if(Test-Path -LiteralPath $ResearchTaskStatePath -PathType Leaf){
    $states = @(Get-Content -LiteralPath $ResearchTaskStatePath -Raw | ConvertFrom-Json)
    foreach($state in $states){
      if($state.existed -ne $true){ continue }
      $task = Get-ScheduledTask -TaskName ([string]$state.name) -ErrorAction SilentlyContinue
      if(!$task){ continue }
      if($state.enabled -eq $false){ Disable-ScheduledTask -TaskName $task.TaskName -TaskPath $task.TaskPath -ErrorAction SilentlyContinue | Out-Null }
      if($state.running -eq $true){ Start-ScheduledTask -TaskName $task.TaskName -TaskPath $task.TaskPath -ErrorAction SilentlyContinue }
    }
  }
}
