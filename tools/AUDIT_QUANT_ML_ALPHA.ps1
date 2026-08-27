param(
    [string]$BaseUrl = "http://127.0.0.1:8086",
    [string]$Symbol = "INFY",
    [int]$TimeoutSec = 180,
    [int]$PerformanceSamples = 3,
    [int]$SimulationPaths = 5000,
    [switch]$SkipWalkForward
)

$ErrorActionPreference = "Continue"
$ProgressPreference = "SilentlyContinue"

$Symbol = $Symbol.Trim().ToUpperInvariant()
$Stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$Root = "C:\Temp\ProjectLaddu"
$InstallDir = Split-Path -Parent $PSScriptRoot
$ReleaseIdentity = Get-Content -LiteralPath (Join-Path $InstallDir "RELEASE_IDENTITY.json") -Raw | ConvertFrom-Json
$ExpectedVersion = [string]$ReleaseIdentity.version
$OutDir = Join-Path $Root ("Quant-Success-Audit-" + $Stamp)
$ApiDir = Join-Path $OutDir "api"
$PerfDir = Join-Path $OutDir "performance"
New-Item -ItemType Directory -Force -Path $Root, $OutDir, $ApiDir, $PerfDir | Out-Null

$EndpointRows = New-Object System.Collections.Generic.List[object]
$Summary = New-Object System.Collections.Generic.List[string]

function Safe-Name([string]$Text) {
    return (($Text -replace '[^A-Za-z0-9._-]', '_').Trim('_'))
}

function Add-Line([string]$Text = "") {
    $Summary.Add($Text)
    Write-Host $Text
}

function Save-Json($Value, [string]$Path) {
    try {
        $Value | ConvertTo-Json -Depth 100 | Set-Content -LiteralPath $Path -Encoding UTF8
    } catch {
        $_.Exception.ToString() | Set-Content -LiteralPath ($Path + ".error.txt") -Encoding UTF8
    }
}

