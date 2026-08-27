# R41 Workspace Design System + Intraday Lane Closure

R41 is an agile market-hours correction from exact R40.

## Customer UI
- `frontend/ui-system.css` is loaded last and is the release-level typography/spacing authority.
- Workspace desk cards no longer compress identity, progress, context, and five KPIs into one line.
- Intraday exposes Universe Sweep and Live Analysis as separate visible truths.
- Workspace, Stock Intelligence, Opportunities, Model Paper, Accuracy, Research, and Progress share one 14px financial UI scale with 11px minimum metadata.
- Financial numbers use tabular numerals; color remains semantic.

## Intraday lane ownership
- Every automatic/manual/lifecycle Intraday scan request now executes the same `_run_live_mode_scan_impl("intraday")` implementation under the single `intraday_analysis` lane.
- The legacy fast-lane public entry delegates to the canonical live scanner instead of owning a competing implementation on the same lane.

## Frozen
Installer, database schema/pools, Delivery, PIT/WFA/model governance, chart mechanics, DecisionRecord/risk, and broker authority are unchanged from R40.

## Index-level trust progress
- A successful periodic index-level refresh now publishes a changing verified refresh generation/cursor.
- A cycle that refreshes zero level sets fails closed as `NO_LEVELS_REFRESHED`; heartbeat alone is never treated as useful progress.
- This closes the false `index_levels no_progress` trust flip seen after R40 even when index refresh work was being accepted.
