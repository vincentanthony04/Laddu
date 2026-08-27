param(
    [string]$Symbol = "INFY",
    [int]$TimeoutSec = 45,
    [int]$LogTail = 5000
)

$ErrorActionPreference = "Continue"
$ProgressPreference = "SilentlyContinue"

$Symbol = $Symbol.Trim().ToUpperInvariant()
$BaseUrl = "http://127.0.0.1:8086"
$InstallDir = Join-Path $env:ProgramData "ProjectLaddu"
$Stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$Root = "C:\Temp\ProjectLaddu"
$OutDir = Join-Path $Root ("Stock-Chart-Diagnostics-" + $Stamp)
$ApiDir = Join-Path $OutDir "api"
$LogDir = Join-Path $OutDir "logs"
$SystemDir = Join-Path $OutDir "system"

New-Item -ItemType Directory -Force -Path $Root, $OutDir, $ApiDir, $LogDir, $SystemDir | Out-Null

$Rows = New-Object System.Collections.Generic.List[object]

function Safe-Name([string]$Text) {
    return (($Text -replace '[^A-Za-z0-9._-]', '_').Trim('_'))
}

function Save-Json($Value, [string]$Path) {
    try {
        $Value | ConvertTo-Json -Depth 80 | Set-Content -LiteralPath $Path -Encoding UTF8
    } catch {
        $_.Exception.ToString() | Set-Content -LiteralPath ($Path + ".error.txt") -Encoding UTF8
    }
}

function Get-Endpoint {
    param(
        [string]$Name,
        [string]$Path,
        [int]$Timeout = $TimeoutSec
    )

    $Timer = [Diagnostics.Stopwatch]::StartNew()
    $File = Join-Path $ApiDir ((Safe-Name $Name) + ".json")
    try {
        $Result = Invoke-RestMethod -Uri ($BaseUrl + $Path) -Method Get -TimeoutSec $Timeout
        $Timer.Stop()
        Save-Json $Result $File
        $Rows.Add([pscustomobject]@{
            Name = $Name
            Path = $Path
            Success = $true
            ElapsedMs = [int]$Timer.ElapsedMilliseconds
            Error = ""
        })
        Write-Host ("[PASS] {0} - {1} ms" -f $Name, $Timer.ElapsedMilliseconds) -ForegroundColor Green
        return $Result
    } catch {
        $Timer.Stop()
        $Message = $_.Exception.Message
        $Message | Set-Content -LiteralPath ($File + ".error.txt") -Encoding UTF8
        $Rows.Add([pscustomobject]@{
            Name = $Name
            Path = $Path
            Success = $false
            ElapsedMs = [int]$Timer.ElapsedMilliseconds
            Error = $Message
        })
        Write-Host ("[FAIL] {0} - {1} ms - {2}" -f $Name, $Timer.ElapsedMilliseconds, $Message) -ForegroundColor Red
        return $null
    }
}

function Capture-Command {
    param([string]$Name, [scriptblock]$Command)
    $Path = Join-Path $SystemDir ((Safe-Name $Name) + ".txt")
    try {
        & $Command 2>&1 | Out-String -Width 500 | Set-Content -LiteralPath $Path -Encoding UTF8
    } catch {
        $_.Exception.ToString() | Set-Content -LiteralPath $Path -Encoding UTF8
    }
}

Write-Host ""
Write-Host "Project Laddu selected-stock and chart diagnostics" -ForegroundColor Cyan
Write-Host ("Symbol: " + $Symbol)
Write-Host ("Output: " + $OutDir)
Write-Host ""

# Core runtime and scanner authority
$Ready = Get-Endpoint "ready" "/api/ready" 15
$Health = Get-Endpoint "health" "/api/health" 30
$Product = Get-Endpoint "product-readiness" "/api/product-readiness" 30
$Scanner = Get-Endpoint "scanner-status" "/api/scanner/status" 30
$Pipeline = Get-Endpoint "pipeline-health" "/api/pipeline-health" 30
$Cards = Get-Endpoint "dashboard-cards" "/api/dashboard-cards?mode=all" 45
$Radar = Get-Endpoint "market-radar" "/api/market-radar" 45
$Indices = Get-Endpoint "indices" "/api/indices" 30
$Heat = Get-Endpoint "market-heatmap" "/api/market/heatmap" 30
$CoverageAggregate = Get-Endpoint "coverage-aggregate" "/api/data-coverage" 30

# Selected-stock canonical truth
$Encoded = [uri]::EscapeDataString($Symbol)
$Coverage = Get-Endpoint "$Symbol-coverage" ("/api/data-coverage?symbol=" + $Encoded) 45
$Mtf = Get-Endpoint "$Symbol-mtf" ("/api/mtf-trend?symbol=" + $Encoded + "&refresh=false") 45
$IntelDelivery = Get-Endpoint "$Symbol-intelligence-delivery" ("/api/stock-intelligence?symbol=" + $Encoded + "&mode=delivery&refresh=false") 45
$IntelIntraday = Get-Endpoint "$Symbol-intelligence-intraday" ("/api/stock-intelligence?symbol=" + $Encoded + "&mode=intraday&refresh=false") 45

