param(
    [int]$Minutes = 90,
    [string[]]$Symbols = @("INFY", "TCS"),
    [int]$ApiTimeoutSec = 30
)

$ErrorActionPreference = "Continue"
$ProgressPreference = "SilentlyContinue"

$InstallDir = Join-Path $env:ProgramData "ProjectLaddu"
$BaseUrl = "http://127.0.0.1:8086"
$Stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$EvidenceBase = "C:\Temp\ProjectLaddu"
$OutDir = Join-Path $EvidenceBase ("Operational-Evidence-" + $Stamp)
$ApiDir = Join-Path $OutDir "api"
$LogDir = Join-Path $OutDir "logs"
$SystemDir = Join-Path $OutDir "system"
$ManifestDir = Join-Path $OutDir "manifests"
$Since = (Get-Date).AddMinutes(-1 * [Math]::Abs($Minutes))

New-Item -ItemType Directory -Force -Path $EvidenceBase, $OutDir, $ApiDir, $LogDir, $SystemDir, $ManifestDir | Out-Null

$Summary = New-Object System.Collections.Generic.List[string]
$EndpointRows = New-Object System.Collections.Generic.List[object]

function Add-Summary {
    param([string]$Text)
    $Summary.Add($Text)
    Write-Host $Text
}

function Safe-Name {
    param([string]$Text)
    return (($Text -replace '[^A-Za-z0-9._-]', '_').Trim('_'))
}

function Save-Json {
    param(
        [object]$Object,
        [string]$Path
    )
    try {
        $Object | ConvertTo-Json -Depth 60 | Set-Content -LiteralPath $Path -Encoding UTF8
    } catch {
        ("JSON save failed: " + $_.Exception.Message) | Set-Content -LiteralPath ($Path + ".error.txt") -Encoding UTF8
    }
}

function Invoke-ApiEvidence {
    param(
        [string]$Name,
        [string]$Path,
        [int]$TimeoutSec = $ApiTimeoutSec
    )

    $Safe = Safe-Name $Name
    $Target = Join-Path $ApiDir ($Safe + ".json")
    $Timer = [System.Diagnostics.Stopwatch]::StartNew()
    try {
        $Result = Invoke-RestMethod -Uri ($BaseUrl + $Path) -Method Get -TimeoutSec $TimeoutSec
        $Timer.Stop()
        Save-Json -Object $Result -Path $Target

        $State = ""
        foreach ($Property in @("state", "product_state", "service", "status", "data_status", "projection_state")) {
            if ($Result.PSObject.Properties.Name -contains $Property) {
                $State = [string]$Result.$Property
                if ($State) { break }
            }
        }

        $EndpointRows.Add([pscustomobject]@{
            Name = $Name
            Path = $Path
            Success = $true
            ElapsedMs = [int]$Timer.ElapsedMilliseconds
            State = $State
            Error = ""
        })
        Write-Host ("[PASS] {0} - {1} ms {2}" -f $Name, $Timer.ElapsedMilliseconds, $State) -ForegroundColor Green
        return $Result
    } catch {
        $Timer.Stop()
        $Message = $_.Exception.Message
        $Message | Set-Content -LiteralPath (Join-Path $ApiDir ($Safe + ".error.txt")) -Encoding UTF8
        $EndpointRows.Add([pscustomobject]@{
            Name = $Name
            Path = $Path
            Success = $false
            ElapsedMs = [int]$Timer.ElapsedMilliseconds
            State = ""
            Error = $Message
        })
        Write-Host ("[FAIL] {0} - {1} ms - {2}" -f $Name, $Timer.ElapsedMilliseconds, $Message) -ForegroundColor Red
        return $null
    }
}

