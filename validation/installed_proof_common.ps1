Set-StrictMode -Version 2.0

function Get-Prop([object]$Object,[string]$Name) {
  if($null -eq $Object){ return $null }
  # Candidate 24: PowerShell ordered/plain hashtables are used by restart proof.
  # PSObject.Properties does not expose their keys as normal properties, so read
  # IDictionary values explicitly before falling back to object properties.
  if($Object -is [System.Collections.IDictionary]) {
    if($Object.Contains($Name)){ return $Object[$Name] }
    return $null
  }
  $property = $Object.PSObject.Properties[$Name]
  if($null -eq $property){ return $null }
  return $property.Value
}
function Arr([object]$Value) {
  # PowerShell 5.1 enumerates function output.  Return one array object so
  # empty/singleton collections never collapse to $null/scalars under StrictMode.
  if($null -eq $Value){ return ,@() }
  return ,@($Value)
}
function Property-Values([object]$Value) {
  if($null -eq $Value){ return ,@() }
  $rows=@()
  foreach($property in $Value.PSObject.Properties){ $rows += ,$property.Value }
  return ,@($rows)
}
function Num([object]$Value) { $number=0.0; [double]::TryParse([string]$Value,[ref]$number)|Out-Null; return $number }
function Text([object]$Value) { if($null -eq $Value){ return '' }; return [string]$Value }
function Format-HttpFailure([object]$ErrorRecord,[string]$Method,[string]$Uri) {
  $message=Text (Get-Prop (Get-Prop $ErrorRecord 'Exception') 'Message')
  $status=''
  $response=Get-Prop (Get-Prop $ErrorRecord 'Exception') 'Response'
  if($null -ne $response){
    $code=Get-Prop $response 'StatusCode'
    if($null -ne $code){ $status=Text $code }
  }
  $errorDetails=Get-Prop $ErrorRecord 'ErrorDetails'
  $body=Text (Get-Prop $errorDetails 'Message')
  if([string]::IsNullOrWhiteSpace($body) -and $null -ne $response){
    try {
      $stream=$response.GetResponseStream()
      if($null -ne $stream){
        $reader=New-Object System.IO.StreamReader($stream)
        try { $body=$reader.ReadToEnd() } finally { $reader.Dispose() }
      }
    } catch {}
  }
  $body=($body -replace '[\r\n]+',' ').Trim()
  if($body.Length -gt 2000){ $body=$body.Substring(0,2000) }
  return ("HTTP_REQUEST_FAILED method={0}; uri={1}; status={2}; message={3}; body={4}" -f $Method,$Uri,$status,$message,$body)
}
function Get-Json([string]$BaseUrl,[string]$Path,[int]$Timeout=20) {
  $uri=$BaseUrl.TrimEnd('/')+$Path
  try { return Invoke-RestMethod -UseBasicParsing -Method Get -Uri $uri -TimeoutSec $Timeout }
  catch { throw (Format-HttpFailure $_ 'GET' $uri) }
}
function Post-Json([string]$BaseUrl,[string]$Path,[object]$Body,[int]$Timeout=60) {
  $uri=$BaseUrl.TrimEnd('/')+$Path
  try { return Invoke-RestMethod -UseBasicParsing -Method Post -Uri $uri -ContentType 'application/json' -Body ($Body|ConvertTo-Json -Depth 30 -Compress) -TimeoutSec $Timeout }
  catch { throw (Format-HttpFailure $_ 'POST' $uri) }
}
function Wait-Ready([string]$BaseUrl,[int]$Seconds=120) {
  $deadline=(Get-Date).AddSeconds($Seconds)
  do {
    try { $ready=Get-Json $BaseUrl '/api/ready' 10; if((Get-Prop $ready 'ready') -eq $true){ return $ready } } catch {}
    Start-Sleep -Seconds 2
  } while((Get-Date)-lt $deadline)
  throw "Project Laddu did not become ready within $Seconds seconds."
}
function Add-Gate([hashtable]$Context,[string]$GateId,[ValidateSet('PASS','FAIL','TARGET_PENDING')][string]$Status,[string]$Detail,[object]$Evidence=$null) {
  if($Context.Gates.Contains($GateId)){ throw "duplicate installed proof gate: $GateId" }
  $Context.Gates[$GateId]=[ordered]@{gate_id=$GateId;status=$Status;detail=$Detail;evidence=$Evidence;captured_at=(Get-Date).ToString('o')}
  $tag = if($Status -eq 'PASS'){'PASS'}elseif($Status -eq 'FAIL'){'FAIL'}else{'PENDING'}
  Write-Host ("[{0}] {1} - {2}" -f $tag,$GateId,$Detail)
}
function Add-GateFromBool([hashtable]$Context,[string]$GateId,[bool]$Passed,[string]$Detail,[object]$Evidence=$null) {
  Add-Gate $Context $GateId ($(if($Passed){'PASS'}else{'FAIL'})) $Detail $Evidence
}
function Provider-Id([object]$Value) { return ([string]$Value) -match '^(NSE|BSE)_(EQ|INDEX)\|' }
function Last-Candle-Time([object]$Payload) {
  foreach($name in @('last_candle','last_timestamp','last_bar','to','last')){
    $value=Get-Prop $Payload $name; if(-not [string]::IsNullOrWhiteSpace([string]$value)){ return [string]$value }
  }
  $rows=Arr (Get-Prop $Payload 'candles'); if($rows.Count -eq 0){$rows=Arr (Get-Prop $Payload 'rows')}
  if($rows.Count -gt 0){
    $row=$rows[-1]
    foreach($name in @('timestamp','time','datetime','date','ts')){ $value=Get-Prop $row $name; if($value){return [string]$value} }
  }
  return ''
}
function Row-Count([object]$Payload) {
  foreach($name in @('count','row_count','rows_count')){ $n=Get-Prop $Payload $name; if($null -ne $n){return [int](Num $n)} }
  foreach($name in @('candles','rows','data')){ $rows=Arr (Get-Prop $Payload $name); if($rows.Count -gt 0){return $rows.Count} }
  return 0
}
function Invoke-TimedGet([string]$BaseUrl,[string]$Path,[int]$Timeout=20) {
  $watch=[Diagnostics.Stopwatch]::StartNew(); $payload=Get-Json $BaseUrl $Path $Timeout; $watch.Stop()
  return [ordered]@{elapsed_ms=[double]$watch.ElapsedMilliseconds;payload=$payload}
}
function Invoke-TimedGetWire([string]$BaseUrl,[string]$Path,[int]$Timeout=20) {
  Add-Type -AssemblyName System.Net.Http -ErrorAction Stop
  # Candidate 23 separates the product's localhost HTTP/body-transfer latency from
  # Windows PowerShell ConvertFrom-Json cost.  The wire stopwatch stops after the
  # complete response body is received; JSON parsing is measured independently.
  $uri=$BaseUrl.TrimEnd('/')+$Path
  $handler=New-Object System.Net.Http.HttpClientHandler
  $client=New-Object System.Net.Http.HttpClient($handler)
  try {
    $client.Timeout=[TimeSpan]::FromSeconds($Timeout)
    $watch=[Diagnostics.Stopwatch]::StartNew()
    $response=$client.GetAsync($uri).GetAwaiter().GetResult()
    $bytes=$response.Content.ReadAsByteArrayAsync().GetAwaiter().GetResult()
    $watch.Stop()
    if(-not $response.IsSuccessStatusCode){ throw ("HTTP_REQUEST_FAILED method=GET; uri={0}; status={1}; body={2}" -f $uri,[int]$response.StatusCode,[Text.Encoding]::UTF8.GetString($bytes)) }
    $parse=[Diagnostics.Stopwatch]::StartNew()
    $text=[Text.Encoding]::UTF8.GetString($bytes)
    $payload=$text|ConvertFrom-Json
    $parse.Stop()
    return [ordered]@{wire_ms=[double]$watch.ElapsedMilliseconds;parse_ms=[double]$parse.ElapsedMilliseconds;elapsed_ms=[double]($watch.ElapsedMilliseconds+$parse.ElapsedMilliseconds);bytes=[int64]$bytes.Length;payload=$payload}
  } finally {
    $client.Dispose(); $handler.Dispose()
  }
}
function Invoke-TimedPost([string]$BaseUrl,[string]$Path,[object]$Body,[int]$Timeout=60) {
  $watch=[Diagnostics.Stopwatch]::StartNew(); $payload=Post-Json $BaseUrl $Path $Body $Timeout; $watch.Stop()
  return [ordered]@{elapsed_ms=[double]$watch.ElapsedMilliseconds;payload=$payload}
}
function Read-ReleaseIdentity([string]$Root) {
  $path=Join-Path $Root 'RELEASE_IDENTITY.json'
  if(-not (Test-Path -LiteralPath $path)){ return $null }
  return Get-Content -LiteralPath $path -Raw | ConvertFrom-Json
}
function Canonical-Name([object]$Row) {
  foreach($name in @('canonical_display_name','display_name','name','symbol')){
    $value=[string](Get-Prop $Row $name); if(-not [string]::IsNullOrWhiteSpace($value)){return $value.Trim().ToUpperInvariant()}
  }
  return ''
}
function Find-ReportCheck([object]$Report,[string]$Name) {
  return @(Arr (Get-Prop $Report 'checks') | Where-Object { [string](Get-Prop $_ 'name') -eq $Name }) | Select-Object -First 1
}
function Gate-Pass([object]$Gate) { return $null -ne $Gate -and [string](Get-Prop $Gate 'status') -eq 'PASS' }
