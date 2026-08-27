param(
  [string]$InstallDir = "$env:ProgramData\ProjectLaddu",
  [string]$BaseUrl = 'http://127.0.0.1:8086',
  [switch]$FullLive,
  [int]$WaitSeconds = 180,
  [int]$PollSeconds = 60,
  [double]$MaxHours = 8
)
$ErrorActionPreference='Stop'
Set-StrictMode -Version 2.0
$PackageRoot=$PSScriptRoot
$release=Get-Content -LiteralPath (Join-Path $PackageRoot 'frontend\release-identity.json') -Raw | ConvertFrom-Json
$expectedVersion=[string]$release.version
$expectedBuild=[string]$release.build_marker
$backendPointer=Join-Path $InstallDir 'runtime\backend_python.txt'
$python=''
if(Test-Path -LiteralPath $backendPointer -PathType Leaf){$python=(Get-Content -LiteralPath $backendPointer -Raw).Trim()}
if([string]::IsNullOrWhiteSpace($python) -or !(Test-Path -LiteralPath $python -PathType Leaf)){
  $fallback=Join-Path $env:ProgramFiles 'Python312\python.exe'
  if(Test-Path -LiteralPath $fallback -PathType Leaf){$python=$fallback}
}
if([string]::IsNullOrWhiteSpace($python) -or !(Test-Path -LiteralPath $python -PathType Leaf)){throw 'Installed backend Python is unavailable.'}
$stamp=Get-Date -Format 'yyyyMMdd-HHmmss'
$outBase=Join-Path $InstallDir 'logs\validation\exact-customer-vertical'
$outDir=Join-Path $outBase $stamp
New-Item -ItemType Directory -Path $outDir -Force|Out-Null
$binding=Join-Path $outDir 'installed-package-binding.json'
$browser=Join-Path $outDir 'installed-customer-vertical-r3.json'
$summaryPath=Join-Path $outDir 'EXACT_PRODUCT_ACCEPTANCE.json'
$tracker=Join-Path $outBase 'EXACT_VERTICAL_TRACKER.json'

Write-Host 'Project Laddu - exact customer vertical acceptance'
Write-Host ("Build: {0} | {1}" -f $expectedVersion,$expectedBuild)
if($FullLive){Write-Host 'Mode: FULL LIVE SAME-DECISION TRACKING (Intraday preferred)'}

& $python (Join-Path $PackageRoot 'validation\verify_installed_package_binding.py') --package-root $PackageRoot --install-dir $InstallDir --output $binding | Out-Host
$bindingRc=$LASTEXITCODE
if($bindingRc -ne 0){throw 'Installed package binding failed; full customer acceptance cannot start.'}

function Invoke-BrowserGate([string[]]$ExtraArgs){
  $args=@(
    (Join-Path $PackageRoot 'validation\installed_customer_vertical_acceptance_r3.py'),
    '--base-url',$BaseUrl,'--output',$browser,'--install-dir',$InstallDir,
    '--expected-version',$expectedVersion,'--expected-build',$expectedBuild,
    '--wait-seconds',[string]$WaitSeconds
  )
  if($ExtraArgs){$args += $ExtraArgs}
  & $python @args | Out-Host
  return $LASTEXITCODE
}

function Get-Ready(){
  try{return Invoke-RestMethod -Uri ($BaseUrl.TrimEnd('/')+'/api/ready') -TimeoutSec 5 -Headers @{'Cache-Control'='no-store'}}catch{return $null}
}

