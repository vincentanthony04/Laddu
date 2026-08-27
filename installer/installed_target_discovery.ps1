# Read-only discovery of the runtime currently installed at the target.
# Release-source lineage is validated separately and never depends on this value.
function Get-PriorRuntimeVersion {
  $identityPath = Join-Path $InstallDir 'RELEASE_IDENTITY.json'
  if(!(Test-Path -LiteralPath $identityPath -PathType Leaf)){ return '' }
  try {
    $identity = Get-Content -LiteralPath $identityPath -Raw -ErrorAction Stop | ConvertFrom-Json -ErrorAction Stop
    return [string]$identity.version
  }
  catch {
    return ''
  }
}
