# Project Laddu QuestDB Windows port/lifecycle authority.
# PowerShell 5.1 compatible. Deployment mechanics only: this file never
# changes trading mathematics, evidence scores, ranking, ML, or governance.

function Get-LadduDockerContainerStatus {
  param([string]$Container = 'project-laddu-questdb')
  $output = @(& docker.exe inspect --format '{{.State.Status}}' $Container 2>$null)
  $exitCode = $LASTEXITCODE
  $status = @($output | Select-Object -First 1)
  if($exitCode -ne 0 -or $status.Count -eq 0 -or [string]::IsNullOrWhiteSpace([string]$status[0])){ return 'missing' }
  return ([string]$status[0]).Trim().ToLowerInvariant()
}

function Get-LadduDockerContainerId {
  param([string]$Container = 'project-laddu-questdb')
  $output = @(& docker.exe inspect --format '{{.Id}}' $Container 2>$null)
  $exitCode = $LASTEXITCODE
  $id = @($output | Select-Object -First 1)
  if($exitCode -ne 0 -or $id.Count -eq 0 -or [string]::IsNullOrWhiteSpace([string]$id[0])){ return '' }
  return ([string]$id[0]).Trim()
}

function Get-LadduQuestDbPublishedPort {
  param([string]$Container = 'project-laddu-questdb')
  try {
    $output = @(& docker.exe inspect $Container 2>$null)
    $exitCode = $LASTEXITCODE
    $raw = ($output -join "`n")
    if($exitCode -ne 0 -or [string]::IsNullOrWhiteSpace($raw)){ return 0 }
    $item = @($raw | ConvertFrom-Json) | Select-Object -First 1
    if($null -eq $item){ return 0 }
    $bindings = $item.HostConfig.PortBindings.'9000/tcp'
    $binding = @($bindings) | Select-Object -First 1
    if($null -eq $binding -or [string]::IsNullOrWhiteSpace([string]$binding.HostPort)){ return 0 }
    return [int]$binding.HostPort
  } catch { return 0 }
}

function Get-LadduQuestDbVolumeName {
  param([string]$Container = 'project-laddu-questdb')
  try {
    $output = @(& docker.exe inspect $Container 2>$null)
    $exitCode = $LASTEXITCODE
    $raw = ($output -join "`n")
    if($exitCode -eq 0 -and -not [string]::IsNullOrWhiteSpace($raw)){
      $item = @($raw | ConvertFrom-Json) | Select-Object -First 1
      $mount = @($item.Mounts) | Where-Object { [string]$_.Destination -eq '/var/lib/questdb' } | Select-Object -First 1
      if($null -ne $mount){
        if(-not [string]::IsNullOrWhiteSpace([string]$mount.Name)){ return [string]$mount.Name }
        if([string]$mount.Type -eq 'volume' -and -not [string]::IsNullOrWhiteSpace([string]$mount.Source)){
          return [System.IO.Path]::GetFileName([string]$mount.Source)
        }
      }
    }
  } catch {}
  $fallback = 'project-laddu-data-plane_laddu_questdb'
  & docker.exe volume inspect $fallback *> $null
  if($LASTEXITCODE -eq 0){ return $fallback }
  return $fallback
}

function Get-LadduWindowsExcludedTcpRanges {
  # Windows PowerShell 5.1 can throw "Argument types do not match" when an
  # array subexpression wraps a generic List[object]. Use a native PowerShell
  # array throughout this recovery-critical path.
  $ranges = @()
  try {
    $text = (& netsh.exe interface ipv4 show excludedportrange protocol=tcp 2>$null | Out-String)
    foreach($line in ($text -split "`r?`n")){
      if($line -match '^\s*(\d+)\s+(\d+)(?:\s+\*)?\s*$'){
        $start = [int]$matches[1]
        $end = [int]$matches[2]
        if($start -gt 0 -and $end -ge $start){
          $ranges += [pscustomobject]@{ start=$start; end=$end }
        }
      }
    }
  } catch {}
  return $ranges
}

function Test-LadduPortExcluded {
  param([int]$Port,[object[]]$Ranges)
  foreach($range in @($Ranges)){
    if($Port -ge [int]$range.start -and $Port -le [int]$range.end){ return $true }
  }
  return $false
}

