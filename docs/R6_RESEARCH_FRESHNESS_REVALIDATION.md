# R6 — Research Freshness Lineage + Runtime Revalidation

## Boundary

Project Laddu remains **v131.0.0** and **Model Paper / broker authority NONE**. R6 is an installation candidate, not a production release. R6 descends from the exact R5 persistent-Research/healthy-cadence candidate and does not change canonical trade geometry, risk, settlement, cost, outcome, Intraday session mathematics, or production decision authority.

## Defect 1 — verified quote truth was dropped before Research capture

The live scanner already consumed a verified quote, but the immutable Research capture boundary previously received only `decision + instrument`. Provider timestamp, quote freshness, quote age and bid/ask evidence could therefore disappear before `QuantScanCaptureService` created the point-in-time feature snapshot. The snapshot then failed closed as freshness `UNKNOWN` even when feature coverage already met the 60% training floor.

R6 passes the **exact quote used by the scanner** into `research_capture_row` and revalidates it with the existing pure `quote_integrity_service` authority. Only provider-timestamp evidence is accepted. Receipt time is never promoted to provider time. A stale/unverified quote remains ineligible.

Preserved fields include:

- provider/source timestamp;
- `provider_timestamp_verified`;
- quote/price freshness state and reason;
- quote age;
- bid/ask when present (allowing the existing spread calculation to operate);
- received time when present.

This can convert a legitimately covered live Intraday snapshot from `PARTIAL / freshness UNKNOWN` to `COMPLETE / LIVE`, but **does not reduce the 60% feature floor** and does not fabricate sector-relative, delivery, event, spread or expected-move values when their source is absent.

## Defect 2 — corrected Research worker could remain hidden behind a stale persisted failure

The LightGBM local-name shadowing defect had already been corrected in the inherited R50 code, but an installed database could still retain the older `WORKER_FAILED` result. The Research orchestrator previously retriggered only when label/snapshot counts advanced, so a code correction alone could remain invisible for hours.

R6 adds an executable Research runtime fingerprint. `maybe_run_cycle()` now re-runs one governed single-spec cycle when the installed worker/orchestrator fingerprint changes even if data counts are unchanged. Normal cadence still suppresses unchanged-data + unchanged-code reruns. Production influence remains fail-closed.

## Acceptance invariants

R6 source validation must prove:

1. verified scanner quote lineage survives into immutable Research capture;
2. a 60%+ covered, lineage-verified, live Intraday snapshot can become `COMPLETE` without threshold weakening;
3. stale/unverified quotes remain `PARTIAL`;
4. bid/ask evidence is carried only when actually present;
5. worker source change forces one Research revalidation even with unchanged evidence counts;
6. unchanged worker + unchanged evidence remains cadence-suppressed;
7. R5 persistent Research and healthy cadence files remain byte-identical;
8. protected decision/math/geometry/cost/outcome authorities remain byte-identical to R5/R3;
9. full deployable, Python compile, frontend syntax, runtime lifecycle, data-utilization and terminal E2E gates remain green.

## Installed proof still required

After installation during market hours, exact runtime evidence must show:

- new Intraday Research captures carry `LIVE/FRESH` provider-timestamp lineage when the scanner quote is verified;
- `complete_snapshots` begins to advance when coverage/regime/lineage are otherwise sufficient;
- the old `production_validation_ready` worker exception is superseded by a new R6 worker evaluation result;
- Research history remains durable and separate from Final performance;
- normal scanner sleeping remains healthy and does not reduce trust;
- no broker authority or unqualified learned-model production influence is introduced.
