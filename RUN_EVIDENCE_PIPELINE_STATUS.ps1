param([int]$Port = 8086)
$ErrorActionPreference='Stop'
$uri = "http://127.0.0.1:$Port/api/evidence-pipeline/status"
try {
  $result = Invoke-RestMethod -Method Get -Uri $uri -TimeoutSec 30
  $result | ConvertTo-Json -Depth 30
  $g = $result.gates
  Write-Host ""
  Write-Host ("Current PIT cycle completed    : {0}" -f $g.historical_training_completed)
  Write-Host ("Retained training artifact    : {0}" -f $g.retained_training_artifact_exists)
  Write-Host ("Publication outbox drained   : {0}" -f $g.publication_outbox_drained)
  Write-Host ("Research catalogue ready     : {0}" -f $g.research_catalogue_ready)
  if($null -ne $result.catalogue_evidence){ Write-Host ("Persisted catalogue proof    : {0} · {1} rows / {2} dates" -f $result.catalogue_evidence.state,$result.catalogue_evidence.rows,$result.catalogue_evidence.dates) }
  Write-Host ("Governed model exists        : {0}" -f $g.governed_model_exists)
  Write-Host ("Capital WFA persisted        : {0}" -f $g.capital_walk_forward_result_persisted)
  $fwd = $result.forward_selector_evidence_depth
  if($null -ne $fwd){
    Write-Host ("Forward selector Intraday    : {0} obs / {1} days" -f $fwd.intraday.observations,$fwd.intraday.trading_days)
    Write-Host ("Forward selector Delivery    : {0} obs / {1} days" -f $fwd.delivery.observations,$fwd.delivery.trading_days)
  }
  Write-Host ("ML production influence safe : {0}" -f $g.production_ml_influence_zero_until_qualified)
  if(-not $g.historical_training_completed -or -not $g.capital_walk_forward_result_persisted){ exit 2 }
  exit 0
} catch {
  Write-Error $_.Exception.Message
  exit 3
}
