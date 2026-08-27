# R37 Workspace Numeric & Connection Resilience

R37 is a narrow browser-only correction over exact R36.

## Exact root cause
R35 introduced the odometer activation call `activateOdometers($('[data-page-panel="workspace"]'))`. The `$` helper is `document.getElementById`, not a CSS-selector helper, so that expression returns `null`. `activateOdometers` then attempted `root.querySelectorAll`, throwing after the Workspace API had already returned successfully. `loadWorkspace` caught that render exception as if the API request had failed, which produced the misleading `Connection interrupted` banner. At the same time, the odometer HTML started as an empty span, so market prices/scanner numerators/percentages remained blank. R36 preserved the R35 frontend and inherited both symptoms.

## Correction
- Workspace animation root now uses `document.querySelector(...)`, with a null-root guard.
- Every dynamic number is real formatted DOM text on first paint.
- Smooth numeric tweening updates the real text over ~520ms; if animation is unavailable or throws, the final value remains visible.
- Reduced-motion users receive the final value immediately.
- Existing R36 Workspace polling/retry semantics are otherwise unchanged.

## Non-regression boundary
No backend, service, installer, database, scanner, decision/risk, lifecycle, WFA or historical-PIT training implementation is changed from R36.

R37 remains NOT ACCEPTED / NOT RELEASE until exact installed browser proof passes.
