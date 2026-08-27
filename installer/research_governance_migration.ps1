function Invoke-LegacyResearchGovernanceMigration {
  param(
    [Parameter(Mandatory=$true)][string]$PythonExe,
    [Parameter(Mandatory=$true)][string]$StageDir,
    [Parameter(Mandatory=$true)][string]$OutputPath,
    [Parameter(Mandatory=$true)][string]$AuthorityAfterSchemaPath,
    [Parameter(Mandatory=$true)][string]$AuthorityAfterResearchMigrationPath
  )
  $migrator = Join-Path $StageDir 'backend\tools\migrate_legacy_research_governance.py'
  if(!(Test-Path -LiteralPath $migrator -PathType Leaf)){ throw "Legacy research governance migrator missing: $migrator" }
  & $PythonExe $migrator --output $OutputPath
  if($LASTEXITCODE -ne 0){ throw 'Legacy research governance migration failed before payload activation.' }
  $proof = Get-Content -LiteralPath $OutputPath -Raw | ConvertFrom-Json
  if($proof.ok -ne $true -or $proof.count_verified -ne $true -or $proof.hash_verified -ne $true -or $proof.quarantine_verified -ne $true){
    throw 'Legacy research governance migration did not produce a verified completion checkpoint.'
  }
  $authority = Invoke-AuthorityRetentionEvidence -PythonExe $PythonExe -OutputPath $AuthorityAfterResearchMigrationPath -Label 'AFTER_RESEARCH_MIGRATION' -CompareBefore $AuthorityAfterSchemaPath
  return [pscustomobject]@{ Proof=$proof; Authority=$authority }
}
