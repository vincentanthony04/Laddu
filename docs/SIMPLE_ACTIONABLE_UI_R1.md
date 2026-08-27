# Project Laddu — Simple Actionable UI R1

## Product objective
The customer surface answers one question first: **what is actionable now?**

The UI is a projection of canonical mathematics and lifecycle authorities. It does not calculate trading truth and does not promote research candidates itself.

## Primary page
### Actionable Now
Columns are deliberately limited to:

`Rank | Stock | Mode | Setup | Evidence | LTP | Entry | Target | SL | R:R | Signal Age | Holding | Status | Result | After`

Rules:
- only canonical `final_signals` with `final_signal_authority` and immutable decision/signal identity are eligible;
- Entry/Target/SL are canonical/frozen geometry and Model Paper fill/managed-stop projections where exact lineage exists;
- Status describes lifecycle (`READY`, `ACTIVE`, reconciliation state);
- Result describes immutable trade result (`TARGET HIT`, `SL HIT`, `TIME EXIT`, etc.);
- After is a separate post-exit observation (`CONTINUED`, `REVERSED`, `RECOVERED`, `FLAT`);
- missing evidence is `—`, never manufactured as zero or a business state.

### Watch Next
Research candidates remain separate from Actionable Now. The table shows the closest candidates and the exact missing trigger/evidence. It cannot create a trade.

### Recent Outcomes
Settled canonical Model Paper records show Result, outcome taxonomy, post-cost P&L, realized R, holding time and independent After state.

## Market truth
A compact market pulse shows Market, Breadth, Volatility and System/Trust. If canonical trust does not permit decision admission, the customer sees `NOT ACTIONABLE`; no fallback row is manufactured.

## Chart boundary
The internal recreated chart is disabled from the customer decision path. Stock Intelligence links externally to `https://tv.upstox.com` for broker-hosted live charting. The hidden legacy chart DOM is retained only for compatibility while the rest of the frontend is converged; `INTERNAL_CHART_ENABLED = false` prevents it from becoming a live/decision authority.

## Navigation
Primary navigation is intentionally small:
1. Actionable
2. Stock Intelligence
3. Model Paper
4. Accuracy
5. Research
6. Diagnostics

Research Watch/Opportunities is reachable from Watch Next but is not a first-order trading destination. Engineering diagnostics are explicitly secondary.

## Acceptance
This UI source checkpoint is not a Windows-installed release. Before release it still requires installed-browser/live-market acceptance against the mathematics-green reconstruction.
