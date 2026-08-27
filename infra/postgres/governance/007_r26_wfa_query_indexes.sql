-- Project Laddu v131 R26 — bounded offline WFA read-path indexes.
-- These indexes do not alter any evidence row or authority semantics. They only
-- accelerate immutable selector replay/reconciliation reads used by the
-- off-market capital WFA lifecycle.
BEGIN;

CREATE INDEX IF NOT EXISTS ix_selector_members_desk_population_candidate
    ON research.selector_population_members(desk, population_fingerprint, candidate_id);

CREATE INDEX IF NOT EXISTS ix_selector_outcomes_horizon_population_candidate
    ON research.selector_outcomes(horizon, population_fingerprint, candidate_id);

CREATE INDEX IF NOT EXISTS ix_selector_outcomes_population_candidate_settled
    ON research.selector_outcomes(population_fingerprint, candidate_id, settled_at DESC);

COMMIT;
