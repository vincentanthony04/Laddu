# R34 Customer UI & S/R Semantic Closure

R34 is a **frontend-only** branch from the exact R33 artifact (`ae2ded08d...b6a6e0`). The R33 installer/runtime/data-plane/scanner/WFA/lifecycle/research implementation is hash-frozen.

## Customer closure

- `S/R + Entry/T1/SL`: one canonical support + one canonical resistance, thin solid lines. Entry/T1/SL render only from authorised canonical desk geometry.
- `Major S/R`: at most one major support and one major resistance, deduplicated against the primary pair. No extra support/resistance ladder.
- Camarilla remains internal evidence only and is hidden from the customer chart.
- No-setup Decision & Risk view is progressive: no Entry/T1/SL/R:R placeholder wall; downstream `NOT_APPLICABLE`/`NOT_REQUIRED` gates are collapsed until a setup exists.
- Available RVOL/volume/index facts may appear as **Context** only; they never fabricate PASS for liquidity, official NSE delivery, or market/sector regime authority.
- Workspace desk cards now show state, scan progress, KPI chips, pace/wait context and larger financial typography with semantic colours.
- Chart/evidence panes use hard clipping/paint containment against browser compositor overflow.

## Acceptance boundary

R34 remains NOT ACCEPTED / NOT RELEASE until the exact installed artifact is visually checked on Workspace and Stock Intelligence. A failed customer visual check must not reopen R33 architecture; only the explicit frontend delta may change.
