param(
  [string]$InstallDir = "$env:ProgramData\ProjectLaddu",
  [int]$Port = 8086,
  [int]$InitialDelaySeconds = 0,
  [string]$ExpectedBuildMarker = ''
)
$ErrorActionPreference='Stop'
Set-StrictMode -Version 2.0
if($InitialDelaySeconds -gt 0){ Start-Sleep -Seconds ([Math]::Min(900,[Math]::Max(0,$InitialDelaySeconds))) }
try { (Get-Process -Id $PID).PriorityClass='BelowNormal' } catch {}
$identityPath=Join-Path $InstallDir 'frontend\release-identity.json'
if(!(Test-Path -LiteralPath $identityPath -PathType Leaf)){ exit 3 }
$identity=Get-Content -LiteralPath $identityPath -Raw | ConvertFrom-Json
if($ExpectedBuildMarker -and [string]$identity.build_marker -ne $ExpectedBuildMarker){
  Write-Host "Historical PIT enrichment skipped: installed build marker changed to $($identity.build_marker)."
  exit 0
}
$envFile=Join-Path $InstallDir 'secure\data-plane.env.ps1'
if(!(Test-Path -LiteralPath $envFile -PathType Leaf)){ throw "Data-plane environment missing: $envFile" }
. $envFile
$python=[Environment]::GetEnvironmentVariable('PROJECT_LADDU_RESEARCH_PYTHON','Machine')
if(!$python){ $python=(Get-Content -LiteralPath (Join-Path $InstallDir 'runtime\research_python.txt') | Select-Object -First 1).Trim() }
if(!(Test-Path -LiteralPath $python -PathType Leaf)){ throw "Research Python missing: $python" }
$dataDir=Join-Path $InstallDir 'data'
$manifestDir=Join-Path $dataDir 'manifests\historical_pit'
$logDir=Join-Path $InstallDir 'logs\research'
New-Item -ItemType Directory -Force -Path $manifestDir,$logDir | Out-Null
$stamp=Get-Date -Format 'yyyyMMdd_HHmmss'
$log=Join-Path $logDir "historical-pit-$stamp.log"
$report=Join-Path $manifestDir "historical-pit-$stamp.json"
$latest=Join-Path $manifestDir 'latest.json'
Start-Transcript -Path $log -Force | Out-Null
$started=(Get-Date).ToString('o')
$result=[ordered]@{version='historical-pit-enrichment-1.1.0';started_at=$started;state='STARTING';mode='delivery';training_source='INCREMENTAL_POINT_IN_TIME_PARQUET_FEATURE_STORE';minimum_training_dates=504;production_influence=0.0;broker_authority='NONE'}
function Get-OptionalProperty([object]$Object,[string]$Name){
  if($null -eq $Object){ return $null }
  $property=$Object.PSObject.Properties[$Name]
  if($null -eq $property){ return $null }
  return $property.Value
}
function Test-NseCashSession {
  try { $ist=[System.TimeZoneInfo]::ConvertTimeBySystemTimeZoneId((Get-Date),'India Standard Time') } catch { return $false }
  if($ist.DayOfWeek -in @([DayOfWeek]::Saturday,[DayOfWeek]::Sunday)){ return $false }
  $minutes=($ist.Hour*60)+$ist.Minute
  return ($minutes -ge 555 -and $minutes -le 925) # 09:15 through 15:25 IST
}
function Wait-ForSafeResearchWindow([int]$MaxWaitSeconds=1200){
  if(Test-NseCashSession){
    Write-Host 'Historical PIT enrichment deferred: NSE cash session is active.'
    return $false
  }
  $deadline=(Get-Date).AddSeconds([Math]::Max(0,$MaxWaitSeconds))
  do {
    try {
      $ops=Invoke-RestMethod -Method Get -Uri ("http://127.0.0.1:{0}/api/operations/summary" -f $Port) -TimeoutSec 5
      $wg=Get-OptionalProperty $ops 'workload_governor'
      if($null -eq $wg){ $outer=Get-OptionalProperty $ops 'operations'; $wg=Get-OptionalProperty $outer 'workload_governor' }
      $interactive=[bool](Get-OptionalProperty $wg 'interactive_priority_active')
      $pressure=Get-OptionalProperty $wg 'database_pressure'
      $saturated=[bool](Get-OptionalProperty $pressure 'saturated')
      $dbRecovery=[bool](Get-OptionalProperty $pressure 'required_database_recovery')
      $lifecycleBusy=$false
      $jobs=Get-OptionalProperty $ops 'jobs'
      if($null -eq $jobs){ $outer=Get-OptionalProperty $ops 'operations'; $jobs=Get-OptionalProperty $outer 'jobs' }
      foreach($job in @($jobs)){
        if([string](Get-OptionalProperty $job 'job_id') -eq 'lifecycle:closure'){
          $jobState=([string](Get-OptionalProperty $job 'state')).ToUpperInvariant()
          if($jobState -in @('RUNNING','PREPARING','RECOVERING')){ $lifecycleBusy=$true }
        }
      }
      if(-not $interactive -and -not $saturated -and -not $dbRecovery -and -not $lifecycleBusy){ return $true }
      Write-Host ("Historical PIT enrichment yielding: interactive={0} saturated={1} dbRecovery={2} lifecycleBusy={3}" -f $interactive,$saturated,$dbRecovery,$lifecycleBusy)
    } catch {
      Write-Warning ('Historical PIT enrichment cannot prove a safe research window yet: ' + $_.Exception.Message)
    }
    Start-Sleep -Seconds 30
  } while((Get-Date) -lt $deadline)
  return $false
}
try {
  if(-not (Wait-ForSafeResearchWindow -MaxWaitSeconds 1200)){
    $result.state='DEFERRED_FOREGROUND_PRIORITY'
    $result.ok=$true
    $result.reason='Research enrichment deferred rather than competing with market/interactive/lifecycle/database priority.'
    return
  }

  & $python (Join-Path $InstallDir 'backend\tools\refresh_research_catalog.py') --data-dir $dataDir
  if($LASTEXITCODE -ne 0){ throw 'Research catalogue refresh failed.' }
  $trainer=Join-Path $InstallDir 'backend\tools\train_nse_smart_model.py'
  # The packaged trainer already owns PIT feature construction, historical regime labels,
  # purged/embargoed OOF, capital WFA and governed SHADOW publication.  This runner merely
  # ensures the deep retained history is actually consumed; it does not invent another path.
  & $python $trainer --data-dir $dataDir --api-url "http://127.0.0.1:$Port" --horizon 10 --min-dates 504
  if($LASTEXITCODE -ne 0){ throw 'Deep historical PIT/WFA training did not qualify or failed.' }
  $result.state='COMPLETED_OR_CURRENT'
  $result.ok=$true
} catch {
  $result.state='BLOCKED'
  $result.ok=$false
  $result.error=$_.Exception.Message
  throw
} finally {
  $result.completed_at=(Get-Date).ToString('o')
  $json=$result | ConvertTo-Json -Depth 8
  Set-Content -LiteralPath $report -Value $json -Encoding UTF8
  Set-Content -LiteralPath $latest -Value $json -Encoding UTF8
  try { Stop-Transcript | Out-Null } catch {}
}
