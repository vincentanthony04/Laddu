# Project Laddu PL27 — Materialized Training Panel Schema Closure

## Windows-proven failure
A real governed run on 2026-08-21 successfully refreshed the research catalogue and materialized `research_delivery_training_panel` with 4,694,211 rows across 3,523 symbols and 3,729 dates (2011-08-08 through 2026-08-20). Training then failed before model fitting with DuckDB `BinderException`: `research_liquidity_rank` was listed in `SELECT * EXCLUDE(...)` but is not a column in the materialized panel.

## Exact correction
`load_panel_from_lake()` now excludes only `research_liquidity_value`, the auxiliary column that the R46/R4.2 materializer actually creates. No rows, features, labels, WFA gates or model parameters are altered.

## Frozen authorities
PL26 factor IC/IR/redundancy governance, PL25 catalogue/WFA arbitration, historical PIT scheduling, workload governor, scanner orchestration, DecisionRecord/trade geometry, exact broker cash costs, risk/admission and WFA mathematics remain unchanged. Broker authority remains `NONE`.

## Required installed proof
Re-run `train_ai_model.ps1`. A schema BinderException about `research_liquidity_rank` is no longer acceptable. The next outcome must be model/factor/WFA progress or a new explicit downstream blocker.
