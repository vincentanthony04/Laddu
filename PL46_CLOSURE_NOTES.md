# PL46 Defect Cluster Closure — v131.1.x

Build marker: `production-usability-r8-pl46-defect-cluster-closure-8086`
Base: PL45 baseline (v131.0.0) as supplied in PL45.zip.
Scope: the six P0 defects listed in DEFECT_REGISTER.md, cluster A-E of
CLAUDE_CODE_PROMPT.txt. No WFA/statistical/risk/cost/PIT gate weakened.
Broker authority unchanged: NONE.

## What changed, and why

### P0-01 — PIT timestamp/feature-lineage semantics (source-fixed, tested)
File: `backend/core/scan_orchestration_rows.py`

Root cause: `classify_quote()` deliberately never emits a `received_at`
(a local HTTP clock is not a freshness signal). `research_capture_row()`
only overwrote `received_at` when the quote transport itself supplied one.
Real Upstox quotes usually don't, so a **stale `received_at` inherited from
an earlier pipeline hop** survived untouched. Once `source_as_of` refreshed
to the new quote's time, it could land after that stale value, manufacturing
`INVALID_TIMESTAMP_ORDER` on a valid observation (observed on
MAHABANK/MCX/PAYTM/SYNGENE).

Fix: `received_at` now always binds to the actual local receipt of *this*
quote unless the transport supplies its own — never carries a stale value
forward.

Test: `validation/verify_pl46_pit_timestamp_lineage_closure.py`

### P0-02 — Missing three-arm prediction self-repair (source-fixed, tested)
File: `backend/core/research_lifecycle_advance_service.py`

Root cause: `_advance_desk()` only ever rebuilt a population when it was
*absent*. If the population existed but heuristic/quant/hybrid predictions
were partially missing, it reported `THREE_ARM_CAPTURE_INCOMPLETE` forever
with no repair path.

Fix: on incompleteness, calls `SelectionPlatformService.evaluate_population()`
against the same frozen population's own metadata/rows. Safe because
`record_population()` is fail-closed/idempotent (identical content in =
identical fingerprint out, never a second identity) and prediction
persistence is `INSERT OR IGNORE` — only backfills missing candidate/arm
pairs. Hard safety check: if repair would return a different
`population_fingerprint`, it is refused and surfaced as an error.

Test: `validation/verify_pl46_three_arm_self_repair_closure.py`

### P0-03 — Monitoring agent reporting HEALTHY while semantically blocked (source-fixed, tested)
File: `backend/core/operations_control_service.py`

Root cause: `_monitoring_agent_pass()` computed HEALTHY/WATCHING/
RECOVERY_REQUIRED purely from supervisor **worker liveness** states.
Research being semantically blocked (`FEATURES_INCOMPLETE`,
`THREE_ARM_CAPTURE_INCOMPLETE`) never touches a worker's liveness state, so
with every worker fine it reported HEALTHY.

Fix: new `_research_semantic_blockers()` reads
`ResearchLifecycleReconciliationService.status()` per desk and classifies
each state into recoverable / terminal-blocked / expected-wait.
`_monitoring_agent_pass()` folds this in: recoverable blockers trigger the
already-existing bounded P0-02 repair; terminal blockers force
`RESEARCH_BLOCKED`; either rules out HEALTHY.

Test: `validation/verify_pl46_monitoring_semantic_blocker_closure.py`

### P0-04 — Mutable/inconsistent E2E lifecycle snapshots (source-fixed, tested)
File: `backend/core/operations_control_service.py`

Root cause: `_run_full_lifecycle()` builds ONE `results`/`agents` dict and
mutates it in place across all 8 stages, passing the same object reference
into every `_publish_lifecycle()` call. The old `_publish_lifecycle`/
`lifecycle_status()` did only a shallow `dict(...)` copy — nested objects
stayed shared, so a caller holding an early snapshot could silently see
later-stage content once the runner mutated the same dict further.

Fix: both `_publish_lifecycle()` and `lifecycle_status()` now
`copy.deepcopy` — every publish is frozen at that instant, every read is an
independent copy.

Test: `validation/verify_pl46_lifecycle_snapshot_immutability_closure.py`

### P0-05 — Capital WFA 503 (partial closure — diagnosability only, honestly incomplete)
Files: `backend/http_server.py`, `backend/routes_get_research.py`

I could not find an unguarded crash path in `WalkForwardValidationService.
validate()` or `SelectionWalkForwardReplayService.replay()` for sparse/empty
data — both already return graceful `ok:True` blocked states. I cannot
confirm the exact cause of the installed 503 without your machine's real
data. What I found and fixed instead: **every route failure in this product
logged and returned only `str(exc)`** — no file/line/function — anywhere,
which is why PL44's installed evidence showed "HTTP 503" with no way to
diagnose it.

Fix: `Handler._diagnostic_location()` walks the traceback and reports the
deepest backend-code frame (`file:line in function`); wired into the
top-level GET/POST exception handlers and the capital-WFA route's own catch
block; full traceback now logged via `app.event()` for evidence collection.
No statistical/risk/cost/PIT gate touched.

