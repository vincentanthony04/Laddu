# R38 Production Usability Sprint

R38 is the market-hours launch triage candidate. It does not reopen chart feature work. It protects R37 outside a narrow usability boundary and concentrates on: truthful trader trust state, visible shared root cause in Operations, governance write capacity, stale-wait cleanup, and autonomous low-priority historical PIT/WFA enrichment.

## Binding customer contract

- Trading pages always expose TRUSTED / DEGRADED / DO NOT TRUST from runtime blockers, customer read latency and database authority pressure.
- DO NOT TRUST forces customer Decision presentation to NO-TRADE/System blocked; it never rewrites stored canonical evidence.
- Governance `requests_queued` is treated as a cumulative pool statistic, not live queue depth. Current waiting/availability drive saturation truth.
- Historical PIT/WFA starts with the runtime as P5 work, checkpoints, yields to selected-stock/scanner/database pressure, and resumes automatically. The 18:30 scheduled task is only a watchdog/retraining trigger.
- Research shows historical PIT state and actual settled post-cost evidence. No alpha is fabricated and production influence remains governed.
- Operations groups symptoms behind the shared dependency/root cause instead of making the operator debug unrelated worker alerts.
- Chart feature development is frozen for this sprint. Existing chart surfaces remain secondary evidence and cannot override trust/admission.