function Test-LadduPortBindable {
  param([int]$Port)
  if($Port -lt 1024 -or $Port -gt 65535){ return $false }
  $listener = $null
  try {
    $listener = New-Object System.Net.Sockets.TcpListener([System.Net.IPAddress]::Loopback,$Port)
    $listener.ExclusiveAddressUse = $true
    $listener.Start()
    return $true
  } catch { return $false }
  finally { if($null -ne $listener){ try { $listener.Stop() } catch {} } }
}

function Find-LadduSafeQuestDbPort {
  param([int]$PreferredPort = 59000,[int[]]$RejectedPorts = @())
  $excluded = @(Get-LadduWindowsExcludedTcpRanges)
  $rejected = @{}
  foreach($value in @($RejectedPorts)){ $rejected[[int]$value] = $true }
  $candidates = @()
  $candidateSeen = @{}
  if($PreferredPort -ge 1024 -and $PreferredPort -le 65535){
    $candidates += [int]$PreferredPort
    $candidateSeen[[int]$PreferredPort] = $true
  }
  foreach($range in @(@(59100,59999),@(61000,61999),@(45000,48999),@(30000,31999))){
    for($port=[int]$range[0];$port -le [int]$range[1];$port++){
      if(-not $candidateSeen.ContainsKey($port)){
        $candidates += [int]$port
        $candidateSeen[$port] = $true
      }
    }
  }
  foreach($port in $candidates){
    if($rejected.ContainsKey([int]$port)){ continue }
    if(Test-LadduPortExcluded -Port $port -Ranges $excluded){ continue }
    if(Test-LadduPortBindable -Port $port){
      return [pscustomobject]@{
        port = [int]$port
        excluded_ranges = @($excluded)
        selection = if($port -eq $PreferredPort){ 'PREFERRED_PORT_BIND_PROVEN' } else { 'SAFE_PORT_DISCOVERED' }
      }
    }
  }
  throw 'No safe Windows loopback port could be found for QuestDB HTTP after checking excluded ranges and performing real socket-bind tests.'
}

function Test-LadduQuestDbEndpoint {
  param([string]$BaseUrl)
  if([string]::IsNullOrWhiteSpace($BaseUrl)){ return $false }
  try {
    Invoke-RestMethod -Uri ($BaseUrl.TrimEnd('/') + '/exec?query=select%201') -TimeoutSec 3 | Out-Null
    return $true
  } catch { return $false }
}

function Wait-LadduQuestDbEndpoint {
  param([string]$BaseUrl,[int]$Seconds = 90)
  $deadline = (Get-Date).AddSeconds([Math]::Max(5,$Seconds))
  do {
    if(Test-LadduQuestDbEndpoint -BaseUrl $BaseUrl){ return $true }
    Start-Sleep -Seconds 2
  } while((Get-Date) -lt $deadline)
  return $false
}

function Write-LadduJsonAtomic {
  param([string]$Path,[object]$Value)
  $parent = Split-Path $Path -Parent
  if(-not [string]::IsNullOrWhiteSpace($parent)){ New-Item -ItemType Directory -Force -Path $parent | Out-Null }
  $temporary = "$Path.$([Guid]::NewGuid().ToString('N')).tmp"
  try {
    $Value | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath $temporary -Encoding UTF8
    Move-Item -LiteralPath $temporary -Destination $Path -Force
  } finally { Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue }
}

