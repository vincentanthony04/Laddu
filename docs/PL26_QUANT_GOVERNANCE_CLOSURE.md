# PL26 — Quant Governance Deterministic-Defect Closure

PL26 is a narrow offline engineering closure built from the exact PL25 source parent.
It does not change scanner execution, trade geometry, broker-cost mathematics, catalogue
arbitration, workload priority, walk-forward thresholds, or forward-selector semantics.

## Proven deterministic defects closed

1. `CapitalReadinessService.assess()` referenced an undefined `paper_desk`, causing
   `/api/capital-readiness` and its engineering-quality consumer to return HTTP 500.
2. The research trainer computed local NSE daily rank-IC/decay but never populated
   `factor_registry`; factor authority therefore reported `registry_missing` regardless
   of available empirical data.
3. HistGradientBoosting research models had no declared challenger family and could be
   reported as `UNKNOWN_MODEL_FAMILY`.
4. The Quant Success Audit called an unused `/api/shadow-portfolio` route that does not
   exist, creating a permanent spurious 404.

## PL26 behavior

- Local NSE factor IC/IR classification uses the existing IC threshold and existing
  cross-sectional rank-IC calculation. No threshold is weakened.
- Predictive decay uses the existing monitor unchanged.
- Redundancy is measured on the latest up-to-252 dates of the already materialized PIT
  training frame using the existing 0.98 threshold and 60-row minimum overlap.
- Offline factor publication always carries `production_influence=0` and formula identity
  `UNVERIFIED`. Empirical qualification hashes identify the measured evidence; they are
  not formula-verification hashes.
- Existing independently verified formula/production fields in the live compatibility
  registry are never elevated or overwritten by offline publication.
- The PostgreSQL training publication retains the complete measured factor evidence in
  immutable model metadata; the SQLite factor registry is a rebuildable projection.
- HistGradientBoosting is a governed nonlinear tabular challenger family, but all normal
  evidence gates still apply; family recognition does not grant research or production approval.

## Frozen authorities

PL25 historical PIT/workload governor, persisted-catalogue probe, capital WFA mathematics,
selection WFA replay, scanner orchestration, decision engine, trade geometry and exact cash
cost authority are byte-frozen in the PL26 focused validator.

Broker authority remains `NONE`. Source/fresh-ZIP QC is not Windows/live acceptance.
