# R40 — Intraday Single-Authority Closure

R40 is a surgical market-hours correction from exact R39.

## Evidence being closed

R39 production evidence showed healthy PostgreSQL capacity and a Delivery sweep
advancing normally, while Intraday exposed contradictory operational counters:
the supervised live-analysis worker, the customer universe-coverage projection,
and a duplicate virtual Operations row were not the same authority. Recovery
requests could also coalesce behind an in-flight `intraday_analysis` lane.

## Functional boundary

- Delivery analysis execution remains the deterministic Clean-Core executor.
- Intraday prepared analysis uses one bounded local worker so a pathological
  pure calculation cannot own the scan lane indefinitely.
- `intraday_scanner` is the recurrent live-analysis cycle authority.
- `intraday_coverage` is the whole-universe sweep authority and publishes its
  cumulative immutable-sweep counter directly to the supervisor.
- Operations no longer creates a duplicate virtual Intraday scanner row with
  coverage counters under the live-analysis component name.
- A coalesced live-analysis request is expected-idle only while the existing
  lane owner remains inside a bounded 75-second window; an older lane remains
  eligible for NO_PROGRESS/recovery.

No frontend, installer, chart, historical PIT, WFA, model-governance, broker,
DecisionRecord, risk, database-schema or Delivery-algorithm change is part of R40.
