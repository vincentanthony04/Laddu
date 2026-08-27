# Project Laddu R36 — QC Task Contract & Historical PIT Closure

## Why R36 exists

R35 passed source packaging but failed on the Windows target after task registration. The installed Research verifier requires the authoritative scheduled task `ProjectLaddu-AI-Training` to execute `train_ai_model.ps1`. R35 repointed that task to `run_historical_pit_enrichment.ps1`, so the verifier correctly returned `TASK_ACTION_MISMATCH:train_ai_model.ps1` and the installer rolled back.

## R36 correction

- Preserve the R35 decision-dashboard frontend implementation.
- Restore `installer/register_research_tasks.ps1` to the proven R34/R33 task contract.
- `ProjectLaddu-AI-Training` again executes `train_ai_model.ps1` at the established 18:30 schedule.
- Installer registration does **not** start AI training immediately.
- Normal `train_ai_model.ps1` execution now passes `--min-dates 504` to the existing canonical `train_nse_smart_model.py` pipeline.
- First-useful diagnostic mode remains explicitly separate and is not silently promoted to the 504-date normal-training path.
- Historical training stays Parquet/DuckDB, PIT, governed and shadow-only until WFA/forward promotion gates pass. Broker authority remains `NONE`.

## Non-regression boundary

R36 does not redesign PostgreSQL, QuestDB, scanner, risk, canonical decision, lifecycle, WFA service, service startup or installer transaction semantics. The Windows failure was a task action/verification contract mismatch, so the repair is limited to the scheduled-task registration contract, the canonical training launcher depth requirement, release identity/cache binding and validation.

## Acceptance

The candidate remains **NOT ACCEPTED / NOT RELEASE** until the exact ZIP passes Windows installation through Research READY and OPERATIONAL_PROOF, then the R35 decision-dashboard UI is browser-accepted on the installed target.
