param([string]$InstallDir = "$env:ProgramData\ProjectLaddu")
$root = Join-Path $InstallDir 'logs'
$today = Join-Path $root (Get-Date -Format 'yyyy-MM-dd')
Write-Host "Opening logs: $root" -ForegroundColor Cyan
Write-Host "Today's logs: $today" -ForegroundColor Cyan
Write-Host "Last 10 minutes: powershell -ExecutionPolicy Bypass -File `"$InstallDir\installer\logs_last10.ps1`"" -ForegroundColor DarkCyan
if (Test-Path $today) { explorer.exe $today }
elseif (Test-Path $root) { explorer.exe $root }
else { Write-Host "No logs folder found." -ForegroundColor Yellow }
