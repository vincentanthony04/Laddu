# R39 — Backend Import Preflight QC Closure

R38 failed before runtime quiescence during the installer's staged-backend import gate because the newly added autonomous historical PIT service imported `PORT` from `config.py`, where the declared authority is `DEFAULT_PORT`.

R39 changes one runtime line of authority: `HistoricalPitSweepService` imports and uses `DEFAULT_PORT`. It does not change scanner mathematics, database schemas, installer orchestration, chart behavior, canonical decisions, risk, model-paper authority, or broker authority.

The R39 release gate reproduces the installer's exact `PROJECT_LADDU_DATA_PLANE_MODE=test` backend import and independently imports the changed PIT module. The remainder of R38 is hash-frozen.
