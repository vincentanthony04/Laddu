# Clean Core R4 package integrity and authoritative lineage gate.
function Clear-PackageTransientPythonBytecode {
  # Python bytecode is never release authority. A previous interrupted/rerun
  # preflight or extraction into a reused folder may leave __pycache__/pyc/pyo
  # beside the sealed sources. Remove only those transient interpreter artefacts
  # before the exact manifest proof; every other unmanifested file still fails.
  $removed = @()
  $cacheDirs = @(Get-ChildItem -LiteralPath $PackageRoot -Recurse -Directory -Force -ErrorAction SilentlyContinue | Where-Object { $_.Name -eq '__pycache__' } | Sort-Object { $_.FullName.Length } -Descending)
  foreach($dir in $cacheDirs){
    if(Test-Path -LiteralPath $dir.FullName -PathType Container){
      $removed += @(Get-ChildItem -LiteralPath $dir.FullName -Recurse -File -Force -ErrorAction SilentlyContinue | ForEach-Object { $_.FullName.Substring($PackageRoot.Length + 1).Replace('\','/') })
      Remove-Item -LiteralPath $dir.FullName -Recurse -Force -ErrorAction Stop
    }
  }
  $orphanBytecode = @(Get-ChildItem -LiteralPath $PackageRoot -Recurse -File -Force -ErrorAction SilentlyContinue | Where-Object { $_.Extension -in @('.pyc','.pyo') })
  foreach($file in $orphanBytecode){
    $removed += $file.FullName.Substring($PackageRoot.Length + 1).Replace('\','/')
    Remove-Item -LiteralPath $file.FullName -Force -ErrorAction Stop
  }
  $removed = @($removed | Sort-Object -Unique)
  $proof = [pscustomobject]@{
    removed_count = $removed.Count
    removed_paths = @($removed | Select-Object -First 250)
    policy = 'ONLY___PYCACHE___PYC_PYO_TRANSIENTS_REMOVED_BEFORE_EXACT_MANIFEST_PROOF'
    all_other_unmanifested_files_fail_closed = $true
  }
  Write-Json -Path (Join-Path $EvidenceDir 'package-transient-bytecode-hygiene.json') -Value $proof
  if($removed.Count -gt 0){ Write-Step 'PACKAGE-HYGIENE' "Removed $($removed.Count) transient Python bytecode artifact(s) before exact package proof" }
  return $proof
}
function Assert-PackageManifest {
  $manifest = Join-Path $PackageRoot 'validation\package_manifest.sha256'
  $identityPath = Join-Path $PackageRoot 'RELEASE_IDENTITY.json'
  if(!(Test-Path -LiteralPath $manifest -PathType Leaf)){ throw 'Package integrity manifest is missing.' }
  if(!(Test-Path -LiteralPath $identityPath -PathType Leaf)){ throw 'RELEASE_IDENTITY.json is missing before package proof.' }
  $packageIdentity = Get-Content -LiteralPath $identityPath -Raw -Encoding UTF8 | ConvertFrom-Json
  $manifestMembers = @()
  $verified = 0
  foreach($line in Get-Content -LiteralPath $manifest -Encoding UTF8){
    if([string]::IsNullOrWhiteSpace($line)){ continue }
    $parts = $line -split '\s{2,}',2
    if($parts.Count -ne 2){ throw "Malformed package manifest line: $line" }
    $expected = $parts[0].Trim().ToUpperInvariant()
    $relativeSlash = $parts[1].Trim().Replace('\','/')
    if($relativeSlash -eq 'validation/package_manifest.sha256'){ throw 'Package manifest must not self-hash.' }
    if($manifestMembers -contains $relativeSlash){ throw "Duplicate package manifest member: $relativeSlash" }
    $manifestMembers += $relativeSlash
    $relative = $relativeSlash.Replace('/','\')
    $path = Join-Path $PackageRoot $relative
    if(!(Test-Path -LiteralPath $path -PathType Leaf)){ throw "Package file missing: $relative" }
    $actual = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToUpperInvariant()
    if($actual -ne $expected){ throw "Package checksum mismatch: $relative" }
    $verified++
  }
  if($verified -lt 700){ throw "Package manifest is incomplete: only $verified files." }

  # R2: the identity carries only non-circular structural expectations. This
  # cannot repair or replace a manifest; it detects a stale/mixed extraction
  # before installation mutates the target machine.
  $contract = $packageIdentity.package_contract
  if($null -ne $contract){
    $expectedCount = 0
    try { $expectedCount = [int]$contract.manifest_files } catch { $expectedCount = 0 }
    if($expectedCount -gt 0 -and $verified -ne $expectedCount){
      throw "STALE_OR_MIXED_PACKAGE_TREE: release $($packageIdentity.version) requires $expectedCount manifest files but this extracted manifest contains $verified. Use Windows Extract All into a NEW EMPTY folder; do not overwrite an older Laddu package folder."
    }
    $requiredMembers = @($contract.required_manifest_members)
    $missingRequired = @($requiredMembers | Where-Object { $manifestMembers -notcontains ([string]$_) })
    if($missingRequired.Count -gt 0){
      throw "STALE_OR_MIXED_PACKAGE_TREE: release $($packageIdentity.version) manifest is missing required sealed member(s): $($missingRequired -join '; '). Use Windows Extract All into a NEW EMPTY folder; do not overwrite an older Laddu package folder."
    }
  }

  $actualMembers = @(Get-ChildItem -LiteralPath $PackageRoot -Recurse -File -ErrorAction Stop | ForEach-Object {
    $_.FullName.Substring($PackageRoot.Length + 1).Replace('\','/')
  } | Sort-Object -Unique)
  $expectedMembers = @($manifestMembers + 'validation/package_manifest.sha256' | Sort-Object -Unique)

  # Build deterministic ordinal-ignore-case sets rather than relying on
  # Compare-Object formatting/collection coercion across Windows PowerShell versions.
  $expectedSet = New-Object 'System.Collections.Generic.HashSet[string]' ([System.StringComparer]::OrdinalIgnoreCase)
  $actualSet = New-Object 'System.Collections.Generic.HashSet[string]' ([System.StringComparer]::OrdinalIgnoreCase)
  foreach($member in $expectedMembers){ [void]$expectedSet.Add([string]$member) }
  foreach($member in $actualMembers){ [void]$actualSet.Add([string]$member) }
  $extras = @($actualMembers | Where-Object { -not $expectedSet.Contains([string]$_) } | Sort-Object -Unique)
  $missing = @($expectedMembers | Where-Object { -not $actualSet.Contains([string]$_) } | Sort-Object -Unique)
  if($extras.Count -gt 0 -or $missing.Count -gt 0){
    $manifestHash = (Get-FileHash -LiteralPath $manifest -Algorithm SHA256).Hash.ToLowerInvariant()
    $detailParts = @()
    if($extras.Count -gt 0){ $detailParts += ('extras=' + (($extras | Select-Object -First 20) -join '; ')) }
    if($missing.Count -gt 0){ $detailParts += ('missing=' + (($missing | Select-Object -First 20) -join '; ')) }
    throw "STALE_OR_MIXED_PACKAGE_TREE: release=$($packageIdentity.version) manifest_files=$verified manifest_sha256=$manifestHash $($detailParts -join ' ') . Use Windows Extract All into a NEW EMPTY folder; do not overwrite an older Laddu package folder."
  }
  Write-Step 'INTEGRITY' "Verified exact package inventory: $verified manifest files + manifest; release=$($packageIdentity.version)"
}

function Assert-ReleaseLineage {
  # Release lineage protects the package construction chain only. Installation
  # eligibility is deliberately independent of whatever Project Laddu payload
  # (if any) is already present on the target machine.
  $identityPath = Join-Path $PackageRoot 'RELEASE_IDENTITY.json'
  $attestationPath = Join-Path $PackageRoot 'RELEASE_ATTESTATION.json'
  if(!(Test-Path -LiteralPath $identityPath -PathType Leaf)){ throw 'RELEASE_IDENTITY.json is missing.' }
  if(!(Test-Path -LiteralPath $attestationPath -PathType Leaf)){ throw 'RELEASE_ATTESTATION.json is missing.' }
  $candidate = Get-Content -LiteralPath $identityPath -Raw | ConvertFrom-Json
  $attestation = Get-Content -LiteralPath $attestationPath -Raw | ConvertFrom-Json
  if(-not [bool]$candidate.installable){ throw "Artifact is not installable: type=$($candidate.artifact_type) installable=$($candidate.installable)" }
  if([string]$candidate.artifact_type -ne 'PRODUCTION_RELEASE' -and [string]$candidate.artifact_type -ne 'INSTALLATION_CANDIDATE'){
    throw "Artifact type cannot execute installation: $($candidate.artifact_type)"
  }
  if([string]$candidate.artifact_type -eq 'INSTALLATION_CANDIDATE'){
    if([bool]$candidate.production_ready){ throw 'Installation candidate cannot claim production readiness.' }
    if([string]$candidate.installation_purpose -ne 'EXACT_WINDOWS_TARGET_PROOF'){ throw 'Installation candidate has invalid proof purpose.' }
    if([string]$candidate.broker_authority -ne 'NONE'){ throw 'Installation candidate changed broker authority.' }
    if([string]$attestation.artifact_type -ne 'INSTALLATION_CANDIDATE' -or -not [bool]$attestation.installable){ throw 'Installation-candidate attestation mismatch.' }
    if([bool]$attestation.production_ready){ throw 'Installation-candidate attestation claims production readiness.' }
    if([string]$attestation.installation_purpose -ne 'EXACT_WINDOWS_TARGET_PROOF'){ throw 'Installation-candidate attestation proof purpose mismatch.' }
    if([string]$attestation.certification.current_level -ne 'SOURCE_SEALED' -or [string]$attestation.certification.SOURCE_SEALED -ne 'PASS'){ throw 'Installation candidate is not SOURCE_SEALED.' }
    if([string]$attestation.certification.INSTALLABLE -ne 'PENDING_INSTALLED_PROOF'){ throw 'Installation candidate overstates installed proof.' }
    if([string]$attestation.certification.END_TO_END_ACCEPTED -ne 'PENDING_ACCEPTANCE_GATE'){ throw 'Installation candidate overstates end-to-end acceptance.' }
  }
  if([string]$candidate.version -ne [string]$attestation.version){ throw 'Release identity/attestation version mismatch.' }
  if([string]$candidate.parent.version -ne [string]$attestation.parent.version){ throw 'Release identity/attestation source-parent version mismatch.' }
  if([string]$candidate.parent.release_identity_sha256 -ne [string]$attestation.parent.release_identity_sha256){ throw 'Release identity/attestation source-parent identity mismatch.' }
  if([string]$candidate.parent.archive_sha256 -ne [string]$attestation.parent.archive_sha256){ throw 'Release identity/attestation source-parent archive mismatch.' }
  if(([string]$candidate.parent.archive_sha256).Length -ne 64 -or ([string]$candidate.parent.release_identity_sha256).Length -ne 64){
    throw 'Release source-parent cryptographic identity is incomplete.'
  }
  try {
    $parentVersion = [Version](([string]$candidate.parent.version).TrimStart('v'))
    $candidateVersion = [Version](([string]$candidate.version).TrimStart('v'))
    if($candidateVersion -le $parentVersion){ throw "Release version must advance its sealed source lineage: parent=$parentVersion candidate=$candidateVersion" }
  } catch { throw "Release semantic version validation failed: $($_.Exception.Message)" }
  Write-Step 'LINEAGE' "Verified sealed package source lineage $($candidate.parent.version) -> $($candidate.version); installed application version is not an eligibility input"
  return $candidate
}
