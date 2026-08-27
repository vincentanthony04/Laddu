param(
  [string]$InstallDir = "$env:ProgramData\ProjectLaddu",
  [ValidateSet('Auto','Docker','External')][string]$Mode = 'Auto',
  [string]$OperationalAdminDsn = '',
  [string]$GovernanceAdminDsn = '',
  [string]$QuestDbUrl = '',
  [int]$QuestDbHttpPort = 0,
  [string]$PythonExe = '',
  [ValidateSet('Prepare','Apply')][string]$Phase = 'Apply',
  [switch]$InPlaceUpgrade,
  [string]$TransactionId = ''
)
$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
$SecureDir = Join-Path $InstallDir 'secure'
$InfraDir = Join-Path $InstallDir 'infra'
$ReportPath = Join-Path $InstallDir 'logs\data-plane-provision.json'
$MutationPath = Join-Path $InstallDir 'logs\data-plane-mutation-state.json'
$PrepareProofPath = Join-Path $InstallDir 'logs\data-plane-prepare.json'
$PrepareHandoffPath = Join-Path $SecureDir 'data-plane.prepare-handoff.json'
$PrepareHandoffHashPath = Join-Path $SecureDir 'data-plane.prepare-handoff.sha256'
if([string]::IsNullOrWhiteSpace($TransactionId)){ throw 'Data-plane phase transaction ID is required; Prepare and Apply may not rely on process-local state.' }
$containerChanged = $false
New-Item -ItemType Directory -Force -Path $SecureDir,(Split-Path $ReportPath -Parent) | Out-Null

$QuestLifecycle = Join-Path $Root 'installer\questdb_port_resilience.ps1'
if(!(Test-Path $QuestLifecycle -PathType Leaf)){ throw "QuestDB lifecycle authority is missing: $QuestLifecycle" }
. $QuestLifecycle

function Write-MutationState {
  param(
    [string]$Stage,
    [bool]$OperationalPostgres = $false,
    [bool]$GovernancePostgres = $false,
    [bool]$QuestDb = $false,
    [bool]$ContainerLifecycleChanged = $false
  )
  [ordered]@{
    schema_version = 'data-plane-mutation-journal-1.0.0'
    stage = $Stage
    operational_postgres_mutated = $OperationalPostgres
    governance_postgres_mutated = $GovernancePostgres
    questdb_mutated = $QuestDb
    container_lifecycle_changed = $ContainerLifecycleChanged
    updated_at = (Get-Date).ToString('o')
  } | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $MutationPath -Encoding UTF8
}
Write-MutationState -Stage 'NOT_STARTED'