function Invoke-PostEvidence {
    param(
        [string]$Name,
        [string]$Path,
        [object]$Body = @{},
        [int]$TimeoutSec = $ApiTimeoutSec
    )
    $Safe = Safe-Name $Name
    $Target = Join-Path $ApiDir ($Safe + ".json")
    $Timer = [System.Diagnostics.Stopwatch]::StartNew()
    try {
        $JsonBody = $Body | ConvertTo-Json -Depth 30 -Compress
        $Result = Invoke-RestMethod -Uri ($BaseUrl + $Path) -Method Post -ContentType "application/json" -Body $JsonBody -TimeoutSec $TimeoutSec
        $Timer.Stop()
        Save-Json -Object $Result -Path $Target
        $State = ""
        foreach ($Property in @("state", "product_state", "service", "status")) {
            if ($Result.PSObject.Properties.Name -contains $Property) { $State = [string]$Result.$Property; if ($State) { break } }
        }
        $EndpointRows.Add([pscustomobject]@{Name=$Name;Path=$Path;Success=$true;ElapsedMs=[int]$Timer.ElapsedMilliseconds;State=$State;Error=""})
        Write-Host ("[PASS] {0} - {1} ms {2}" -f $Name, $Timer.ElapsedMilliseconds, $State) -ForegroundColor Green
        return $Result
    } catch {
        $Timer.Stop(); $Message = $_.Exception.Message
        $Message | Set-Content -LiteralPath (Join-Path $ApiDir ($Safe + ".error.txt")) -Encoding UTF8
        $EndpointRows.Add([pscustomobject]@{Name=$Name;Path=$Path;Success=$false;ElapsedMs=[int]$Timer.ElapsedMilliseconds;State="";Error=$Message})
        Write-Host ("[FAIL] {0} - {1} ms - {2}" -f $Name, $Timer.ElapsedMilliseconds, $Message) -ForegroundColor Red
        return $null
    }
}

function Get-CandleCount {
    param([object]$Payload)
    if ($null -eq $Payload) { return 0 }

    foreach ($Name in @("candles", "data", "rows", "bars")) {
        if ($Payload.PSObject.Properties.Name -contains $Name) {
            $Value = $Payload.$Name
            if ($Value -is [System.Array]) { return @($Value).Count }
            if ($null -ne $Value -and $Value.PSObject.Properties.Name -contains "candles") {
                return @($Value.candles).Count
            }
        }
    }
    return 0
}

function Capture-Command {
    param(
        [string]$Name,
        [scriptblock]$Command
    )
    $Path = Join-Path $SystemDir ((Safe-Name $Name) + ".txt")
    try {
        & $Command 2>&1 | Out-String -Width 500 | Set-Content -LiteralPath $Path -Encoding UTF8
    } catch {
        $_.Exception.ToString() | Set-Content -LiteralPath $Path -Encoding UTF8
    }
}

Write-Host ""
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host " Project Laddu Operational Evidence Collector" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host ("Output: " + $OutDir)
Write-Host ""

Add-Summary ("Collected at: " + (Get-Date).ToString("o"))
Add-Summary ("Computer: " + $env:COMPUTERNAME)
Add-Summary ("Install directory: " + $InstallDir)
Add-Summary ("Evidence window: last " + $Minutes + " minutes")
Add-Summary ""

