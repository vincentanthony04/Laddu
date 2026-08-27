# Project Laddu — Security and Recovery

- Broker authority is `NONE`; the product operates in automatic Model Paper only.
- Upstox access tokens are stored using Windows DPAPI LocalMachine encryption, never plaintext package configuration.
- Existing `data`, `secure`, `logs`, and release-isolated runtime state are outside application-payload replacement.
- Historical PostgreSQL migration hashes are immutable. New schema changes are forward-only migrations.
- PostgreSQL recovery replaces a failed physical pool only after a fresh
  generation passes checked SQL. The logical authority remains stable; no
  compatibility database or cached-write fallback is activated.
- Transactions never migrate between pool generations. Writes are not retried
  implicitly, and an uncertain COMMIT is surfaced for idempotency-key/outbox
  reconciliation instead of being reported as success or known rollback.
- Recovery callers and pool waiters are bounded. Service shutdown invalidates
  in-flight recovery epochs, joins the supervisor, and prevents post-close pool
  publication.
- Installer evidence is written under `C:\Temp\ProjectLaddu\installer`.
- A failed pre-commit upgrade restores the prior application payload/runtime owner when target mutation has begun and records the durable transaction outcome.
- Legacy research rows that cannot satisfy current immutable governance contracts are retained as quarantine evidence, never silently promoted into canonical authority.

Installer path policy: `C:\ProgramData\ProjectLaddu` owns all installed/runtime and transactional installer working state. `C:\Temp\ProjectLaddu` is reserved for logs, diagnostics, and evidence exports only.


## v123 recovery freeze

The C28 generation-based PostgreSQL recovery authority is frozen in v123.0.0. The integrated fixes do not alter pool generation construction, verification, publication, stale-generation fencing, commit-ambiguity handling, or write-retry policy.
