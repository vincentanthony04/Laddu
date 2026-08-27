param(
  [string]$InstallDir = "$env:ProgramData\ProjectLaddu"
)
$ErrorActionPreference = 'Stop'
$helper = Join-Path $InstallDir 'backend\security\token_helper.ps1'
if (!(Test-Path $helper)) { throw "Project Laddu is not installed at $InstallDir. Run INSTALL_UPDATE.cmd first." }
Write-Host "Project Laddu - Secure Upstox Token Setup" -ForegroundColor Cyan
Write-Host "Token will be encrypted using Windows DPAPI LocalMachine. It will not be stored as plaintext." -ForegroundColor DarkYellow
$secure = Read-Host "Paste Upstox access token" -AsSecureString
$plain = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure))
try {
  if ([string]::IsNullOrWhiteSpace($plain)) { throw 'Empty token' }
  & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $helper write -Token $plain | Out-Null
  Write-Host "[OK] Upstox token encrypted and saved." -ForegroundColor Green
  Write-Host "Restarting the ProjectLaddu service..." -ForegroundColor Cyan
  & (Join-Path $InstallDir 'installer\runtime.ps1') -Action Restart -InstallDir $InstallDir
} finally {
  $plain = $null
  [GC]::Collect()
}
