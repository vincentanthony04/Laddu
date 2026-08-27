# Project Laddu R30 — End-to-End Architecture Convergence

State: **NOT ACCEPTED / NOT RELEASE** until exact installed Windows and live-market acceptance passes.
Broker authority: **NONE**.

## Why R30 is a rebuild rather than another micro-fix

R29 installed far enough to expose a runtime-system failure cluster rather than an installer defect:

- one-shot lifecycle closure failed at `intraday_capital_wfa` with PostgreSQL statement timeout;
- Research/Forward browser reads performed synchronous governance fan-out and could run for minutes;
- the governance WFA index migration existed in the package but was absent from the authoritative migration plan;
- deep-history convergence could continue consuming background capacity while lifecycle/WFA needed deterministic proof capacity;
- evidence already showing zero complete snapshots / insufficient settled calendar depth still allowed expensive WFA replay attempts.

R30 changes the ownership boundaries so these failures cannot simply migrate from one surface to another.

## Runtime lanes

1. **Operational write authority** — canonical decisions, risk, runtime state and operational mutations.
2. **Interactive operational read authority** — search, stock intelligence and latency-sensitive operational reads.
3. **Governance write authority** — model/research publication, settlement and immutable governance mutations.
4. **Governance read authority** — separate pool for Research UI projections, forward-progress reconciliation and WFA diagnostic reads.
5. **QuestDB/time-series** and **Parquet/DuckDB historical** authorities remain unchanged.

The governance read pool is intentionally a second connection pool against the same governance PostgreSQL database, not a second database or a second source of truth.

## Cache-only Research / Forward HTTP

`/api/quant-research-plane`, `/api/forward-progress`, and `/api/forward-evidence-clock` no longer execute database fan-out on browser threads.

`ResearchControlProjectionService` owns the expensive reads in one supervised background lane and publishes immutable in-memory snapshots. HTTP returns the latest snapshot immediately, including WARMING/DEGRADED truth when the producer cannot refresh.

## Lifecycle resource window

The one-shot lifecycle closure temporarily requests a bounded background-work pause. Deep-history/backfill yields while Research settlement/WFA/read-model reconciliation runs. The pause is always released in `finally`.

This is workload scheduling only. It does not weaken scanner, WFA, research, risk, decision or acceptance criteria.

## WFA evidence short-circuit

Before capital WFA replay, the lifecycle reads selector evidence depth. If complete snapshots are zero or settled calendar depth cannot possibly form the requested train/purge/test/embargo geometry, R30 persists `INSUFFICIENT_SETTLED_EVIDENCE` and skips the replay query.

This prevents a database-expensive query from being executed merely to rediscover a mathematically predetermined zero-fold result.

## Migration plan closure

`infra/postgres/governance/007_r26_wfa_query_indexes.sql` is now part of `MIGRATION_PLAN.json` as immutable migration `680007`. The migration remains idempotent and adds only read-path indexes.

## Deep-history acceptance budget

During customer acceptance, deep-history convergence uses one worker and smaller batches. Historical convergence continues, but it cannot monopolize PostgreSQL while customer/runtime proof is being collected.

## TradingAgents boundary

The external TradingAgents project remains architecture-reference only. R30 adopts no LangGraph/LLM runtime dependency and grants no LLM production-decision or broker authority. Useful patterns such as explicit staged roles, checkpoint/resume, structured outputs and separated risk review may be adopted only after the deterministic R30 customer path is accepted.

## R30 source gates

R30 must retain all inherited R27/R29 customer, installer, data-utilization, intelligence, lifecycle and Level-5 source gates, and additionally prove:

- dedicated governance-read capacity;
- cache-only Research/Forward HTTP;
- supervised Research projection;
- lifecycle bulk-yield window;
- WFA evidence-depth short-circuit;
- WFA migration 007 included in the authoritative plan;
- conservative deep-history concurrency;
- broker authority remains NONE.

Installed acceptance must then prove endpoint responsiveness, migration/index presence, lifecycle completion/final reconciliation, bounded database pool pressure and the exact customer workflow.
