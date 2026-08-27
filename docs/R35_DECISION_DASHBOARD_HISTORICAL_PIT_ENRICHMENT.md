# R35 Decision Dashboard & Historical PIT Enrichment

R35 is a bounded convergence release candidate over exact R34.

## Visual decision dashboard
- Premium semantic colour system: green bullish/actionable, red bearish/risk, amber waiting/deferred, cyan/blue live/navigation.
- Odometer-style rolling numeric transitions for market prices, workspace counts and scanner progress.
- Compact Market tape with full names and stronger financial typography.
- Compact Intraday/Delivery desk cards: state, scanned/universe, percentage, progress, Eligible/Short/Deep/Research/Final and context without empty space.
- One coherent time-axis grammar: intraday session date separators + clock time; daily/weekly/monthly `dd MMM` with year only at year boundary.
- Canonical S/R remains one nearest support/resistance; Major S/R uses explicit major evidence first and ranked structural fallback second, with ATR/price tolerance to avoid accidental suppression. Camarilla remains hidden.

## Historical PIT enrichment
R35 does **not** create a second model path. The existing `train_nse_smart_model.py` already owns:
- curated Parquet/DuckDB historical panel,
- point-in-time feature construction,
- historical regime labels,
- incremental content-addressed feature store,
- purged/embargoed OOF,
- capital-profile WFA,
- governed SHADOW publication.

The existing rollback-owned `ProjectLaddu-AI-Training` task is repointed to `run_historical_pit_enrichment.ps1`, which runs that same trainer at BelowNormal priority with a 504-day minimum and exact installed-build marker check. No historical reconstruction may overwrite genuine forward evidence or grant production/broker authority.

## Acceptance
R35 remains NOT ACCEPTED / NOT RELEASE until exact installed browser proof and post-install lifecycle non-regression are observed.