function New-StrongSecret([int]$Bytes = 32) {
  $buffer = New-Object byte[] $Bytes
  [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($buffer)
  return [Convert]::ToBase64String($buffer).Replace('+','A').Replace('/','B').TrimEnd('=')
}
function Resolve-Python {
  if($PythonExe -and (Test-Path $PythonExe)){ return $PythonExe }
  $runtimeFile = Join-Path $InstallDir 'runtime\backend_python.txt'
  if(Test-Path $runtimeFile){
    $saved = (Get-Content $runtimeFile | Select-Object -First 1).Trim()
    if($saved -and (Test-Path $saved)){ return $saved }
  }
  foreach($candidate in @('C:\Program Files\Python312\python.exe')){
    if(Test-Path $candidate){ return $candidate }
  }
  $cmd = Get-Command python.exe -ErrorAction SilentlyContinue
  if($cmd){
    $version = & $cmd.Source -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>$null
    if($LASTEXITCODE -eq 0 -and [string]$version.Trim() -eq '3.12'){ return $cmd.Source }
  }
  throw 'Python 3.12 x64 executable not found for data-plane migration.'
}
function Protect-File([string]$Path) {
  & icacls.exe $Path /inheritance:r /grant:r "SYSTEM:(F)" "Administrators:(F)" | Out-Null
  if($LASTEXITCODE -ne 0){ throw "Could not secure $Path" }
}

function Get-ReleaseIdentityDigest {
  $identityPath = Join-Path $Root 'RELEASE_IDENTITY.json'
  if(!(Test-Path -LiteralPath $identityPath -PathType Leaf)){ $identityPath = Join-Path $InstallDir 'RELEASE_IDENTITY.json' }
  if(!(Test-Path -LiteralPath $identityPath -PathType Leaf)){ throw 'Release identity is unavailable for data-plane phase binding.' }
  return (Get-FileHash -LiteralPath $identityPath -Algorithm SHA256).Hash.ToLowerInvariant()
}
function Write-PrepareHandoff([string]$ResolvedMode,[string]$OperationalDsn,[string]$GovernanceDsn,[string]$ResolvedQuestDbUrl,[bool]$LifecycleChanged) {
  $payload = [ordered]@{
    schema_version = 'data-plane-phase-handoff-1.0.0'
    state = 'PREPARED'
    transaction_id = $TransactionId
    release_identity_sha256 = Get-ReleaseIdentityDigest
    mode = $ResolvedMode
    operational_admin_dsn = $OperationalDsn
    governance_admin_dsn = $GovernanceDsn
    questdb_url = $ResolvedQuestDbUrl
    questdb_http_port = ([System.Uri]$ResolvedQuestDbUrl).Port
    container_lifecycle_changed = $LifecycleChanged
    prepared_at = (Get-Date).ToString('o')
  }
  $payload | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $PrepareHandoffPath -Encoding UTF8
  Protect-File $PrepareHandoffPath
  $digest = (Get-FileHash -LiteralPath $PrepareHandoffPath -Algorithm SHA256).Hash.ToLowerInvariant()
  $digest | Set-Content -LiteralPath $PrepareHandoffHashPath -Encoding ASCII
  Protect-File $PrepareHandoffHashPath
  return $digest
}
function Read-PrepareHandoff {
  if(!(Test-Path -LiteralPath $PrepareHandoffPath -PathType Leaf) -or !(Test-Path -LiteralPath $PrepareHandoffHashPath -PathType Leaf)){
    throw 'Prepared data-plane handoff is missing; Apply cannot infer state from a previous PowerShell invocation.'
  }
  $expected = [string](Get-Content -LiteralPath $PrepareHandoffHashPath | Select-Object -First 1).Trim().ToLowerInvariant()
  $actual = (Get-FileHash -LiteralPath $PrepareHandoffPath -Algorithm SHA256).Hash.ToLowerInvariant()
  if([string]::IsNullOrWhiteSpace($expected) -or $expected -ne $actual){ throw 'Prepared data-plane handoff checksum mismatch.' }
  try { $handoff = Get-Content -LiteralPath $PrepareHandoffPath -Raw | ConvertFrom-Json }
  catch { throw "Prepared data-plane handoff is unreadable: $($_.Exception.Message)" }
  if([string]$handoff.schema_version -ne 'data-plane-phase-handoff-1.0.0' -or [string]$handoff.state -ne 'PREPARED'){ throw 'Prepared data-plane handoff schema/state is invalid.' }
  if([string]$handoff.transaction_id -ne $TransactionId){ throw 'Prepared data-plane handoff belongs to a different installer transaction.' }
  if([string]$handoff.release_identity_sha256 -ne (Get-ReleaseIdentityDigest)){ throw 'Prepared data-plane handoff belongs to a different release identity.' }
  foreach($name in @('mode','operational_admin_dsn','governance_admin_dsn','questdb_url')){
    if([string]::IsNullOrWhiteSpace([string]$handoff.$name)){ throw "Prepared data-plane handoff is incomplete: $name" }
  }
  return $handoff
}
function New-RuntimePostgresDsn([string]$AdminDsn,[string]$Role,[string]$Password) {
  try { $builder = [System.UriBuilder]::new($AdminDsn) }
  catch { throw "Invalid PostgreSQL DSN: $($_.Exception.Message)" }
  if($builder.Scheme -notin @('postgresql','postgres')){ throw "Unsupported PostgreSQL DSN scheme '$($builder.Scheme)'" }
  if([string]::IsNullOrWhiteSpace($builder.Host) -or [string]::IsNullOrWhiteSpace($builder.Path)){ throw 'PostgreSQL DSN must include host and database name.' }
  $builder.UserName = $Role
  $builder.Password = $Password
  return $builder.Uri.AbsoluteUri
}

function Get-LadduRequestedPort([string]$Name,[int]$DefaultPort) {
  $raw = [Environment]::GetEnvironmentVariable($Name)
  if([string]::IsNullOrWhiteSpace($raw)){ return $DefaultPort }
  $value = 0
  if(-not [int]::TryParse([string]$raw,[ref]$value) -or $value -lt 1024 -or $value -gt 65535){
    throw "$Name must be an integer TCP port between 1024 and 65535."
  }
  return [int]$value
}
function Get-LadduPostgresPublishedPort([string]$Container) {
  try {
    $raw = (& $script:DockerExe inspect $Container 2>$null | Out-String)
    if($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($raw)){ return 0 }
    $item = @($raw | ConvertFrom-Json) | Select-Object -First 1
    if($null -eq $item){ return 0 }
    $binding = @($item.HostConfig.PortBindings.'5432/tcp') | Select-Object -First 1
    if($null -eq $binding -or [string]::IsNullOrWhiteSpace([string]$binding.HostPort)){ return 0 }
    return [int]$binding.HostPort
  } catch { return 0 }
}
function Resolve-LadduPostgresHostPort {
  param(
    [Parameter(Mandatory=$true)][string]$Container,
    [Parameter(Mandatory=$true)][int]$PreferredPort,
    [Parameter(Mandatory=$true)][int]$FallbackStart,
    [int[]]$ReservedPorts = @()
  )
  $existing = Get-LadduPostgresPublishedPort -Container $Container
  $status = Get-LadduDockerContainerStatus -Container $Container
  if($existing -gt 0 -and $status -eq 'running'){
    return [pscustomobject]@{ port=[int]$existing; selection='EXISTING_RUNNING_BINDING'; excluded_ranges=@() }
  }
  $excluded = @(Get-LadduWindowsExcludedTcpRanges)
  $reserved = @{}
  foreach($value in @($ReservedPorts)){ $reserved[[int]$value] = $true }
  $candidates = @([int]$PreferredPort)
  foreach($base in @($FallbackStart,16432,24432,34432)){
    for($port=[int]$base; $port -lt ([int]$base + 200); $port++){
      if($port -ne $PreferredPort){ $candidates += [int]$port }
    }
  }
  foreach($port in @($candidates | Select-Object -Unique)){
    if($reserved.ContainsKey([int]$port)){ continue }
    if(Test-LadduPortExcluded -Port $port -Ranges $excluded){ continue }
    if(Test-LadduPortBindable -Port $port){
      return [pscustomobject]@{
        port = [int]$port
        selection = if($port -eq $PreferredPort){'PREFERRED_PORT_BIND_PROVEN'}else{'SAFE_PORT_DISCOVERED'}
        excluded_ranges = @($excluded)
      }
    }
  }
  throw "No safe Windows loopback port could be found for $Container PostgreSQL after excluded-range and real socket-bind checks."
}
function Assert-LadduPostgresConnectivity {
  param(
    [Parameter(Mandatory=$true)][string]$OperationalDsn,
    [Parameter(Mandatory=$true)][string]$GovernanceDsn,
    [Parameter(Mandatory=$true)][string]$PythonExe
  )
  $requestPath = Join-Path $SecureDir 'data-plane.connectivity.request.json'
  $probeRoot = if(Test-Path (Join-Path $Root 'installer\postgres_connectivity_probe.py')){ $Root } else { $InstallDir }
  $probePath = Join-Path $probeRoot 'installer\postgres_connectivity_probe.py'
  if(!(Test-Path -LiteralPath $probePath -PathType Leaf)){ throw "DATABASE_CONNECTIVITY_PROBE_MISSING: $probePath" }
  $request = [ordered]@{
    operational = [ordered]@{ dsn=$OperationalDsn; database='laddu_operational'; user='laddu_admin' }
    governance = [ordered]@{ dsn=$GovernanceDsn; database='laddu_governance'; user='laddu_admin' }
  }
  $request | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $requestPath -Encoding UTF8
  Protect-File $requestPath
  try {
    & $PythonExe $probePath $requestPath --retry-seconds 35 | Out-Host
    $rc = $LASTEXITCODE
    if($rc -ne 0){ throw "DATABASE_CONNECTIVITY_FAILED: exact PostgreSQL admin DSNs did not pass authenticated SELECT 1/identity proof (exit=$rc)." }
  } finally {
    Remove-Item -LiteralPath $requestPath -Force -ErrorAction SilentlyContinue
  }
}
function Write-LadduRuntimeDataPlaneEnv {
  param(
    [Parameter(Mandatory=$true)][string]$OperationalAdminDsn,
    [Parameter(Mandatory=$true)][string]$GovernanceAdminDsn,
    [Parameter(Mandatory=$true)][string]$ResolvedQuestDbUrl
  )
  $operationalRuntimeDsn = New-RuntimePostgresDsn $OperationalAdminDsn 'laddu_runtime' ([string]$secrets.operational_app)
  $governanceRuntimeDsn = New-RuntimePostgresDsn $GovernanceAdminDsn 'laddu_governance_writer' ([string]$secrets.governance_app)
  $envScript = Join-Path $SecureDir 'data-plane.env.ps1'
  $envLines = @(
    "`$env:PROJECT_LADDU_DATA_PLANE_MODE = 'production'",
    "`$env:PROJECT_LADDU_OPERATIONAL_DSN = '$operationalRuntimeDsn'",
    "`$env:PROJECT_LADDU_GOVERNANCE_DSN = '$governanceRuntimeDsn'",
    "`$env:PROJECT_LADDU_QUESTDB_HTTP_URL = '$ResolvedQuestDbUrl'",
    "`$env:PROJECT_LADDU_REQUIRE_OPERATIONAL_POSTGRES = '1'",
    "`$env:PROJECT_LADDU_REQUIRE_GOVERNANCE_POSTGRES = '1'",
    "`$env:PROJECT_LADDU_REQUIRE_QUESTDB = '1'"
  )
  $envLines | Set-Content -LiteralPath $envScript -Encoding UTF8
  Protect-File $envScript
  return $envScript
}

$secretState = Join-Path $SecureDir 'data-plane.secrets.json'
$adminState = Join-Path $SecureDir 'data-plane.admin.json'
if(Test-Path $secretState){
  $secrets = Get-Content $secretState -Raw | ConvertFrom-Json
} else {
  $secrets = [ordered]@{
    operational_admin = New-StrongSecret
    governance_admin = New-StrongSecret
    operational_app = New-StrongSecret
    governance_app = New-StrongSecret
  }
  $secrets | ConvertTo-Json | Set-Content -Path $secretState -Encoding UTF8
  Protect-File $secretState
}
$priorRuntimeEnvPath = Join-Path $SecureDir 'data-plane.env.ps1'
$hadPriorRuntimeEnv = Test-Path -LiteralPath $priorRuntimeEnvPath -PathType Leaf

function Resolve-DockerExecutable {
  $command = Get-Command docker.exe -ErrorAction SilentlyContinue
  if($command -and (Test-Path -LiteralPath $command.Source -PathType Leaf)){ return [string]$command.Source }
  $candidates = @(
    (Join-Path $env:ProgramFiles 'Docker\Docker\resources\bin\docker.exe'),
    (Join-Path ${env:ProgramFiles(x86)} 'Docker\Docker\resources\bin\docker.exe'),
    (Join-Path $env:LOCALAPPDATA 'Docker\resources\bin\docker.exe')
  ) | Where-Object { $_ -and (Test-Path -LiteralPath $_ -PathType Leaf) }
  return [string]($candidates | Select-Object -First 1)
}
$script:DockerExe = Resolve-DockerExecutable
$dockerAvailable = -not [string]::IsNullOrWhiteSpace($script:DockerExe)

if($Phase -eq 'Apply'){
  $handoff = Read-PrepareHandoff
  $Mode = [string]$handoff.mode
  $OperationalAdminDsn = [string]$handoff.operational_admin_dsn
  $GovernanceAdminDsn = [string]$handoff.governance_admin_dsn
  $QuestDbUrl = [string]$handoff.questdb_url
  $QuestDbHttpPort = [int]$handoff.questdb_http_port
  $containerChanged = [bool]$handoff.container_lifecycle_changed
}

function Test-DockerEngineReady {
  if(!$dockerAvailable){ return $false }
  try {
    & $script:DockerExe info *> $null
    return $LASTEXITCODE -eq 0
  } catch { return $false }
}
function Ensure-DockerEngineReady([int]$TimeoutSeconds = 180) {
  if(Test-DockerEngineReady){
    return [ordered]@{ ok=$true; action='ALREADY_READY'; waited_seconds=0 }
  }
  Write-Host '[INFO] Docker is installed but its engine is not ready. Starting Docker Desktop before any application stop.' -ForegroundColor Yellow
  $service = Get-Service -Name 'com.docker.service' -ErrorAction SilentlyContinue
  if($service -and $service.Status -ne 'Running'){
    try { Start-Service -Name 'com.docker.service' -ErrorAction Stop } catch {}
  }
  $desktopCandidates = @(
    (Join-Path $env:ProgramFiles 'Docker\Docker\Docker Desktop.exe'),
    (Join-Path ${env:ProgramFiles(x86)} 'Docker\Docker\Docker Desktop.exe'),
    (Join-Path $env:LOCALAPPDATA 'Docker\Docker Desktop.exe')
  ) | Where-Object { $_ -and (Test-Path -LiteralPath $_ -PathType Leaf) }
  $desktop = $desktopCandidates | Select-Object -First 1
  if($desktop){
    $runningDesktop = Get-Process -Name 'Docker Desktop' -ErrorAction SilentlyContinue
    if(!$runningDesktop){ Start-Process -FilePath $desktop | Out-Null }
  }
  $started = Get-Date
  $deadline = $started.AddSeconds($TimeoutSeconds)
  $lastNotice = [datetime]::MinValue
  while((Get-Date) -lt $deadline){
    if(Test-DockerEngineReady){
      $waited = [int]((Get-Date) - $started).TotalSeconds
      Write-Host "[OK] Docker engine ready after $waited seconds." -ForegroundColor Green
      return [ordered]@{ ok=$true; action=if($desktop){'DESKTOP_STARTED'}else{'SERVICE_STARTED'}; waited_seconds=$waited }
    }
    if(((Get-Date) - $lastNotice).TotalSeconds -ge 5){
      $elapsed = [int]((Get-Date) - $started).TotalSeconds
      Write-Host "[WAIT] Docker engine startup $elapsed/$TimeoutSeconds seconds" -ForegroundColor DarkYellow
      $lastNotice = Get-Date
    }
    Start-Sleep -Seconds 2
  }
  throw "Docker Desktop is installed but the Linux engine did not become ready within $TimeoutSeconds seconds. Start Docker Desktop once and rerun INSTALL_UPDATE.cmd; the existing Project Laddu runtime and all data remain untouched."
}
if($Mode -eq 'Auto' -and (Test-Path $adminState -PathType Leaf)){
  try {
    $savedAdmin = Get-Content $adminState -Raw | ConvertFrom-Json
    if([string]::IsNullOrWhiteSpace([string]$savedAdmin.mode) -or
       [string]::IsNullOrWhiteSpace([string]$savedAdmin.operational_admin_dsn) -or
       [string]::IsNullOrWhiteSpace([string]$savedAdmin.governance_admin_dsn)){
      throw 'saved provisioning state is incomplete'
    }
    $Mode = [string]$savedAdmin.mode
    $OperationalAdminDsn = [string]$savedAdmin.operational_admin_dsn
    $GovernanceAdminDsn = [string]$savedAdmin.governance_admin_dsn
    if([string]::IsNullOrWhiteSpace($QuestDbUrl)){ $QuestDbUrl = [string]$savedAdmin.questdb_url }
  } catch { throw "Production data-plane provisioning state is unreadable: $($_.Exception.Message)" }
}
if($Mode -eq 'Auto'){
  if($OperationalAdminDsn -and $GovernanceAdminDsn){ $Mode = 'External' }
  elseif($dockerAvailable){ $Mode = 'Docker' }
  else { throw 'Production data plane requires either Docker Desktop/Engine or explicit external PostgreSQL DSNs. No SQLite production fallback is allowed.' }
}
if($Mode -eq 'External' -and ([string]::IsNullOrWhiteSpace($OperationalAdminDsn) -or [string]::IsNullOrWhiteSpace($GovernanceAdminDsn))){
  throw 'External production data-plane mode requires both operational and governance PostgreSQL administrator DSNs.'
}

if($Mode -eq 'Docker'){
  if(!$dockerAvailable){ throw 'Docker Desktop/Engine is not installed or docker.exe is not on PATH.' }
  $preferredOperationalPort = Get-LadduRequestedPort -Name 'LADDU_OPERATIONAL_PORT' -DefaultPort 15432
  $preferredGovernancePort = Get-LadduRequestedPort -Name 'LADDU_GOVERNANCE_PORT' -DefaultPort 15433
  if($Phase -eq 'Prepare'){
    $dockerStartupProof = Ensure-DockerEngineReady -TimeoutSeconds 180
    $composeRoot = if(Test-Path (Join-Path $Root 'infra\compose\docker-compose.yml')){ $Root } else { $InstallDir }
    $composeFile = Join-Path $composeRoot 'infra\compose\docker-compose.yml'
    if(!(Test-Path $composeFile)){ throw "Compose file missing: $composeFile" }

    $operationalPortProof = Resolve-LadduPostgresHostPort -Container 'project-laddu-operational-postgres' -PreferredPort $preferredOperationalPort -FallbackStart 15432
    $governancePortProof = Resolve-LadduPostgresHostPort -Container 'project-laddu-governance-postgres' -PreferredPort $preferredGovernancePort -FallbackStart 15433 -ReservedPorts @([int]$operationalPortProof.port)
    $env:LADDU_OPERATIONAL_PORT = [string]$operationalPortProof.port
    $env:LADDU_GOVERNANCE_PORT = [string]$governancePortProof.port
    $env:LADDU_OPERATIONAL_ADMIN_PASSWORD = [string]$secrets.operational_admin
    $env:LADDU_GOVERNANCE_ADMIN_PASSWORD = [string]$secrets.governance_admin

    $beforeOperationalId = Get-LadduDockerContainerId -Container 'project-laddu-operational-postgres'
    $beforeGovernanceId = Get-LadduDockerContainerId -Container 'project-laddu-governance-postgres'
    Write-Host ("[INFO] PostgreSQL Windows ports operational={0} ({1}) governance={2} ({3})" -f $operationalPortProof.port,$operationalPortProof.selection,$governancePortProof.port,$governancePortProof.selection) -ForegroundColor Cyan
    & $script:DockerExe compose -f $composeFile up -d operational-postgres governance-postgres
    if($LASTEXITCODE -ne 0){ throw 'docker compose failed to start the Project Laddu PostgreSQL planes.' }
    $afterOperationalId = Get-LadduDockerContainerId -Container 'project-laddu-operational-postgres'
    $afterGovernanceId = Get-LadduDockerContainerId -Container 'project-laddu-governance-postgres'
    $postgresContainerChanged = ($beforeOperationalId -ne $afterOperationalId -or $beforeGovernanceId -ne $afterGovernanceId)

    $actualOperationalPort = Get-LadduPostgresPublishedPort -Container 'project-laddu-operational-postgres'
    $actualGovernancePort = Get-LadduPostgresPublishedPort -Container 'project-laddu-governance-postgres'
    if($actualOperationalPort -le 0 -or $actualGovernancePort -le 0){ throw 'POSTGRES_PORT_BINDING_MISSING: Docker started but one or both PostgreSQL host bindings are absent.' }
    if($actualOperationalPort -eq $actualGovernancePort){ throw 'POSTGRES_PORT_COLLISION: operational and governance PostgreSQL resolved to the same Windows port.' }
    $OperationalAdminDsn = "postgresql://laddu_admin:$($secrets.operational_admin)@127.0.0.1:$actualOperationalPort/laddu_operational"
    $GovernanceAdminDsn = "postgresql://laddu_admin:$($secrets.governance_admin)@127.0.0.1:$actualGovernancePort/laddu_governance"

    Assert-LadduPostgresConnectivity -OperationalDsn $OperationalAdminDsn -GovernanceDsn $GovernanceAdminDsn -PythonExe $PythonExe

    if($QuestDbHttpPort -le 0){ $QuestDbHttpPort = Get-LadduPreferredQuestDbPort -InstallDir $InstallDir -DefaultPort 59000 }
    $beforeQuestId = Get-LadduDockerContainerId
    $questProof = Ensure-LadduQuestDbContainer -ComposeFile $composeFile -InstallDir $InstallDir -PreferredPort $QuestDbHttpPort -ReadyTimeoutSeconds 600
    $afterQuestId = Get-LadduDockerContainerId
    $QuestDbUrl = [string]$questProof.questdb_url
    $containerChanged = ($postgresContainerChanged -or $beforeQuestId -ne $afterQuestId)

    # Existing installs need their managed runtime DSNs rebound to the actual
    # Docker host ports before lineage/retention proof.  This file is explicitly
    # managed-mutable in the preservation contract and contains no new secret.
    if($hadPriorRuntimeEnv){
      $null = Write-LadduRuntimeDataPlaneEnv -OperationalAdminDsn $OperationalAdminDsn -GovernanceAdminDsn $GovernanceAdminDsn -ResolvedQuestDbUrl $QuestDbUrl
    }
    Write-MutationState -Stage 'CONTAINERS_READY_AUTHENTICATED_NO_SCHEMA_MUTATION' -ContainerLifecycleChanged $containerChanged
  } else {
    if(!(Test-DockerEngineReady)){ throw 'Docker engine became unavailable after non-mutating data-plane preparation. Schema apply is blocked.' }
    if([string]::IsNullOrWhiteSpace($QuestDbUrl)){ throw 'Prepared QuestDB URL is missing; schema apply will not restart or mutate containers.' }
  }
}

if([string]::IsNullOrWhiteSpace($QuestDbUrl)){ throw 'QuestDB URL is unavailable after data-plane lifecycle resolution.' }
if(!(Wait-LadduQuestDbEndpoint -BaseUrl $QuestDbUrl -Seconds 180)){ throw "QuestDB is not reachable at $QuestDbUrl" }

if($Phase -eq 'Prepare'){
  $handoffSha256 = Write-PrepareHandoff -ResolvedMode $Mode -OperationalDsn $OperationalAdminDsn -GovernanceDsn $GovernanceAdminDsn -ResolvedQuestDbUrl $QuestDbUrl -LifecycleChanged $containerChanged
  [ordered]@{
    schema_version = 'data-plane-prepare-3.0.0'
    ok = $true
    phase = 'Prepare'
    transaction_id = $TransactionId
    release_identity_sha256 = Get-ReleaseIdentityDigest
    mode = $Mode
    operational_port = ([System.Uri]$OperationalAdminDsn).Port
    governance_port = ([System.Uri]$GovernanceAdminDsn).Port
    postgres_connectivity = 'AUTHENTICATED_SELECT_1_PASS'
    questdb_url = $QuestDbUrl
    questdb_http_port = ([System.Uri]$QuestDbUrl).Port
    schema_mutation = $false
    container_lifecycle_changed = $containerChanged
    secure_handoff_sha256 = $handoffSha256
    prepared_at = (Get-Date).ToString('o')
  } | ConvertTo-Json | Set-Content -LiteralPath $PrepareProofPath -Encoding UTF8
  [ordered]@{
    schema_version = 'questdb-port-resilience-1.0.0'
    mode = $Mode
    operational_admin_dsn = $OperationalAdminDsn
    governance_admin_dsn = $GovernanceAdminDsn
    operational_port = ([System.Uri]$OperationalAdminDsn).Port
    governance_port = ([System.Uri]$GovernanceAdminDsn).Port
    postgres_connectivity = 'AUTHENTICATED_SELECT_1_PASS'
    questdb_url = $QuestDbUrl
    questdb_http_port = ([System.Uri]$QuestDbUrl).Port
    prepared_at = (Get-Date).ToString('o')
  } | ConvertTo-Json | Set-Content -LiteralPath $adminState -Encoding UTF8
  Protect-File $adminState
  Write-MutationState -Stage 'PREPARED_NO_SCHEMA_MUTATION' -ContainerLifecycleChanged $containerChanged
  Write-Host '[OK] Data-plane infrastructure is reachable; no schema or role mutation was performed.' -ForegroundColor Green
  exit 0
}

$python = Resolve-Python
$toolRoot = if(Test-Path (Join-Path $Root 'backend\tools\provision_production_data_plane.py')){ $Root } else { $InstallDir }
$tool = Join-Path $toolRoot 'backend\tools\provision_production_data_plane.py'
$requestFile = Join-Path $SecureDir 'data-plane.provision.request.json'
[ordered]@{
  operational_admin_dsn = $OperationalAdminDsn
  governance_admin_dsn = $GovernanceAdminDsn
  operational_app_role = 'laddu_runtime'
  operational_app_password = [string]$secrets.operational_app
  governance_app_role = 'laddu_governance_writer'
  governance_app_password = [string]$secrets.governance_app
  questdb_url = $QuestDbUrl
  root = $toolRoot
  report = $ReportPath
  require_parent_applied = [bool]$InPlaceUpgrade
} | ConvertTo-Json | Set-Content -Path $requestFile -Encoding UTF8
Protect-File $requestFile
Write-MutationState -Stage 'SCHEMA_MIGRATION_STARTED' -OperationalPostgres $true -GovernancePostgres $true -QuestDb $true -ContainerLifecycleChanged $containerChanged
try {
  & $python $tool --request-file $requestFile
  if($LASTEXITCODE -ne 0){ throw 'Data-plane schema migration failed.' }
} finally {
  Remove-Item -Force -ErrorAction SilentlyContinue $requestFile
}
if(!(Test-Path $ReportPath -PathType Leaf)){ throw 'Data-plane provisioner did not emit its proof report.' }
try { $ProvisionProof = Get-Content $ReportPath -Raw | ConvertFrom-Json }
catch { throw "Data-plane provision proof is unreadable: $($_.Exception.Message)" }
if($ProvisionProof.operational.ok -ne $true -or
   $ProvisionProof.operational.repository_smoke.ok -ne $true -or
   $ProvisionProof.governance.ok -ne $true -or
   $ProvisionProof.questdb.ok -ne $true){
  throw 'Data-plane provision proof did not pass real PostgreSQL repository and QuestDB smoke gates.'
}
Write-MutationState -Stage 'SCHEMA_MIGRATION_COMPLETE' -OperationalPostgres $true -GovernancePostgres $true -QuestDb $true -ContainerLifecycleChanged $containerChanged

$envScript = Write-LadduRuntimeDataPlaneEnv -OperationalAdminDsn $OperationalAdminDsn -GovernanceAdminDsn $GovernanceAdminDsn -ResolvedQuestDbUrl $QuestDbUrl
[ordered]@{
  schema_version = 'questdb-port-resilience-1.0.0'
  mode = $Mode
  operational_admin_dsn = $OperationalAdminDsn
  governance_admin_dsn = $GovernanceAdminDsn
  operational_port = ([System.Uri]$OperationalAdminDsn).Port
  governance_port = ([System.Uri]$GovernanceAdminDsn).Port
  postgres_connectivity = 'AUTHENTICATED_SELECT_1_PASS'
  questdb_url = $QuestDbUrl
  questdb_http_port = ([System.Uri]$QuestDbUrl).Port
  provisioned_at = (Get-Date).ToString('o')
} | ConvertTo-Json | Set-Content -Path $adminState -Encoding UTF8
Protect-File $adminState
Write-Host '[OK] Dedicated operational PostgreSQL, governance PostgreSQL and QuestDB are provisioned.' -ForegroundColor Green
Write-Host "QuestDB authority: $QuestDbUrl"
Write-Host "Evidence: $ReportPath"