**Action needed from you:** re-run the installed capital WFA. If it 503s
again, the response body/logs will now show exactly where — send that back
and I can fix the real cause instead of guessing. P0-01's fix is a plausible
contributing cause (corrupted lineage feeding degenerate data into WFA
math) but this is not confirmed.

Test: `validation/verify_pl46_error_diagnosability_closure.py`

### P0-06 — Corporate-action resumable acquisition (already correct — verified, not rewritten)
No code change. A prior brief (pasted mid-session) asserted an
all-or-nothing acquisition defect that does **not** match this baseline.
`backend/core/corporate_action_chunk_manifest.py` and
`backend/tools/sync_nse_corporate_action_history.py` already implement
durable per-chunk manifests, resume-skips-completed, correct HTTP failure
classification, and scoped per-symbol coverage
(`reconcile_corporate_action_authority.py`). Verified with a dynamic
end-to-end test (not just source reading) using a fake fetcher that fails
one chunk of three: run 1 leaves the two good chunks PUBLISHED and the bad
one FAILED_RETRYABLE; run 2 (fresh fetcher/ingestion instances) makes
exactly one network call and completes the range without re-touching the
already-published chunks. DB reconciliation itself needs a live PostgreSQL
server (unavailable in this sandbox) — stubbed for that one assertion only;
still needs proving on your installed machine.

`price_factor`/`volume_factor` direction was also hand-verified against a
real 1:2 split in both derivation branches (face-value-transition and
ratio-text): both correctly produce `price_factor=0.5, volume_factor=2.0`.

Test: `validation/verify_pl46_corporate_action_resume_dynamic_proof.py`

## Build identity

- `backend/config.py`: `APP_VERSION` v131.0.0 → v131.1.0,
  `BUILD_MARKER` → `production-usability-r8-pl46-defect-cluster-closure-8086`
- `frontend/index.html`: `data-build-version`, `data-frontend-owner`,
  `data-build-marker`, `data-ui-merge`, title, version pill updated to match
- `frontend/app.js`: fallback version-pill strings updated to match
- `frontend/release-identity.json`: version/frontend_owner/build_marker/
  candidate_revision/release_name/ui_refinement/acceptance_state updated;
  all 5 asset sha256 hashes recomputed against actual file content
- `RELEASE_IDENTITY.json` (root lineage ledger): added a truthful `pl46_*`
  entry following the existing per-PL pattern, including
  `pl46_exact_parent_sha256` computed from the actual `PL45.zip` you
  supplied (`81e109968fe56df5ec4a0cc59272713daf25dc2019a927c876c043db8bd65ef5`)
  — not fabricated. `version`/`candidate_revision`/`release_name`/
  `acceptance_state`/`summary` updated; a `pl46_defect_cluster_closure_acceptance`
  entry added to `acceptance_requirements` alongside the existing per-PL ones
  (nothing historical removed or altered).
- `RELEASE_ATTESTATION.json` (root): **left unedited.** I didn't have
  confident enough understanding of its exact schema/consumer to hand-edit
  it truthfully without risking a wrong or misleading field. If your install
  process regenerates it automatically, no action needed; if it's meant to
  be hand-maintained per release, it still says PL45 and needs a PL46 pass.