# ---------------------------------------------------------------------------
# 1. Service, process, port, task and host evidence
# ---------------------------------------------------------------------------
Capture-Command "service" {
    Get-Service ProjectLaddu -ErrorAction SilentlyContinue | Format-List *
    Get-CimInstance Win32_Service -Filter "Name='ProjectLaddu'" -ErrorAction SilentlyContinue |
        Select-Object Name, DisplayName, State, StartMode, ProcessId, PathName |
        Format-List
}
Capture-Command "port-8086" {
    Get-NetTCPConnection -LocalPort 8086 -ErrorAction SilentlyContinue |
        Select-Object State, LocalAddress, LocalPort, OwningProcess |
        Format-Table -AutoSize
}
Capture-Command "processes" {
    Get-Process -ErrorAction SilentlyContinue |
        Where-Object { $_.ProcessName -match "python|projectladdu|docker" } |
        Select-Object Id, ProcessName, StartTime, CPU, WorkingSet64, PagedMemorySize64, Path |
        Sort-Object WorkingSet64 -Descending |
        Format-Table -AutoSize
    $Pids = @(Get-NetTCPConnection -LocalPort 8086 -State Listen -ErrorAction SilentlyContinue |
        Select-Object -ExpandProperty OwningProcess -Unique)
    foreach ($PidValue in $Pids) {
        Get-CimInstance Win32_Process -Filter ("ProcessId=" + [int]$PidValue) -ErrorAction SilentlyContinue |
            Select-Object ProcessId, ParentProcessId, Name, ExecutablePath, CommandLine, CreationDate |
            Format-List
    }
}
Capture-Command "scheduled-tasks" {
    Get-ScheduledTask -ErrorAction SilentlyContinue |
        Where-Object { $_.TaskName -match "ProjectLaddu|Laddu" } |
        Select-Object TaskName, State, TaskPath |
        Format-Table -AutoSize
    Get-ScheduledTaskInfo -TaskName "ProjectLaddu-AI-Training" -ErrorAction SilentlyContinue | Format-List *
    Get-ScheduledTaskInfo -TaskName "ProjectLaddu-Model-Governance" -ErrorAction SilentlyContinue | Format-List *
    Get-ScheduledTaskInfo -TaskName "ProjectLaddu-Weekend-Research" -ErrorAction SilentlyContinue | Format-List *
    Get-ScheduledTaskInfo -TaskName "ProjectLaddu-NSE-Official-Data" -ErrorAction SilentlyContinue | Format-List *
}
Capture-Command "disk-space" {
    Get-PSDrive -PSProvider FileSystem |
        Select-Object Name, Root,
            @{n="UsedGB";e={[math]::Round($_.Used/1GB,2)}},
            @{n="FreeGB";e={[math]::Round($_.Free/1GB,2)}} |
        Format-Table -AutoSize
}
Capture-Command "docker-status" {
    docker version
    docker ps -a
    docker stats --no-stream
}

# ---------------------------------------------------------------------------
# 2. Built-in status and official installed-product verifier
# ---------------------------------------------------------------------------
$StatusScript = Join-Path $InstallDir "status.ps1"
$StatusOutput = Join-Path $OutDir "STATUS.txt"
if (Test-Path -LiteralPath $StatusScript) {
    $Lines = & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $StatusScript 2>&1
    $StatusExit = $LASTEXITCODE
    $Lines | Set-Content -LiteralPath $StatusOutput -Encoding UTF8
    Add-Summary ("STATUS exit code: " + $StatusExit)
} else {
    ("Missing: " + $StatusScript) | Set-Content -LiteralPath $StatusOutput -Encoding UTF8
    Add-Summary "STATUS script missing"
}

$Verifier = Join-Path $InstallDir "VERIFY_OPERATIONAL_PRODUCT.ps1"
$VerifierOutput = Join-Path $OutDir "VERIFY_OPERATIONAL_PRODUCT.txt"
if (Test-Path -LiteralPath $Verifier) {
    $Lines = & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $Verifier -FailOnBlocked 2>&1
    $VerifierExit = $LASTEXITCODE
    $Lines | Set-Content -LiteralPath $VerifierOutput -Encoding UTF8
    Add-Summary ("Official verifier exit code: " + $VerifierExit + " (0=mandatory checks passed, 2=blocked)")
} else {
    ("Missing: " + $Verifier) | Set-Content -LiteralPath $VerifierOutput -Encoding UTF8
    $VerifierExit = -1
    Add-Summary "Official verifier script missing"
}

