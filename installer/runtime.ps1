param(
  [ValidateSet('Start','Stop','Restart','Status')]
  [string]$Action,
  [string]$InstallDir = "$env:ProgramData\ProjectLaddu",
  [int]$Port = 8086,
  [int]$StartupDeadlineSec = 120
)

$ErrorActionPreference = 'Stop'
$ServiceName = 'ProjectLaddu'

function Test-Administrator {
  $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
  $principal = New-Object Security.Principal.WindowsPrincipal($identity)
  return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Ensure-Administrator {
  if(Test-Administrator){ return }
  $self = $MyInvocation.MyCommand.Path
  $arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$self`" -Action $Action -InstallDir `"$InstallDir`" -Port $Port -StartupDeadlineSec $StartupDeadlineSec"
  Start-Process powershell.exe -Verb RunAs -ArgumentList $arguments
  exit
}

function Test-Ready {
  try {
    $request = [Net.HttpWebRequest]::Create("http://127.0.0.1:$Port/api/ready")
    $request.Method = 'GET'
    $request.Timeout = 1800
    $request.ReadWriteTimeout = 1800
    $request.Proxy = $null
    $response = $request.GetResponse()
    try {
      $reader = New-Object IO.StreamReader($response.GetResponseStream())
      return [pscustomobject]@{ ok=$true; body=($reader.ReadToEnd() | ConvertFrom-Json) }
    } finally { $response.Close() }
  } catch { return [pscustomobject]@{ ok=$false; error=$_.Exception.Message } }
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

function Show-RecentFailure {
  foreach($path in @(
    (Join-Path $InstallDir 'logs\backend-startup-error.log'),
    (Join-Path $InstallDir 'logs\service.log')
  )){
    if(Test-Path -LiteralPath $path){
      Write-Host "--- $path ---" -ForegroundColor Yellow
      Get-Content -LiteralPath $path -Tail 100 -ErrorAction SilentlyContinue | Out-Host
    }
  }
}

function Start-Runtime {
  $service = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
  if(!$service){ throw 'ProjectLaddu service is not installed. Run INSTALL_UPDATE.cmd.' }
  if($service.Status -ne 'Running'){ Start-Service -Name $ServiceName }
  $ready = Wait-Ready
  if($null -eq $ready){ Show-RecentFailure; throw "Project Laddu did not reach /api/ready within $StartupDeadlineSec seconds." }
  Write-Host "[OK] Project Laddu $($ready.version) is ready at http://127.0.0.1:$Port" -ForegroundColor Green
}

function Stop-Runtime {
  $service = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
  if($service -and $service.Status -ne 'Stopped'){
    Stop-Service -Name $ServiceName -Force
    $service.WaitForStatus('Stopped',[TimeSpan]::FromSeconds(20))
  }
  Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
    $_.CommandLine -and $_.CommandLine -like '*ProjectLaddu*backend*main.py*'
  } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
  Write-Host '[OK] Project Laddu is stopped.' -ForegroundColor Green
}

function Show-Status {
  $service = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
  Write-Host ('Service: ' + $(if($service){$service.Status}else{'MISSING'}))
  $probe = Test-Ready
  if($probe.ok){
    Write-Host "API: READY · version=$($probe.body.version) · http://127.0.0.1:$Port" -ForegroundColor Green
  } else {
    Write-Host "API: NOT READY · $($probe.error)" -ForegroundColor Yellow
    Show-RecentFailure
  }
}

if($Action -in @('Start','Stop','Restart')){ Ensure-Administrator }

switch($Action){
  'Start'   { Start-Runtime }
  'Stop'    { Stop-Runtime }
  'Restart' { Stop-Runtime; Start-Sleep -Seconds 1; Start-Runtime }
  'Status'  { Show-Status }
}