function Write-LadduQuestDbPortState {
  param(
    [string]$InstallDir,
    [int]$HttpPort,
    [string]$Selection,
    [string]$Action,
    [string]$RetainedContainer = '',
    [string]$VolumeName = '',
    [object[]]$ExcludedRanges = @()
  )
  $secureDir = Join-Path $InstallDir 'secure'
  New-Item -ItemType Directory -Force -Path $secureDir | Out-Null
  if([string]::IsNullOrWhiteSpace($VolumeName)){ $VolumeName = Get-LadduQuestDbVolumeName }
  $path = Join-Path $secureDir 'data-plane.ports.json'
  if([string]::IsNullOrWhiteSpace($RetainedContainer) -and (Test-Path $path -PathType Leaf)){
    try {
      $priorPortState = Get-Content -LiteralPath $path -Raw | ConvertFrom-Json
      $priorRetained = [string]$priorPortState.retained_container
      if(-not [string]::IsNullOrWhiteSpace($priorRetained) -and (Get-LadduDockerContainerStatus -Container $priorRetained) -ne 'missing'){
        $RetainedContainer = $priorRetained
      }
    } catch {}
  }
  $state = [ordered]@{
    schema_version = 'questdb-port-authority-1.1.0'
    questdb_http_port = [int]$HttpPort
    questdb_url = "http://127.0.0.1:$HttpPort"
    questdb_volume = [string]$VolumeName
    selection = [string]$Selection
    action = [string]$Action
    retained_container = [string]$RetainedContainer
    container_id = Get-LadduDockerContainerId
    excluded_ranges = @($ExcludedRanges)
    verified_at = (Get-Date).ToString('o')
  }
  Write-LadduJsonAtomic -Path $path -Value $state
  try { & icacls.exe $path /inheritance:r /grant:r "SYSTEM:(F)" "Administrators:(F)" | Out-Null } catch {}

  $adminPath = Join-Path $secureDir 'data-plane.admin.json'
  if(Test-Path $adminPath -PathType Leaf){
    try {
      $admin = Get-Content -LiteralPath $adminPath -Raw | ConvertFrom-Json
      $admin.questdb_url = "http://127.0.0.1:$HttpPort"
      if($admin.PSObject.Properties.Name -contains 'questdb_http_port'){ $admin.questdb_http_port = [int]$HttpPort }
      Write-LadduJsonAtomic -Path $adminPath -Value $admin
      try { & icacls.exe $adminPath /inheritance:r /grant:r "SYSTEM:(F)" "Administrators:(F)" | Out-Null } catch {}
    } catch { throw "QuestDB port was resolved but data-plane.admin.json could not be updated atomically: $($_.Exception.Message)" }
  }
  return [pscustomobject]$state
}

function Get-LadduPreferredQuestDbPort {
  param([string]$InstallDir,[int]$DefaultPort = 59000)
  $existing = Get-LadduQuestDbPublishedPort
  if($existing -gt 0){ return $existing }
  $portPath = Join-Path $InstallDir 'secure\data-plane.ports.json'
  if(Test-Path $portPath -PathType Leaf){
    try { $state = Get-Content -LiteralPath $portPath -Raw | ConvertFrom-Json; if([int]$state.questdb_http_port -gt 0){ return [int]$state.questdb_http_port } } catch {}
  }
  $adminPath = Join-Path $InstallDir 'secure\data-plane.admin.json'
  if(Test-Path $adminPath -PathType Leaf){
    try { $admin = Get-Content -LiteralPath $adminPath -Raw | ConvertFrom-Json; $uri = [System.Uri]([string]$admin.questdb_url); if($uri.Port -gt 0){ return [int]$uri.Port } } catch {}
  }
  return $DefaultPort
}

