# Project Laddu — Current Architecture

## v124 Product Recovery boundary

The customer surface is one responsive seven-page shell: Workspace, Stock Report, Opportunities, Accuracy, Performance, Research & Learning, and System & Operations. The primary Workspace and Research reads are bounded cache-only projections; browser requests do not invoke provider, cold-storage, training, repair, or discovery work. Stock Report starts with at most 500 canonical bars, pages older history on demand, and consumes backend-owned forming bars. The UI publishes LIVE only at two seconds or fresher, DELAYED through ten seconds, STALE after ten seconds, and CLOSED only from explicit market-session authority. Accuracy uses settled decisive outcomes only; Performance uses settled Model Paper economics and reports open mark-to-market separately.

Project Laddu is an automatic Model-Paper-only NSE/BSE cash-equity trading-intelligence system. Broker authority is `NONE`.

## Runtime authorities

- Hot runtime: current-session quote/tick/in-flight state in process memory.
- Operational authority: PostgreSQL for canonical decisions, Model Paper positions, risk, runtime control, and ledgers.
- Governance authority: separate PostgreSQL database for immutable research populations, features, predictions, model publication, forward evidence, learning governance, and legacy-research quarantine/checkpoint state.
- Market time-series authority: QuestDB for ticks/bars/quality events.
- Historical/research authority: content-addressed Parquet + DuckDB read/analytics plane.

### PostgreSQL runtime resilience

Each repository holds a stable logical `PostgresAuthority`; it does not own or
cache a physical pool. The authority publishes one verified physical pool
generation at a time. A persistent single-owner supervisor detects current-
generation connection loss, constructs a fresh candidate pool off the request
path, requires checked SQL, and atomically swaps generations. The damaged pool is
retired and never admitted again. Transactions remain pinned to their admitting
generation and late failures from retired generations cannot degrade the current
one.

Recovery admission and pool waiters are bounded. Pool saturation, statement/lock
timeouts, serialization/deadlock, constraints, and SQL errors are not database-
liveness evidence. Idempotent reads may retry once after a newer healthy
generation is published; writes never retry implicitly. A lost COMMIT
acknowledgement is an explicit unknown outcome that must be reconciled by the
operation's PostgreSQL identity or transactional-outbox record.

## Trading/product boundaries

- Production desks: `Intraday` and `Delivery` only.
- Universe: NSE cash first, BSE cash fallback/BSE-only listings, required cash indices and sector indices.
- Futures/options are excluded from the active scanner and trade universe.
- Intraday is same-day only and subject to the governed late-entry/close policy.
- One canonical DecisionRecord must remain consistent across Today Entries, Stock Intelligence, Model Paper and Signal Ledger projections.
- Model Paper is the only economic position ledger used by the active application.

## Evidence and learning

Signal Age, continuous reassessment, append-only signal lifecycle evidence, settlement, performance and governed learning remain first-class authorities. Learning cannot grant broker authority or bypass risk, cost, point-in-time-data, walk-forward, holdout or human-governance boundaries.

## Installer transaction

The Windows installer is a fail-closed durable state machine. Package proof, environment/prerequisite proof, data-authority proof, retention snapshot, runtime quiescence, state preservation, forward-only schema work, research-governance migration, payload activation, startup, identity verification and operational proof are distinct ordered phases. Any pre-commit failure must either require no rollback or prove restoration of the prior runtime owner.

## Materialized foreground serving boundary

Interactive usability is a separate architectural authority from durable storage. A browser/API request may read hot runtime state and the last completed materialized projection, but it may not synchronously reconstruct truth by opening the historical/research lake or by running physical reconciliation across multiple stores.

- `ForegroundProjectionCache` is the bounded app-scoped projection primitive. It coalesces same-key refresh work, retains the last completed value, and schedules stale/cold production on bounded worker threads.
- Cold Parquet/DuckDB reads, QuestDB history reads, physical-store reconciliation, provider historical planning, and expensive PostgreSQL/reference aggregation are producer/research operations, not HTTP request work.
- A cold foreground request returns explicit `WARMING` or last-known/stale truth promptly. `WARMING` is never evidence of completeness; installed acceptance must separately prove that the producer converges to authoritative data.
- Stock Report snapshots are keyed by exact symbol + desk/mode. Cross-symbol cache reuse remains fail-closed.
- Chart/history reads serve hot runtime plus the last completed canonical candle projection. Explicit refresh/backfill schedules repair independently and cannot block the browser.
- `/api/system-health` is a projection read. Physical Parquet/QuestDB/PostgreSQL/reference reconciliation runs in the health producer and publishes the next completed snapshot.
- The cold historical/research plane remains authoritative for point-in-time backtests, model research and deterministic historical depth. Removing it from the HTTP request path does not reduce history, timeframes, mathematics or evidence families.

