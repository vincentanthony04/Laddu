# Bounded installed-runtime HTTP/readiness orchestration.
function Test-Ready {
  try {
    $request = [Net.HttpWebRequest]::Create("http://127.0.0.1:$Port/api/ready")
    $request.Method = 'GET'; $request.Timeout = 1800; $request.ReadWriteTimeout = 1800; $request.Proxy = $null
    $response = $request.GetResponse()
    try {
      $reader = New-Object IO.StreamReader($response.GetResponseStream())
      return [pscustomobject]@{ ok=$true; body=($reader.ReadToEnd() | ConvertFrom-Json) }
    } finally { $response.Close() }
  } catch { return [pscustomobject]@{ ok=$false; error=$_.Exception.Message } }
}
function Get-ApiJson([string]$Path){
  $request = [Net.HttpWebRequest]::Create("http://127.0.0.1:$Port$Path")
  $request.Method = 'GET'; $request.Timeout = 5000; $request.ReadWriteTimeout = 5000; $request.Proxy = $null
  $response = $request.GetResponse()
  try {
    $reader = New-Object IO.StreamReader($response.GetResponseStream())
    return ($reader.ReadToEnd() | ConvertFrom-Json)
  } finally { $response.Close() }
}

function Get-ApiJsonWithRetry {
  param(
    [Parameter(Mandatory=$true)][string]$Path,
    [int]$OverallDeadlineSec = 90,
    [int]$AttemptTimeoutMs = 20000,
    [string]$Label = 'API projection'
  )
  $deadline = (Get-Date).AddSeconds($OverallDeadlineSec)
  $attempt = 0
  $lastError = ''
  while((Get-Date) -lt $deadline){
    $attempt += 1
    $remainingMs = [Math]::Max(1000,[int](($deadline-(Get-Date)).TotalMilliseconds))
    $timeoutMs = [Math]::Min($AttemptTimeoutMs,$remainingMs)
    try {
      $request = [Net.HttpWebRequest]::Create("http://127.0.0.1:$Port$Path")
      $request.Method = 'GET'
      $request.Timeout = $timeoutMs
      $request.ReadWriteTimeout = $timeoutMs
      $request.Proxy = $null
      $response = $request.GetResponse()
      try {
        $reader = New-Object IO.StreamReader($response.GetResponseStream())
        $body = $reader.ReadToEnd()
        $parsed = $body | ConvertFrom-Json
        Write-Host ("[PROJECTION] {0} ready on attempt {1}" -f $Label,$attempt) -ForegroundColor DarkGreen
        return $parsed
      } finally { $response.Close() }
    } catch {
      $lastError = $_.Exception.Message
      $remaining = [Math]::Max(0,[int]($deadline-(Get-Date)).TotalSeconds)
      Write-Host ("[PROJECTION] {0} pending attempt={1} remaining={2}s error={3}" -f $Label,$attempt,$remaining,$lastError) -ForegroundColor DarkYellow
      if($remaining -le 0){ break }
      Start-Sleep -Seconds ([Math]::Min(3,[Math]::Max(1,$remaining)))
    }
  }
  throw "$Label did not become readable within $OverallDeadlineSec seconds at $Path. Last error: $lastError"
}

function Get-ResearchRetentionProjectionProof {
  param([int]$OverallDeadlineSec = 120,[int]$AttemptTimeoutMs = 30000)
  $preservation = Get-ApiJsonWithRetry -Path '/api/research-preservation-manifest' -OverallDeadlineSec $OverallDeadlineSec -AttemptTimeoutMs $AttemptTimeoutMs -Label 'Research preservation projection'
  if(-not $preservation.ok -or [string]$preservation.state -ne 'PRESERVED'){ throw 'Research preservation manifest failed after upgrade.' }
  $invalid = @($preservation.datasets | Where-Object { $_.classification -notin @('RETAINED','MIGRATED','QUARANTINED','SUPERSEDED_WITH_LINEAGE') })
  if($invalid.Count -gt 0){ throw 'Research preservation manifest contains an invalid/destructive classification.' }
  $continuity = [pscustomobject][ordered]@{
    ok = [bool]$preservation.ok
    state = [string]$preservation.retention_state
    content_hash = [string]$preservation.retention_content_hash
    counts = $preservation.counts
    desk_lineage = $preservation.desk_lineage
    regressions = $preservation.regressions
    projected_via = '/api/research-preservation-manifest'
  }
  if([string]$continuity.state -eq 'RETENTION_REGRESSION_DETECTED'){
    throw "Research retention regression detected: $(@($continuity.regressions | ConvertTo-Json -Compress) -join ',')"
  }
  return [pscustomobject]@{ preservation=$preservation; continuity=$continuity }
}
function Try-Get-ApiJson([string]$Path){
  try { return Get-ApiJson $Path } catch { return $null }
}
function Get-HttpText([string]$Path){
  $request = [Net.HttpWebRequest]::Create("http://127.0.0.1:$Port$Path")
  $request.Method = 'GET'; $request.Timeout = 5000; $request.ReadWriteTimeout = 5000; $request.Proxy = $null
  $response = $request.GetResponse()
  try {
    $reader = New-Object IO.StreamReader($response.GetResponseStream())
    return $reader.ReadToEnd()
  } finally { $response.Close() }
}
function Wait-Ready {
  $deadline = (Get-Date).AddSeconds($StartupDeadlineSec)
  $nextProgress = Get-Date
  while((Get-Date) -lt $deadline){
    $probe = Test-Ready
    if($probe.ok){ return $probe.body }
    if((Get-Date) -ge $nextProgress){
      $service = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
      $state = if($service){ [string]$service.Status } else { 'MISSING' }
      $remaining = [Math]::Max(0,[int]($deadline-(Get-Date)).TotalSeconds)
      Write-Host ("[STARTING] service={0} remaining={1}s ready=false" -f $state,$remaining) -ForegroundColor DarkYellow
      $nextProgress = (Get-Date).AddSeconds(5)
    }
    Start-Sleep -Seconds 1
  }
  return $null
}