# ---------------------------------------------------------------------------
# 3. Core API evidence
# ---------------------------------------------------------------------------
$Ready = Invoke-ApiEvidence "ready" "/api/ready" 15
$Health = Invoke-ApiEvidence "health" "/api/health" 20
$Product = Invoke-ApiEvidence "product-readiness" "/api/product-readiness" 20
$Architecture = Invoke-ApiEvidence "architecture" "/api/architecture" 20
$Scanner = Invoke-ApiEvidence "scanner-status" "/api/scanner/status" 20
$Pipeline = Invoke-ApiEvidence "pipeline-health" "/api/pipeline-health" 20
$CanonicalBars = Invoke-ApiEvidence "canonical-bars" "/api/canonical-bars" 20
$BindingMtf = Invoke-ApiEvidence "binding-mtf-contract" "/api/binding-mtf-contract" 20
$LiveMarket = Invoke-ApiEvidence "live-market-status" "/api/live-market/status" 20
$Storage = Invoke-ApiEvidence "storage-architecture" "/api/storage-architecture" 20
$Risk = Invoke-ApiEvidence "risk-authority" "/api/risk-authority" 20
$Quant = Invoke-ApiEvidence "quant-research-plane" "/api/quant-research-plane" 30
$Research = Invoke-ApiEvidence "research-libraries" "/api/research-libraries" 30
$Methods = Invoke-ApiEvidence "active-research-methods" "/api/active-research-methods" 30
$CoverageAggregate = Invoke-ApiEvidence "data-coverage-aggregate" "/api/data-coverage" 20
$Cards = Invoke-ApiEvidence "dashboard-cards" "/api/dashboard-cards?mode=all" 30
$Indices = Invoke-ApiEvidence "indices" "/api/indices" 20
$Heatmap = Invoke-ApiEvidence "market-heatmap" "/api/market/heatmap" 20
$Radar = Invoke-ApiEvidence "market-radar" "/api/market-radar" 20
$NseAuthority = Invoke-ApiEvidence "nse-data-authority" "/api/nse-data-authority" 30
$Level5 = Invoke-ApiEvidence "level5-forward-maturity" "/api/level5-forward-maturity" 30
$ModelLifecycle = Invoke-ApiEvidence "model-lifecycle" "/api/model-lifecycle" 30
$Performance = Invoke-ApiEvidence "performance" "/api/performance" 30
$PriorityRecovery = Invoke-ApiEvidence "priority-pipeline-recovery" "/api/priority-pipeline/recovery" 30
$Level5Matrix = Invoke-ApiEvidence "level5-evidence-matrix" "/api/level5-evidence-matrix" 30
$ResilienceDrill = Invoke-PostEvidence "level5-resilience-drill" "/api/validation/level5-resilience-drill" @{} 30

# Three bounded snapshots distinguish a progressing scanner from a frozen counter.
for ($Snapshot = 1; $Snapshot -le 3; $Snapshot++) {
    Invoke-ApiEvidence ("scanner-progress-" + $Snapshot) "/api/scanner/status" 20 | Out-Null
    if ($Snapshot -lt 3) { Start-Sleep -Seconds 3 }
}

# ---------------------------------------------------------------------------
# 4. Selected-stock/chart/MTF evidence for INFY and TCS
# ---------------------------------------------------------------------------
$SymbolSummary = New-Object System.Collections.Generic.List[object]

