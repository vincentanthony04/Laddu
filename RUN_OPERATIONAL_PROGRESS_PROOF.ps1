param(
  [int]$Port = 8086,
  [int]$Samples = 6,
  [int]$IntervalSeconds = 30
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0
$base = "http://127.0.0.1:$Port"
$root = 'C:\Temp\ProjectLaddu\Operational-Progress-Proof'
New-Item -ItemType Directory -Path $root -Force | Out-Null
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$work = Join-Path $root $stamp
New-Item -ItemType Directory -Path $work -Force | Out-Null
$reportPath = Join-Path $work 'operational-progress-proof.json'
$zipPath = Join-Path $root ("ProjectLaddu-Operational-Progress-Proof-$stamp.zip")
$shaPath = "$zipPath.sha256"

function Prop([object]$Object,[string]$Name) {
  if($null -eq $Object){return $null}
  if($Object -is [System.Collections.IDictionary]) {
    if($Object.Contains($Name)){return $Object[$Name]}
    return $null
  }
  $p=$Object.PSObject.Properties[$Name]; if($null -eq $p){return $null}; return $p.Value
}
function Arr([object]$Value) { if($null -eq $Value){return @()}; return @($Value) }
function To-Int([object]$Value) { try { return [int]$Value } catch { return 0 } }
function Add-Finding([System.Collections.Generic.List[object]]$List,[string]$Level,[string]$Code,[string]$Detail) {
  $List.Add([ordered]@{level=$Level;code=$Code;detail=$Detail})
}
function Get-CoreState {
  $attemptErrors = New-Object System.Collections.Generic.List[string]
  for($attempt=1; $attempt -le 3; $attempt++) {
    try {
      $value = Invoke-RestMethod -UseBasicParsing -Method Get -Uri ($base+'/api/product-state') -TimeoutSec 5
      if([string]::IsNullOrWhiteSpace([string](Prop $value 'snapshot_id'))){ throw 'Product State returned no snapshot_id' }
      return $value
    } catch {
      $attemptErrors.Add($_.Exception.Message)
      if($attempt -lt 3){ Start-Sleep -Milliseconds 750 }
    }
  }
  throw ("Canonical /api/product-state unavailable after 3 bounded attempts: {0}" -f ($attemptErrors -join ' | '))
}
function Find-Job([object]$Operations,[string[]]$Ids) {
  foreach($row in (Arr (Prop $Operations 'jobs'))) {
    $jid=[string](Prop $row 'job_id'); $component=[string](Prop $row 'component')
    if($Ids -contains $jid -or $Ids -contains $component){return $row}
  }
  return $null
}

Write-Host 'Project Laddu - Operational Progress Self-Proof'
Write-Host 'Read-only observation. No scanner/model/database/broker authority is changed.'
Write-Host 'Core authority: cache-only /api/product-state'
Write-Host ("Evidence root: {0}" -f $root)
Write-Host ("Sampling {0} snapshots every {1}s" -f $Samples,$IntervalSeconds)

$sampleRows = New-Object System.Collections.Generic.List[object]
$sampleCount = [Math]::Max(2,$Samples)
for($i=0; $i -lt $sampleCount; $i++) {
  Write-Host ("[{0}/{1}] Capturing canonical useful-progress snapshot" -f ($i+1),$sampleCount)
  $core = Get-CoreState
  $operations = Prop $core 'operations'
  $deliveryJob = Find-Job $operations @('loop:delivery_scanner','delivery_scanner')
  $historyJob = Find-Job $operations @('loop:deep_history_backfill','deep_history_backfill','data:deep-history')
  $dataJob = Find-Job $operations @('loop:data_conveyor','data_conveyor')
  $forwardJob = Find-Job $operations @('loop:forward_evidence_lifecycle','forward_evidence_lifecycle')
  $controllerJob = Find-Job $operations @('loop:autonomic_controller','autonomic_controller')
  $deliveryDesk = Prop (Prop $core 'desks') 'delivery'
  $intradayDesk = Prop (Prop $core 'desks') 'intraday'
  $deliveryScanner = Prop $deliveryDesk 'scanner'
  $deliveryResearch = Prop $deliveryDesk 'research'
  $intradayResearch = Prop $intradayDesk 'research'
  $row=[ordered]@{
    sample=$i
    captured_at=(Get-Date).ToString('o')
    snapshot_id=Prop $core 'snapshot_id'
    business_signature=Prop $core 'business_signature'
    snapshot_age_sec=Prop $core 'snapshot_age_sec'
    stale=Prop $core 'stale'
    product_state=Prop $core 'state'
    primary_blocker=Prop $core 'primary_blocker'
    delivery=[ordered]@{
      state=Prop $deliveryScanner 'state'; attempted=Prop $deliveryScanner 'attempted'; total=Prop $deliveryScanner 'universe'; analysed=Prop $deliveryScanner 'analysed'; last_progress_at=Prop $deliveryScanner 'last_progress_at'; waiting_on=Prop $deliveryScanner 'waiting_on'
    }
    research=[ordered]@{
      delivery=[ordered]@{state=Prop $deliveryResearch 'state';population=Prop $deliveryResearch 'population';features=Prop $deliveryResearch 'features';baseline=Prop $deliveryResearch 'baseline';ml=Prop $deliveryResearch 'ml';hybrid=Prop $deliveryResearch 'hybrid';paper=Prop $deliveryResearch 'paper';settled=Prop $deliveryResearch 'settled';next_action=Prop $deliveryResearch 'next_action'}
      intraday=[ordered]@{state=Prop $intradayResearch 'state';population=Prop $intradayResearch 'population';features=Prop $intradayResearch 'features';baseline=Prop $intradayResearch 'baseline';ml=Prop $intradayResearch 'ml';hybrid=Prop $intradayResearch 'hybrid';paper=Prop $intradayResearch 'paper';settled=Prop $intradayResearch 'settled';next_action=Prop $intradayResearch 'next_action'}
    }
    maturity=Prop $core 'maturity'
    operations=[ordered]@{state=Prop $operations 'state';counts=Prop $operations 'counts';primary_blocker=Prop $operations 'primary_blocker';controller=Prop $operations 'controller'}
    jobs=[ordered]@{
      delivery=if($null -ne $deliveryJob){$deliveryJob}else{$null}
      history=if($null -ne $historyJob){$historyJob}else{$null}
      data_conveyor=if($null -ne $dataJob){$dataJob}else{$null}
      forward_evidence=if($null -ne $forwardJob){$forwardJob}else{$null}
      controller=if($null -ne $controllerJob){$controllerJob}else{$null}
    }
    canonical=$core
  }
  $sampleRows.Add($row)
  $row | ConvertTo-Json -Depth 100 | Set-Content -LiteralPath (Join-Path $work ("sample-{0:D2}.json" -f $i)) -Encoding UTF8
  if($i -lt ($sampleCount-1)){ Start-Sleep -Seconds ([Math]::Max(5,$IntervalSeconds)) }
}

$findings = New-Object System.Collections.Generic.List[object]
$first=$sampleRows[0]; $last=$sampleRows[$sampleRows.Count-1]
$firstD=To-Int (Prop (Prop $first 'delivery') 'attempted'); $lastD=To-Int (Prop (Prop $last 'delivery') 'attempted'); $dTotal=To-Int (Prop (Prop $last 'delivery') 'total')
$dState=[string](Prop (Prop $last 'delivery') 'state'); $dWait=[string](Prop (Prop $last 'delivery') 'waiting_on')
$deliveryIncomplete=($dTotal -gt 0 -and $lastD -lt $dTotal)
$deliveryAdvanced=($lastD -gt $firstD)
if($deliveryIncomplete -and -not $deliveryAdvanced -and $dState -notmatch 'WAIT|PAUSED|BLOCKED|FAILED|STUCK|NO_PROGRESS') {
  Add-Finding $findings 'FAIL' 'DELIVERY_SILENT_NO_PROGRESS' ("Delivery remained {0}/{1}; state={2}; waiting_on={3}" -f $lastD,$dTotal,$dState,$dWait)
} elseif($deliveryAdvanced) {
  Add-Finding $findings 'PASS' 'DELIVERY_ADVANCED' ("Delivery advanced {0}->{1}/{2}" -f $firstD,$lastD,$dTotal)
} else {
  Add-Finding $findings 'PASS' 'DELIVERY_EXPLICIT_STATE' ("Delivery {0}/{1}; state={2}; waiting_on={3}" -f $lastD,$dTotal,$dState,$dWait)
}

$lastOps=Prop $last 'operations'; $counts=Prop $lastOps 'counts'; $attention=0
foreach($key in @('FAILED','STUCK','CIRCUIT_OPEN','NO_PROGRESS','UNINSTRUMENTED')){$attention += To-Int (Prop $counts $key)}
$corePrimary=Prop $last 'primary_blocker'; $occPrimary=Prop $lastOps 'primary_blocker'
if($attention -gt 0 -and $null -eq $corePrimary){
  Add-Finding $findings 'FAIL' 'ATTENTION_WITHOUT_CANONICAL_BLOCKER' ("{0} actionable/stalled job(s), but canonical primary blocker is empty" -f $attention)
} elseif(([string](Prop $corePrimary 'key')) -ne ([string](Prop $occPrimary 'key'))) {
  Add-Finding $findings 'FAIL' 'SYSTEM_OCC_BLOCKER_DIVERGENCE' ("canonical={0}; occ={1}" -f ([string](Prop $corePrimary 'key')),([string](Prop $occPrimary 'key')))
} else {
  Add-Finding $findings 'PASS' 'SINGLE_BLOCKER_TRUTH' ("attention={0}; primary={1}" -f $attention,([string](Prop $corePrimary 'key')))
}

foreach($desk in @('delivery','intraday')) {
  $r=Prop (Prop $last 'research') $desk; $state=[string](Prop $r 'state'); $next=[string](Prop $r 'next_action')
  $hybrid=To-Int (Prop $r 'hybrid'); $paper=To-Int (Prop $r 'paper')
  if($hybrid -gt 0 -and $paper -eq 0 -and [string]::IsNullOrWhiteSpace($next)) {
    Add-Finding $findings 'FAIL' ("{0}_PAPER_ZERO_WITHOUT_REASON" -f $desk.ToUpperInvariant()) ("Hybrid={0}; Paper=0; state={1}" -f $hybrid,$state)
  } else {
    Add-Finding $findings 'PASS' ("{0}_RESEARCH_EXPLICIT" -f $desk.ToUpperInvariant()) ("state={0}; hybrid={1}; paper={2}; next={3}" -f $state,$hybrid,$paper,$next)
  }
}

$firstSig=[string](Prop $first 'business_signature'); $lastSig=[string](Prop $last 'business_signature')
if($deliveryAdvanced -and $firstSig -eq $lastSig) {
  Add-Finding $findings 'FAIL' 'CANONICAL_SIGNATURE_DID_NOT_TRACK_PROGRESS' 'Delivery advanced but Product State business signature did not change.'
} else {
  Add-Finding $findings 'PASS' 'CANONICAL_LONGITUDINAL_SIGNATURE' ("changed={0}" -f ($firstSig -ne $lastSig))
}
if((Prop $last 'stale') -eq $true) {
  Add-Finding $findings 'FAIL' 'CANONICAL_SNAPSHOT_STALE' ("snapshot_age_sec={0}" -f (Prop $last 'snapshot_age_sec'))
} else {
  Add-Finding $findings 'PASS' 'CANONICAL_SNAPSHOT_FRESH' ("snapshot_age_sec={0}" -f (Prop $last 'snapshot_age_sec'))
}

$failures=@($findings | Where-Object {(Prop $_ 'level') -eq 'FAIL'})
$report=[ordered]@{
  ok=($failures.Count -eq 0)
  proof='INSTALLED_CANONICAL_LONGITUDINAL_USEFUL_PROGRESS'
  captured_at=(Get-Date).ToString('o')
  install_root='C:\ProgramData\ProjectLaddu'
  evidence_root=$root
  core_authority='/api/product-state'
  samples=$sampleRows
  findings=$findings
  summary=[ordered]@{delivery_first=$firstD;delivery_last=$lastD;delivery_total=$dTotal;delivery_advanced=$deliveryAdvanced;occ_attention=$attention;failures=$failures.Count}
}
$report | ConvertTo-Json -Depth 100 | Set-Content -LiteralPath $reportPath -Encoding UTF8

if(Test-Path -LiteralPath $zipPath){Remove-Item -LiteralPath $zipPath -Force}
Compress-Archive -Path (Join-Path $work '*') -DestinationPath $zipPath -CompressionLevel Optimal
if(-not (Test-Path -LiteralPath $zipPath)){throw "Evidence ZIP was not created: $zipPath"}
$zipItem=Get-Item -LiteralPath $zipPath
if($zipItem.Length -le 0){throw "Evidence ZIP is empty: $zipPath"}
Add-Type -AssemblyName System.IO.Compression.FileSystem
$archive=[System.IO.Compression.ZipFile]::OpenRead($zipPath)
try { if($archive.Entries.Count -lt 2){throw "Evidence ZIP has too few entries: $($archive.Entries.Count)"} } finally { $archive.Dispose() }
$sha=(Get-FileHash -Algorithm SHA256 -LiteralPath $zipPath).Hash.ToLowerInvariant()
("{0}  {1}" -f $sha,[IO.Path]::GetFileName($zipPath)) | Set-Content -LiteralPath $shaPath -Encoding ASCII

Write-Host ''
Write-Host '=== Operational Progress Self-Proof ==='
foreach($f in $findings){Write-Host ("[{0}] {1} - {2}" -f (Prop $f 'level'),(Prop $f 'code'),(Prop $f 'detail'))}
Write-Host ("Evidence ZIP: {0}" -f $zipPath)
Write-Host ("SHA-256: {0}" -f $sha)
if($failures.Count -gt 0){exit 2}
exit 0
