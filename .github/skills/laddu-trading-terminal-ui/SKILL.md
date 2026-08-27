---
name: laddu-trading-terminal-ui
description: Design and review Project Laddu as a compact, premium, high-trust trading intelligence terminal. Use for Actionable, Stock Intelligence, Model Paper, Accuracy, Research, and Diagnostics UI work.
---

# Project Laddu Trading Terminal UI

## Mission
Design for one customer job: **identify the few stocks that are genuinely actionable now, understand the reason/risk in seconds, and see whether the evidence is live and trustworthy.**

The UI is a projection of canonical backend authorities. It never calculates or manufactures trading truth.

## Product hierarchy
1. Actionable Now
2. Watch Next
3. Stock Intelligence drill-down
4. Model Paper / open trade lifecycle
5. Accuracy + measurable outcomes
6. Research / learning
7. Engineering Diagnostics

Diagnostics must never dominate the normal trading workflow.

## Visual character
- Premium financial workstation, not generic SaaS dashboard.
- Dense but calm; high information-to-decoration ratio.
- Strong hierarchy, minimal card chrome, restrained borders.
- Light and dark themes must both feel intentional.
- Semantic color is functional: green actionable/positive, red risk/failure, amber waiting/weakening, blue/cyan live/context/navigation, grey unavailable only.
- Avoid decorative gradients, glassmorphism, parallax, giant hero typography, excessive animation, and large empty areas.

## First-screen rule
At 1366x768 and above, the first viewport should show:
- compact market/live strip;
- Actionable Now heading + mode controls;
- at least 4–5 actionable rows or a clear empty state;
- no large hero card pushing the list below the fold.

## Top market strip
Use one compact horizontal tape, not four equal KPI cards.
Recommended content:
`MARKET Bearish -0.55%  |  BREADTH 35/37 Negative  |  VIX 11.32 Normal  |  DATA 2s  |  MARKET CLOSED/LIVE`

Rules:
- 32–42px height where practical.
- `MARKET CLOSED`/`LIVE` is a compact status chip, never a large card.
- Timestamp and data age remain visible but secondary.
- If trust/freshness fails, show `NOT ACTIONABLE` prominently and disable action styling.

## Actionable table
The table is the product. It receives the strongest visual priority.

Primary columns:
`Rank | Stock | Mode | Setup | Evidence | LTP | Entry | Target | SL | R:R | Age | Status`

Secondary details (`Holding`, `Result`, `After`, full evidence lineage) may be shown in a compact secondary line, expandable row, or outcome view to prevent horizontal sprawl.

Rules:
- 40–48px row height.
- Strong stock symbol and status contrast.
- Numeric columns tabular and right-aligned.
- Entry/Target/SL visually grouped.
- Status is a small semantic badge, not a sentence.
- Whole row is clickable; stock symbol opens canonical Stock Intelligence.
- No browser-side admission or recomputation.
- Empty state says exactly why there are no actionable trades.

## Watch Next
Research candidates are visually distinct from actionable trades.
Use amber/blue cues, never green BUY styling.
Show the **one missing condition** in plain language, e.g. `Waiting: 5m close above 665.70 + RVOL > 1.4`.

## Stock Intelligence
The top section answers, in this order:
1. Current decision: BUY READY / WAIT / NO TRADE / ACTIVE / CLOSED
2. Freshness / as-of
3. Entry / Target / SL / R:R when canonical
4. Operating S/R + structural/major levels with explicit timeframe and role
5. Evidence families: regime, structure, trend, momentum, participation, relative strength, volatility, execution quality
6. What changes/invalidate the decision
7. Open external Upstox chart

Do not recreate a live chart in the critical path.

## Model Paper and outcomes
Lifecycle stays simple:
`READY -> ACTIVE -> CLOSED`
Result is immutable: `TARGET HIT / SL HIT / TIME EXIT / FORCED EXIT / EXPIRED`.
After-state is separate: `CONTINUED / REVERSED / RECOVERED / FLAT`.
Keep MFE/MAE/R/costs available in drill-down/learning views without cluttering the primary list.

## Typography
- Use a clear system/UI sans family already shipped with the product.
- Page title: ~20–22px, not marketing scale.
- Section title: 14–17px.
- Table body: 12–13px, never below 11px for meaningful data.
- Labels: 10–11px uppercase only where it improves scanning.
- Tabular numerals for market/trade data.

## Spacing and geometry
- Base spacing: 4/8/12/16/24.
- Small radii: 6–8px; avoid pills for ordinary containers.
- Prefer dividers and whitespace over nested boxes.
- Main content should use available width; no unnecessary centered narrow column.
- Remove dead whitespace before adding decorative content.

## Interaction
- Keyboard accessible.
- Visible hover/focus for every clickable stock/row/control.
- Sticky table header for long lists.
- Sorting is allowed only where it does not disguise canonical rank; default sort is canonical rank.
- No motion unless it explains state change; 120–180ms subtle transitions maximum.

## Accessibility
- WCAG AA contrast target.
- Do not encode state by color alone; pair color with text/icon.
- Minimum practical hit area ~32px desktop, 44px touch layouts.
- Respect reduced-motion.

## Required design review before completion
Review screenshots at minimum:
- 1920x1080
- 1600x900
- 1366x768
- dark theme
- light theme
- actionable rows present
- no-actionable empty state
- stale/not-actionable state
- market closed state
- long symbol/setup text

Before calling UI complete, perform the Simplification Audit in `references/acceptance-checklist.md`.

## Anti-patterns
Never ship:
- four or more equal KPI cards competing with the actionable list;
- marketing-style hero panels in the trading workspace;
- huge `What matters now` block with the table below the fold;
- monochrome tables where action/risk cannot be scanned;
- excessive bordered boxes inside bordered boxes;
- engineering worker progress on the main trading page;
- stale timestamps styled as live;
- a home-built chart presented as live without hard freshness/continuity proof;
- generic copy like `Ready` without the object/state it refers to;
- UI-calculated Entry/Target/SL/status/result.