foreach ($SymbolRaw in $Symbols) {
    $Symbol = ([string]$SymbolRaw).Trim().ToUpperInvariant()
    if (-not $Symbol) { continue }

    Write-Host ""
    Write-Host ("--- Selected stock proof: " + $Symbol + " ---") -ForegroundColor Cyan

    $Coverage = Invoke-ApiEvidence ($Symbol + "-coverage") ("/api/data-coverage?symbol=" + [uri]::EscapeDataString($Symbol)) 30
    $History3m = Invoke-ApiEvidence ($Symbol + "-historical-3minute") ("/api/historical?symbol=" + [uri]::EscapeDataString($Symbol) + "&interval=3minute&refresh=false") 30
    $HistoryDay = Invoke-ApiEvidence ($Symbol + "-historical-day") ("/api/historical?symbol=" + [uri]::EscapeDataString($Symbol) + "&interval=day&refresh=false") 30
    $Mtf = Invoke-ApiEvidence ($Symbol + "-mtf") ("/api/mtf-trend?symbol=" + [uri]::EscapeDataString($Symbol) + "&refresh=false") 30
    $Intel = Invoke-ApiEvidence ($Symbol + "-stock-intelligence") ("/api/stock-intelligence?symbol=" + [uri]::EscapeDataString($Symbol) + "&mode=delivery&refresh=false") 30
    $HistoricalReadiness = Invoke-ApiEvidence ($Symbol + "-historical-readiness") ("/api/historical-readiness?symbol=" + [uri]::EscapeDataString($Symbol) + "&interval=day&years=10") 30
    $PriorityPipeline = Invoke-ApiEvidence ($Symbol + "-priority-pipeline") ("/api/priority-pipeline?symbol=" + [uri]::EscapeDataString($Symbol) + "&mode=delivery") 30
    $EvidenceSnapshot = Invoke-ApiEvidence ($Symbol + "-evidence-snapshot") ("/api/evidence-snapshot?symbol=" + [uri]::EscapeDataString($Symbol) + "&mode=delivery") 30
    $CrossPlane = Invoke-ApiEvidence ($Symbol + "-cross-plane-reconciliation") ("/api/cross-plane-reconciliation?symbol=" + [uri]::EscapeDataString($Symbol) + "&mode=delivery&interval=day") 30

    $Timeframes = 0
    $IndexedTimeframes = 0
    if ($null -ne $Coverage) {
        $Timeframes = @($Coverage.timeframes).Count
        $IndexedTimeframes = @($Coverage.timeframes | Where-Object { $_.indexed -eq $true }).Count
    }

    $MtfCount = 0
    if ($null -ne $Mtf) {
        foreach ($Name in @("timeframes", "frames", "mtf", "mtf_trend")) {
            if ($Mtf.PSObject.Properties.Name -contains $Name) {
                $MtfCount = @($Mtf.$Name).Count
                if ($MtfCount -gt 0) { break }
            }
        }
    }

    $IntelState = ""
    if ($null -ne $Intel) {
        if ($Intel.selected_stock_truth) {
            $IntelState = [string]$Intel.selected_stock_truth.data_status
        } elseif ($Intel.pipeline) {
            $IntelState = [string]$Intel.pipeline.state
        }
    }

    $SymbolSummary.Add([pscustomobject]@{
        Symbol = $Symbol
        CoverageTimeframes = $Timeframes
        IndexedTimeframes = $IndexedTimeframes
        Candles3m = Get-CandleCount $History3m
        CandlesDay = Get-CandleCount $HistoryDay
        MtfFrames = $MtfCount
        IntelligenceState = $IntelState
        PriorityState = if ($null -ne $PriorityPipeline) { [string]$PriorityPipeline.state } else { "" }
        SnapshotState = if ($null -ne $EvidenceSnapshot) { [string]$EvidenceSnapshot.state } else { "" }
        ReconciliationState = if ($null -ne $CrossPlane) { [string]$CrossPlane.state } else { "" }
    })
}

# ---------------------------------------------------------------------------
# 5. Manifests, inventories and retained-data evidence
# ---------------------------------------------------------------------------
$ManifestCandidates = @(
    (Join-Path $InstallDir "DEPLOY_MANIFEST.json"),
    (Join-Path $InstallDir "RELEASE_IDENTITY.json"),
    (Join-Path $InstallDir "data\manifests\market-lake.json"),
    (Join-Path $InstallDir "data\manifests\candle-catalog.json"),
    (Join-Path $InstallDir "data\manifests\nse_official\last-cycle.json"),
    (Join-Path $InstallDir "data\manifests\nse_official\collector-state.json"),
    (Join-Path $InstallDir "data\research\catalog.json"),
    (Join-Path $InstallDir "data\research\feature-store-state.json"),
    (Join-Path $InstallDir "runtime\research_runtime.json")
)

foreach ($Path in $ManifestCandidates) {
    if (Test-Path -LiteralPath $Path -PathType Leaf) {
        Copy-Item -LiteralPath $Path -Destination (Join-Path $ManifestDir (Split-Path $Path -Leaf)) -Force
    }
}

