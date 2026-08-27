# R43 — Workspace Ledger + Route Isolation

R43 is a frontend-only correction on the exact R42 package.

## Functional closure

- Fixes the R42 CSS regression that forced Workspace visible under every routed subpage.
- Enforces one active top-level page at a time.
- Makes Selected Candidates the single Workspace lifecycle ledger with Stock, LTP, ₹/% change, Entry, Target, SL, Hit, Next, Status and Net P&L.
- Folds canonical candidate, preparing and active rows into one de-duplicated list.
- Uses only backend-projected lifecycle/outcome fields; the browser does not manufacture trading authority.
- Settled outcomes leave active Workspace authority and remain represented by canonical Accuracy/Performance settlement projections.
- Removes the duplicate Final Decisions Workspace panel.

## Frozen scope

All R42 backend, installer, database, scanner, PIT/WFA/model governance, chart, DecisionRecord/risk and broker authority are frozen.