$browserRc=0
$finalState='FAILED'
$restartEvidence=$null
if(!$FullLive){
  $browserRc=Invoke-BrowserGate @()
  $browserObj=if(Test-Path $browser){Get-Content -LiteralPath $browser -Raw|ConvertFrom-Json}else{$null}
  if($browserRc -eq 0 -and $null -ne $browserObj -and $browserObj.ok -eq $true){
    $finalState=if([string]$browserObj.state -eq 'INSTALLED_CLOSED_MARKET_PROOF_PASSED_LIVE_VERTICAL_PENDING'){'INSTALLED_TRUTH_PASSED_LIVE_VERTICAL_PENDING'}else{'INSTALLED_CUSTOMER_TRUTH_PASSED'}
  }
}else{
  $deadline=(Get-Date).AddHours([Math]::Max(0.25,$MaxHours))
  while((Get-Date) -lt $deadline){
    $browserRc=Invoke-BrowserGate @('--track-lifecycle','--tracker',$tracker,'--preferred-tracker-mode','intraday')
    if($browserRc -eq 2){$finalState='FAILED';break}
    $trackerObj=if(Test-Path $tracker){Get-Content -LiteralPath $tracker -Raw|ConvertFrom-Json}else{$null}
    $stage=if($null -ne $trackerObj){[string]$trackerObj.stage}else{'WAITING_FOR_ACTIONABLE'}
    Write-Host ("Same-decision tracker: {0}" -f $stage)
    if($stage -eq 'AFTER_OBSERVED'){
      $identity=[Security.Principal.WindowsIdentity]::GetCurrent()
      $principal=New-Object Security.Principal.WindowsPrincipal($identity)
      if(!$principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)){
        throw 'FULL LIVE restart/persistence proof requires an elevated shell. Run RUN_FULL_EXACT_CUSTOMER_VERTICAL.cmd.'
      }
      $before=Get-Ready
      $beforeBoot=if($null -ne $before){[string]$before.process_boot_id}else{''}
      if([string]::IsNullOrWhiteSpace($beforeBoot)){throw 'Pre-restart process boot identity unavailable.'}
      Write-Host ("Restart proof: stopping/starting ProjectLaddu; before boot {0}" -f $beforeBoot)
      Restart-Service -Name 'ProjectLaddu' -Force -ErrorAction Stop
      $after=$null
      $restartDeadline=(Get-Date).AddSeconds(150)
      while((Get-Date) -lt $restartDeadline){
        Start-Sleep -Seconds 2
        $candidate=Get-Ready
        if($null -ne $candidate -and $candidate.ok -eq $true -and [string]$candidate.version -eq $expectedVersion -and ![string]::IsNullOrWhiteSpace([string]$candidate.process_boot_id) -and [string]$candidate.process_boot_id -ne $beforeBoot){$after=$candidate;break}
      }
      if($null -eq $after){throw 'Service restart did not produce a new verified process boot identity within 150 seconds.'}
      $afterBoot=[string]$after.process_boot_id
      $browserRc=Invoke-BrowserGate @('--track-lifecycle','--tracker',$tracker,'--preferred-tracker-mode','intraday','--verify-restart-before-boot-id',$beforeBoot)
      $restartEvidence=[ordered]@{before_boot_id=$beforeBoot;after_boot_id=$afterBoot;changed=($beforeBoot -ne $afterBoot);verified_at=(Get-Date).ToString('o')}
      $trackerObj=if(Test-Path $tracker){Get-Content -LiteralPath $tracker -Raw|ConvertFrom-Json}else{$null}
      if($browserRc -eq 0 -and $null -ne $trackerObj -and [string]$trackerObj.stage -eq 'RESTART_VERIFIED' -and $trackerObj.complete -eq $true){$finalState='FULL_EXACT_CUSTOMER_VERTICAL_PASSED'}else{$finalState='FAILED'}
      break
    }
    if($stage -eq 'RESTART_VERIFIED'){$finalState='FULL_EXACT_CUSTOMER_VERTICAL_PASSED';break}
    Start-Sleep -Seconds ([Math]::Max(15,$PollSeconds))
  }
  if($finalState -eq 'FAILED' -and (Get-Date) -ge $deadline){$finalState='TRACKING_WINDOW_EXPIRED_NOT_ACCEPTED'}
}

$bindingObj=if(Test-Path $binding){Get-Content -LiteralPath $binding -Raw|ConvertFrom-Json}else{$null}
$browserObj=if(Test-Path $browser){Get-Content -LiteralPath $browser -Raw|ConvertFrom-Json}else{$null}
$trackerFinal=if(Test-Path $tracker){Get-Content -LiteralPath $tracker -Raw|ConvertFrom-Json}else{$null}
$ok=($finalState -in @('INSTALLED_TRUTH_PASSED_LIVE_VERTICAL_PENDING','INSTALLED_CUSTOMER_TRUTH_PASSED','FULL_EXACT_CUSTOMER_VERTICAL_PASSED'))
if($FullLive){$ok=($finalState -eq 'FULL_EXACT_CUSTOMER_VERTICAL_PASSED')}
$summary=[ordered]@{
  ok=$ok; state=$finalState; captured_at=(Get-Date).ToString('o'); expected_version=$expectedVersion; expected_build=$expectedBuild;
  full_live=[bool]$FullLive; package_binding=$bindingObj; browser_vertical=$browserObj; same_decision_tracker=$trackerFinal; restart_evidence=$restartEvidence;
  claim_boundary=if($finalState -eq 'FULL_EXACT_CUSTOMER_VERTICAL_PASSED'){'One exact installed Intraday decision was observed Actionable, opened in Model Paper, settled with Result/Outcome, gained a separate After observation, survived an actual service restart, and retained the same decision/settlement identity.'}else{'NOT ACCEPTED. No final product/release claim; exact same-decision live lifecycle remains incomplete.'};
  broker_authority='NONE'
}
$summary|ConvertTo-Json -Depth 100|Set-Content -LiteralPath $summaryPath -Encoding UTF8
$hash=(Get-FileHash -LiteralPath $summaryPath -Algorithm SHA256).Hash.ToLowerInvariant()
Set-Content -LiteralPath ($summaryPath+'.sha256') -Value ("{0}  {1}" -f $hash,[IO.Path]::GetFileName($summaryPath)) -Encoding ASCII
$zip=Join-Path $outBase ("ProjectLaddu-ExactAcceptance-"+$stamp+'.zip')
Compress-Archive -Path (Join-Path $outDir '*') -DestinationPath $zip -Force
if(Test-Path $tracker){Compress-Archive -Path $tracker -Update -DestinationPath $zip}
Write-Host ("State: {0}" -f $finalState)
Write-Host ("Evidence: {0}" -f $zip)
if(!$ok){exit 2}
exit 0