Capture-Command "installed-file-inventory" {
    Get-ChildItem -LiteralPath $InstallDir -Recurse -File -ErrorAction SilentlyContinue |
        Select-Object FullName, Length, LastWriteTime |
        Sort-Object FullName |
        Format-Table -AutoSize
}
Capture-Command "data-manifest-inventory" {
    Get-ChildItem -LiteralPath (Join-Path $InstallDir "data") -Recurse -File -ErrorAction SilentlyContinue |
        Where-Object { $_.Extension -in @(".json", ".parquet", ".duckdb") } |
        Select-Object FullName, Length, LastWriteTime |
        Sort-Object FullName |
        Format-Table -AutoSize
}
Capture-Command "validation-report-inventory" {
    Get-ChildItem -LiteralPath (Join-Path $InstallDir "logs\validation") -File -ErrorAction SilentlyContinue |
        Select-Object FullName, Length, LastWriteTime |
        Sort-Object LastWriteTime -Descending |
        Format-Table -AutoSize
}
Capture-Command "installer-evidence-inventory" {
    Get-ChildItem -LiteralPath "C:\Temp\ProjectLaddu" -Recurse -File -ErrorAction SilentlyContinue |
        Select-Object -First 300 FullName, Length, LastWriteTime |
        Sort-Object LastWriteTime -Descending |
        Format-Table -AutoSize
}

$ValidationDir = Join-Path $InstallDir "logs\validation"
if (Test-Path -LiteralPath $ValidationDir) {
    Get-ChildItem -LiteralPath $ValidationDir -File -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 10 |
        ForEach-Object {
            Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $ManifestDir $_.Name) -Force
        }
}

# ---------------------------------------------------------------------------
# 6. Recent application logs
# ---------------------------------------------------------------------------
$SourceLogRoot = Join-Path $InstallDir "logs"
$LogFiles = @()
if (Test-Path -LiteralPath $SourceLogRoot) {
    $LogFiles = @(Get-ChildItem -LiteralPath $SourceLogRoot -Recurse -File -ErrorAction SilentlyContinue |
        Where-Object {
            $_.Extension -in @(".log", ".txt", ".json") -and
            ($_.LastWriteTime -ge $Since -or $_.DirectoryName -match "\\validation$")
        } |
        Sort-Object FullName -Unique)
}

$LogInventory = $LogFiles |
    Select-Object FullName, Length, LastWriteTime
$LogInventory | Format-Table -AutoSize | Out-String -Width 500 |
    Set-Content -LiteralPath (Join-Path $LogDir "log-inventory.txt") -Encoding UTF8

