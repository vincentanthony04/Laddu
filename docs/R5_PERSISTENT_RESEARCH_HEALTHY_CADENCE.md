# R5 — Persistent Research History + Healthy Scanner Cadence

## Customer invariants

1. **Research publication never disappears.** Once a candidate is returned to the customer Research surface, its stable candidate identity and first-seen timestamp are persisted through the retained Research observation authority before publication.
2. **Scanner rerank changes lifecycle, not history.** A candidate leaving the current shortlist becomes `RESEARCH_HISTORY / RERANKED_OUT`; it remains visible and counterfactually measured. If it re-enters, the same identity resumes.
3. **Research performance is independent.** Research Target/SL outcomes, R and percentage performance are Research-only evidence. They never enter Final/Model Paper realised INR P&L or Final accuracy.
4. **Normal scanner sleep is healthy.** `EXPECTED_IDLE` with an alive, non-stale worker is displayed as `SLEEPING`, including last cycle, next cycle and countdown. Scheduled sleep alone cannot create `DO_NOT_TRUST`.
5. **Real blockers remain fail-closed.** Stale/dead scanner state, database saturation/pressure and customer-read latency thresholds continue to degrade or block trust independently of scanner cadence.

## Frozen parent boundary

R5 is derived from the exact v131 Terminal Actionable UI R3 E2E candidate SHA-256 `903b4190666ed08ab0bb4e63dda2f60f9b509fc5269903c8b91a86529be8c620`.

R5 does not modify the deterministic decision engine, evidence engine, trade geometry, intraday session-structure mathematics, structural trade map, exact broker cost authority, Model Paper lifecycle authority, outcome taxonomy or vectorized evidence-screening mathematics. Broker authority remains `NONE`.

## Acceptance boundary

Source validation is not installed-market acceptance. The exact Windows target must still prove the R3 same-decision lifecycle/restart contract plus R5 Research retention and scanner-cadence presentation during a live session. The candidate remains **NOT ACCEPTED / NOT RELEASE** until those installed proofs pass.
