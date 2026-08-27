param(
  [string]$InstallDir = "$env:ProgramData\ProjectLaddu",
  [string]$BaseUrl = 'http://127.0.0.1:8086',
  [string]$Symbol = 'TCS'
)
$ErrorActionPreference='Stop'
Set-StrictMode -Version 2.0
$PackageRoot=(Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$ExpectedBuild=((Get-Content -LiteralPath (Join-Path $PackageRoot 'RELEASE_IDENTITY.json') -Raw | ConvertFrom-Json).version)
if([string]::IsNullOrWhiteSpace([string]$ExpectedBuild)){ throw 'Package release version is missing.' }
$stamp=Get-Date -Format 'yyyyMMdd_HHmmss'
$outDir=Join-Path 'C:\Temp\ProjectLaddu' ("premarket-customer-acceptance\"+$stamp)
New-Item -ItemType Directory -Force -Path $outDir|Out-Null
$steps=New-Object System.Collections.ArrayList
function Add-Step([string]$name,[string]$state,[string]$detail,[string]$evidence=''){
  [void]$steps.Add([ordered]@{name=$name;state=$state;detail=$detail;evidence=$evidence})
}
function Write-Json($obj,[string]$path){$obj|ConvertTo-Json -Depth 40|Set-Content -LiteralPath $path -Encoding UTF8}
$firstFail=$null
try {
  $python=(Get-Command python.exe -ErrorAction SilentlyContinue).Source
  if([string]::IsNullOrWhiteSpace($python)){$python=(Get-Command python -ErrorAction SilentlyContinue).Source}
  if([string]::IsNullOrWhiteSpace($python)){throw 'Python is unavailable for packaged self-test.'}

  $selfOut=Join-Path $outDir 'packaged-vertical-selftest.txt'
  & $python (Join-Path $PackageRoot 'validation\verify_customer_vertical_slice.py') *> $selfOut
  if($LASTEXITCODE -ne 0){Add-Step 'PACKAGED_VERTICAL_SELFTEST' 'FAIL' 'Deterministic packaged customer vertical slice failed.' $selfOut; $firstFail='PACKAGED_VERTICAL_SELFTEST'}
  else{Add-Step 'PACKAGED_VERTICAL_SELFTEST' 'PASS' 'Deterministic packaged customer vertical slice passed.' $selfOut}

  if(-not $firstFail){
    if(-not (Test-Path -LiteralPath $InstallDir -PathType Container)){Add-Step 'INSTALLED_PRODUCT_PRESENT' 'FAIL' "Install directory missing: $InstallDir"; $firstFail='INSTALLED_PRODUCT_PRESENT'}
    else{Add-Step 'INSTALLED_PRODUCT_PRESENT' 'PASS' "Installed product found at $InstallDir"}
  }

  if(-not $firstFail){
    try{
      $health=Invoke-RestMethod -Uri ($BaseUrl.TrimEnd('/')+'/api/health') -TimeoutSec 15
      $hp=Join-Path $outDir 'api-health.json'; Write-Json $health $hp
      Add-Step 'API_HEALTH' 'PASS' 'Runtime health endpoint responded.' $hp
    }catch{Add-Step 'API_HEALTH' 'FAIL' $_.Exception.Message; $firstFail='API_HEALTH'}
  }

  if(-not $firstFail){
    $browserOut=Join-Path $outDir 'installed-browser-acceptance.json'
    & $python (Join-Path $PackageRoot 'validation\installed_browser_acceptance.py') --base-url $BaseUrl --output $browserOut --symbol $Symbol --expected-build $ExpectedBuild *> (Join-Path $outDir 'installed-browser-console.txt')
    if($LASTEXITCODE -ne 0){
      $detail='Independent Edge/CDP customer flow failed.'
      if(Test-Path $browserOut){try{$b=Get-Content $browserOut -Raw|ConvertFrom-Json; $bad=@($b.checks|Where-Object{-not $_.ok}|Select-Object -First 1); if($bad.Count -gt 0){$detail=([string]$bad[0].name+': '+[string]($bad[0].detail|ConvertTo-Json -Compress -Depth 6))}}catch{}}
      Add-Step 'BROWSER_CUSTOMER_FLOW' 'FAIL' $detail $browserOut; $firstFail='BROWSER_CUSTOMER_FLOW'
    }else{Add-Step 'BROWSER_CUSTOMER_FLOW' 'PASS' 'Independent Edge/CDP Workspace -> Stock Intelligence flow passed.' $browserOut}
  }

  if(-not $firstFail){
    $proofConsole=Join-Path $outDir 'installed-product-proof-console.txt'
    & PowerShell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PackageRoot 'VERIFY_INSTALLED_PRODUCT.ps1') -InstallDir $InstallDir -BaseUrl $BaseUrl -SkipBrowser -SkipRestart -SkipFaultInjection *> $proofConsole
    if($LASTEXITCODE -ne 0){Add-Step 'INSTALLED_SUPPORTING_GATES' 'FAIL' 'Installed supporting proof runner failed.' $proofConsole; $firstFail='INSTALLED_SUPPORTING_GATES'}
    else{Add-Step 'INSTALLED_SUPPORTING_GATES' 'PASS' 'Installed supporting proof runner completed without failed target gates.' $proofConsole}
  }
}catch{
  if(-not $firstFail){$firstFail='HARNESS_FATAL'; Add-Step 'HARNESS_FATAL' 'FAIL' $_.Exception.ToString()}
}
$ok=($null -eq $firstFail)
$summary=[ordered]@{
  product='Project Laddu'; version='v131.0.0'; authority='PREMARKET_CUSTOMER_ACCEPTANCE'; captured_at=(Get-Date).ToString('o');
  ok=$ok; state=$(if($ok){'PASS'}else{'FAIL_OR_BLOCKED'}); first_failing_customer_step=$firstFail; base_url=$BaseUrl; symbol=$Symbol;
  broker_authority='NONE'; windows_live_market_pass='NOT_CLAIMED_BY_HARNESS'; steps=$steps
}
$summaryPath=Join-Path $outDir 'PREMARKET-CUSTOMER-ACCEPTANCE.json'; Write-Json $summary $summaryPath
$sha=(Get-FileHash $summaryPath -Algorithm SHA256).Hash.ToLowerInvariant(); Set-Content ($summaryPath+'.sha256') ($sha+'  '+[IO.Path]::GetFileName($summaryPath)) -Encoding ASCII
$zip=$outDir+'.zip'; Compress-Archive -Path (Join-Path $outDir '*') -DestinationPath $zip -Force
Write-Host ('Evidence: '+$outDir)
Write-Host ('Bundle:   '+$zip)
Write-Host ('First failing customer step: '+$(if($firstFail){$firstFail}else{'NONE'}))
if($ok){exit 0}else{exit 2}
