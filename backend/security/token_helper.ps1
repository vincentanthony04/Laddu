param(
  [ValidateSet('read','write')][string]$Action = 'read',
  [string]$Token = ''
)
$ErrorActionPreference = 'Stop'
$InstallDir = $env:PROJECT_LADDU_HOME
if ([string]::IsNullOrWhiteSpace($InstallDir)) { $InstallDir = Join-Path $env:ProgramData 'ProjectLaddu' }
$SecureDir = Join-Path $InstallDir 'secure'
$TokenFile = Join-Path $SecureDir 'upstox_token.dpapi'
New-Item -ItemType Directory -Force -Path $SecureDir | Out-Null
Add-Type -AssemblyName System.Security
function Get-EntropyBytes {
  $machineGuid = 'ProjectLaddu-Machine'
  try { $machineGuid = (Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Cryptography').MachineGuid } catch {}
  return [Text.Encoding]::UTF8.GetBytes('ProjectLaddu-Upstox-v1|' + $machineGuid)
}
if ($Action -eq 'write') {
  if ([string]::IsNullOrWhiteSpace($Token)) { throw 'Token is empty' }
  $bytes = [Text.Encoding]::UTF8.GetBytes($Token.Trim())
  $protected = [Security.Cryptography.ProtectedData]::Protect($bytes, (Get-EntropyBytes), [Security.Cryptography.DataProtectionScope]::LocalMachine)
  [IO.File]::WriteAllBytes($TokenFile, $protected)
  try {
    $acl = Get-Acl $TokenFile
    $acl.SetAccessRuleProtection($true,$false)
    foreach($rule in @(
      New-Object System.Security.AccessControl.FileSystemAccessRule('SYSTEM','FullControl','Allow'),
      New-Object System.Security.AccessControl.FileSystemAccessRule('Administrators','FullControl','Allow')
    )) { $acl.AddAccessRule($rule) }
    Set-Acl -Path $TokenFile -AclObject $acl
  } catch {}
  Write-Output 'OK'
  exit 0
}
if (!(Test-Path $TokenFile)) { exit 2 }
$protected = [IO.File]::ReadAllBytes($TokenFile)
$bytes = [Security.Cryptography.ProtectedData]::Unprotect($protected, (Get-EntropyBytes), [Security.Cryptography.DataProtectionScope]::LocalMachine)
[Console]::Out.Write([Text.Encoding]::UTF8.GetString($bytes))