foreach ($File in $LogFiles) {
    $Relative = $File.FullName.Substring($SourceLogRoot.Length).TrimStart("\")
    $DestName = Safe-Name ($Relative -replace "\\", "__")
    $Destination = Join-Path $LogDir $DestName

    if ($File.Extension -eq ".log" -or $File.Extension -eq ".txt") {
        try {
            Get-Content -LiteralPath $File.FullName -Tail 5000 -ErrorAction Stop |
                Set-Content -LiteralPath $Destination -Encoding UTF8
        } catch {
            ("Unable to read: " + $_.Exception.Message) |
                Set-Content -LiteralPath ($Destination + ".error.txt") -Encoding UTF8
        }
    } elseif ($File.Length -le 20MB) {
        Copy-Item -LiteralPath $File.FullName -Destination $Destination -Force
    }
}

# ---------------------------------------------------------------------------
# 7. Windows event evidence around service/runtime failures
# ---------------------------------------------------------------------------
Capture-Command "windows-system-events" {
    Get-WinEvent -FilterHashtable @{ LogName = "System"; StartTime = $Since } -ErrorAction SilentlyContinue |
        Where-Object {
            $_.ProviderName -eq "Service Control Manager" -or
            $_.Message -match "ProjectLaddu|Project Laddu|8086"
        } |
        Select-Object TimeCreated, Id, LevelDisplayName, ProviderName, Message |
        Format-List
}
Capture-Command "windows-application-events" {
    Get-WinEvent -FilterHashtable @{ LogName = "Application"; StartTime = $Since } -ErrorAction SilentlyContinue |
        Where-Object {
            $_.Message -match "ProjectLaddu|Project Laddu|python|uvicorn|8086"
        } |
        Select-Object TimeCreated, Id, LevelDisplayName, ProviderName, Message |
        Format-List
}

# ---------------------------------------------------------------------------
# 8. Final concise progress summary
# ---------------------------------------------------------------------------
Add-Summary ""
Add-Summary "=== Runtime summary ==="

if ($null -ne $Ready) {
    Add-Summary ("Version: " + [string]$Ready.version + "; ready=" + [string]$Ready.ready)
}
if ($null -ne $Health) {
    Add-Summary ("Service: " + [string]$Health.service + "; market_open=" + [string]$Health.market_open)
    if ($Health.instruments) {
        Add-Summary ("Instrument authority: " + [string]$Health.instruments.count + "; loaded=" + [string]$Health.instruments.loaded)
    }
    if ($Health.startup_phases) {
        Add-Summary ("Startup: " + [string]$Health.startup_phases.state +
            "; required_complete=" + [string]$Health.startup_phases.required_complete +
            "; optional_complete=" + [string]$Health.startup_phases.optional_complete)
    }
}
if ($null -ne $Product) {
    Add-Summary ("Product state: " + [string]$Product.product_state +
        "; acceptance=" + [string]$Product.installation_acceptance.state)
    if ($Product.customer_usefulness) {
        Add-Summary ("Customer usefulness: " + [string]$Product.customer_usefulness.state)
    }
}

$PassCount = @($EndpointRows | Where-Object { $_.Success }).Count
$FailCount = @($EndpointRows | Where-Object { -not $_.Success }).Count
Add-Summary ("API checks: PASS=" + $PassCount + "; FAIL=" + $FailCount)

$EndpointRows |
    Sort-Object Name |
    Export-Csv -LiteralPath (Join-Path $OutDir "endpoint-latency.csv") -NoTypeInformation -Encoding UTF8

$SymbolSummary |
    Export-Csv -LiteralPath (Join-Path $OutDir "selected-stock-summary.csv") -NoTypeInformation -Encoding UTF8

foreach ($Row in $SymbolSummary) {
    Add-Summary (("{0}: coverage={1}, indexed={2}, 3m candles={3}, day candles={4}, MTF frames={5}, intelligence={6}" -f
        $Row.Symbol, $Row.CoverageTimeframes, $Row.IndexedTimeframes,
        $Row.Candles3m, $Row.CandlesDay, $Row.MtfFrames, $Row.IntelligenceState))
}

Add-Summary ""
Add-Summary "Interpretation:"
Add-Summary "- Official verifier exit 0 means mandatory installed-product checks passed."
Add-Summary "- API failures/timeouts or version mismatch require review."
Add-Summary "- On weekends/market-closed periods, live quote advancement and Intraday promotion cannot be proven."
Add-Summary "- Re-run this same collector during market hours to prove live subscriptions, current candles and scanner progression."

$Summary | Set-Content -LiteralPath (Join-Path $OutDir "SUMMARY.txt") -Encoding UTF8

$ZipPath = Join-Path $EvidenceBase ("ProjectLaddu-Operational-Evidence-" + $Stamp + ".zip")
if (Test-Path -LiteralPath $ZipPath) {
    Remove-Item -LiteralPath $ZipPath -Force
}
Compress-Archive -Path (Join-Path $OutDir "*") -DestinationPath $ZipPath -CompressionLevel Optimal -Force

Write-Host ""
Write-Host "============================================================" -ForegroundColor Green
Write-Host " Evidence collection complete" -ForegroundColor Green
Write-Host "============================================================" -ForegroundColor Green
Write-Host ("Folder: " + $OutDir)
Write-Host ("ZIP:    " + $ZipPath) -ForegroundColor Cyan
Write-Host ""
Write-Host "Upload the ZIP here for review." -ForegroundColor Yellow
