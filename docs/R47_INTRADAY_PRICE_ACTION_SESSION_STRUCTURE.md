# Project Laddu R47 — Intraday Price-Action Session Structure + UI Clarity

## Purpose

R47 converges existing Intraday evidence into one auditable session-structure authority. It does not add another independent indicator and does not claim guaranteed profitability. The final Intraday geometry must be explainable from actual session price action first, with advanced mathematics and official NSE evidence acting as confirmation, risk, and context.

## Intraday session authority

The binding clock is Asia/Kolkata:

- 09:00–09:15 — pre-open intelligence only.
- 09:15–09:20 — observe-only while the first completed five-minute opening range forms.
- 09:20 onward — ORB5/live entries may be admitted immediately when every hard gate passes; there is no mandatory wait for 09:25.
- 14:15–14:30 — A+ setups only.
- 14:30 onward — no new Intraday entry.
- 15:00 — mandatory flat.

`IntradaySessionStructureAuthority` reconciles ORB5, session high/low and completed 5-minute swings, VWAP, EMA20/EMA50, same-clock relative-volume evidence, previous-session H/L, and already-validated historical S/R. A prior resistance/support changes role only after accepted price action proves the flip. A breakout candle cannot count as its own retest.

The resulting `operating_support` / `operating_resistance` are the session levels used for Intraday geometry. Major higher-timeframe levels remain visible as obstacles/context rather than overriding a clearly defended current-session zone.

## Official NSE evidence

Local curated NSE data is consumed as confirmation or risk evidence only. The live decision context now projects delivery-percentage surprise, delivered-quantity surprise, turnover/trade-count surprise, impact cost, surveillance state and the retained official context already available in the catalogue.

Official NSE evidence can increase/decrease confidence or block promotion. It cannot manufacture the rupee price of support, resistance, Entry, Target or Stop.

## Structural Entry / Stop / Target

- Planned Intraday trigger is derived from the session authority rather than a generic LTP placeholder.
- Structure owns invalidation. For a LONG the stop must sit beyond defended support; for a SHORT it must sit beyond defended resistance.
- If the structurally correct stop exceeds the desk risk budget, the setup is rejected instead of moving the stop inside normal price-action noise.
- The first validated structural obstacle owns T1 when it is inside the statistically reachable ATR envelope. ATR remains a feasibility/reachability measure rather than the primary target generator.
- Late extension/chasing is rejected.
- At the 15:00 cutoff, a target not reached is a signal FAILURE even when the forced economic exit happens to have positive net P&L. Signal quality and economic P&L remain separate truths.

## Support / Resistance role semantics

The selected-timeframe S/R engine now preserves native roles. A prior resistance cannot become support merely because current price is above it. The engine requires a directional break plus subsequent acceptance or a later retest/hold. Rejection scoring is directional: support earns credit from a reclaim/hold followed by upside response; resistance earns credit from rejection/hold followed by downside response.

## UI clarity included in R47

R47 also includes the already-requested bounded frontend improvements without changing chart mathematics:

- major sections on Stock Intelligence, Opportunities, Model Paper, Accuracy, Research and Progress & Proof can expand/collapse; section state persists locally;
- Progress & Proof has Expand all / Collapse all;
- research display states are SHORTLIST → WATCH → QUALIFIED → LIVE VALIDATION → FINAL; incomplete Entry/Target/SL can never display FINAL;
- Workspace market context becomes a compact two-row market map with NIFTY 50, SENSEX, BANK, VIX, breadth and the major sector indices available from the canonical market context;
- expanded market context shows a two-way Advancers / Decliners mover table using bounded in-memory Market Radar observations;
- subtle green/red move-intensity shading keeps numbers primary;
- chart functionality remains frozen apart from generic section collapse/resize containment.

## Research / WFA / ML boundary

R46 research-data-engine files and the selector/WFA qualification policy are frozen by R47. R47 does not promote learned ML, does not weaken the 252/504-day evidence requirements, does not fabricate PIT history, and does not claim alpha. Learned-model production influence remains governed/fail-closed and broker authority remains NONE.

## Acceptance boundary

R47 is a source-sealed installation candidate only. It is not release accepted until the exact R47 package passes Windows installation, browser workflows and market-hours proof. Installed acceptance must demonstrate at minimum:

1. ORB5 is available at 09:20 with no entry before 09:20.
2. A+ gating applies from 14:15; no new entry is admitted from 14:30; Intraday is flat by 15:00.
3. displayed/decision S/R role flips are backed by acceptance/retest evidence rather than current-side reclassification.
4. session support/resistance aligns with ORB5/VWAP/EMA/volume price action for sampled governed instruments.
5. structurally invalid Stop/Target geometry is rejected rather than silently tightened or stretched.
6. official NSE evidence changes confidence/risk only, not the price of a level.
7. R46 historical PIT/WFA/ML convergence remains non-regressed and ML production influence does not increase without qualification.
8. the compact market-map and collapsible-section frontend behaves correctly in the installed browser.
