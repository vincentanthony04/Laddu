# R32 Final Windows Runtime Metadata Release Closure

Parent: exact R31 SHA-256 `26a82d65546e9da7b8797db167f8f36c0bb07209e24f8c28b78bad0b41de8975`.

## Evidence that triggered R32
R31 completed package, data-plane, migration, retention, payload activation, service readiness and frontend identity. It failed only when `verify_authoritative_quant_research_lifecycle.py` attempted `os.replace()` onto `runtime/research_runtime.json` while the Windows service was live, returning WinError 5. Rollback restored the prior runtime.

## R32 scope
Only installer runtime-metadata publication changes. The verifier writes a candidate manifest into installer evidence. The service is quiesced, the rebuildable runtime manifest is published with bounded Windows retry and exact SHA-256 proof, then the service is restarted. `/api/quant-research-plane` must return READY and frontend identity must pass again before OPERATIONAL_PROOF.

## Frozen
All R31 browser/customer implementation, all R30 PostgreSQL/WFA/scanner/lifecycle architecture, migrations, risk/decision authorities, Model Paper authority and broker boundary.

## Release boundary
R32 remains NOT ACCEPTED / NOT RELEASE until the exact ZIP passes installed OPERATIONAL_PROOF and browser acceptance on the target Windows machine.