Verified locally (with `PROJECT_LADDU_HOME` pointed at this build's root):
`GET /api/frontend-identity` → `{"ok": true, "mismatches": []}`.
This does NOT substitute for an installed Windows run — it proves the
identity contract is internally consistent in this candidate, not that the
installer/runtime path resolves it correctly on your machine.

## What is proven here vs. what still needs your machine

Proven in this sandbox (deterministic, re-runnable):
- All 6 `validation/verify_pl46_*.py` tests pass
- Full `backend/` syntax sweep is clean (`ast.parse` on every .py file)
- Frontend/backend build-identity contract is internally consistent

NOT provable here, needs your installed Windows run:
- Real Upstox/NSE network access (this sandbox's egress is PyPI/npm/GitHub
  only)
- The capital WFA 503 root cause (P0-05) — diagnosability is fixed, the
  actual trigger is not yet confirmed
- Real PostgreSQL reconciliation for corporate actions (P0-06) — one test
  assertion stubs this
- UI_REVAMP_SPEC.md and ACCEPTANCE_CONTRACT.md acceptance criteria — not
  yet reviewed against current frontend code in this session
- Browser vertical flow against real APIs

## Suggested install/verify command sequence

From the installed build root:
```
python -m compileall backend
python validation\verify_pl46_pit_timestamp_lineage_closure.py
python validation\verify_pl46_three_arm_self_repair_closure.py
python validation\verify_pl46_monitoring_semantic_blocker_closure.py
python validation\verify_pl46_lifecycle_snapshot_immutability_closure.py
python validation\verify_pl46_error_diagnosability_closure.py
python validation\verify_pl46_corporate_action_resume_dynamic_proof.py
```
Then run End-to-End as usual and capture the evidence bundle, especially
the capital WFA response body if it still fails — the new
`error_type`/`error_location` fields are the actionable part.


## Source Seal R1 — v131.1.1

The uploaded v131.1.0 candidate contained the six intended PL46 functional fixes, and all six focused PL46 regression tests passed, but the package was not source-sealable as shipped. Source Seal R1 closes only release-engineering defects: RELEASE_ATTESTATION is regenerated for PL46, the master deployable-candidate validator has a PL46 branch that runs all six focused guards, stale PL45 frontend cache-busters are replaced by the v131.1.1/PL46 marker, README/release identities are aligned, and this closure note itself is included in the exact allowlist/manifest. No trading, WFA, statistical, cost, risk, PIT-threshold, or broker-authority mathematics changed.

## Source Seal R2 — v131.1.2

Windows evidence at 2026-08-22 21:26 IST reported the seven PL46 files as unmanifested extras. The sealed R1 archive itself contains those seven manifest entries, so this signature is consistent with a mixed/stale extraction tree rather than missing PL46 source. R2 does not weaken package integrity. It adds an explicit release-level package contract (1160 manifest files plus the manifest), requires all seven PL46 closure members before any target mutation, emits a dedicated stale/mixed extraction diagnosis, and ships under a version-isolated top-level archive directory. No trading, WFA, statistical, cost, risk, PIT-threshold, or broker-authority mathematics changed.


## QuestDB Recovery R3 + UI7 — v131.1.5

Bounded micro-agile closure after exact Windows v131.1.2 install evidence. Package inventory, lineage, runtimes, backend import and both PostgreSQL planes passed; QuestDB candidate `project-laddu-questdb-candidate-0cedfe181d` started on port 59178 but did not become healthy under the prior 120-second readiness contract.

R3 changes only the Windows QuestDB recovery authority, release/version identity, and the user-supplied UI7 frontend. Before a new QuestDB candidate is attached to the retained volume, installer-owned candidate containers using that exact volume are inspected. One healthy candidate may be reused; unhealthy running/restarting/created/paused candidates have bounded logs/state captured and are stopped, preserving the named volume and historical authoritative container. Candidate readiness is state-aware and expanded to 600 seconds for retained-volume replay/WAL recovery. A candidate that exits/dead fails immediately with state/log evidence; a candidate that times out is retained stopped so the next installer attempt cannot create concurrent live writers on the same volume.

Focused proof: `validation/verify_pl46_questdb_recovery_retry_closure.py`. Trading, scanner, WFA, statistical, transaction-cost, risk, PIT and broker-authority mathematics are unchanged. UI7 is a presentation-only merge over the sealed authority.

## v131.1.5 Runtime Retry R4
- Exact pinned backend/research venvs may be reused across micro-releases only after full pin + pip-check verification.
- Partial/inconsistent cached venvs from interrupted installs are never mutated or trusted.
- If no compatible verified venv exists, a new isolated candidate/recovery venv is created and reverified fail-closed.
- This directly closes the Windows retry failure where an existing research venv reported numpy/duckdb/lightgbm as missing and permanently blocked later installs.


## v131.1.5 — Research Terminality R5

- Replaces historical research subprocess `stdout/stderr=PIPE` polling with durable file spooling so Windows pipe-buffer backpressure cannot deadlock WFA/ML completion.
- Publishes phase elapsed time, child PID, output byte counts and durable log paths while the child is running.
- Dynamic regression emits >2 MB to both stdout and stderr and proves the supervised phase terminates successfully.
- Disables the unused legacy operational-SQLite candle loader; active training remains fail-closed on `research_delivery_training_panel` in Parquet/DuckDB.
- No trading, WFA admission, capital, statistical, cost, risk, factor or broker-authority thresholds changed.


## v131.1.6 — Simple Install R6 / Field-Proven P0 Closure

Exact Windows v131.1.5 evidence proved the retry-runtime optimization works, then failed at QuestDB because a restarting Project-Laddu QuestDB owner still held `/var/lib/questdb/db` while recovery attached a second writer. R6 inventories every container mounting the retained QuestDB volume, preserves one healthy Project-Laddu owner, quiesces only installer-owned conflicting owners, re-checks the volume before candidate creation, and fails closed on any unknown/manual owner. The exact `cannot lock table name registry file` signature now fails fast rather than consuming the full readiness window.

The market-closed Research lineage residual is also closed surgically: `completed_session_quote()` may return a cached historical row with an old receipt timestamp; when that quote is consumed for the current research pass, the historical `source_as_of` is preserved but `received_at` is refreshed to the local consumption moment. The `INVALID_TIMESTAMP_ORDER` guard remains unchanged and still rejects genuine source-after-receipt inversion.

Focused proofs: `validation/verify_pl46_questdb_recovery_retry_closure.py`, `validation/verify_pl46_closed_market_received_at_freshness.py`, `validation/verify_pl46_pit_timestamp_lineage_closure.py`. Trading/scanner ranking/WFA/ML/statistical/cost/risk mathematics and broker authority remain unchanged. Project Laddu chess icon/logo is unchanged.
