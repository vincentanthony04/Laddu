param(
  [string]$InstallDir = "$env:ProgramData\ProjectLaddu",
  [int]$Minutes = 10,
  [int]$Tail = 300
)
$ErrorActionPreference = 'SilentlyContinue'
$since = (Get-Date).AddMinutes(-1 * [Math]::Abs($Minutes))
$today = Get-Date -Format 'yyyy-MM-dd'
$paths = @()
$dayDir = Join-Path (Join-Path $InstallDir 'logs') $today
if (Test-Path $dayDir) { $paths += Get-ChildItem $dayDir -Filter '*.log' -File }
$rootDir = Join-Path $InstallDir 'logs'
if (Test-Path $rootDir) { $paths += Get-ChildItem $rootDir -Filter '*.log' -File }
$paths = $paths | Sort-Object FullName -Unique
if (-not $paths -or $paths.Count -eq 0) { Write-Host "No log files found under $rootDir" -ForegroundColor Yellow; exit 0 }
Write-Host "Project Laddu logs -- last $Minutes minutes" -ForegroundColor Cyan
foreach ($f in $paths) {
  if ($f.LastWriteTime -lt $since) { continue }
  Write-Host "`n--- $($f.FullName) ---" -ForegroundColor DarkCyan
  Get-Content $f.FullName -Tail $Tail | Where-Object {
    if ($_ -match '^\d{4}-\d{2}-\d{2}T') {
      try { ([datetimeoffset]::Parse($_.Substring(0, [Math]::Min(25, $_.Length))).DateTime) -ge $since } catch { $true }
    } else { $true }
  }
}
