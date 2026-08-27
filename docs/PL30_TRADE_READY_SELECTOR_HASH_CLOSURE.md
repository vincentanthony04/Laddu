# PL30 Trade Ready + Selector Snapshot Hash Closure

PL30 is a bounded usability + research-governance correction derived from PL29.

## Customer surface
- Canonical Final / Trade Ready decisions are the first major Workspace decision surface, immediately below live market/trust context.
- Rows expose Rank, Stock, Desk, Action, Setup, LTP, change, Entry, T1, SL, R:R, governed quantity, explicit governed risk, freshness, signal age, holding period, lifecycle status, outcome and net P&L.
- Missing quantity/risk/freshness remains unavailable; the browser does not synthesize trading truth.
- No Final decision renders as `NO TRADE READY DECISIONS` and Research remains below as separate Watch Next evidence.

## Selector snapshot root cause and correction
PL29 could hash Python NaN/Infinity inside the quant snapshot before governance persistence. PostgreSQL JSONB persistence uses strict JSON and converts non-finite reals to `null`. The immediate read-back therefore contained semantically identical evidence but a different derived snapshot hash, producing `SELECTOR_FEATURE_SNAPSHOT_HASH_CONFLICT`.

PL30 normalizes the governed quant snapshot feature payload with the same JSON-safe semantics before hashing. Existing PL29 rows are not rewritten. A compatibility path accepts an old hash only when every immutable member field and every snapshot field other than the derived `snapshot_hash` is semantically identical. Any genuine feature, identity, timing, lineage, cost or population difference remains fail-closed.

## Frozen boundaries
No scanner engine, entry/target/SL/R:R mathematics, risk admission mathematics, WFA thresholds/metrics, estimator configuration, capital authority, broker authority or historical evidence is weakened or fabricated.