function Get-Api {
    param(
        [string]$Name,
        [string]$Path,
        [int]$Timeout = $TimeoutSec
    )

    $Timer = [Diagnostics.Stopwatch]::StartNew()
    try {
        $Result = Invoke-RestMethod -Uri ($BaseUrl + $Path) -Method Get -TimeoutSec $Timeout
        $Timer.Stop()
        Save-Json $Result (Join-Path $ApiDir ((Safe-Name $Name) + ".json"))
        $EndpointRows.Add([pscustomobject]@{
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
        $Message | Set-Content -LiteralPath (Join-Path $ApiDir ((Safe-Name $Name) + ".error.txt")) -Encoding UTF8
        $EndpointRows.Add([pscustomobject]@{
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

function Get-PropertyValue {
    param(
        [object]$Object,
        [string[]]$Path
    )
    $Current = $Object
    foreach ($Part in $Path) {
        if ($null -eq $Current) { return $null }
        $Property = $Current.PSObject.Properties[$Part]
        if ($null -eq $Property) { return $null }
        $Current = $Property.Value
    }
    return $Current
}

function As-Bool($Value) {
    return ($Value -eq $true -or [string]$Value -match '^(?i:true|ready|accepted|operational|passed|approved)$')
}

function As-Number($Value) {
    try { return [double]$Value } catch { return $null }
}

function Percentile {
    param([double[]]$Values, [double]$P)
    if (-not $Values -or $Values.Count -eq 0) { return $null }
    $Sorted = @($Values | Sort-Object)
    $Index = [Math]::Ceiling($P * $Sorted.Count) - 1
    $Index = [Math]::Max(0, [Math]::Min($Sorted.Count - 1, $Index))
    return [double]$Sorted[$Index]
}

function Measure-Endpoint {
    param(
        [string]$Name,
        [string]$Path,
        [int]$Samples = $PerformanceSamples,
        [int]$Timeout = $TimeoutSec
    )
    $Times = New-Object System.Collections.Generic.List[double]
    $Failures = 0
    for ($i = 1; $i -le [Math]::Max(1, $Samples); $i++) {
        $Timer = [Diagnostics.Stopwatch]::StartNew()
        try {
            Invoke-RestMethod -Uri ($BaseUrl + $Path) -Method Get -TimeoutSec $Timeout | Out-Null
            $Timer.Stop()
            $Times.Add([double]$Timer.ElapsedMilliseconds)
        } catch {
            $Timer.Stop()
            $Failures++
        }
    }
    $P50 = Percentile -Values @($Times) -P 0.50
    $P95 = Percentile -Values @($Times) -P 0.95
    $Result = [pscustomobject]@{
        Name = $Name
        Path = $Path
        Samples = $Samples
        Successes = $Times.Count
        Failures = $Failures
        MinMs = if ($Times.Count) { [Math]::Round(($Times | Measure-Object -Minimum).Minimum, 1) } else { $null }
        P50Ms = if ($null -ne $P50) { [Math]::Round($P50, 1) } else { $null }
        P95Ms = if ($null -ne $P95) { [Math]::Round($P95, 1) } else { $null }
        MaxMs = if ($Times.Count) { [Math]::Round(($Times | Measure-Object -Maximum).Maximum, 1) } else { $null }
    }
    return $Result
}

function Failed-Gates($Validation) {
    if ($null -eq $Validation) { return @("validation unavailable") }
    $Gates = Get-PropertyValue $Validation @("gates")
    if ($null -eq $Gates) { return @("gates unavailable") }
    $Failed = @()
    foreach ($Prop in $Gates.PSObject.Properties) {
        if ($Prop.Value -ne $true) { $Failed += $Prop.Name }
    }
    return $Failed
}

function Write-ArmSummary {
    param(
        [string]$Desk,
        [string]$Arm,
        [object]$ArmData
    )
    $V = Get-PropertyValue $ArmData @("validation")
    if ($null -eq $V) {
        Add-Line ("- {0}/{1}: unavailable" -f $Desk, $Arm)
        return
    }
    $Approved = Get-PropertyValue $V @("approved")
    $Status = [string](Get-PropertyValue $V @("status"))
    $N = Get-PropertyValue $V @("n_test")
    $Days = Get-PropertyValue $V @("n_test_days")
    $Symbols = Get-PropertyValue $V @("universe_symbols")
    $Mean = As-Number (Get-PropertyValue $V @("mean_net_return"))
    $Excess = As-Number (Get-PropertyValue $V @("mean_excess_return"))
    $Win = As-Number (Get-PropertyValue $V @("win_rate"))
    $PF = As-Number (Get-PropertyValue $V @("profit_factor"))
    $Sharpe = As-Number (Get-PropertyValue $V @("sharpe"))
    $Sortino = As-Number (Get-PropertyValue $V @("sortino"))
    $DD = As-Number (Get-PropertyValue $V @("max_drawdown"))
    $Stability = As-Number (Get-PropertyValue $V @("fold_stability"))
    $DSR = As-Number (Get-PropertyValue $V @("deflated_sharpe_probability"))
    $PValue = As-Number (Get-PropertyValue $V @("multiple_test_adjusted_pvalue"))
    $Failed = Failed-Gates $V

    $MeanBps = if ($null -ne $Mean) { [Math]::Round($Mean * 10000, 2) } else { $null }
    $ExcessBps = if ($null -ne $Excess) { [Math]::Round($Excess * 10000, 2) } else { $null }
    $WinPct = if ($null -ne $Win) { [Math]::Round($Win * 100, 1) } else { $null }
    $DDPct = if ($null -ne $DD) { [Math]::Round($DD * 100, 1) } else { $null }
    $StabilityPct = if ($null -ne $Stability) { [Math]::Round($Stability * 100, 1) } else { $null }
    $DSRPct = if ($null -ne $DSR) { [Math]::Round($DSR * 100, 1) } else { $null }

    Add-Line ("- {0}/{1}: {2}; approved={3}; samples={4}; days={5}; symbols={6}" -f
        $Desk, $Arm, $Status, $Approved, $N, $Days, $Symbols)
    Add-Line ("  post-cost expectancy={0} bps; benchmark excess/alpha={1} bps; win={2}%; PF={3}; Sharpe={4}; Sortino={5}; max DD={6}%" -f
        $MeanBps, $ExcessBps, $WinPct, $PF, $Sharpe, $Sortino, $DDPct)
    Add-Line ("  fold stability={0}%; deflated-Sharpe probability={1}%; adjusted p={2}" -f
        $StabilityPct, $DSRPct, $PValue)
    if ($Failed.Count -gt 0) {
        Add-Line ("  failed gates: " + ($Failed -join ", "))
    }
}

Write-Host ""
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host " Project Laddu Operational, Quant, ML and Alpha Audit" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host ("Output: " + $OutDir)
Write-Host ""

# ---------------------------------------------------------------------------
# 1. Operational and authority truth
# ---------------------------------------------------------------------------
$Ready = Get-Api "ready" "/api/ready" 20
$Health = Get-Api "health" "/api/health" 45
$Product = Get-Api "product-readiness" "/api/product-readiness" 45
$Engineering = Get-Api "engineering-quality" "/api/engineering-quality" 45
$Capital = Get-Api "capital-readiness" "/api/capital-readiness" 45
$Risk = Get-Api "risk-authority" "/api/risk-authority" 45
$Scanner = Get-Api "scanner-status" "/api/scanner/status" 45
$Storage = Get-Api "storage-architecture" "/api/storage-architecture" 45
$QuantPlane = Get-Api "quant-research-plane" "/api/quant-research-plane" 45

# ---------------------------------------------------------------------------
# 2. Mathematical and evidence authority
# ---------------------------------------------------------------------------
$Strategy = Get-Api "strategy-validation" "/api/strategy-validation" 60
$EvidenceIntraday = Get-Api "evidence-score-intraday" "/api/evidence-score-validation?mode=intraday" 90
$EvidenceDelivery = Get-Api "evidence-score-delivery" "/api/evidence-score-validation?mode=delivery" 90
$BindingMtf = Get-Api "binding-mtf-contract" "/api/binding-mtf-contract" 30
$CostModel = Get-Api "cost-model" "/api/cost-model" 30
$WinrateControls = Get-Api "winrate-controls" "/api/winrate-controls" 45
$FactorDedup = Get-Api "factor-dedup" "/api/factor-dedup" 45
$Counterfactual = Get-Api "counterfactual-learning" "/api/counterfactual-learning" 45

# ---------------------------------------------------------------------------
# 3. Settled portfolio and live paper performance
# ---------------------------------------------------------------------------
$Performance = Get-Api "performance-summary" "/api/performance/summary" 60
$Institutional = Get-Api "institutional-performance" "/api/institutional-performance" 60
$ModelPortfolio = Get-Api "model-portfolio" "/api/model-portfolio" 60
$Daily = Get-Api "daily-performance" "/api/daily-performance?start=2025-01-01&end=2099-12-31" 60

# ---------------------------------------------------------------------------
# 4. Model and ML governance
# ---------------------------------------------------------------------------
$Tournament = Get-Api "model-tournament-all" "/api/model-tournament" 60
$TournamentIntra = Get-Api "model-tournament-intraday" "/api/model-tournament?desk=intraday" 60
$TournamentDelivery = Get-Api "model-tournament-delivery" "/api/model-tournament?desk=delivery" 60
$SelectionIntra = Get-Api "selection-platform-intraday" "/api/selection-platform?desk=intraday" 60
$SelectionDelivery = Get-Api "selection-platform-delivery" "/api/selection-platform?desk=delivery" 60
$ResearchValidationIntra = Get-Api "selection-research-validation-intraday" "/api/selection-research-validation?desk=intraday&horizon=30m" 90
$ResearchValidationDelivery = Get-Api "selection-research-validation-delivery" "/api/selection-research-validation?desk=delivery&horizon=10d" 90
$ChallengerIntra = Get-Api "calibrated-challenger-intraday" "/api/calibrated-challenger/status?desk=intraday&horizon=30m" 90
$ChallengerDelivery = Get-Api "calibrated-challenger-delivery" "/api/calibrated-challenger/status?desk=delivery&horizon=10d" 90
$QuantEdge = Get-Api "quant-edge-status" "/api/quant-edge/status" 90
$QuantPaper = Get-Api "quant-paper-status" "/api/quant-edge/paper-status" 90
$ResearchMaturity = Get-Api "research-maturity" "/api/research-maturity" 90
$ModelGovernance = Get-Api "quant-model-governance" "/api/quant-model-governance" 60
$AiGovernance = Get-Api "ai-governance" "/api/ai/governance" 60

# ---------------------------------------------------------------------------
# 5. Backtest and robustness
# ---------------------------------------------------------------------------
$WfIntra = $null
$WfDelivery = $null
if (-not $SkipWalkForward) {
    $WfIntra = Get-Api "walk-forward-intraday-capital" `
        "/api/selection-walk-forward-replay?desk=intraday&horizon=30m&top_fraction=0.20&min_train_days=252&test_days=63&max_folds=8&embargo_days=1&min_samples=300&profile=capital" 300
    $WfDelivery = Get-Api "walk-forward-delivery-capital" `
        "/api/selection-walk-forward-replay?desk=delivery&horizon=10d&top_fraction=0.20&min_train_days=252&test_days=63&max_folds=8&embargo_days=1&min_samples=300&profile=capital" 300
}
$SimIntra = Get-Api "simulation-intraday" `
    ("/api/simulation-robustness?desk=intraday&paths=" + $SimulationPaths + "&horizon=100&seed=7") 300
$SimDelivery = Get-Api "simulation-delivery" `
    ("/api/simulation-robustness?desk=delivery&paths=" + $SimulationPaths + "&horizon=100&seed=7") 300

# ---------------------------------------------------------------------------
# 6. Installed-product latency / performance
# ---------------------------------------------------------------------------
$Encoded = [uri]::EscapeDataString($Symbol)
$Latency = @(
    Measure-Endpoint "ready" "/api/ready" $PerformanceSamples 30
    Measure-Endpoint "scanner-status" "/api/scanner/status" $PerformanceSamples 60
    Measure-Endpoint "dashboard-cards" "/api/dashboard-cards?mode=all" $PerformanceSamples 90
    Measure-Endpoint "performance-summary" "/api/performance/summary" $PerformanceSamples 60
    Measure-Endpoint "$Symbol-coverage" ("/api/data-coverage?symbol=" + $Encoded) $PerformanceSamples 60
    Measure-Endpoint "$Symbol-history-3m" ("/api/historical?symbol=" + $Encoded + "&interval=3minute&refresh=false") $PerformanceSamples 90
    Measure-Endpoint "$Symbol-history-day" ("/api/historical?symbol=" + $Encoded + "&interval=day&refresh=false") $PerformanceSamples 90
    Measure-Endpoint "$Symbol-mtf" ("/api/mtf-trend?symbol=" + $Encoded + "&refresh=false") $PerformanceSamples 90
    Measure-Endpoint "$Symbol-intelligence" ("/api/stock-intelligence?symbol=" + $Encoded + "&mode=delivery&refresh=false") $PerformanceSamples 120
)
$Latency | Export-Csv -LiteralPath (Join-Path $PerfDir "latency.csv") -NoTypeInformation -Encoding UTF8
Save-Json $Latency (Join-Path $PerfDir "latency.json")

# ---------------------------------------------------------------------------
# 7. Human-readable verdict
# ---------------------------------------------------------------------------
Add-Line "# Project Laddu Quant Success Audit"
Add-Line ""
Add-Line ("Collected: " + (Get-Date).ToString("o"))
Add-Line ("Symbol used for latency proof: " + $Symbol)
Add-Line ""

$Version = [string](Get-PropertyValue $Ready @("version"))
$ReadyState = Get-PropertyValue $Ready @("ready")
$ProductState = [string](Get-PropertyValue $Product @("product_state"))
$Acceptance = [string](Get-PropertyValue $Product @("installation_acceptance","state"))
Add-Line "## 1. Operational"
Add-Line ("- Version: {0}" -f $Version)
Add-Line ("- Process ready: {0}" -f $ReadyState)
Add-Line ("- Product state: {0}" -f $ProductState)
Add-Line ("- Installation acceptance: {0}" -f $Acceptance)
$OperationalPass = (
    $Version -eq $ExpectedVersion -and
    $ReadyState -eq $true -and
    $ProductState -eq "OPERATIONAL" -and
    $Acceptance -eq "ACCEPTED"
)
Add-Line ("- Operational verdict: " + $(if ($OperationalPass) { "PASS" } else { "FAIL / REVIEW" }))
Add-Line ""

Add-Line "## 2. Engineering performance"
foreach ($Row in $Latency) {
    Add-Line ("- {0}: p50={1} ms; p95={2} ms; failures={3}" -f $Row.Name, $Row.P50Ms, $Row.P95Ms, $Row.Failures)
}
Add-Line ""
Add-Line "Acceptance targets:"
Add-Line "- readiness/scanner/dashboard p95: preferably <500 ms"
Add-Line "- indexed coverage p95: <250 ms"
Add-Line "- cached chart history p95: <500 ms"
Add-Line "- cached MTF p95: <1,000 ms"
Add-Line "- Stock Intelligence p95: <2,000 ms"
Add-Line ""

Add-Line "## 3. Settled performance"
$AccuracyState = [string](Get-PropertyValue $Performance @("accuracy_state"))
$Closed = Get-PropertyValue $Performance @("closed")
Add-Line ("- Accuracy state: {0}" -f $AccuracyState)
Add-Line ("- Settled outcomes: {0}" -f $Closed)
if ([int]($Closed -as [int]) -eq 0) {
    Add-Line "- Verdict: NO EMPIRICAL PERFORMANCE CLAIM YET. Installation success is not trading success."
} else {
    Add-Line "- Review by_mode, by_day, by_month and by_year in api/performance-summary.json."
}
Add-Line ""

Add-Line "## 4. Purged walk-forward alpha/backtest"
if ($SkipWalkForward) {
    Add-Line "- Skipped by operator."
} else {
    foreach ($Pair in @(
        @("Intraday", $WfIntra),
        @("Delivery", $WfDelivery)
    )) {
        $Desk = $Pair[0]
        $Report = $Pair[1]
        if ($null -eq $Report) {
            Add-Line ("- {0}: unavailable" -f $Desk)
            continue
        }
        foreach ($Arm in @("heuristic","quant","hybrid")) {
            $ArmData = Get-PropertyValue $Report @("arms",$Arm)
            Write-ArmSummary $Desk $Arm $ArmData
        }
    }
}
Add-Line ""
Add-Line "A backtest is accepted only when the capital-profile gates pass:"
Add-Line "- at least 5 walk-forward folds, 300 out-of-sample observations and 25 symbols"
Add-Line "- positive post-cost expectancy and positive benchmark excess"
Add-Line "- at least 70% positive folds and profit factor >=1.10"
Add-Line "- moving-block bootstrap lower bound >0"
Add-Line "- HAC net and excess t-statistics >=1.645"
Add-Line "- deflated-Sharpe probability >=95% and multiple-test-adjusted p <=0.05"
Add-Line "- maximum drawdown no worse than -25%"
Add-Line "- complete costs, benchmarks, three baselines, lineage, point-in-time, corporate-action and survivorship evidence"
Add-Line "- zero look-ahead violations"
Add-Line ""
Add-Line "Passing backtest authorizes shadow/automatic paper evaluation only. It does not authorize broker orders."
Add-Line ""

Add-Line "## 5. ML model success"
foreach ($Pair in @(
    @("Intraday", $ChallengerIntra),
    @("Delivery", $ChallengerDelivery)
)) {
    $Desk = $Pair[0]
    $Data = $Pair[1]
    $State = [string](Get-PropertyValue $Data @("state"))
    if (-not $State) { $State = [string](Get-PropertyValue $Data @("validation","state")) }
    $Eligible = Get-PropertyValue $Data @("eligible")
    $DecisionWeight = Get-PropertyValue $Data @("decision_weight")
    Add-Line ("- {0}: state={1}; eligible={2}; decision_weight={3}" -f $Desk, $State, $Eligible, $DecisionWeight)
}
Add-Line ""
Add-Line "Minimum ML evidence:"
Add-Line "- 340 total observations"
Add-Line "- 300 development observations over at least 126 trading days"
Add-Line "- untouched holdout: at least 40 observations over at least 20 trading days"
Add-Line "- at least 3 market regimes and 3 purged walk-forward folds"
Add-Line "- Brier score better than prevalence baseline; AUC >0.50"
Add-Line "- positive top-quintile lift"
Add-Line "- return MAE better than mean baseline and positive return top-quintile lift"
Add-Line "- at least 60% development-fold stability"
Add-Line "- untouched chronological holdout and all governance gates passed"
Add-Line ""

Add-Line "## 6. Forward-paper and alpha verdict"
$PaperState = [string](Get-PropertyValue $QuantPaper @("state"))
$LiveWeight = Get-PropertyValue $QuantPaper @("live_production_weight")
$BrokerWeight = Get-PropertyValue $QuantPaper @("broker_execution_weight")
$ForwardGate = Get-PropertyValue $QuantPaper @("forward_edge_claim_gate")
Add-Line ("- Quant paper state: {0}" -f $PaperState)
Add-Line ("- Live production weight: {0}" -f $LiveWeight)
Add-Line ("- Broker execution weight: {0}" -f $BrokerWeight)
if ($null -ne $ForwardGate) {
    Add-Line ("- Forward edge claim gate: " + (($ForwardGate | ConvertTo-Json -Compress -Depth 10)))
}
Add-Line ""
Add-Line "Final rule:"
Add-Line "- OPERATIONAL means the system runs correctly."
Add-Line "- BACKTEST_APPROVED means historical out-of-sample evidence passed strict gates."
Add-Line "- FORWARD-PAPER PROVEN means independent future outcomes remain positive after costs and drift review."
Add-Line "- ALPHA PROVEN requires positive benchmark excess with statistical support, not merely a high win rate."
Add-Line "- Until those stages pass, production and broker weight must remain zero."
Add-Line ""

$EndpointRows | Export-Csv -LiteralPath (Join-Path $OutDir "endpoint-results.csv") -NoTypeInformation -Encoding UTF8
$Summary | Set-Content -LiteralPath (Join-Path $OutDir "SUMMARY.md") -Encoding UTF8

$Zip = Join-Path $Root ("ProjectLaddu-Quant-Success-Audit-" + $Stamp + ".zip")
if (Test-Path -LiteralPath $Zip) { Remove-Item -LiteralPath $Zip -Force }
Compress-Archive -Path (Join-Path $OutDir "*") -DestinationPath $Zip -CompressionLevel Optimal -Force

Write-Host ""
Write-Host "============================================================" -ForegroundColor Green
Write-Host " Quant success audit complete" -ForegroundColor Green
Write-Host "============================================================" -ForegroundColor Green
Write-Host ("Report: " + (Join-Path $OutDir "SUMMARY.md"))
Write-Host ("ZIP:    " + $Zip) -ForegroundColor Cyan
Write-Host ""
Write-Host "Upload the ZIP here for independent review." -ForegroundColor Yellow