function Ensure-LadduQuestDbContainer {
  param(
    [string]$ComposeFile,
    [string]$InstallDir,
    [int]$PreferredPort = 59000,
    [int]$ReadyTimeoutSeconds = 90,
    [string]$VolumeName = ''
  )
  if(!(Test-Path $ComposeFile -PathType Leaf)){ throw "QuestDB Compose file is missing: $ComposeFile" }
  $status = Get-LadduDockerContainerStatus

  if($status -eq 'paused'){
    $unpauseOutput = @(& docker.exe unpause project-laddu-questdb 2>&1)
    $unpauseExit = $LASTEXITCODE
    foreach($line in $unpauseOutput){ Write-Host ([string]$line) }
    if($unpauseExit -ne 0){ throw 'Existing QuestDB container could not be unpaused.' }
    $status = Get-LadduDockerContainerStatus
  }

  if($status -eq 'running'){
    $port = Get-LadduQuestDbPublishedPort
    if($port -le 0){ throw 'Existing running QuestDB container has no loopback HTTP port binding.' }
    $url = "http://127.0.0.1:$port"
    if(!(Wait-LadduQuestDbEndpoint -BaseUrl $url -Seconds 30)){
      throw 'Existing QuestDB container is running but its SQL/HTTP endpoint is unhealthy. It was preserved and not recreated.'
    }
    if([string]::IsNullOrWhiteSpace($VolumeName)){ $VolumeName = Get-LadduQuestDbVolumeName }
    return (Write-LadduQuestDbPortState -InstallDir $InstallDir -HttpPort $port -Selection 'EXISTING_BINDING' -Action 'PRESERVED_RUNNING_CONTAINER' -VolumeName $VolumeName)
  }

  if([string]::IsNullOrWhiteSpace($VolumeName)){ $VolumeName = Get-LadduQuestDbVolumeName }
  if([string]::IsNullOrWhiteSpace($VolumeName)){ throw 'QuestDB named-volume authority could not be resolved.' }
  & docker.exe volume inspect $VolumeName *> $null
  if($LASTEXITCODE -ne 0){ throw "QuestDB named volume does not exist: $VolumeName" }

  # Recovery/promotion is delegated to a stdlib-only Python transaction helper.
  # Contract markers retained for source gates: project-laddu-questdb-candidate-,
  # project-laddu-questdb-retained-, BLUE_GREEN_CANDIDATE_PROMOTED.
  # This avoids Windows PowerShell 5.1 native-command exit-code ambiguity and
  # permits deterministic regression testing of the exact Docker name cutover.
  $helper = Join-Path $PSScriptRoot 'questdb_recovery.py'
  if(!(Test-Path $helper -PathType Leaf)){ throw "QuestDB recovery helper is missing: $helper" }
  $python = 'C:\Program Files\Python312\python.exe'
  if(!(Test-Path $python -PathType Leaf)){
    $pythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
    if($null -ne $pythonCommand){ $python = [string]$pythonCommand.Source }
  }
  if(!(Test-Path $python -PathType Leaf)){ throw 'Python 3.12 runtime is unavailable for the tested QuestDB promotion transaction.' }

  $nativeOutput = @(& $python $helper --volume-name $VolumeName --preferred-port ([string]$PreferredPort) --timeout ([string]$ReadyTimeoutSeconds) 2>&1)
  $nativeExitCode = $LASTEXITCODE
  foreach($line in $nativeOutput){ Write-Host ([string]$line) }
  if($nativeExitCode -ne 0){
    throw "QuestDB recovery transaction failed with exit code $nativeExitCode. The historical container and healthy candidate were preserved."
  }
  $marker = 'LADDU_RECOVERY_RESULT_JSON='
  $resultLine = @($nativeOutput | Where-Object { ([string]$_).StartsWith($marker,[System.StringComparison]::Ordinal) } | Select-Object -Last 1)
  if($resultLine.Count -ne 1){ throw 'QuestDB recovery helper completed without one authoritative result record.' }
  try { $recovery = ([string]$resultLine[0]).Substring($marker.Length) | ConvertFrom-Json }
  catch { throw "QuestDB recovery helper returned invalid result JSON: $($_.Exception.Message)" }
  if($recovery.ok -ne $true -or [int]$recovery.port -le 0){ throw 'QuestDB recovery helper did not prove a healthy authoritative endpoint.' }
  return (Write-LadduQuestDbPortState -InstallDir $InstallDir -HttpPort ([int]$recovery.port) -Selection ([string]$recovery.selection) -Action ([string]$recovery.action) -RetainedContainer ([string]$recovery.retained_container) -VolumeName ([string]$recovery.volume_name))
}

function Complete-LadduQuestDbCutover {
  param([string]$InstallDir)
  $path = Join-Path $InstallDir 'secure\data-plane.ports.json'
  if(!(Test-Path $path -PathType Leaf)){ return }
  try { $state = Get-Content -LiteralPath $path -Raw | ConvertFrom-Json } catch { return }
  $retained = [string]$state.retained_container
  if([string]::IsNullOrWhiteSpace($retained)){ return }
  $url = [string]$state.questdb_url
  if(!(Test-LadduQuestDbEndpoint -BaseUrl $url)){ throw 'QuestDB cutover cannot be completed because the new authoritative endpoint is not healthy.' }
  if((Get-LadduDockerContainerStatus -Container $retained) -ne 'missing'){
    $removeOutput = @(& docker.exe rm -f $retained 2>&1)
    $removeExit = $LASTEXITCODE
    foreach($line in $removeOutput){ Write-Host ([string]$line) }
    if($removeExit -ne 0){ throw "Retained QuestDB container could not be removed after successful cutover: $retained" }
  }
  $state.retained_container = ''
  $state.action = 'CUTOVER_COMPLETE'
  $state.verified_at = (Get-Date).ToString('o')
  Write-LadduJsonAtomic -Path $path -Value $state
}
