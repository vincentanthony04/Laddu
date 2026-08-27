# R46 Research Data Engine Closure

R46 is a backend-only research-compute correction from exact R45. It does not change the customer UI, chart, broker authority, scanner universe, Intraday session policy, decision admission, WFA qualification minimums, or ML production influence.

## Installed evidence that drove this build

R45/V4 proved the DuckDB catalogue is readable and contains 4,882,321 daily candle rows, 4,117 instruments and 3,726 historical dates. The expensive step begins after the raw daily-count proof when the trainer reconstructs identity/delivery/NSE joins and then performs multi-million-row Pandas merges. R45 also swallowed any `load_panel_from_lake` exception into `None`, causing unrelated failures to surface as an inaccurate "authoritative research panel is empty" message.

## R46 changes

1. `refresh_research_catalog.py` materializes `research_delivery_training_panel` once per changed catalogue fingerprint under the existing analytical-pipeline lock.
2. The panel performs candle identity, Delivery and NSE official-feature reconciliation in DuckDB. It uses effective-dated security-master identity where available and explicitly tags row-level current-catalogue fallback.
3. The source Parquet lake stays complete. The shadow trainer projection preserves every historical session while bounding each date to a deterministic research-compute cohort: 256 liquidity-core names plus up to 128 deterministic long-tail exploration names. This cap does not alter the NSE/BSE scanner universe or production selector authority.
4. `train_nse_smart_model.py` reads the materialized panel directly instead of loading separate multi-million-row candle, Delivery and official frames and merging them in Pandas.
5. Panel failures are stage-explicit (`ANALYTICS_DB_OPEN_FAILED`, `MATERIALIZED_PANEL_MISSING`, `MATERIALIZED_PANEL_READ_FAILED`, `MATERIALIZED_PANEL_EMPTY`, `PANEL_NORMALIZATION_FAILED`). The prior blanket `except Exception: return None` path is removed.
6. The Delivery historical trainer remains isolated Shadow research, uses one declared policy trial, and retains production weight/influence zero. WFA and alpha gates are not weakened.

## Acceptance boundary

Source QC cannot prove installed research convergence. On exact Windows R46, the catalogue refresh must report a non-empty materialized training panel spanning the retained historical date range; the historical trainer must complete or expose an explicit later-stage model/WFA blocker; historical PIT/feature-store progress must move; ML remains non-production and alpha remains NOT_VALIDATED until governed evidence qualifies it.