### Process isolation intent

The installed product remains one Project Laddu application, but workload ownership is explicit:

1. live ingestion/materialization owns stream-driven current state;
2. foreground API owns bounded projection reads only;
3. historical/backfill/compaction owns provider repair and cold-store maintenance;
4. scanner/research workers own broad quantitative/ML work;
5. PostgreSQL remains canonical operational/governance authority and no projection can create trading authority.

## UI information hierarchy (Candidate 15)

`Trading Workspace` and `Stock Report` are intentionally information-dense trader surfaces. `Performance`, `Research`, `Settings`, and `System & Operations` are customer-polished summary-first surfaces.

- Internal scheduler/controller/thread vocabulary is diagnostics-only. States such as `yielding_to_selected_stock` must never be humanized into visible trading-workspace text.
- The chart toolbar is a two-row visibility contract; the timeframe/candle-control row cannot collapse to zero height and all 10 canonical timeframe controls must be visible/clickable in browser proof.
- `System & Operations` is one control plane ordered: consolidated status -> primary blocker -> safe recovery -> available capabilities -> market-data/scanner state. Jobs, controller detail, logs, endpoint catalogue and raw proof are progressively disclosed diagnostics.
- Secondary pages must answer what matters, what changed, and what needs action before exposing implementation detail.

## Candidate 17 memory-only foreground and priority materialization closure

Candidate 16 Windows evidence is forensic-only and is not release lineage. Candidate 17 is rebuilt from the Candidate 13 source boundary. C16 improved selected-stock completeness but still proved that persisted foreground fallbacks and N+1 cold materialization could starve convergence on the real retained data set.

The runtime has two asynchronous work classes. `LocalProjectionDispatcher` owns deterministic materialization over local canonical authorities with bounded coalesced workers. `BackgroundRepairDispatcher` owns provider/network/exact-gap repair with separate low concurrency. Provider delay therefore cannot starve selected-stock, chart or system-health local materialization.

Technical materialization is single-pass: bounded intraday source frames plus the required deep daily source are read once and reused for all ten canonical MTF frames, S/R, indicator mathematics and price-performance anchors. The 12 approved QuantStrategyEvidenceContract families, Master Candle, breakout/retest, compression/expansion, NSE Delivery mathematics, volume/RVOL, sector/breadth and shadow pattern evidence remain intact.

Workspace and Stock Report remain dense trader surfaces. Trading UI shows market state, counts, results and concise blockers only; scheduler/controller/thread implementation vocabulary is diagnostics-only. Performance, Research, Settings and unified System & Operations remain summary-first. System & Operations orders its primary surface as status -> blocker -> safe recovery -> capability/data state, with jobs/logs/proof progressively disclosed.

Storage is governed, not hard-capped: Docker VHDX warning 20 GB, critical 23 GB, operational target <=25 GB. The system never automatically prunes Docker volumes or applies a hard database quota. Durable PostgreSQL/QuestDB authorities are protected; maintenance/reclamation is explicit and observable.


## v123.0.0 integrated product-test boundary

- PostgreSQL generation-supervisor recovery is inherited unchanged from the C28/v122.0.0 10-cycle Windows destructive qualification.
- Transactional outbox claim acquisition is explicit writer work even though the PostgreSQL function is invoked through `SELECT`; it cannot use a read-only/retryable transaction.
- Research-to-Final attribution is joined by exact `population_fingerprint` lineage, preventing ambiguous or cross-population candidate joins.
- Scanner supervision distinguishes deliberate higher-priority interactive yielding from failure, and Intraday full-cycle completion is an accountable progress unit.
- v123.0.0 remains an installation candidate for full UI/E2E product testing; it is not production-ready and broker authority remains NONE.


## Intelligence evaluation guard (R18)

The deterministic mathematical selector remains the production baseline. Any ML/Hybrid complexity must prove incremental post-cost value on the exact same immutable candidate populations; absolute profitability is insufficient. Promotion evidence now includes executable leakage canaries, within-population null-alpha permutation falsification, paired moving-block-bootstrap complexity contribution, matured RankIC/NDCG efficacy monitoring, and ML-only authority withdrawal. Forward learning remains append-only and human-governed: deterioration may require quarantine review, but no learning loop may mutate production rules, grant capital, or create broker authority automatically.
