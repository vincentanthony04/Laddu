-- Project Laddu v122 candidate-5 — durable completion checkpoint for one-time
-- legacy SQLite -> PostgreSQL research-governance migration.
BEGIN;

CREATE TABLE IF NOT EXISTS research.legacy_research_migration_checkpoints (
    checkpoint_key text PRIMARY KEY,
    source_manifest_sha256 text NOT NULL,
    source_manifest jsonb NOT NULL,
    expected_counts jsonb NOT NULL,
    verified_counts jsonb NOT NULL,
    quarantine jsonb NOT NULL,
    completed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    payload_sha256 text NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_legacy_research_migration_checkpoints_completed
    ON research.legacy_research_migration_checkpoints(completed_at DESC);

DROP TRIGGER IF EXISTS trg_legacy_research_migration_checkpoints_immutable
    ON research.legacy_research_migration_checkpoints;
CREATE TRIGGER trg_legacy_research_migration_checkpoints_immutable
BEFORE UPDATE OR DELETE ON research.legacy_research_migration_checkpoints
FOR EACH ROW EXECUTE FUNCTION research.reject_mutation();

GRANT SELECT, INSERT ON TABLE research.legacy_research_migration_checkpoints
    TO laddu_governance_writer;

COMMIT;