# Historical chart paths used by the browser
foreach ($Interval in @("3minute","15minute","30minute","60minute","240minute","day","week","month")) {
    Get-Endpoint ("$Symbol-historical-" + $Interval) `
        ("/api/historical?symbol=" + $Encoded + "&interval=" + $Interval + "&refresh=false") 60 | Out-Null
}

# Try common quote/search paths. Unsupported routes are retained as evidence.
Get-Endpoint "$Symbol-search" ("/api/search?q=" + $Encoded) 20 | Out-Null
Get-Endpoint "$Symbol-quote" ("/api/quote?symbol=" + $Encoded) 20 | Out-Null
Get-Endpoint "$Symbol-stock-report" ("/api/stock-report?symbol=" + $Encoded) 45 | Out-Null

# Capture the same symbol repeatedly to expose inconsistent canonical price projections.
1..3 | ForEach-Object {
    Start-Sleep -Seconds 2
    Get-Endpoint ("$Symbol-intelligence-repeat-" + $_) `
        ("/api/stock-intelligence?symbol=" + $Encoded + "&mode=delivery&refresh=false") 45 | Out-Null
    Get-Endpoint ("dashboard-cards-repeat-" + $_) "/api/dashboard-cards?mode=all" 45 | Out-Null
}

# Service, process, version, port and event state
Capture-Command "service" {
    Get-Service ProjectLaddu -ErrorAction SilentlyContinue | Format-List *
    Get-CimInstance Win32_Service -Filter "Name='ProjectLaddu'" -ErrorAction SilentlyContinue |
        Select-Object Name, State, StartMode, ProcessId, PathName | Format-List
}
Capture-Command "port-8086" {
    Get-NetTCPConnection -LocalPort 8086 -ErrorAction SilentlyContinue |
        Select-Object State, LocalAddress, LocalPort, OwningProcess |
        Format-Table -AutoSize
}
Capture-Command "deploy-manifest" {
    $Manifest = Join-Path $InstallDir "DEPLOY_MANIFEST.json"
    if (Test-Path -LiteralPath $Manifest) { Get-Content -LiteralPath $Manifest -Raw }
}
Capture-Command "status" {
    $Status = Join-Path $InstallDir "status.ps1"
    if (Test-Path -LiteralPath $Status) {
        powershell.exe -NoProfile -ExecutionPolicy Bypass -File $Status
    }
}
Capture-Command "windows-events" {
    $Since = (Get-Date).AddHours(-3)
    Get-WinEvent -FilterHashtable @{LogName="Application"; StartTime=$Since} -ErrorAction SilentlyContinue |
        Where-Object { $_.Message -match "ProjectLaddu|Project Laddu|python|uvicorn|8086" } |
        Select-Object TimeCreated, Id, LevelDisplayName, ProviderName, Message |
        Format-List
}

# Recent application logs
$SourceLogRoot = Join-Path $InstallDir "logs"
if (Test-Path -LiteralPath $SourceLogRoot) {
    Get-ChildItem -LiteralPath $SourceLogRoot -Recurse -File -ErrorAction SilentlyContinue |
        Where-Object { $_.Extension -in @(".log",".txt",".json") } |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 30 |
        ForEach-Object {
            $Name = Safe-Name (($_.FullName.Substring($SourceLogRoot.Length)).TrimStart("\") -replace "\\","__")
            $Dest = Join-Path $LogDir $Name
            if ($_.Extension -in @(".log",".txt")) {
                try {
                    Get-Content -LiteralPath $_.FullName -Tail $LogTail -ErrorAction Stop |
                        Set-Content -LiteralPath $Dest -Encoding UTF8
                } catch {
                    $_.Exception.ToString() | Set-Content -LiteralPath ($Dest + ".error.txt") -Encoding UTF8
                }
            } elseif ($_.Length -le 25MB) {
                Copy-Item -LiteralPath $_.FullName -Destination $Dest -Force
            }
        }
}

$Rows | Export-Csv -LiteralPath (Join-Path $OutDir "endpoint-latency.csv") -NoTypeInformation -Encoding UTF8

@(
    "Collected: " + (Get-Date).ToString("o")
    "Symbol: " + $Symbol
    "Purpose: closed-session freshness, canonical price consistency, chart/MTF path, scanner progress"
    "API pass: " + @($Rows | Where-Object Success).Count
    "API fail: " + @($Rows | Where-Object { -not $_.Success }).Count
    ""
    "Important: a verified Friday close should be CURRENT_AT_CLOSE on a Sunday, not STALE."
    "Unsupported quote/search routes may appear as FAIL and are retained only to reveal the active API contract."
) | Set-Content -LiteralPath (Join-Path $OutDir "README.txt") -Encoding UTF8

$Zip = Join-Path $Root ("ProjectLaddu-Stock-Chart-Diagnostics-" + $Stamp + ".zip")
if (Test-Path -LiteralPath $Zip) { Remove-Item -LiteralPath $Zip -Force }
Compress-Archive -Path (Join-Path $OutDir "*") -DestinationPath $Zip -CompressionLevel Optimal -Force

Write-Host ""
Write-Host "Diagnostics complete" -ForegroundColor Green
Write-Host ("ZIP: " + $Zip) -ForegroundColor Cyan
Write-Host "Upload this ZIP here." -ForegroundColor Yellow
