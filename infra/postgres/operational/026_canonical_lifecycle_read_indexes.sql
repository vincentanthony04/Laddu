-- v122 Candidate-10 installed evidence repair: compact canonical lifecycle reads.
-- Additive indexes only; no canonical decision rows are rewritten.
CREATE INDEX IF NOT EXISTS ix_canonical_decisions_updated_at
    ON trading.canonical_decisions(updated_at DESC);

CREATE INDEX IF NOT EXISTS ix_canonical_decisions_mode_updated_at
    ON trading.canonical_decisions(mode, updated_at DESC);
