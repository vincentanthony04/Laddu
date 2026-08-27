# PL25 Persisted Catalogue & Capital-WFA Activation

PL25 is a narrow activation build. It does not change scanner execution, decision mathematics, Entry/Target/SL/R:R, costs, risk/admission, Model Paper, forward-selector semantics, or broker authority.

## Proven installed blocker from PL24
The installed `market-lake.json` retained a valid `RESEARCH_CATALOG_REFRESHED` state but predated the manifest field that reports the materialized `research_training_panel`. PL24 therefore reported the persisted catalogue as not ready and the autonomous PIT worker remained before training, so the PL24 capital-WFA publication path never executed.

## Closure
- Add a bounded read-only DuckDB catalogue-evidence authority.
- Prove `research_delivery_training_panel` directly by relation existence, non-empty valid rows and minimum distinct-date depth.
- When capital WFA is still missing, allow exactly that proven persisted panel to activate the historical trainer without waiting behind a redundant catalogue refresh lock.
- Once explicit capital WFA exists, normal refresh-first cadence resumes; this is not a permanent refresh bypass.
- Persisted-panel evidence is diagnostic/shadow only and carries production influence 0.
- Forward selector evidence remains prospective-only and is never backfilled from history.
- Fix the status PowerShell variable so forward selector depth is displayed from the actual result.

An APPROVED or REJECTED capital WFA is a valid engineering result. PL25 does not weaken gates or manufacture alpha.
