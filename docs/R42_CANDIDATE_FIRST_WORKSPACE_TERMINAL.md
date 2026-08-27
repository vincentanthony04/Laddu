# R42 Candidate-First Workspace Terminal

R42 is a frontend-only Workspace correction on the exact R41 parent.

Binding hierarchy:
1. Trust / market state summary.
2. Selected candidates — ranked from the published candidate population, not final decisions only.
3. Intraday and Delivery execution state as two compact one-line rows.
4. Market and sector context as supporting one-line rails.
5. Final canonical decisions.

Removed from Workspace:
- MARKET PULSE descriptive heading.
- "Verified benchmark context" helper copy.
- SECTOR PULSE descriptive heading.
- "NSE sector/index participation from the same market authority" helper copy.
- tall two-card desk layout.

R42 changes no backend, installer, database, scanner, PIT/WFA, model-governance, chart, DecisionRecord/risk or broker authority code.
