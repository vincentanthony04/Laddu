# Clean-machine prerequisite bootstrap. Must run before installer transaction
# creation or any Project Laddu target/runtime mutation.
function Resolve-ProjectLadduWinget {
  $cmd = Get-Command winget.exe -ErrorAction SilentlyContinue
  if($cmd){ return [string]$cmd.Source }
  $candidate = Join-Path $env:LOCALAPPDATA 'Microsoft\WindowsApps\winget.exe'
  if(Test-Path -LiteralPath $candidate -PathType Leaf){ return $candidate }
  return ''
}
function Resolve-ProjectLadduPython312 {
  foreach($candidate in @(
    'C:\Program Files\Python312\python.exe',
    (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe')
  )){
    if(Test-Path -LiteralPath $candidate -PathType Leaf){
      $version = & $candidate -c "import platform,sys; print(f'{sys.version_info.major}.{sys.version_info.minor}|{platform.architecture()[0]}')" 2>$null
      if($LASTEXITCODE -eq 0 -and [string]$version.Trim() -eq '3.12|64bit'){ return $candidate }
    }
  }
  $py = Get-Command py.exe -ErrorAction SilentlyContinue
  if($py){
    $resolved = & $py.Source -3.12 -c "import platform,sys; print(sys.executable if platform.architecture()[0]=='64bit' else '')" 2>$null
    if($LASTEXITCODE -eq 0 -and $resolved -and (Test-Path -LiteralPath $resolved.Trim() -PathType Leaf)){ return $resolved.Trim() }
  }
  $python = Get-Command python.exe -ErrorAction SilentlyContinue
  if($python){
    $version = & $python.Source -c "import platform,sys; print(f'{sys.version_info.major}.{sys.version_info.minor}|{platform.architecture()[0]}')" 2>$null
    if($LASTEXITCODE -eq 0 -and [string]$version.Trim() -eq '3.12|64bit'){ return [string]$python.Source }
  }
  return ''
}
function Resolve-ProjectLadduDocker {
  $cmd = Get-Command docker.exe -ErrorAction SilentlyContinue
  if($cmd){ return [string]$cmd.Source }
  foreach($candidate in @(
    (Join-Path $env:ProgramFiles 'Docker\Docker\resources\bin\docker.exe'),
    (Join-Path ${env:ProgramFiles(x86)} 'Docker\Docker\resources\bin\docker.exe'),
    (Join-Path $env:LOCALAPPDATA 'Docker\resources\bin\docker.exe')
  )){
    if(Test-Path -LiteralPath $candidate -PathType Leaf){ return $candidate }
  }
  return ''
}
function Invoke-ProjectLadduWingetInstall {
  param(
    [Parameter(Mandatory=$true)][string]$Winget,
    [Parameter(Mandatory=$true)][string]$PackageId,
    [switch]$MachineScope
  )
  $args = @('install','--id',$PackageId,'-e','--silent','--accept-source-agreements','--accept-package-agreements','--disable-interactivity')
  if($MachineScope){ $args += @('--scope','machine') }
  & $Winget @args | Out-Host
  if($LASTEXITCODE -ne 0){ throw "Automatic prerequisite installation failed for $PackageId (winget exit $LASTEXITCODE)." }
}
function Get-ProjectLadduOptionalFeatureState([string]$Name){
  try {
    return [string](Get-WindowsOptionalFeature -Online -FeatureName $Name -ErrorAction Stop).State
  } catch {
    return 'UNKNOWN'
  }
}
function Ensure-ProjectLadduPrerequisites {
  param([Parameter(Mandatory=$true)][string]$EvidenceDir)
  if(-not [Environment]::Is64BitOperatingSystem){ throw 'Project Laddu requires 64-bit Windows.' }
  $os = Get-CimInstance Win32_OperatingSystem -ErrorAction Stop
  $version = [Version]$os.Version
  if($version.Major -lt 10){ throw "Unsupported Windows version: $($os.Caption) $($os.Version). Windows 10/11 x64 is required." }

  $actions = New-Object System.Collections.Generic.List[string]
  $rebootRequired = $false
  $winget = Resolve-ProjectLadduWinget

  $python = Resolve-ProjectLadduPython312
  if([string]::IsNullOrWhiteSpace($python)){
    if([string]::IsNullOrWhiteSpace($winget)){ throw 'Python 3.12 x64 is missing and Windows Package Manager (winget/App Installer) is unavailable for automatic bootstrap.' }
    Write-Step 'PREREQUISITE' 'Python 3.12 x64 not found; installing the pinned major/minor runtime with Windows Package Manager before target mutation'
    Invoke-ProjectLadduWingetInstall -Winget $winget -PackageId 'Python.Python.3.12' -MachineScope
    $actions.Add('INSTALLED_PYTHON_3_12')
    $python = Resolve-ProjectLadduPython312
    if([string]::IsNullOrWhiteSpace($python)){ throw 'Python 3.12 installation completed but a verified x64 interpreter could not be resolved.' }
  }

  foreach($featureName in @('Microsoft-Windows-Subsystem-Linux','VirtualMachinePlatform')){
    $state = Get-ProjectLadduOptionalFeatureState $featureName
    if($state -eq 'Disabled'){
      Write-Step 'PREREQUISITE' "Enabling Windows feature $featureName before target mutation"
      $result = Enable-WindowsOptionalFeature -Online -FeatureName $featureName -All -NoRestart -ErrorAction Stop
      $actions.Add("ENABLED_$($featureName.ToUpperInvariant())")
      if([bool]$result.RestartNeeded){ $rebootRequired = $true }
    } elseif($state -notin @('Enabled','Enable Pending','UNKNOWN')){
      throw "Windows prerequisite feature $featureName is in unsupported state: $state"
    }
  }

  $docker = Resolve-ProjectLadduDocker
  if([string]::IsNullOrWhiteSpace($docker)){
    if([string]::IsNullOrWhiteSpace($winget)){ throw 'Docker Desktop/Engine is missing and Windows Package Manager (winget/App Installer) is unavailable for automatic bootstrap.' }
    Write-Step 'PREREQUISITE' 'Docker Desktop/Engine not found; installing Docker Desktop before target mutation'
    Invoke-ProjectLadduWingetInstall -Winget $winget -PackageId 'Docker.DockerDesktop'
    $actions.Add('INSTALLED_DOCKER_DESKTOP')
    $docker = Resolve-ProjectLadduDocker
    if([string]::IsNullOrWhiteSpace($docker)){ $rebootRequired = $true }
  }

  $csc = 'C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe'
  if(!(Test-Path -LiteralPath $csc -PathType Leaf)){
    throw '.NET Framework 4.x C# compiler is unavailable; Project Laddu service bootstrap cannot be built safely on this machine.'
  }

  $report = [ordered]@{
    ok = -not $rebootRequired
    contract = 'project-laddu-clean-machine-prerequisites-1.0.0'
    windows_caption = [string]$os.Caption
    windows_version = [string]$os.Version
    os_64bit = [Environment]::Is64BitOperatingSystem
    python_312_x64 = $python
    docker_executable = $docker
    wsl_feature = Get-ProjectLadduOptionalFeatureState 'Microsoft-Windows-Subsystem-Linux'
    virtual_machine_platform = Get-ProjectLadduOptionalFeatureState 'VirtualMachinePlatform'
    dotnet_csc = $csc
    winget = $winget
    actions = @($actions)
    reboot_required = $rebootRequired
    checked_at = (Get-Date).ToUniversalTime().ToString('o')
  }
  Write-Json (Join-Path $EvidenceDir 'prerequisites.json') $report
  if($rebootRequired){
    throw 'PREREQUISITES_INSTALLED_REBOOT_REQUIRED: Windows/Docker prerequisites were installed or enabled successfully. Reboot Windows, then rerun INSTALL_UPDATE.cmd. Project Laddu runtime/data have not been stopped or modified.'
  }
  Write-Step 'PREREQUISITE' 'Clean-machine prerequisite contract passed'
  return [pscustomobject]$report
}
