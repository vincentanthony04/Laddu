# PL24 Capital WFA Governance Closure

PL24 does not alter scanner execution, decision mathematics, R:R, costs, risk/admission, Model Paper lifecycle, or broker authority.

## Root cause
The isolated historical trainer can compute research-profile and capital-profile WFA, but only the research validation payload was committed to governance PostgreSQL. Capital WFA was retained only in the trainer scratch/compatibility path, so a valid APPROVED or REJECTED capital result could disappear after the isolated trainer exited. Separately, forward selector evidence depth was being presented next to historical WFA, creating the false impression that historical WFA must populate prospective selector tables.

## Closure
- Governance migration 008 adds immutable `research.training_validation_evidence`.
- Training publication commits both `research` and `capital` validation payloads atomically with the publication.
- The evidence status reads capital WFA from governance PostgreSQL first.
- Prospective selector depth remains separate and is never backfilled from history.
- Persisted `market-lake.json` + DuckDB training panel may prove catalogue readiness while an incremental peer refresh is in progress.
- The trainer evidence contract/version advances once so unchanged retained data is re-evaluated under the PL24 publication contract without changing estimator hyperparameters.

A capital WFA `REJECTED` result is a successful engineering outcome: it truthfully means the current model did not qualify. PL24 does not weaken statistical gates or manufacture alpha.
