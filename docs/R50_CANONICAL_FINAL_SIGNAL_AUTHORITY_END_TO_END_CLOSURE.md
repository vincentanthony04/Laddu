# R50 — Canonical Final Signal Authority End-to-End Closure

## Boundary

Project Laddu remains **v131.0.0**. R50 is an installation candidate, not a production release. Broker authority remains `NONE`; learned-model production influence remains governed/fail-closed. Exact Windows installation, browser acceptance, and market-hours acceptance remain required before production acceptance.

R50 is rebuilt from the exact R49 source-sealed installation candidate. The change set closes the live defects observed on 19 Aug 2026 without weakening the R47 intraday mathematics or R46 research/WFA governance contracts.

## P0 — Final Signals single authority

Final Signals no longer synthesizes rows from `active`, `preparing`, `candidates`, sticky scanner projections, or the legacy selected-signal projection.

The only clean Final Signal source is `ProductionCanonicalDecisionRepository.active_decisions()`. Canonical decisions own immutable signal identity and frozen Entry / Target / original Stop. An open Model Paper position may augment that decision only through exact namespaced persisted lineage:

- `decision_id` ↔ `decision_id`
- canonical `signal_id` / `source_signal_id` ↔ position `signal_id` / `source_signal_id`

Symbol, timestamp proximity, rank, or latest-row heuristics are forbidden as join authority.

An open Model Paper position with broken/missing canonical lineage is retained for risk visibility but is explicitly rendered `RECONCILIATION_REQUIRED`; it cannot masquerade as a clean Final Signal.

## Quote overlay contract

Current LTP, absolute change and percentage change may refresh independently from the quote authority. Quote refresh is prohibited from replacing Entry, Target, Stop, signal timestamp, holding horizon, decision ID, or lifecycle state.

If a verified quote has already crossed the frozen Target or active Stop for an open position while lifecycle settlement is lagging, the read model renders `RECONCILIATION_REQUIRED` / `TARGET CROSSED` or `STOP CROSSED`. The read path never mutates settlement authority.

## Timeline semantics

The former combined `Age / Timeline` column is removed. Final Signals now exposes:

1. **Signal Age** — elapsed time from the persisted signal/decision generation timestamp.
2. **Holding Period** — only a strategy/canonical declared `holding_period`, `target_window`, `horizon`, or `expected_horizon`. Generic Delivery defaults such as `max_holding_period` are deliberately ignored; missing authority renders `—`.
3. **Position Age** — elapsed time from persisted Model Paper `opened_at`; absent before a position is actually opened.

## Model Paper admission

Quote-side Model Paper admission no longer reads `selected_signals`. It evaluates canonical active decisions with production publication authority and exact `decision_id` lineage only.

## Runtime closures included in R50

- Fixes the LightGBM worker `production_validation_ready` local-name shadowing defect that produced `UnboundLocalError` while preserving fail-closed production influence.
- Expands the dedicated interactive PostgreSQL read pool from 1–4 to 2–8 connections to prevent the observed four-connection foreground starvation from monopolizing customer reads. Operational/governance pools remain separate.
- Reworks Index Levels refresh to be local-candle/cache first, schedule exact missing history, and truthfully yield to selected-stock priority instead of repeatedly performing blocking foreground provider bursts and being labelled `NO_PROGRESS`.
- Startup card cache contract now includes `final_signals` and `active_positions` explicitly.

## Acceptance invariants

R50 source validation must prove all of the following:

- Final Signals browser source is only `payload.final_signals`.
- every clean Final Signal has canonical final-signal authority and a persisted decision/signal ID.
- Final rows use exact ID lineage; no symbol/time join is allowed.
- canonical-only and exact-position-linked rows preserve frozen trade geometry.
- orphan open positions fail visibly as reconciliation-required.
- Signal Age / Holding Period / Position Age are separate fields and columns.
- generic `max_holding_period` cannot populate Holding Period.
- Model Paper admission source is canonical active decisions, not selected-signal projections.
- quote crossing can never remain a clean active/hold presentation while lifecycle reconciliation is pending.
- LightGBM validation-name shadowing is absent.
- interactive read capacity and cache-first Index Levels logic are present.
- R49 parent bytes outside the declared R50 boundary are unchanged.
- customer vertical, lifecycle authority, data utilization, intelligence evaluation, Level-5 source gates, package inventory, source attestation, Python compile and frontend JS syntax remain green.

## Installed proof still required

Source proof cannot manufacture Windows/runtime evidence. Exact installed R50 must still prove:

- installer transaction and state preservation;
- browser asset binding to R50;
- TITAN/any Final row has one decision lineage across Workspace, Stock Intelligence and Model Paper;
- no stale legacy Final rows reappear after cache refresh/restart;
- target/stop lifecycle reconciliation under live/closed verified quote conditions;
- Index Levels reaches useful progress without foreground starvation;
- interactive customer read latency under the normal scanner/research workload;
- R47 market-hours intraday timing/price-action acceptance.
