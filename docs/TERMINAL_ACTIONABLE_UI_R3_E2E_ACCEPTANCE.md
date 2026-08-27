# Project Laddu — Terminal Actionable UI R3 End-to-End Acceptance

State: INSTALLATION CANDIDATE ONLY. NOT ACCEPTED. NOT RELEASE.

R3 exists to stop source-test success from being confused with customer-product success.

## Mandatory installed gate

The installer must launch a clean Microsoft Edge profile against the exact installed service before commit and prove:

- `/api/frontend-identity`, served bytes, installed frontend files, DOM build marker and visible version marker are the same exact build.
- Browser trust equals the newest revision from the canonical live/workspace trust authority.
- Frontend identity failure, workspace staleness or runtime distrust makes the customer surface non-actionable.
- Scanner coverage determines ranking scope numerically; incomplete sweeps are `EVALUATED_SUBSET_ONLY`, never a full-market rank.
- Partial/unknown/stale candidate evidence cannot show a normal Evidence score.
- Closed-market research cannot be styled as `LIVE VALIDATION`.
- Empty Actionable/Outcome surfaces collapse and the customer workspace uses the actual viewport without horizontal overflow.
- If an actionable Final decision exists, its exact `decision_id`, authority and frozen Entry/Target/SL must match the Stock Intelligence / Model Paper lineage rules.
- Browser console and material network failures are zero.

A source test cannot substitute for this gate.

## Persistent same-decision vertical tracker

Full acceptance follows **one exact canonical `decision_id`**. A different historical signal, position or settlement cannot satisfy a later stage.

The tracker state machine is:

`WAITING_FOR_ACTIONABLE`
→ `ACTIONABLE_OBSERVED`
→ `MODEL_OPEN_OBSERVED`
→ `SETTLED_OBSERVED`
→ `AFTER_OBSERVED`
→ `RESTART_VERIFIED`

At first capture the preferred Intraday desk must have a numerically complete governed sweep and runtime trust must permit admission. Frozen Entry/Target/SL geometry is recorded and any later geometry drift is a hard failure.

The exact decision must then be observed in Model Paper, canonical settlement, Accuracy/Performance and post-exit follow-through. Settlement requires an immutable result/outcome, finite net P&L and exact settlement identity.

## Follow-through / After authority

`Result` is immutable. `After` is a separate observational label and can never rewrite the settlement result.

Intraday follow-through uses exact post-exit horizons: `15m`, `30m`, `60m`, and session `close`. A much-later sparse candle cannot substitute for a missing exact horizon.

Delivery follow-through uses exact trading-session horizons: `1D`, `3D`, `5D`, `10D`, and `20D`. Missing expected trading-session evidence leaves the horizon pending; later sessions cannot silently shift the label.

Valid measured After states are `CONTINUED`, `REVERSED`, `RECOVERED`, or `FLAT`; unavailable/incomplete evidence remains pending.

## Actual restart persistence proof

`/api/ready` exposes a per-process `process_boot_id`. Restart acceptance requires:

- capture of the pre-restart boot ID;
- an actual `Restart-Service ProjectLaddu`;
- a different post-restart boot ID;
- the **same tracked settlement and After evidence** after restart.

A browser refresh does not count as a restart.

## Full live acceptance runner

Run `RUN_FULL_EXACT_CUSTOMER_VERTICAL.cmd` during a live market session. It elevates once, invokes the persistent `-FullLive` acceptance loop, tracks one exact Intraday decision through the full lifecycle, performs the real service restart after `AFTER_OBSERVED`, and rechecks persisted evidence.

The only final live product pass state is:

`FULL_EXACT_CUSTOMER_VERTICAL_PASSED`

`SAME_DECISION_LIFECYCLE_PENDING`, `TRACKING_WINDOW_EXPIRED_NOT_ACCEPTED`, installation/source tests, or partial historical evidence are **not** release acceptance.

## Full live acceptance contract

`market data → complete desk sweep → Watch → canonical Final decision → Actionable → Stock Intelligence → Model Paper → lifecycle → settlement → Result/Outcome → After → Accuracy → actual restart → same immutable settlement`

The exact decision identity must survive every transition. Target/stop crossing may settle or display reconciliation-required; it may never remain a clean ACTIVE/HOLD state after the crossing is known.

Predictive ML/Alpha authority remains zero until its independent forward qualification gates pass.
