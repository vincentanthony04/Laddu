# PL29 Publication Authority Contract Closure

## Windows-proven defect
PL28 completed model training and capital WFA, but durable publication replay was rejected with `new training publications require PARQUET_DUCKDB authority`. The trainer had placed the descriptive lineage `R46_MATERIALIZED_RESEARCH_PANEL_TO_INCREMENTAL_FEATURE_STORE` into the canonical `training_data_source` field.

## Correction
- Future trainer bundles set `training_data_source=PARQUET_DUCKDB`.
- The descriptive R46 lineage is retained as `training_pipeline_source`.
- Publication normalisation maps only the exact known PL28 R46 token to the canonical authority, preserving the token separately.
- Any other unknown training authority fails closed.
- Startup durable-outbox recovery therefore replays the existing completed PL28 bundle without retraining.

No model, factor, WFA threshold, scanner, trading, risk, R:R, cost or broker-authority logic is changed.
