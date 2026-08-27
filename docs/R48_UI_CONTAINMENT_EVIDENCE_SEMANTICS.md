# R48 — UI Containment + Evidence Semantics Closure

R48 is a narrow frontend/control-plane closure built from the exact R47 installation candidate. It does not change R47 intraday mathematics, R46 research/WFA/ML authority, broker authority, or backend runtime algorithms.

## Closed defects

1. **Progress evidence row collapse** — long blocker/evidence payloads can no longer collapse the text column into a character-wide strip. The row owns a bounded `minmax(0,1fr)` text column, uses normal word breaking, and truncates only the rendered preview. Copy actions retain the complete evidence object.
2. **Failure vs maturity semantics** — `Active Problems` contains only actionable/runtime failures and blockers. `Evidence Still Maturing` contains non-failure proof gaps and defaults collapsed. Global progress continues to show both counts.
3. **Hidden chart compositor containment** — leaving Stock Intelligence destroys Lightweight Charts canvas/compositor instances and suspends chart/live/projection timers. Returning to Stock Intelligence rebuilds the chart from retained verified in-memory candles and projections. Hidden routes cannot retain an active chart surface.
4. **Asset identity** — browser cache keys are explicitly R48 for app.css, ui-system.css and app.js. No R44 cache key remains in the browser entrypoint.
5. **Section controls** — the existing persistent Expand/Collapse contract is retained; `Evidence Still Maturing` defaults collapsed while active problems remain visible.

## Safety boundary

- Broker authority remains `NONE`.
- Production ML influence is unchanged/fail-closed.
- Alpha remains `NOT_VALIDATED` until governed evidence qualifies it.
- R47 Intraday Session Structure mathematics are frozen byte-for-byte.
- R46 Research Data Engine/WFA files are frozen byte-for-byte.
- R48 is source-sealed only. Installed Windows/browser proof remains required.
